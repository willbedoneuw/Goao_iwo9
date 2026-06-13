"""
tg_panel.py — the TELEGRAM section of the customer bot.
=======================================================

A fully self-contained, decoupled module that adds a Telegram user-account
panel ALONGSIDE the existing Rubika side. It NEVER touches the Rubika code
paths: it has its own conversation state, its own login clients and its own
DB tables (tg_accounts / tg_settings via db.py).

It is wired in by customer_bot.amain() calling ``tg_panel.setup(bot)`` once the
shared Telethon bot client is created. All callbacks are namespaced ``tg_*`` and
its NewMessage router only acts when the user is in a *Telegram* conversation
step (the Rubika router uses a separate ``state`` dict, so the two never clash).

Send logic mirrors the reference panel: forward ONE pre-set content (text /
photo / file) to the account's mutual contacts + groups, sequentially, with
live progress, a stop button, FloodWait tolerance and a configurable error cap
(config.TG_MAX_ERRORS). Every event is logged to the central group AND mirrored
to the customer's own chat (logbus.event(..., pv_user=uid)).
"""
import asyncio
import os
import time as _time
from datetime import datetime

from telethon import events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
    PhoneNumberInvalidError,
)
from telethon.tl.functions.contacts import GetContactsRequest

import config
import db
import logbus
import ratelimit

# Telethon is imported lazily inside setup to keep this module importable even
# if a tool only wants the helpers.
TelegramClient = None  # set in setup()

LINE = logbus.LINE
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TG_MEDIA_DIR = os.path.join(DATA_DIR, "tg_media")
os.makedirs(TG_MEDIA_DIR, exist_ok=True)

bot = None  # the shared Telethon bot client (injected by setup)

# Telegram-only conversation state (separate from customer_bot.state).
_state: dict = {}
# Login clients mid-flow: uid -> {"client","phone","hash"}
_pending: dict = {}
# Manual stop flags: account_id -> True
_stop: dict = {}
# account_ids currently sending (avoid double-enqueue)
_active: set = set()
# global sequential send queue (one send at a time, like the reference panel)
_send_queue = None
# reference to customer_bot's Rubika conversation-state dict (set in setup), so
# entering the Telegram section can clear any half-finished Rubika flow and vice
# versa — prevents BOTH NewMessage routers acting on the same message.
_rubika_state = None


def now() -> str:
    return config.now_str()


def card(title, rows):
    return logbus.card(title, rows)


# --------------------------------------------------------------------------- #
# Gate (shared free/time/block model, decoupled implementation).
# --------------------------------------------------------------------------- #
async def _gate(event) -> bool:
    uid = event.sender_id
    if db.is_blocked(uid):
        return False
    user = await event.get_sender()
    name = getattr(user, "first_name", "") or ""
    username = getattr(user, "username", "") or ""
    db.ensure_customer(uid, name, username)
    if db.maintenance_on():
        await _respond(event, "🛠 ربات در حال تعمیر است. کمی بعد دوباره امتحان کن.")
        return False
    if not await ratelimit.guard(uid, name):
        await _respond(event, "⛔ به‌خاطر فعالیت بیش از حد، حساب شما مسدود شد.")
        return False
    cust = db.get_customer(uid) or {}
    if config.FREE_MODE:
        # free for everyone unless the owner set a time that has now passed
        if (cust.get("expires_at") or "") and db.seconds_left(uid) <= 0:
            await _respond(event, "🔴 زمانِ دسترسی‌ات تموم شده. با پشتیبانی تماس بگیر.")
            return False
    else:
        if not db.is_active(uid):
            await _respond(event, "🔴 دسترسی فعال نیست. با پشتیبانی تماس بگیر.")
            return False
    return True


async def _respond(event, text, buttons=None):
    try:
        if isinstance(event, events.CallbackQuery.Event):
            await event.edit(text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)
    except Exception:
        try:
            await bot.send_message(event.sender_id, text, buttons=buttons)
        except Exception:
            pass


