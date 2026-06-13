"""
customer_bot.py — the CUSTOMER subscription bot.
================================================

Customers /start this bot, see how many days are left (or "expired"), buy a
subscription (3-day / weekly / monthly, paid in USDT-TRC20 and auto-verified
via TronGrid), and use the tools: add account, send (marker — SAME proven logic
as the previous project), import PV photos -> PDF, wallet, and account health
check. No limit on the number of accounts.

Isolation: this process uses ONLY its own operational database (db.py), always
scoped to the requesting telegram_id, and NEVER imports the owner-only
central_db. Every customer event is mirrored to the customer's own PV and to the
single central log group.
"""
import asyncio
import os
from datetime import datetime

from telethon import TelegramClient, events, Button

import account_conn
import config
import db
import logbus
import ratelimit
import rubika_client as rb
import tron
import worker

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

bot = TelegramClient(os.path.join(DATA_DIR, "customer_bot"),
                     config.API_ID, config.API_HASH)

LINE = logbus.LINE

# per-user conversation state
state: dict = {}
# rubpy login clients mid-flow: user_id -> ctx
pending_login: dict = {}
# prepared sends awaiting confirmation: account_id -> payload
pending_send: dict = {}
# manual stop flags: account_id -> True
stop_flags: dict = {}
# accounts currently running a job
active_jobs: set = set()


def now() -> str:
    return config.now_str()


def card(title, rows):
    return logbus.card(title, rows)


# --------------------------------------------------------------------------- #
# Subscription / access helpers.
# --------------------------------------------------------------------------- #
def _sub_line(uid: int) -> str:
    if db.is_blocked(uid):
        return "⛔ حساب شما مسدود است."
    d = db.days_left(uid)
    if d <= 0:
        return "🔴 اشتراک شما منقضی شده."
    return f"🟢 {d} روز از اشتراک شما باقی مونده."


def main_menu():
    return [
        [Button.inline("🚀 ارسال", b"send_menu"),
         Button.inline("➕ افزودن اکانت", b"addacc")],
        [Button.inline("👤 اکانت‌های من", b"accounts"),
         Button.inline("🩺 چک‌حساب", b"health")],
        [Button.inline("🖼 ایمپورت عکس پیوی (PDF)", b"pvexport")],
        [Button.inline("📌 مارکر", b"marker"),
         Button.inline("⚙️ سرعت ارسال", b"speed")],
        [Button.inline("🛒 خرید اشتراک", b"buy"),
         Button.inline("👛 کیف پول", b"wallet")],
    ]


def buy_menu():
    rows = []
    for key in ("3day", "weekly", "monthly"):
        p = config.PLANS[key]
        rows.append([Button.inline(f"{p['title']} — {p['price']:g} USDT",
                                   f"plan_{key}".encode())])
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    return rows


async def _gate(event, *, need_active: bool = True) -> bool:
    """Common entry guard for every customer action. Returns True if the action
    may proceed. Handles maintenance, rate-limit/auto-block, and (optionally)
    subscription validity."""
    uid = event.sender_id
    user = await event.get_sender()
    name = getattr(user, "first_name", "") or ""
    username = getattr(user, "username", "") or ""
    db.ensure_customer(uid, name, username)

    # maintenance mode (read via shared flag file, not central_db)
    if db.maintenance_on():
        await _respond(event, "🛠 ربات در حال تعمیر است. کمی بعد دوباره امتحان کن.")
        return False

    # anti-flood: counts every action; auto-blocks on exceed
    if not await ratelimit.guard(uid, name):
        await _respond(event, "⛔ به‌خاطر فعالیت بیش از حد، حساب شما مسدود شد. "
                              "برای رفع مسدودی با پشتیبانی در تماس باش.")
        return False

    if need_active and not db.is_active(uid):
        if db.is_blocked(uid):
            await _respond(event, "⛔ حساب شما مسدود است.")
        else:
            await _respond(event,
                           "🔴 برای استفاده از امکانات، اول اشتراک تهیه کن.",
                           buttons=buy_menu())
        return False
    return True


async def _respond(event, text, buttons=None):
    """Reply whether the event is a message or a callback."""
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