async def _safe_edit(uid, msg_id, text, buttons=None):
    try:
        await bot.edit_message(uid, msg_id, text, buttons=buttons)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Menu + small UI helpers (kept "book-like": consistent cards, breadcrumbs,
# 🔙/🏠 buttons everywhere).
# --------------------------------------------------------------------------- #
def _menu():
    return [
        [Button.inline("🚀 ارسال", b"tg_accounts"),
         Button.inline("➕ افزودن اکانت", b"tg_addacc")],
        [Button.inline("👤 اکانت‌های من", b"tg_accounts"),
         Button.inline("🩺 چک‌حساب", b"tg_health")],
        [Button.inline("✍️ محتوا", b"tg_content"),
         Button.inline("⚙️ سرعت/تاخیر", b"tg_speed")],
        [Button.inline("📊 آمار من", b"tg_stats"),
         Button.inline("📖 راهنما", b"tg_help")],
        [Button.inline("🏠 منوی اصلی", b"mainmenu")],
    ]


def _back_home():
    return [[Button.inline("🔙 تلگرام", b"tg_home"),
             Button.inline("🏠 منوی اصلی", b"mainmenu")]]


def _stop_btn(account_id):
    return [[Button.inline("⛔ توقف ارسال", f"tg_stop_{account_id}".encode())]]


def _content_summary(s: dict) -> str:
    ct = s.get("content_type")
    if not ct:
        return "هنوز محتوایی تنظیم نشده ❌"
    if ct == "text":
        return f"📝 متن:\n{s.get('content_text') or ''}"
    label = "🖼 عکس" if ct == "photo" else "📎 فایل"
    cap = s.get("content_text")
    return label + (f"\n📝 کپشن: {cap}" if cap else " (بدون کپشن)")


def _bar(done, total):
    if total <= 0:
        return "…"
    frac = max(0.0, min(1.0, done / total))
    n = 10
    filled = int(frac * n)
    return "▓" * filled + "░" * (n - filled) + f" {int(frac * 100)}%"


def _progress_card(acc, ok, fail, total, done):
    return card("🚀 تلگرام › ارسال (زنده)", [
        f"📱 {acc['phone']}  ({acc.get('name') or '-'})",
        f"📊 {_bar(done, total)}",
        f"✅ موفق : {ok}    ❌ ناموفق : {fail}",
        f"🎯 کل گیرنده : {total}",
        f"🕒 {now()}",
    ])


# --------------------------------------------------------------------------- #
# Home / cancel
# --------------------------------------------------------------------------- #
async def tg_home_cb(event):
    if not await _gate(event):
        return
    _state.pop(event.sender_id, None)
    if _rubika_state is not None:
        _rubika_state.pop(event.sender_id, None)  # leave any Rubika flow
    await _respond(event, card("📨 پنل تلگرام", [
        "اکانت‌های تلگرامِ خودت رو اضافه کن و محتوا بفرست.",
        "یکی از گزینه‌ها رو انتخاب کن:",
    ]), buttons=_menu())


async def tg_cancel_cb(event):
    uid = event.sender_id
    p = _pending.pop(uid, None)
    if p:
        try:
            await p["client"].disconnect()
        except Exception:
            pass
    _state.pop(uid, None)
    if _rubika_state is not None:
        _rubika_state.pop(uid, None)
    await _respond(event, "لغو شد.", buttons=_menu())


# --------------------------------------------------------------------------- #
# Add account (phone -> code -> optional 2FA)
# --------------------------------------------------------------------------- #
async def tg_addacc_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    cap = config.TG_MAX_ACCOUNTS
    if cap and db.count_customer_tg_accounts(uid) >= cap:
        await _respond(event, card("➕ افزودن اکانت", [
            f"به سقفِ {cap} اکانت رسیدی.",
            "برای افزایش، با پشتیبانی تماس بگیر.",
        ]), buttons=_back_home())
        return
    _state[uid] = {"step": "tg_await_phone"}
    await _respond(event, card("➕ تلگرام › افزودن اکانت", [
        "📱 شمارهٔ اکانت تلگرام رو با کد کشور بفرست.",
        "مثال: `+989121234567`",
    ]), buttons=[[Button.inline("🔙 لغو", b"tg_cancel")]])


async def _handle_phone(event, st):
    uid = event.sender_id
    phone = (event.raw_text or "").strip().replace(" ", "")
    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await event.respond("❌ شماره نامعتبره. دوباره با کد کشور بفرست (مثل +98...).")
        try:
            await client.disconnect()
        except Exception:
            pass
        return
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خطا در ارسال کد: {repr(e)[:140]}")
        try:
            await client.disconnect()
        except Exception:
            pass
        return
    _pending[uid] = {"client": client, "phone": phone, "hash": sent.phone_code_hash}
    st["step"] = "tg_await_code"
    await event.respond(card("✉️ تلگرام › کد تأیید", [
        "کدی که تلگرام فرستاد رو بفرست.",
        "با فاصله یا بی‌فاصله، هر دو قبوله.",
    ]), buttons=[[Button.inline("🔙 لغو", b"tg_cancel")]])


async def _handle_code(event, st):
    uid = event.sender_id
    p = _pending.get(uid)
    if not p:
        _state.pop(uid, None)
        await event.respond("نشست لاگین منقضی شد. دوباره «افزودن اکانت» رو بزن.",
                            buttons=_menu())
        return
    code = "".join(ch for ch in (event.raw_text or "") if ch.isdigit())
    try:
        await p["client"].sign_in(phone=p["phone"], code=code,
                                  phone_code_hash=p["hash"])
    except SessionPasswordNeededError:
        st["step"] = "tg_await_password"
        await event.respond("🔐 این اکانت رمز دومرحله‌ای داره. رمز رو بفرست.",
                            buttons=[[Button.inline("🔙 لغو", b"tg_cancel")]])
        return
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await event.respond("❌ کد اشتباه یا منقضیه. دوباره کد رو بفرست (یا لغو کن).")
        return
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خطا در ورود: {repr(e)[:140]}")
        return
    await _finish_login(event)


async def _handle_password(event, st):
    uid = event.sender_id
    p = _pending.get(uid)
    if not p:
        _state.pop(uid, None)
        await event.respond("نشست لاگین منقضی شد. دوباره «افزودن اکانت» رو بزن.",
                            buttons=_menu())
        return
    try:
        await p["client"].sign_in(password=(event.raw_text or "").strip())
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ رمز اشتباهه یا خطایی رخ داد: {repr(e)[:120]}\n"
                            "دوباره رمز رو بفرست.")
        return
    await _finish_login(event)