# --------------------------------------------------------------------------- #
# /start
# --------------------------------------------------------------------------- #
@bot.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    uid = event.sender_id
    user = await event.get_sender()
    name = getattr(user, "first_name", "") or ""
    username = getattr(user, "username", "") or ""
    fresh = db.get_customer(uid) is None
    db.ensure_customer(uid, name, username)
    state.pop(uid, None)

    if db.maintenance_on():
        await event.respond("🛠 ربات در حال تعمیر است. کمی بعد دوباره /start بزن.")
        return

    await ratelimit.guard(uid, name)  # count the action (won't block on first)

    await logbus.event("🟢 START", [
        f"👤 {name} (@{username})" if username else f"👤 {name}",
        f"🆔 {uid}",
        ("🆕 مشتری جدید" if fresh else "↩️ بازگشت مشتری"),
        f"🕒 {now()}"])

    header = _sub_line(uid)
    await event.respond(
        f"🤖 روبیکا تولز\n{LINE}\n{header}\n\nیکی از گزینه‌ها رو انتخاب کن:",
        buttons=main_menu())


@bot.on(events.CallbackQuery(data=b"home"))
async def home_cb(event):
    if not await _gate(event, need_active=False):
        return
    state.pop(event.sender_id, None)
    header = _sub_line(event.sender_id)
    await _respond(event, f"🤖 روبیکا تولز\n{LINE}\n{header}\n\nیکی از گزینه‌ها رو انتخاب کن:",
                   buttons=main_menu())


@bot.on(events.CallbackQuery(data=b"cancel"))
async def cancel_cb(event):
    uid = event.sender_id
    p = pending_login.pop(uid, None)
    if p:
        try:
            await p["client"].disconnect()
        except Exception:
            pass
    state.pop(uid, None)
    await _respond(event, "لغو شد.", buttons=main_menu())


# --------------------------------------------------------------------------- #
# Buy subscription + payment verification.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"buy"))
async def buy_cb(event):
    if not await _gate(event, need_active=False):
        return
    await _respond(event,
                   "🛒 یکی از پلن‌ها رو انتخاب کن (پرداخت با USDT شبکه TRC20):",
                   buttons=buy_menu())


@bot.on(events.CallbackQuery(pattern=b"plan_(.+)"))
async def plan_cb(event):
    if not await _gate(event, need_active=False):
        return
    key = event.pattern_match.group(1).decode()
    plan = config.PLANS.get(key)
    if not plan:
        await event.answer("پلن نامعتبر.", alert=True)
        return
    state[event.sender_id] = {"step": "await_txhash", "plan": key}
    await _respond(event, card("🧾 پرداخت اشتراک", [
        f"📦 پلن : {plan['title']}",
        f"⏳ مدت : {plan['days']} روز",
        f"💵 مبلغ : دقیقاً {plan['price']:g} USDT (TRC20)",
        LINE,
        "1️⃣ مبلغ دقیق رو به آدرس زیر بفرست (شبکه TRON / TRC20):",
        f"`{config.WALLET_ADDRESS}`",
        "2️⃣ بعد از پرداخت، هشِ تراکنش (TxID) رو همینجا بفرست.",
        LINE,
        "⚠️ مبلغ باید دقیق باشه و هر تراکنش فقط یک‌بار قابل استفاده‌ست.",
    ]), buttons=[[Button.inline("🔙 بازگشت", b"buy")]])


async def handle_txhash(event, st):
    uid = event.sender_id
    key = st.get("plan")
    plan = config.PLANS.get(key)
    if not plan:
        state.pop(uid, None)
        await event.respond("پلن نامعتبر. دوباره از «خرید اشتراک» شروع کن.",
                            buttons=main_menu())
        return
    tx_hash = event.raw_text.strip().split()[0] if event.raw_text.strip() else ""
    if not tx_hash:
        await event.respond("هشِ تراکنش رو بفرست.")
        return

    # fast anti-fraud: reject an already-used hash before hitting the network
    if db.payment_exists(tx_hash):
        state.pop(uid, None)
        await logbus.event("♻️ پرداخت تکراری", [
            f"🆔 {uid}", f"🔗 {tx_hash[:24]}…",
            "این هش قبلاً استفاده شده.", f"🕒 {now()}"], pv_user=uid)
        await event.respond("❌ این هشِ تراکنش قبلاً استفاده شده.", buttons=main_menu())
        return

    msg = await event.respond("⏳ در حال بررسی تراکنش روی شبکه TRON ...")
    res = await tron.verify_usdt_payment(tx_hash, plan["price"])
    if not res.ok:
        await msg.edit(f"❌ تأیید نشد: {res.reason}",
                       buttons=[[Button.inline("🔁 تلاش دوباره", f"plan_{key}".encode())],
                                [Button.inline("🏠 منو", b"home")]])
        await logbus.event("⚠️ پرداخت ناموفق", [
            f"🆔 {uid}", f"📦 {plan['title']}", f"🔗 {tx_hash[:24]}…",
            f"💥 {res.reason}", f"🕒 {now()}"])
        return

    # verified -> record (UNIQUE hash guards against a race) + credit days
    ok = db.record_payment(uid, tx_hash, key, res.amount, plan["days"])
    state.pop(uid, None)
    if not ok:
        await msg.edit("❌ این هشِ تراکنش قبلاً ثبت شده.", buttons=main_menu())
        return
    await msg.edit(card("✅ پرداخت تأیید شد", [
        f"📦 {plan['title']}",
        f"💵 {res.amount:g} USDT",
        f"⏳ {plan['days']} روز اضافه شد.",
        f"📅 {_sub_line(uid)}",
    ]), buttons=main_menu())
    await logbus.event("💰 خرید اشتراک", [
        f"🆔 {uid}", f"📦 {plan['title']}", f"💵 {res.amount:g} USDT",
        f"🔗 {tx_hash[:24]}…", f"📅 {_sub_line(uid)}", f"🕒 {now()}"], pv_user=uid)


# --------------------------------------------------------------------------- #
# Wallet.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"wallet"))
async def wallet_cb(event):
    if not await _gate(event, need_active=False):
        return
    uid = event.sender_id
    cust = db.get_customer(uid) or {}
    pays = db.list_payments(uid)[:5]
    rows = [
        f"📅 {_sub_line(uid)}",
        f"💵 مجموع پرداختی : {float(cust.get('total_paid') or 0):g} USDT",
        f"🧾 تعداد پرداخت : {len(db.list_payments(uid))}",
        LINE,
        "آخرین پرداخت‌ها:",
    ]
    if pays:
        for p in pays:
            rows.append(f"• {p['plan']} — {p['amount']:g}$ — {p['created_at']}")
    else:
        rows.append("— هنوز پرداختی ثبت نشده —")
    await _respond(event, card("👛 کیف پول", rows),
                   buttons=[[Button.inline("🛒 خرید اشتراک", b"buy")],
                            [Button.inline("🔙 بازگشت", b"home")]])


# --------------------------------------------------------------------------- #
# Add account (phone -> code -> optional 2FA).  LOCAL login (master-as-worker).
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"addacc"))
async def addacc_cb(event):
    if not await _gate(event):
        return
    state[event.sender_id] = {"step": "await_phone"}
    await _respond(event,
                   "📱 شماره اکانت روبیکای خودت رو بفرست.\nمثال: `09123456789`",
                   buttons=[[Button.inline("🔙 لغو", b"cancel")]])


async def handle_phone(event, st):
    uid = event.sender_id
    phone = rb.normalize_phone(event.raw_text.strip())
    if not phone or len(phone) < 10:
        await event.respond("شماره نامعتبره. دوباره بفرست.")
        return
    await account_conn.close(phone)
    msg = await event.respond("⏳ در حال ارسال کد ورود ...")
    try:
        ctx = await rb.start_login(phone)
    except Exception as e:  # noqa: BLE001
        state.pop(uid, None)
        await msg.edit(f"❌ خطا در شروع لاگین: {repr(e)[:140]}", buttons=main_menu())
        return
    status = str(ctx.get("status") or "").upper()
    pending_login[uid] = ctx
    if "PASS" in status:
        st["step"] = "await_password"
        await msg.edit("🔐 این اکانت رمز دومرحله‌ای داره. رمز رو بفرست:",
                       buttons=[[Button.inline("🔙 لغو", b"cancel")]])
    else:
        st["step"] = "await_code"
        await msg.edit("✉️ کدی که روبیکا فرستاده رو بفرست:",
                       buttons=[[Button.inline("🔙 لغو", b"cancel")]])


async def handle_password(event, st):
    uid = event.sender_id
    pwd = event.raw_text.strip()
    ctx = pending_login.get(uid)
    if not ctx:
        state.pop(uid, None)
        await event.respond("نشست لاگین منقضی شد. دوباره «افزودن اکانت» رو بزن.",
                            buttons=main_menu())
        return
    phone = ctx["phone"]
    msg = await event.respond("⏳ ارسال کد با رمز دومرحله‌ای ...")
    try:
        ctx = await rb.start_login(phone, pass_key=pwd)
    except Exception as e:  # noqa: BLE001
        await msg.edit(f"❌ رمز پذیرفته نشد: {repr(e)[:140]}")
        return
    pending_login[uid] = ctx
    st["step"] = "await_code"
    await msg.edit("✉️ کدی که روبیکا فرستاده رو بفرست:",
                   buttons=[[Button.inline("🔙 لغو", b"cancel")]])