async def _finish_login(event):
    uid = event.sender_id
    p = _pending.pop(uid, None)
    _state.pop(uid, None)
    if not p:
        return
    client = p["client"]
    phone = p["phone"]
    try:
        me = await client.get_me()
        session_str = client.session.save()
        full_name = " ".join(filter(None, [me.first_name, me.last_name])) or "-"
        username = getattr(me, "username", "") or ""
        try:
            result = await client(GetContactsRequest(hash=0))
            users = result.users
            mutual = [u for u in users if getattr(u, "mutual_contact", False)]
            groups = await _get_groups(client)
            n_contacts, n_mutual, n_groups = len(users), len(mutual), len(groups)
        except Exception:
            n_contacts = n_mutual = n_groups = 0

        aid = db.add_tg_account(uid, phone, full_name, username, me.id, session_str)

        await _respond(event, card("✅ اکانت تلگرام اضافه شد", [
            f"📛 {full_name}" + (f"  (@{username})" if username else ""),
            f"📱 {phone}",
            f"👥 مخاطبین : {n_contacts}   ↔️ دوطرفه : {n_mutual}",
            f"💬 گروه‌ها : {n_groups}",
            LINE,
            "حالا «✍️ محتوا» رو تنظیم کن، بعد «🚀 ارسال».",
        ]), buttons=[[Button.inline("✍️ تنظیم محتوا", b"tg_content")],
                     [Button.inline("🔙 تلگرام", b"tg_home")]])

        await logbus.event("➕ TG ADD ACCOUNT", [
            f"🆔 Customer : {uid}",
            f"📱 {phone}  ({full_name})",
            f"👥 مخاطبین : {n_contacts}   ↔️ دوطرفه : {n_mutual}",
            f"💬 گروه‌ها : {n_groups}",
            f"🕒 {now()}"], pv_user=uid)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خطا بعد از ورود: {repr(e)[:140]}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# My accounts + detail + delete