async def handle_code(event, st):
    uid = event.sender_id
    ctx = pending_login.get(uid)
    if not ctx:
        state.pop(uid, None)
        await event.respond("نشست لاگین منقضی شد. دوباره «افزودن اکانت» رو بزن.",
                            buttons=main_menu())
        return
    code = "".join(ch for ch in event.raw_text if ch.isdigit())
    phone = ctx["phone"]
    msg = await event.respond("⏳ در حال ورود ...")
    try:
        await rb.finish_login(ctx, code)
        client = ctx["client"]
        me = await client.get_me()
        guid = rb._guid_of(me) or "-"
        name = rb._name_of(me)
        _ordered, stats = await rb.get_ordered_recipients(client)
        try:
            await client.disconnect()
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        await msg.edit(f"❌ ورود ناموفق: {repr(e)[:160]}")
        return
    finally:
        pending_login.pop(uid, None)
        state.pop(uid, None)

    # round-robin: pick the healthiest worker with fewest accounts; fall back to master
    w = await worker.pick_worker_for_login()
    if not w:
        w = worker.ensure_master_worker()
    aid = db.add_account(uid, phone, name, str(guid))
    if w:
        db.set_account_worker(aid, w["id"])

    await msg.edit(card("✅ اکانت اضافه شد", [
        f"📛 {name}",
        f"📱 {phone}",
        f"👥 مخاطبین : {stats.get('contacts', 0)}",
        f"💬 چت‌ها : {stats.get('with_chat', 0)}",
    ]), buttons=main_menu())
    await logbus.event("➕ ADD ACCOUNT", [
        f"🆔 Customer : {uid}",
        f"📱 {phone}  ({name})",
        f"👥 مخاطبین : {stats.get('contacts', 0)}",
        f"🕒 {now()}"], pv_user=uid)