# --------------------------------------------------------------------------- #
async def tg_accounts_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    accounts = db.list_tg_accounts(uid)
    if not accounts:
        await _respond(event, card("👤 تلگرام › اکانت‌های من", [
            "هنوز اکانتی اضافه نکردی."]),
            buttons=[[Button.inline("➕ افزودن اکانت", b"tg_addacc")],
                     [Button.inline("🔙 تلگرام", b"tg_home")]])
        return
    rows = []
    for i, acc in enumerate(accounts, 1):
        emoji = "🟢" if acc.get("status") == "active" else "🔴"
        rows.append([Button.inline(f"{emoji} {i}- {acc['phone']}",
                                   f"tg_acc_{acc['id']}".encode())])
    rows.append([Button.inline("🔙 تلگرام", b"tg_home"),
                 Button.inline("🏠 منوی اصلی", b"mainmenu")])
    await _respond(event, card("👤 تلگرام › اکانت‌های من",
                               ["یه اکانت رو انتخاب کن:"]), buttons=rows)


async def tg_acc_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    account_id = int(event.pattern_match.group(1))
    acc = db.get_tg_account_owned(account_id, uid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    status = "فعال 🟢" if acc.get("status") == "active" else "غیرفعال 🔴 (سشن باطل)"
    await _respond(event, card(f"👤 تلگرام › {acc['phone']}", [
        f"📛 نام : {acc.get('name') or '-'}",
        (f"🔗 @{acc['username']}" if acc.get("username") else "🔗 یوزرنیم : -"),
        f"📱 شماره : {acc['phone']}",
        f"📅 افزوده‌شده : {acc.get('added_at') or '-'}",
        f"⭐️ وضعیت : {status}",
    ]), buttons=[
        [Button.inline("🚀 شروع ارسال", f"tg_send_{account_id}".encode())],
        [Button.inline("🩺 چک‌حساب", f"tg_chk_{account_id}".encode()),
         Button.inline("🗑 حذف", f"tg_del_{account_id}".encode())],
        [Button.inline("🔙 اکانت‌ها", b"tg_accounts")],
    ])


async def tg_del_cb(event):
    if not await _gate(event):
        return
    account_id = int(event.pattern_match.group(1))
    await _respond(event, "از حذف این اکانت مطمئنی؟",
                   buttons=[[Button.inline("✅ بله، حذف کن",
                                           f"tg_delyes_{account_id}".encode())],
                            [Button.inline("🔙 خیر", f"tg_acc_{account_id}".encode())]])


async def tg_delyes_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    account_id = int(event.pattern_match.group(1))
    acc = db.get_tg_account_owned(account_id, uid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    db.delete_tg_account(account_id)
    await _respond(event, "اکانت حذف شد. ✅",
                   buttons=[[Button.inline("🔙 اکانت‌ها", b"tg_accounts")]])
    await logbus.event("🗑 TG DELETE ACCOUNT", [
        f"🆔 {uid}", f"📱 {acc['phone']}", f"🕒 {now()}"], pv_user=uid)


# --------------------------------------------------------------------------- #
# Health check (one account, or all)
# --------------------------------------------------------------------------- #
async def _check_one(acc) -> dict:
    client = TelegramClient(StringSession(acc.get("session") or ""),
                            config.API_ID, config.API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            db.set_tg_status(acc["id"], "dead")
            return {"ok": False, "reason": "سشن باطل/خارج‌شده"}
        me = await client.get_me()
        db.set_tg_status(acc["id"], "active")
        uname = getattr(me, "username", "") or ""
        name = " ".join(filter(None, [me.first_name, me.last_name])) or "-"
        return {"ok": True, "name": name, "username": uname}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": repr(e)[:80]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def tg_health_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    accounts = db.list_tg_accounts(uid)
    if not accounts:
        await _respond(event, card("🩺 چک‌حساب", ["اکانتی نداری."]),
                       buttons=_back_home())
        return
    await _respond(event, "🩺 در حال بررسی اکانت‌ها ... کمی صبر کن.")
    rows = []
    for acc in accounts:
        r = await _check_one(acc)
        if r["ok"]:
            tag = f"🟢 {acc['phone']} — {r['name']}"
            if r.get("username"):
                tag += f" (@{r['username']})"
        else:
            tag = f"🔴 {acc['phone']} — {r.get('reason')}"
        rows.append(tag)
    await _respond(event, card("🩺 تلگرام › چک‌حساب", rows), buttons=_back_home())


async def tg_chk_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    account_id = int(event.pattern_match.group(1))
    acc = db.get_tg_account_owned(account_id, uid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    await _respond(event, "🩺 در حال بررسی ...")
    r = await _check_one(acc)
    if r["ok"]:
        body = [f"🟢 سالم", f"📛 {r['name']}",
                (f"🔗 @{r['username']}" if r.get("username") else "🔗 -"),
                f"📱 {acc['phone']}"]
    else:
        body = [f"🔴 مشکل : {r.get('reason')}", f"📱 {acc['phone']}",
                "اگه سشن باطله، اکانت رو دوباره اضافه کن."]
    await _respond(event, card("🩺 چک‌حساب", body),
                   buttons=[[Button.inline("🔙 اکانت", f"tg_acc_{account_id}".encode())]])


# --------------------------------------------------------------------------- #
# Content (set text / photo / file)
# --------------------------------------------------------------------------- #
async def tg_content_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    s = db.get_tg_settings(uid)
    _state[uid] = {"step": "tg_await_content"}
    await _respond(event, card("✍️ تلگرام › محتوا", [
        "محتوای فعلی:",
        _content_summary(s),
        LINE,
        "محتوای جدید رو بفرست (متن، یا عکس/فایل با کپشن دلخواه).",
        "همین یک‌بار ست می‌شه و ذخیره می‌مونه.",
    ]), buttons=[[Button.inline("🔙 لغو", b"tg_cancel")]])


async def _handle_content(event, st):
    uid = event.sender_id
    msg = event.message
    cap = (msg.text or "").strip()
    try:
        if msg.photo:
            path = await msg.download_media(file=TG_MEDIA_DIR)
            db.set_tg_content(uid, "photo", msg.text or None, path)
            label = "🖼 عکس"
        elif msg.document:
            path = await msg.download_media(file=TG_MEDIA_DIR)
            db.set_tg_content(uid, "file", msg.text or None, path)
            label = "📎 فایل"
        elif msg.text:
            db.set_tg_content(uid, "text", msg.text, None)
            label = "📝 متن"
        else:
            await event.respond("❌ این نوع محتوا پشتیبانی نمی‌شه. متن، عکس یا فایل بفرست.")
            return
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خطا در ذخیرهٔ محتوا: {repr(e)[:120]}")
        return
    _state.pop(uid, None)
    # show the customer exactly what was saved
    confirm = [f"{label} به‌عنوان محتوای ارسالی ثبت شد."]
    if cap:
        confirm += [LINE, "📝 متنِ ذخیره‌شده:", cap]
    await event.respond(card("✅ محتوا ذخیره شد", confirm), buttons=_menu())
    # log the FULL content (what the user actually set), to group + DM
    log_rows = [f"🆔 {uid}", f"📦 نوع : {label}"]
    if cap:
        log_rows.append(f"📝 متن : {cap[:900]}")
    else:
        log_rows.append("📝 متن : (بدون متن/کپشن)")
    log_rows.append(f"🕒 {now()}")
    await logbus.event("✍️ TG CONTENT SET", log_rows, pv_user=uid)


# --------------------------------------------------------------------------- #
# Speed / delay
# --------------------------------------------------------------------------- #
async def tg_speed_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    cur = db.get_tg_delay(uid)
    rows = [[Button.inline(("✅ " if abs(cur - v) < 0.01 else "") + f"{v}s",
                           f"tg_spd_{v}".encode())]
            for v in (0.5, 1, 2, 3, 5)]
    rows.append([Button.inline("🔙 تلگرام", b"tg_home")])
    await _respond(event, card("⚙️ تلگرام › سرعت/تاخیر ارسال", [
        f"تاخیر فعلی بین هر ارسال : {cur}s",
        "هرچه بیشتر، امن‌تر (کمتر FloodWait).",
    ]), buttons=rows)


async def tg_spd_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    val = event.pattern_match.group(1).decode()
    db.set_tg_delay(uid, float(val))
    await tg_speed_cb(event)


# --------------------------------------------------------------------------- #
# My stats
# --------------------------------------------------------------------------- #
async def tg_stats_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    s = db.get_tg_settings(uid)
    n_acc = db.count_customer_tg_accounts(uid)
    await _respond(event, card("📊 تلگرام › آمار من", [
        f"👤 اکانت‌های تلگرام : {n_acc}",
        f"📤 کل ارسال‌ها : {int(s.get('total_sends') or 0)}",
        f"📦 محتوا : {'تنظیم‌شده ✅' if s.get('content_type') else 'تنظیم‌نشده ❌'}",
        f"⚙️ تاخیر : {config.clamp_tg_delay(s.get('send_delay'))}s",
    ]), buttons=_back_home())


# --------------------------------------------------------------------------- #
# Help
# --------------------------------------------------------------------------- #
async def tg_help_cb(event):
    if not await _gate(event):
        return
    await _respond(event, card("📖 راهنمای بخش تلگرام", [
        "➕ افزودن اکانت : شماره → کد → (در صورت لزوم) رمز دومرحله‌ای.",
        "✍️ محتوا : متن یا عکس/فایل با کپشن که ارسال می‌شه.",
        "🚀 ارسال : محتوا به مخاطب‌های دوطرفه + گروه‌های همون اکانت می‌ره؛",
        "   پیشرفت زنده نشون داده می‌شه و دکمهٔ «⛔ توقف» داری.",
        f"   اگه به {config.TG_MAX_ERRORS} خطا برسه، خودکار متوقف می‌شه.",
        "🩺 چک‌حساب : زنده‌بودن سشنِ اکانت‌ها رو بررسی می‌کنه.",
        "⚙️ سرعت/تاخیر : فاصلهٔ بین ارسال‌ها (برای کم‌کردن محدودیت).",
        "📊 آمار من : تعداد اکانت و کل ارسال‌ها.",
        LINE,
        "⚠️ ارسالِ انبوه ممکنه باعث محدودیتِ اکانت توسط تلگرام بشه؛ "
        "تاخیر مناسب بذار.",
    ]), buttons=_back_home())


# --------------------------------------------------------------------------- #
# Send (confirm -> enqueue -> sequential worker with live progress)
# --------------------------------------------------------------------------- #
async def tg_send_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    account_id = int(event.pattern_match.group(1))
    acc = db.get_tg_account_owned(account_id, uid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    s = db.get_tg_settings(uid)
    if not s.get("content_type"):
        await event.answer("اول محتوا رو تنظیم کن.", alert=True)
        return
    await _respond(event, card("🚀 تلگرام › تأیید ارسال", [
        f"📱 اکانت : {acc['phone']}",
        "محتوایی که ارسال می‌شه:",
        _content_summary(s),
        LINE,
        "به مخاطب‌های دوطرفه + گروه‌ها ارسال می‌شه. مطمئنی؟",
    ]), buttons=[[Button.inline("✅ بله، شروع کن", f"tg_go_{account_id}".encode())],
                 [Button.inline("🔙 خیر", f"tg_acc_{account_id}".encode())]])


async def tg_go_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    account_id = int(event.pattern_match.group(1))
    acc = db.get_tg_account_owned(account_id, uid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    if not db.get_tg_settings(uid).get("content_type"):
        await event.answer("اول محتوا رو تنظیم کن.", alert=True)
        return
    if account_id in _active:
        await event.answer("این اکانت همین الان در حال ارساله.", alert=True)
        return
    _active.add(account_id)
    _stop[account_id] = False
    await _respond(event, card("🚀 تلگرام › ارسال", [
        "✅ شروع شد. پیشرفت در پیامِ پایین نشون داده می‌شه."]))
    pm = await bot.send_message(uid, card("🚀 تلگرام › ارسال (زنده)", [
        f"📱 {acc['phone']}", "⏳ آماده‌سازی ..."]), buttons=_stop_btn(account_id))
    await _send_queue.put({"account_id": account_id, "uid": uid, "msg_id": pm.id})


async def tg_stop_cb(event):
    account_id = int(event.pattern_match.group(1))
    _stop[account_id] = True
    await event.answer("درخواست توقف ثبت شد. بعد از ارسالِ جاری متوقف می‌شه.",
                       alert=True)


async def _get_groups(client):
    groups = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group:
            groups.append(dialog.entity)
    return groups


async def _collect_recipients(client):
    """Mutual contacts + groups (same as the reference panel)."""
    result = await client(GetContactsRequest(hash=0))
    mutual = [u for u in result.users if getattr(u, "mutual_contact", False)]
    groups = await _get_groups(client)
    return list(mutual) + list(groups)


async def _prepare_media(client, s):
    ct = s.get("content_type")
    if ct == "text":
        return None
    caption = s.get("content_text") or ""
    force_doc = ct == "file"
    sent = await client.send_file("me", s["media_path"], caption=caption,
                                  force_document=force_doc)
    return sent.media


async def _send_one(client, peer, s, prepared_media):
    ct = s.get("content_type")
    caption = s.get("content_text") or ""
    if ct == "text":
        await client.send_message(peer, s.get("content_text") or "")
    else:
        await client.send_file(peer, prepared_media, caption=caption)


async def _do_send(job):
    account_id = job["account_id"]
    uid = job["uid"]
    msg_id = job["msg_id"]
    acc = db.get_tg_account(account_id)
    if not acc:
        return
    s = db.get_tg_settings(uid)
    if not s.get("content_type"):
        await _safe_edit(uid, msg_id, "⚠️ محتوایی تنظیم نشده.")
        return
    delay = config.clamp_tg_delay(s.get("send_delay"))
    client = TelegramClient(StringSession(acc.get("session") or ""),
                            config.API_ID, config.API_HASH)
    ok = fail = total = 0
    stopped = False
    hit_max = False
    started = datetime.now()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            db.set_tg_status(account_id, "dead")
            await _safe_edit(uid, msg_id, card("⚠️ اکانت در دسترس نیست", [
                f"📱 {acc['phone']}", "سشن باطل/خارج‌شده. دوباره اضافه‌اش کن."]))
            await logbus.event("⚠️ TG ACCOUNT DEAD", [
                f"🆔 {uid}", f"📱 {acc['phone']}", f"🕒 {now()}"], pv_user=uid)
            return

        recipients = await _collect_recipients(client)
        total = len(recipients)
        prepared = await _prepare_media(client, s)

        await logbus.event("🚀 TG SEND START", [
            f"🆔 {uid}", f"📱 {acc['phone']}", f"🎯 گیرنده : {total}",
            f"🕒 {now()}"], pv_user=uid)
        await _safe_edit(uid, msg_id, _progress_card(acc, 0, 0, total, 0),
                         buttons=_stop_btn(account_id))

        last_edit = 0.0
        for i, peer in enumerate(recipients, 1):
            if _stop.get(account_id):
                stopped = True
                break
            try:
                await _send_one(client, peer, s, prepared)
                ok += 1
            except FloodWaitError as fw:
                await asyncio.sleep(getattr(fw, "seconds", 5) + 1)
                try:
                    await _send_one(client, peer, s, prepared)
                    ok += 1
                except Exception:  # noqa: BLE001
                    fail += 1
            except Exception:  # noqa: BLE001
                fail += 1
            if fail >= config.TG_MAX_ERRORS:
                hit_max = True
                break
            t = _time.time()
            if t - last_edit >= 2:
                last_edit = t
                await _safe_edit(uid, msg_id,
                                 _progress_card(acc, ok, fail, total, i),
                                 buttons=_stop_btn(account_id))
            await asyncio.sleep(delay)

        if ok:
            db.incr_tg_sends(uid, ok)
        duration = int((datetime.now() - started).total_seconds())
        rate = f"{(ok / total * 100):.0f}%" if total else "0%"
        if hit_max:
            head = "🛑 TG SEND STOPPED (سقف خطا)"
            note = f"به سقفِ {config.TG_MAX_ERRORS} خطا رسید و متوقف شد."
        elif stopped:
            head = "🛑 TG SEND STOPPED (توسط کاربر)"
            note = "ارسال به‌درخواستِ کاربر متوقف شد."
        else:
            head = "🏁 TG SEND FINISHED"
            note = "ارسال کامل شد."
        rows = [f"🆔 {uid}", f"📱 {acc['phone']}", note,
                f"✅ موفق : {ok}    ❌ ناموفق : {fail}",
                f"🎯 کل : {total}    📊 {rate}",
                f"⏱ {duration}s    🕒 {now()}"]
        await _safe_edit(uid, msg_id, card(head, rows[1:]),
                         buttons=[[Button.inline("🔙 تلگرام", b"tg_home")]])
        await logbus.event(head, rows, pv_user=uid)
    except Exception as e:  # noqa: BLE001
        await logbus.event("❌ TG SEND ERROR", [
            f"🆔 {uid}", f"📱 {acc['phone']}", f"💥 {repr(e)[:120]}",
            f"🕒 {now()}"], pv_user=uid)
        await _safe_edit(uid, msg_id, card("❌ خطا در ارسال", [
            f"📱 {acc['phone']}", f"💥 {repr(e)[:120]}"]),
            buttons=[[Button.inline("🔙 تلگرام", b"tg_home")]])
    finally:
        _stop.pop(account_id, None)
        _active.discard(account_id)
        try:
            await client.disconnect()
        except Exception:
            pass


async def _send_worker():
    while True:
        job = await _send_queue.get()
        try:
            await _do_send(job)
        except Exception as e:  # noqa: BLE001
            print(f"[tg send_worker] {e}")
        finally:
            _send_queue.task_done()


# --------------------------------------------------------------------------- #
# NewMessage router (only acts on Telegram conversation steps)
# --------------------------------------------------------------------------- #
async def _msg_router(event):
    uid = event.sender_id
    if db.is_blocked(uid):
        return
    txt = event.raw_text or ""
    if txt.startswith("/"):
        return  # commands handled by customer_bot
    st = _state.get(uid)
    if not st:
        return  # not in a Telegram flow -> ignore (Rubika router handles its own)
    # If a Rubika flow also owns this user (shouldn't happen, but be safe),
    # defer to the Rubika router so the message is handled exactly once.
    if _rubika_state is not None and _rubika_state.get(uid):
        return
    if db.maintenance_on():
        await event.respond("🛠 ربات در حال تعمیر است.")
        return
    user = await event.get_sender()
    if not await ratelimit.guard(uid, getattr(user, "first_name", "") or ""):
        await event.respond("⛔ به‌خاطر فعالیت بیش از حد، حساب شما مسدود شد.")
        return
    step = st.get("step")
    if step == "tg_await_phone":
        await _handle_phone(event, st)
    elif step == "tg_await_code":
        await _handle_code(event, st)
    elif step == "tg_await_password":
        await _handle_password(event, st)
    elif step == "tg_await_content":
        await _handle_content(event, st)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def setup(shared_bot, rubika_state=None):
    """Register all Telegram-section handlers on the shared bot and start the
    sequential send worker. Called once from customer_bot.amain().
    rubika_state is customer_bot's conversation-state dict (for cross-section
    mutual exclusion)."""
    global bot, TelegramClient, _send_queue, _rubika_state
    bot = shared_bot
    _rubika_state = rubika_state
    from telethon import TelegramClient as _TC
    TelegramClient = _TC
    _send_queue = asyncio.Queue()

    add = bot.add_event_handler
    add(tg_home_cb, events.CallbackQuery(data=b"tg_home"))
    add(tg_cancel_cb, events.CallbackQuery(data=b"tg_cancel"))
    add(tg_addacc_cb, events.CallbackQuery(data=b"tg_addacc"))
    add(tg_accounts_cb, events.CallbackQuery(data=b"tg_accounts"))
    add(tg_acc_cb, events.CallbackQuery(pattern=b"tg_acc_(\\d+)"))
    add(tg_del_cb, events.CallbackQuery(pattern=b"tg_del_(\\d+)"))
    add(tg_delyes_cb, events.CallbackQuery(pattern=b"tg_delyes_(\\d+)"))
    add(tg_health_cb, events.CallbackQuery(data=b"tg_health"))
    add(tg_chk_cb, events.CallbackQuery(pattern=b"tg_chk_(\\d+)"))
    add(tg_content_cb, events.CallbackQuery(data=b"tg_content"))
    add(tg_speed_cb, events.CallbackQuery(data=b"tg_speed"))
    add(tg_spd_cb, events.CallbackQuery(pattern=b"tg_spd_([0-9.]+)"))
    add(tg_stats_cb, events.CallbackQuery(data=b"tg_stats"))
    add(tg_help_cb, events.CallbackQuery(data=b"tg_help"))
    add(tg_send_cb, events.CallbackQuery(pattern=b"tg_send_(\\d+)"))
    add(tg_go_cb, events.CallbackQuery(pattern=b"tg_go_(\\d+)"))
    add(tg_stop_cb, events.CallbackQuery(pattern=b"tg_stop_(\\d+)"))
    add(_msg_router, events.NewMessage())

    asyncio.create_task(_send_worker())