# --------------------------------------------------------------------------- #
# Accounts list + per-account menu.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"accounts"))
async def accounts_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    accounts = db.list_accounts(uid)
    if not accounts:
        await _respond(event, "هنوز اکانتی اضافه نکردی.",
                       buttons=[[Button.inline("➕ افزودن اکانت", b"addacc")],
                                [Button.inline("🔙 بازگشت", b"home")]])
        return
    rows = []
    for i, a in enumerate(accounts, 1):
        mark = "" if a["status"] == "active" else " ⚠️"
        rows.append([Button.inline(f"{i}- {a['phone']}{mark}",
                                   f"acc_{a['id']}".encode())])
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    await _respond(event, "👤 اکانت‌های تو:", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"acc_(\\d+)"))
async def account_menu_cb(event):
    if not await _gate(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account_owned(aid, event.sender_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    status = "فعال ✅" if acc["status"] == "active" else "غیرفعال ⚠️ (سشن باطل)"
    await _respond(event, card("👤 اکانت", [
        f"📛 نام : {acc['name'] or '-'}",
        f"📱 شماره : {acc['phone']}",
        f"⭐️ وضعیت : {status}",
    ]), buttons=[
        [Button.inline("🚀 ارسال", f"send_{aid}".encode())],
        [Button.inline("🗑 حذف اکانت", f"del_{aid}".encode())],
        [Button.inline("🔙 بازگشت", b"accounts")],
    ])


@bot.on(events.CallbackQuery(pattern=b"del_(\\d+)"))
async def del_confirm_cb(event):
    if not await _gate(event):
        return
    aid = int(event.pattern_match.group(1))
    if not db.get_account_owned(aid, event.sender_id):
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    await _respond(event, "از حذف این اکانت مطمئنی؟",
                   buttons=[[Button.inline("✅ بله", f"delyes_{aid}".encode())],
                            [Button.inline("🔙 خیر", f"acc_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"delyes_(\\d+)"))
async def del_do_cb(event):
    if not await _gate(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account_owned(aid, event.sender_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    try:
        await account_conn.close(acc["phone"])
    except Exception:
        pass
    db.delete_account(aid)
    await _respond(event, "اکانت حذف شد. ✅",
                   buttons=[[Button.inline("🔙 بازگشت", b"accounts")]])


# --------------------------------------------------------------------------- #
# Account health check (چک‌حساب): count + verify each session.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"health")) 
async def health_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    accounts = db.list_accounts(uid)
    if not accounts:
        await _respond(event, "هنوز اکانتی اضافه نکردی.",
                       buttons=[[Button.inline("➕ افزودن اکانت", b"addacc")],
                                [Button.inline("🔙 بازگشت", b"home")]])
        return
    await _respond(event, "🩺 در حال بررسی سلامت اکانت‌ها ... کمی صبر کن.")
    asyncio.create_task(run_health_check(uid))


async def run_health_check(uid: int):
    accounts = db.list_accounts(uid)
    alive = 0
    dead = 0
    rows = []
    for a in accounts:
        phone = a["phone"]
        try:
            is_dead = await account_conn.verify_session_dead(phone)
        except Exception:
            is_dead = False
        if is_dead:
            dead += 1
            db.set_status(a["id"], "inactive")
            rows.append(f"• {phone} : 🔴 سشن پریده")
        else:
            alive += 1
            if a["status"] != "active":
                db.set_status(a["id"], "active")
            rows.append(f"• {phone} : 🟢 سالم")
    summary = card("🩺 چک‌حساب", [
        f"📊 تعداد کل : {len(accounts)}",
        f"🟢 سالم : {alive}   🔴 پریده : {dead}",
        LINE, *rows, f"🕒 {now()}"])
    await logbus.event("🩺 ACCOUNT HEALTH", [
        f"🆔 {uid}", f"کل {len(accounts)} | 🟢 {alive} | 🔴 {dead}",
        f"🕒 {now()}"])
    try:
        await bot.send_message(uid, summary, buttons=main_menu())
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Marker + speed settings (per customer).
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"marker"))
async def marker_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    state[uid] = {"step": "await_marker"}
    await _respond(event,
                   f"📌 مارکر فعلی: «{db.get_marker(uid)}»\n{LINE}\n"
                   "مارکر جدید رو بفرست (همون متنی که آخرِ کپشنِ پیامِ نشان‌دار توی "
                   "Saved Messages روبیکاته):",
                   buttons=[[Button.inline("🔙 لغو", b"cancel")]])


async def handle_marker(event, st):
    uid = event.sender_id
    marker = event.raw_text.strip()
    state.pop(uid, None)
    if not marker:
        await event.respond("مارکر نمی‌تونه خالی باشه.", buttons=main_menu())
        return
    db.set_marker(uid, marker)
    await event.respond(f"✅ مارکر روی «{marker}» تنظیم شد.", buttons=main_menu())
    await logbus.event("📌 MARKER SET", [
        f"🆔 {uid}", f"📌 متن مارکر مشتری: «{marker}»", f"🕒 {now()}"])


def speed_buttons():
    return [
        [Button.inline("0.2s", b"sp_0.2"), Button.inline("0.5s", b"sp_0.5"),
         Button.inline("1s", b"sp_1")],
        [Button.inline("2s", b"sp_2"), Button.inline("5s", b"sp_5"),
         Button.inline("10s", b"sp_10")],
        [Button.inline("🔙 بازگشت", b"home")],
    ]


@bot.on(events.CallbackQuery(data=b"speed"))
async def speed_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    await _respond(event,
                   f"⏱ تأخیر فعلی: {db.get_delay(uid)} ثانیه\n{LINE}\n"
                   "یک سرعت انتخاب کن:", buttons=speed_buttons())


@bot.on(events.CallbackQuery(pattern=b"sp_([0-9.]+)"))
async def speed_set_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    val = config.clamp_delay(event.pattern_match.group(1).decode())
    db.set_delay(uid, val)
    await _respond(event, f"✅ تأخیر روی {val} ثانیه تنظیم شد.",
                   buttons=[[Button.inline("🔙 منو", b"home")]])


# --------------------------------------------------------------------------- #
# Send (marker) — SAME proven logic as the previous project's run_send.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"send_menu"))
async def send_menu_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    accounts = db.list_accounts(uid)
    if not accounts:
        await _respond(event, "اول یک اکانت اضافه کن.",
                       buttons=[[Button.inline("➕ افزودن اکانت", b"addacc")],
                                [Button.inline("🔙 بازگشت", b"home")]])
        return
    rows = [[Button.inline(f"🚀 {a['phone']}", f"send_{a['id']}".encode())]
            for a in accounts if a["status"] == "active"]
    if not rows:
        await _respond(event, "اکانت فعالی نداری. اول «🩺 چک‌حساب» یا افزودن اکانت.",
                       buttons=[[Button.inline("🔙 بازگشت", b"home")]])
        return
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    await _respond(event, "🚀 از کدوم اکانت ارسال بشه؟", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"send_(\\d+)"))
async def send_prepare_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    aid = int(event.pattern_match.group(1))
    acc = db.get_account_owned(aid, uid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    if aid in active_jobs:
        await event.answer("یک ارسال روی این اکانت در حال اجراست.", alert=True)
        return
    marker = db.get_marker(uid)
    await _respond(event, f"⏳ پیدا کردن پیامِ نشان‌دار «{marker}» و آماده‌سازی لیست ...")
    await account_conn.close(acc["phone"])
    client = rb.open_client(acc["phone"])
    try:
        await rb.connect_ready(client)
        saved_guid, mid = await rb.find_marked_message(client, marker)
        if not mid:
            await bot.send_message(uid,
                f"❌ هیچ پیامی با مارکر «{marker}» توی Saved Messages پیدا نشد.",
                buttons=main_menu())
            return
        ordered, _stats = await rb.get_ordered_recipients(client)
    except account_conn.InvalidAuthError:
        db.set_status(aid, "inactive")
        await bot.send_message(uid, "🔴 سشن این اکانت باطله. دوباره اضافه‌اش کن.",
                               buttons=main_menu())
        return
    except Exception as e:  # noqa: BLE001
        await bot.send_message(uid, f"❌ خطا: {repr(e)[:150]}", buttons=main_menu())
        return
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    recipients = [r["guid"] for r in ordered]
    pending_send[aid] = {"customer_id": uid, "account_id": aid,
                         "phone": acc["phone"], "saved_guid": saved_guid,
                         "mid": mid, "recipients": recipients}
    await bot.send_message(uid, card("🚀 آماده‌ی ارسال", [
        f"📱 {acc['phone']}",
        f"📌 مارکر «{marker}» پیدا شد ✅",
        f"🎯 تعداد گیرنده : {len(recipients)}",
        f"⏱ تأخیر : {db.get_delay(uid)}s",
    ]), buttons=[[Button.inline("✅ شروع ارسال", f"sendgo_{aid}".encode())],
                 [Button.inline("🔙 لغو", b"home")]])


@bot.on(events.CallbackQuery(pattern=b"sendgo_(\\d+)"))
async def send_go_cb(event):
    if not await _gate(event):
        return
    aid = int(event.pattern_match.group(1))
    payload = pending_send.get(aid)
    if not payload or payload["customer_id"] != event.sender_id:
        await event.answer("اطلاعات ارسال منقضی شده. دوباره شروع کن.", alert=True)
        return
    if aid in active_jobs:
        await event.answer("ارسال در حال اجراست.", alert=True)
        return
    await _respond(event, "🚀 ارسال شروع شد. گزارش توی همین چت و گروه لاگ میاد.",
                   buttons=[[Button.inline("⛔ توقف", f"sendstop_{aid}".encode())]])
    asyncio.create_task(run_send(payload))


@bot.on(events.CallbackQuery(pattern=b"sendstop_(\\d+)"))
async def send_stop_cb(event):
    if event.sender_id != event.sender_id:
        return
    aid = int(event.pattern_match.group(1))
    stop_flags[aid] = True
    await event.answer("درخواست توقف ثبت شد.")


async def _wait_or_stop(account_id: int, seconds: float, step: float = 2.0) -> bool:
    waited = 0.0
    while waited < seconds:
        if stop_flags.get(account_id):
            return True
        d = min(step, seconds - waited)
        await asyncio.sleep(d)
        waited += d
    return False


async def run_send(payload: dict):
    """Reused send loop from the previous project (forward the marked message to
    every recipient, MAX_ERRORS cap, auto-resume)."""
    uid = payload["customer_id"]
    account_id = payload["account_id"]
    phone = payload["phone"]
    saved_guid = payload["saved_guid"]
    mid = payload["mid"]
    recipients = payload["recipients"]
    marker = db.get_marker(uid)
    delay = db.get_delay(uid)

    total = len(recipients)
    ok = 0
    fail = 0
    started = datetime.now()
    reason = None
    stop_flags.pop(account_id, None)
    active_jobs.add(account_id)

    await logbus.event("🚀 SEND STARTED", [
        f"🆔 Customer : {uid}",
        f"📱 Phone : {phone}",
        f"🎯 Targets : {total}",
        f"⏱ Delay : {delay}s",
        f"📌 Marker : «{marker}» ✅",
        f"🕒 {now()}"], pv_user=uid)

    n = total
    idx = 0
    retry_count = 0
    await account_conn.close(phone)
    client = rb.open_client(phone)
    try:
        await rb.connect_ready(client)
        while True:
            attempt_fail = 0
            hit_max = False
            while idx < n:
                if stop_flags.get(account_id):
                    reason = "توقف دستی توسط کاربر"
                    break
                guid = recipients[idx]
                idx += 1
                try:
                    await asyncio.wait_for(
                        rb.forward_message(client, saved_guid, guid, mid),
                        timeout=config.SEND_TIMEOUT)
                    ok += 1
                    db.incr_customer_sends(uid, 1)
                    w = worker.ensure_master_worker()
                    if w:
                        db.incr_worker_sent(w["id"], 1)
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    attempt_fail += 1
                    await logbus.to_group(card("⚠️ SEND ERROR", [
                        f"📱 {phone}", f"🎯 {guid}", f"💥 {repr(e)[:160]}"]))
                    if attempt_fail >= config.MAX_ERRORS:
                        hit_max = True
                        break
                await asyncio.sleep(delay)

            if reason:
                break
            if not hit_max:
                break
            if retry_count >= config.RESUME_MAX_RETRIES:
                reason = f"رسیدن به سقف خطا ({config.MAX_ERRORS})"
                break
            retry_count += 1
            remaining = max(0, total - ok - fail)
            await logbus.to_group(card("🚨 ALERT — وقفه ۵ دقیقه‌ای", [
                f"✅ {ok}", f"⏳ {remaining}", f"👤 {phone}"]))
            if await _wait_or_stop(account_id, config.RESUME_WAIT):
                reason = "توقف دستی توسط کاربر"
                break
            try:
                await client.disconnect()
            except Exception:
                pass
            client = rb.open_client(phone)
            await rb.connect_ready(client)
    except account_conn.InvalidAuthError:
        reason = "سشن باطل شد (نیاز به افزودن مجدد اکانت)"
        db.set_status(account_id, "inactive")
    except Exception as e:  # noqa: BLE001
        reason = f"خطای کلی: {repr(e)[:160]}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        active_jobs.discard(account_id)
        stop_flags.pop(account_id, None)
        pending_send.pop(account_id, None)

    dur = str(datetime.now() - started).split(".")[0]
    if reason:
        await logbus.event("⛔ SEND STOPPED", [
            f"🆔 {uid}", f"📱 {phone}",
            f"📊 ✅ {ok}   ❌ {fail}   📁 {total}",
            f"⚠️ {reason}", f"⏱ {dur}", f"🕒 {now()}"], pv_user=uid)
        await _safe_send(uid, f"⛔ ارسال متوقف شد. ✅ {ok} / ❌ {fail} از {total}\nدلیل: {reason}")
    else:
        await logbus.event("✅ SEND FINISHED", [
            f"🆔 {uid}", f"📱 {phone}",
            f"✅ {ok}   ❌ {fail}   📁 {total}", f"⏱ {dur}", f"🕒 {now()}"], pv_user=uid)
        await _safe_send(uid, f"✅ ارسال تمام شد. ✅ {ok} / ❌ {fail} از {total}")


async def _safe_send(uid, text):
    try:
        await bot.send_message(uid, text, buttons=main_menu())
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# PV image -> PDF export (reused logic).
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"pvexport"))
async def pvexport_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    accounts = db.list_accounts(uid)
    if not accounts:
        await event.answer("اول یک اکانت اضافه کن.", alert=True)
        return
    rows = [[Button.inline(f"🖼 {a['phone']}", f"pvx_{a['id']}".encode())]
            for a in accounts if a["status"] == "active"]
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    await _respond(event,
                   "🖼 از کدوم اکانت عکس‌های پیوی جمع بشه و PDF بشه؟\n"
                   "(فقط عکس — فیلم/گیف نه.)", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"pvx_(\\d+)"))
async def pvexport_run_cb(event):
    if not await _gate(event):
        return
    uid = event.sender_id
    aid = int(event.pattern_match.group(1))
    acc = db.get_account_owned(aid, uid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    await _respond(event,
                   f"⏳ جمع‌آوری عکس‌های پیویِ {acc['phone']} ... ممکنه چند دقیقه طول بکشه.",
                   buttons=[[Button.inline("🏠 منو", b"home")]])
    asyncio.create_task(run_pv_export(uid, acc))


async def run_pv_export(uid: int, acc):
    phone = acc["phone"]

    async def _do(client):
        out = []
        guids = await rb.get_chat_list_guids(client, only_users=True)
        for g in guids[:config.PV_EXPORT_MAX_CHATS]:
            async for _mid, fi in rb.iter_chat_photos(client, g):
                try:
                    blob = await rb.download_photo(client, fi)
                    if blob:
                        out.append(blob)
                except Exception:
                    continue
                if len(out) >= config.PV_EXPORT_MAX_PHOTOS:
                    return out
        return out

    try:
        photos = await account_conn.call(phone, _do, timeout=1800)
    except account_conn.InvalidAuthError:
        db.set_status(acc["id"], "inactive")
        await _safe_send(uid, "🔴 سشن این اکانت باطله. دوباره اضافه‌اش کن.")
        return
    except Exception as e:  # noqa: BLE001
        await _safe_send(uid, f"❌ جمع‌آوری ناموفق: {repr(e)[:140]}")
        return

    if not photos:
        await _safe_send(uid, f"ℹ️ هیچ عکسی در پیوی‌های {phone} پیدا نشد.")
        return

    import pdf_export
    out_path = os.path.join(DATA_DIR, f"pv_{phone}_{int(datetime.now().timestamp())}.pdf")
    try:
        n = await asyncio.to_thread(pdf_export.build_pdf, photos, out_path)
        await logbus.event("🖼 PV IMAGE EXPORT", [
            f"🆔 {uid}", f"📱 {phone}", f"🖼 {n} عکس", f"🕒 {now()}"])
        # the import-images file itself is logged to the central group
        await logbus.to_group_file(out_path,
                                   caption=f"🖼 فایل ایمپورت تصاویر — {phone} ({n})")
        await bot.send_file(uid, out_path,
                            caption=f"🖼 آرشیو عکس‌های پیویِ {phone} — {n} عکس",
                            force_document=True)
        await _safe_send(uid, "✅ آرشیو ارسال شد.")
    except Exception as e:  # noqa: BLE001
        await _safe_send(uid, f"❌ ساخت/ارسال PDF ناموفق: {repr(e)[:140]}")
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Free-text router (drives the conversation state machine).
# --------------------------------------------------------------------------- #
@bot.on(events.NewMessage(func=lambda e: e.is_private and not (e.raw_text or "").startswith("/")))
async def text_router(event):
    uid = event.sender_id
    st = state.get(uid)
    if not st:
        return
    # maintenance + anti-flood still apply to free-text steps
    if db.maintenance_on():
        await event.respond("🛠 ربات در حال تعمیر است.")
        return
    user = await event.get_sender()
    if not await ratelimit.guard(uid, getattr(user, "first_name", "") or ""):
        await event.respond("⛔ به‌خاطر فعالیت بیش از حد، حساب شما مسدود شد.")
        return

    step = st.get("step")
    if step == "await_txhash":
        await handle_txhash(event, st)
    elif step == "await_phone":
        await handle_phone(event, st)
    elif step == "await_password":
        await handle_password(event, st)
    elif step == "await_code":
        await handle_code(event, st)
    elif step == "await_marker":
        await handle_marker(event, st)


# --------------------------------------------------------------------------- #
# Background loop: expiry warning (2 days) + auto-expire notice.
# --------------------------------------------------------------------------- #
async def expiry_loop():
    while True:
        try:
            for cu in db.list_customers():
                uid = cu["telegram_id"]
                if cu.get("blocked"):
                    continue
                d = db.days_left(uid)
                if 0 < d <= config.EXPIRY_WARN_DAYS and not cu.get("warned"):
                    db.set_warned(uid, True)
                    await logbus.to_pv(uid,
                        f"⏰ هشدار: فقط {d} روز از اشتراکت مونده. "
                        "برای تمدید «🛒 خرید اشتراک» رو بزن.")
                    await logbus.to_group(card("⏰ EXPIRY WARNING", [
                        f"🆔 {uid}", f"{d} روز مونده", f"🕒 {now()}"]))
        except Exception as e:  # noqa: BLE001
            print(f"[expiry loop] {e}")
        await asyncio.sleep(3600)


# --------------------------------------------------------------------------- #
# Entrypoint.
# --------------------------------------------------------------------------- #
async def amain():
    problems = config.validate_customer()
    if problems:
        raise SystemExit("تنظیمات ناقصه (.env): " + ", ".join(problems))
    db.init()
    account_conn.set_invalid_auth_handler(_on_invalid_auth)
    account_conn.start_janitor()
    worker.ensure_master_worker()
    await bot.start(bot_token=config.CUSTOMER_BOT_TOKEN)
    logbus.bind(bot)
    await logbus.to_group(card("🤖 CUSTOMER BOT ONLINE", [
        f"🏷 Version : {config.VERSION}", f"🕒 {now()}"]))
    asyncio.create_task(expiry_loop())
    print("customer bot running")
    await bot.run_until_disconnected()


async def _on_invalid_auth(phone: str):
    try:
        for a in db.list_accounts():
            if rb.normalize_phone(a["phone"]) == rb.normalize_phone(phone):
                db.set_status(a["id"], "inactive")
                break
    except Exception:
        pass
