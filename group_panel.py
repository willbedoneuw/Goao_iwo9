# -*- coding: utf-8 -*-
"""
group_panel.py — the GROUP (Config) section of the customer bot.
================================================================

Lets a customer install the bot in their OWN Telegram group and drive sending
from there. Fully decoupled (own handlers, own DB table group_config). NEVER
touches the Rubika/Telegram/Bale send code beyond REUSING customer_bot.run_send
through a reference passed into setup().

Hard rule: NOTHING here may crash or freeze the customer bot. Every handler is
wrapped, every external call is guarded. A bug in the group panel must never
take the bot offline.

Wiring: customer_bot.amain() calls group_panel.setup(bot, run_send=run_send,
state=..., tg_state=..., bale_state=...).

Behaviour:
  * The bot only acts in groups that exist in group_config (a customer's group).
  * In a configured group, ONLY the configured admin_ids get answered; every
    other message is ignored (but still logged to the central group for the
    owner — group id + customer + sender + full text, truncated).
  * Commands (and inline buttons): /menu /send /stop /status /accounts
    /content /settings /help.
  * Sending REUSES the proven run_send engine (marker-based for now).
"""
import asyncio

from telethon import events, Button

import config
import db
import logbus

# injected in setup()
bot = None
_run_send = None          # customer_bot.run_send reference
_active_jobs = None       # customer_bot.active_jobs set (per-account guard)
_stop_flags = None        # customer_bot.stop_flags dict
_pending_send = None      # customer_bot.pending_send dict
_customer_active_account = None  # customer_bot.customer_active_account (per-customer guard)

# Rubika/Telegram/Bale modules (imported lazily in setup so import stays light)
rb = worker = account_conn = None

LINE = logbus.LINE


def now():
    return config.now_str()


def card(title, rows):
    return logbus.card(title, rows)


# --------------------------------------------------------------------------- #
# Tiny safe helpers (never raise out).
# --------------------------------------------------------------------------- #
async def _safe_reply(event, text, buttons=None):
    try:
        await event.reply(text, buttons=buttons)
    except Exception:
        try:
            await bot.send_message(event.chat_id, text, buttons=buttons)
        except Exception:
            pass


async def _safe_send(chat_id, text, buttons=None):
    try:
        return await bot.send_message(chat_id, text, buttons=buttons)
    except Exception:
        return None


async def _safe_edit(chat_id, msg_id, text, buttons=None):
    try:
        await bot.edit_message(chat_id, msg_id, text, buttons=buttons)
    except Exception:
        pass


def _group_menu():
    return [
        [Button.inline("🚀 شروع ارسال", b"g_send"),
         Button.inline("📊 وضعیت", b"g_status")],
        [Button.inline("📱 اکانت‌ها", b"g_accounts"),
         Button.inline("📌 مارکر", b"g_content")],
        [Button.inline("⚙️ تنظیمات", b"g_settings")],
        [Button.inline("📖 راهنما", b"g_help")],
    ]


# --------------------------------------------------------------------------- #
# Central logging of EVERYTHING that happens in a customer's group (owner-only).
# The customer never sees these — they go to the central log group.
# --------------------------------------------------------------------------- #
async def _log_group_event(title, cfg, rows):
    try:
        gid = (cfg or {}).get("group_id")
        cust = (cfg or {}).get("customer_id")
        head = [f"💬 گروه : {gid}", f"👤 مشتری : {cust}"]
        await logbus.to_group(card(title, head + rows + [f"🕒 {now()}"]))
    except Exception:
        pass


async def _log_incoming_message(event, cfg):
    """Log ANY message in a configured group (from anyone — admin, member, other
    bot) to the central group, with full (truncated) text + sender + group."""
    try:
        sender = await event.get_sender()
        sname = getattr(sender, "first_name", "") or getattr(sender, "title", "") or "-"
        suser = getattr(sender, "username", "") or ""
        sid = getattr(event, "sender_id", None)
        txt = (event.raw_text or "")[:config.GROUP_LOG_TEXT_MAX] or "(بدون متن / رسانه)"
        await _log_group_event("📡 GROUP MESSAGE", cfg, [
            f"👁 فرستنده : {sname}" + (f" (@{suser})" if suser else "") + f"  [{sid}]",
            f"📝 {txt}",
        ])
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Status / panels (read-only, safe).
# --------------------------------------------------------------------------- #
def _accounts_summary(cust_id):
    try:
        accs = db.list_accounts(cust_id)
        active = sum(1 for a in accs if a.get("status") == "active")
        return len(accs), active
    except Exception:
        return 0, 0


async def _panel_text(cfg):
    cust = cfg.get("customer_id")
    total, active = _accounts_summary(cust)
    enabled = "🟢 روشن" if cfg.get("enabled") else "🔴 خاموش"
    try:
        marker = db.get_marker(cust)
    except Exception:
        marker = "-"
    return card("🤖 پنل ارسال گروهی", [
        f"📱 اکانت‌ها : {active} فعال / {total - active} غیرفعال",
        f"📌 مارکر : «{marker}»",
        f"🕒 آخرین ارسال : {cfg.get('last_send_at') or '-'}",
        f"⚡ وضعیت : {enabled}",
    ])


async def _show_menu(event, cfg):
    await _safe_reply(event, await _panel_text(cfg), buttons=_group_menu())


async def _show_status(event, cfg):
    cust = cfg.get("customer_id")
    total, active = _accounts_summary(cust)
    try:
        marker = db.get_marker(cust)
    except Exception:
        marker = "-"
    await _safe_reply(event, card("📊 وضعیت", [
        f"📱 اکانت‌ها : {active} فعال / {total} کل",
        f"📌 مارکر : «{marker}»",
        f"⚡ ربات : {'🟢 روشن' if cfg.get('enabled') else '🔴 خاموش'}",
        f"🕒 آخرین ارسال : {cfg.get('last_send_at') or '-'}",
    ]), buttons=[[Button.inline("🏠 منو", b"g_menu")]])


async def _show_accounts(event, cfg):
    try:
        accs = db.list_accounts(cfg.get("customer_id"))
    except Exception:
        accs = []
    if not accs:
        await _safe_reply(event, "اکانتی نداری. از PV ربات اضافه کن.",
                          buttons=[[Button.inline("🏠 منو", b"g_menu")]])
        return
    rows = []
    for i, a in enumerate(accs, 1):
        emoji = "🟢" if a.get("status") == "active" else "🔴"
        rows.append(f"{emoji} {i}- {a['phone']}")
    await _safe_reply(event, card("📱 اکانت‌های شما", rows),
                      buttons=[[Button.inline("🏠 منو", b"g_menu")]])


async def _show_content(event, cfg):
    cust = cfg.get("customer_id")
    try:
        marker = db.get_marker(cust)
    except Exception:
        marker = "-"
    await _safe_reply(event, card("📌 مارکر", [
        f"مارکرِ فعلی : «{marker}»",
        LINE,
        "ربات پیامی که توی Saved Messages اکانت با این مارکر علامت‌گذاری شده رو",
        "پیدا و ارسال می‌کنه. مارکر برای هم گروه هم پیوی یکیه.",
        "برای تغییرِ مارکر، از PV ربات بخش «📌 مارکر» رو بزن.",
    ]), buttons=[[Button.inline("🏠 منو", b"g_menu")]])


async def _show_settings(event, cfg):
    admins = ", ".join(str(x) for x in sorted(db.group_admin_ids(cfg))) or "-"
    await _safe_reply(event, card("⚙️ تنظیمات", [
        f"👤 ادمین‌ها : {admins}",
        f"💬 گروه : {cfg.get('group_id')}",
        f"🔄 ربات : {'🟢 روشن' if cfg.get('enabled') else '🔴 خاموش'}",
    ]), buttons=[[Button.inline(
        "🔴 خاموش کن" if cfg.get("enabled") else "🟢 روشن کن", b"g_toggle")],
        [Button.inline("🏠 منو", b"g_menu")]])


async def _show_help(event):
    await _safe_reply(event, card("📖 راهنمای ربات", [
        "🚀 /send — انتخاب اکانت و شروع ارسال",
        "🚀 /send_<شماره یا شماره‌تلفن> — ارسال با اون اکانت (از /accounts)",
        "📊 /status — وضعیت فعلی",
        "📱 /accounts — لیست اکانت‌ها",
        "📦 /content — نمایش مارکر (محتوای ارسالی)",
        "⚙️ /settings — تنظیمات (روشن/خاموش)",
        "🏠 /menu — منوی اصلی",
        "❓ /help — این راهنما",
        LINE,
        "⛔ توقفِ ارسال: روی دکمهٔ «توقف» که موقع ارسال میاد بزن.",
        "📊 موقع ارسال یه نوارِ پیشرفتِ زنده همین‌جا (و در PV ربات) نشون داده می‌شه.",
        "ℹ️ هر زمان فقط یک ارسالِ هم‌زمان مجازه؛ تا ارسالِ فعلی تموم/متوقف نشه، "
        "ارسالِ جدید رد می‌شه.",
        "⚠️ فقط ادمین‌های ست‌شده می‌تونن دستور بدن؛ بقیه نادیده گرفته می‌شن.",
    ]), buttons=[[Button.inline("🏠 منو", b"g_menu")]])


# --------------------------------------------------------------------------- #
# Send (reuses the proven run_send engine, marker-based).
# --------------------------------------------------------------------------- #
_PD_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _digits(s) -> str:
    return "".join(c for c in str(s or "").translate(_PD_MAP) if c.isdigit())


def _resolve_account(accs, arg):
    """Resolve /send_<arg> to an account. `arg` may be the row number shown in
    /accounts (1-based) OR the account phone (any format: 0937..., 9890..., +98,
    Persian digits — matched by the last 10 digits)."""
    d = _digits(arg)
    if not d:
        return None
    # short number -> row index (matches /accounts numbering)
    if len(d) <= 4 and 1 <= int(d) <= len(accs):
        return accs[int(d) - 1]
    # otherwise treat as a phone: match by the last 10 digits
    tail = d[-10:]
    if len(tail) >= 7:
        for a in accs:
            if _digits(a.get("phone"))[-10:] == tail:
                return a
    return None


async def _active_accounts(cust):
    try:
        return [a for a in db.list_accounts(cust) if a.get("status") == "active"]
    except Exception:
        return []


async def _show_account_picker(event, cfg):
    """Show buttons to pick WHICH account sends (no auto-pick — clear & explicit)."""
    cust = cfg.get("customer_id")
    if not cfg.get("enabled"):
        await _safe_reply(event, "🔴 ربات خاموشه. از «⚙️ تنظیمات» روشنش کن.",
                          buttons=[[Button.inline("🏠 منو", b"g_menu")]])
        return
    accs_all = []
    try:
        accs_all = db.list_accounts(cust)
    except Exception:
        accs_all = []
    rows = [[Button.inline(f"🚀 {i}- {a['phone']}", f"g_go_{a['id']}".encode())]
            for i, a in enumerate(accs_all, 1) if a.get("status") == "active"]
    if not rows:
        await _safe_reply(event, "⚠️ اکانت فعالی نداری. از PV ربات اضافه کن.",
                          buttons=[[Button.inline("🏠 منو", b"g_menu")]])
        return
    rows.append([Button.inline("🏠 منو", b"g_menu")])
    await _safe_reply(event, card("🚀 انتخاب اکانتِ ارسال", [
        "با کدوم اکانت ارسال بشه؟ یکی رو انتخاب کن:",
        "(یا دستورِ /send_<شماره> رو بزن — شماره از /accounts)",
    ]), buttons=rows)


async def _start_send(event, cfg, aid):
    """Send with a SPECIFIC account (chosen explicitly). Guarded end-to-end so a
    failure can NEVER crash the bot. Reuses the proven run_send engine."""
    cust = cfg.get("customer_id")
    if not cfg.get("enabled"):
        await _safe_reply(event, "🔴 ربات خاموشه. از «⚙️ تنظیمات» روشنش کن.")
        return
    try:
        acc = db.get_account_owned(int(aid), cust)
    except Exception:
        acc = None
    if not acc:
        await _safe_reply(event, "اکانت پیدا نشد.")
        return
    if acc.get("status") != "active":
        await _safe_reply(event, "این اکانت فعال نیست (سشن باطل). از PV چک‌حساب کن.")
        return
    aid = acc["id"]
    if _active_jobs is None:
        await _safe_reply(event, "سرویس ارسال آماده نیست. کمی بعد دوباره امتحان کن.")
        return
    if aid in _active_jobs:
        await _safe_reply(event, "یک ارسال روی این اکانت در حال اجراست.",
                          buttons=[[Button.inline("⛔ توقف", b"g_stop")]])
        return
    # one customer = one concurrent send: reject if ANY other account of this
    # customer is already sending (checked right before the atomic reserve, no
    # await in between, so two rapid /send on different accounts can't race).
    if _customer_active_account is not None:
        try:
            busy = _customer_active_account(cust, exclude_aid=aid)
        except Exception:
            busy = None
        if busy:
            await _safe_reply(event,
                f"⛔ همین حالا یک ارسال با اکانت {busy['phone']} در جریانه. "
                "هر مشتری هم‌زمان فقط یک ارسال می‌تونه داشته باشه — "
                "اول اون تموم یا متوقف بشه.",
                buttons=[[Button.inline("⛔ توقف", b"g_stop")]])
            return
    # RESERVE the account NOW — atomic (no await between the check above and this
    # add). Without it, two rapid /send on the same account would BOTH pass the
    # check (run_send adds active_jobs only later) and open TWO connections to
    # the same Rubika session => AUTH_FROM_ANOTHER / session revoked.
    _active_jobs.add(aid)
    launched = False
    try:
        marker = db.get_marker(cust)
        await _safe_reply(event, f"⏳ آماده‌سازی ارسال با اکانت {acc['phone']} ...")

        # Build payload (local/remote) — mirrors send_prepare_cb, fully guarded.
        try:
            w = worker.worker_for_account(acc)
            if w and not worker.is_local(w):
                data = await asyncio.wait_for(
                    worker.api_call(w, "POST", "/prepare",
                                    {"phone": acc["phone"], "marker": marker},
                                    timeout=180), timeout=200)
                if not data.get("marker_found"):
                    await _safe_reply(event,
                        f"❌ پیامی با مارکر «{marker}» تو Saved پیدا نشد. "
                        "اول از PV ربات مارکر/محتوا رو ست کن.")
                    return
                payload = {"customer_id": cust, "account_id": aid,
                           "phone": acc["phone"], "remote": True, "worker": w,
                           "total": data.get("total", 0)}
            else:
                await account_conn.close(acc["phone"])
                client = rb.open_client(acc["phone"])
                try:
                    await asyncio.wait_for(rb.connect_ready(client), timeout=60)
                    saved_guid, mid = await asyncio.wait_for(
                        rb.find_marked_message(client, marker), timeout=120)
                    if not mid:
                        await _safe_reply(event,
                            f"❌ پیامی با مارکر «{marker}» تو Saved پیدا نشد. "
                            "از PV ربات، بخش «📌 مارکر» تنظیمش کن.")
                        return
                    ordered, _stats = await asyncio.wait_for(
                        rb.get_ordered_recipients(client), timeout=180)
                finally:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                payload = {"customer_id": cust, "account_id": aid,
                           "phone": acc["phone"], "saved_guid": saved_guid,
                           "mid": mid, "recipients": [r["guid"] for r in ordered]}
        except Exception as e:  # noqa: BLE001
            await _safe_reply(event, f"❌ آماده‌سازی ناموفق: {repr(e)[:140]}")
            await _log_group_event("❌ GROUP SEND PREP ERROR", cfg,
                                   [f"💥 {repr(e)[:160]}"])
            return

        # SETTLE: the prep above opened+closed a connection to this session.
        # Give Rubika a moment to fully release it BEFORE run_send opens a new
        # one — a rapid reconnect on the same session triggers AUTH_FROM_ANOTHER.
        await asyncio.sleep(getattr(config, "GROUP_SEND_SETTLE_SEC", 5))

        db.touch_group_send(cfg.get("group_id"))
        await _log_group_event("🚀 GROUP SEND START", cfg,
                               [f"📱 {acc['phone']}",
                                f"🎯 {payload.get('total') or len(payload.get('recipients', []))}"])
        # this message becomes the LIVE progress bar inside the group — run_send
        # edits it in place. We need its id, so use _safe_send (returns the msg).
        gm = await _safe_send(event.chat_id, card("🚀 ارسال شروع شد", [
            f"📱 {acc['phone']}", "⏳ آماده‌سازی پیشرفتِ زنده ..."]),
            buttons=[[Button.inline("⛔ توقف", b"g_stop")]])
        payload["live_chat"] = event.chat_id
        payload["live_msg"] = getattr(gm, "id", None)
        payload["live_stop"] = b"g_stop"
        if _run_send is not None:
            asyncio.create_task(_safe_run_send(payload, cfg))
            launched = True
    finally:
        # release the reservation ONLY if we didn't hand off to run_send (run_send
        # owns active_jobs lifecycle once launched and clears it when finished).
        if not launched:
            _active_jobs.discard(aid)


async def _safe_run_send(payload, cfg):
    aid = payload.get("account_id")
    try:
        await _run_send(payload)
        await _log_group_event("🏁 GROUP SEND DONE", cfg, [f"📱 {payload.get('phone')}"])
    except Exception as e:  # noqa: BLE001
        await _log_group_event("❌ GROUP SEND ERROR", cfg, [f"💥 {repr(e)[:160]}"])
    finally:
        # guarantee the per-account reservation is released no matter what (so an
        # account can never get permanently stuck as 'busy').
        if _active_jobs is not None and aid is not None:
            _active_jobs.discard(aid)


async def _do_stop(event, cfg):
    cust = cfg.get("customer_id")
    stopped = 0
    try:
        for a in db.list_accounts(cust):
            if _stop_flags is not None:
                _stop_flags[a["id"]] = True
                stopped += 1
    except Exception:
        pass
    await _safe_reply(event, "⛔ درخواست توقف ثبت شد. بعد از ارسالِ جاری متوقف می‌شه.")
    await _log_group_event("⛔ GROUP SEND STOP", cfg, [f"by admin {event.sender_id}"])


# --------------------------------------------------------------------------- #
# Routers (all wrapped — a bug here must never crash the bot).
# --------------------------------------------------------------------------- #
def _gate_customer(cust):
    """Mirror customer_bot._gate for the GROUP context: block / maintenance /
    subscription validity. Returns (ok: bool, message: str|None).

    A blocked or unpaid customer must NOT be able to send via their group and
    bypass the PV gate. Fully guarded — on any unexpected error it FAILS OPEN
    (allows) so a transient DB hiccup can never lock a legit customer out of
    their own group; the real enforcement runs whenever the DB answers."""
    try:
        if cust is None:
            return True, None
        if db.is_blocked(cust):
            return False, ("⛔ حساب شما مسدود است. برای رفع مسدودی با پشتیبانی "
                           "در تماس باش.")
        if db.maintenance_on():
            return False, "🛠 ربات در حال تعمیر است. کمی بعد دوباره امتحان کن."
        if config.FREE_MODE:
            c = db.get_customer(cust) or {}
            if (c.get("expires_at") or "") and db.seconds_left(cust) <= 0:
                return False, "🔴 زمانِ دسترسی‌ات تموم شده. با پشتیبانی تماس بگیر."
        elif not db.is_active(cust):
            return False, "🔴 برای استفاده، اول اشتراک تهیه کن (از PV ربات)."
    except Exception:
        return True, None
    return True, None


async def _group_msg_router(event):
    try:
        if event.is_private:
            return
        cfg = db.get_group_config(event.chat_id)
        if not cfg:
            return  # not a customer's configured group -> ignore entirely
        # log EVERYTHING (owner-only), regardless of sender
        await _log_incoming_message(event, cfg)
        # only configured admins get answered
        admins = db.group_admin_ids(cfg)
        if not admins or event.sender_id not in admins:
            return
        txt = (event.raw_text or "").strip()
        if not txt.startswith("/"):
            return
        cmd = txt.split()[0].lstrip("/").split("@")[0].lower()
        # block / maintenance / subscription gate (mirrors PV _gate) — a blocked
        # or unpaid customer can't act in their group. /help stays open so they
        # can still read how to reach support.
        if cmd != "help":
            ok, gmsg = _gate_customer(cfg.get("customer_id"))
            if not ok:
                await _safe_reply(event, gmsg)
                await _log_group_event("⛔ GROUP ACTION BLOCKED", cfg,
                                       [f"cmd=/{cmd}", f"by {event.sender_id}"])
                return
        if cmd in ("menu", "start", "panel"):
            await _show_menu(event, cfg)
        elif cmd == "send":
            await _show_account_picker(event, cfg)
        elif cmd.startswith("send_") and len(cmd) > 5:
            # /send_<arg> : arg = row number (from /accounts) OR account phone
            try:
                accs = db.list_accounts(cfg.get("customer_id")) or []
            except Exception:
                accs = []
            acc = _resolve_account(accs, cmd[5:])
            if acc:
                await _start_send(event, cfg, acc["id"])
            else:
                await _safe_reply(event,
                    "شماره یا شماره‌تلفنِ اکانت نامعتبره. /accounts رو ببین.")
        elif cmd == "status":
            await _show_status(event, cfg)
        elif cmd == "accounts":
            await _show_accounts(event, cfg)
        elif cmd == "content":
            await _show_content(event, cfg)
        elif cmd == "settings":
            await _show_settings(event, cfg)
        elif cmd == "help":
            await _show_help(event)
    except Exception as e:  # noqa: BLE001
        print(f"[group router] {e}")


async def _group_cb_router(event):
    try:
        if event.is_private:
            return
        cfg = db.get_group_config(event.chat_id)
        if not cfg:
            await event.answer()
            return
        admins = db.group_admin_ids(cfg)
        if not admins or event.sender_id not in admins:
            await event.answer("فقط ادمین‌های ست‌شده.", alert=True)
            return
        data = (event.data or b"").decode(errors="ignore")
        # block / maintenance / subscription gate (mirrors PV _gate). g_stop and
        # g_help bypass it: a customer must ALWAYS be able to stop a running send
        # and read the help, even if their account just got blocked/expired.
        if data not in ("g_stop", "g_help"):
            ok, gmsg = _gate_customer(cfg.get("customer_id"))
            if not ok:
                await event.answer(gmsg, alert=True)
                await _log_group_event("⛔ GROUP ACTION BLOCKED", cfg,
                                       [f"cb={data}", f"by {event.sender_id}"])
                return
        if data == "g_menu":
            await _show_menu(event, cfg)
        elif data == "g_send":
            await _show_account_picker(event, cfg)
        elif data.startswith("g_go_") and data[5:].isdigit():
            await _start_send(event, cfg, int(data[5:]))
        elif data == "g_stop":
            await _do_stop(event, cfg)
        elif data == "g_status":
            await _show_status(event, cfg)
        elif data == "g_accounts":
            await _show_accounts(event, cfg)
        elif data == "g_content":
            await _show_content(event, cfg)
        elif data == "g_settings":
            await _show_settings(event, cfg)
        elif data == "g_help":
            await _show_help(event)
        elif data == "g_toggle":
            db.set_group_enabled(cfg["group_id"], not cfg.get("enabled"))
            cfg = db.get_group_config(event.chat_id)
            await _show_settings(event, cfg)
            await _log_group_event("⚙️ GROUP TOGGLE", cfg,
                                   [f"enabled={cfg.get('enabled')}"])
        await event.answer()
    except Exception as e:  # noqa: BLE001
        print(f"[group cb] {e}")
        try:
            await event.answer()
        except Exception:
            pass


async def _chat_action_router(event):
    """Bot added to / removed from a group."""
    try:
        gid = event.chat_id
        cfg = db.get_group_config(gid)
        me = await bot.get_me()
        if event.user_added or event.user_joined:
            # was it US that got added?
            if me.id in (event.user_ids or []) or getattr(event, "user_id", None) == me.id:
                if cfg:
                    db.set_group_installed(gid, True)
                    await _safe_send(gid, card("✅ ربات با موفقیت نصب شد!", [
                        "🤖 ربات ارسال آماده‌ی کاره.",
                        LINE,
                        "📌 دستورات:",
                        "  /send — انتخاب اکانت و شروع ارسال",
                        "  /send_<شماره> — ارسال با اکانتِ شماره‌دار",
                        "  /status — وضعیت",
                        "  /menu — منوی اصلی",
                        "  /help — راهنما",
                        LINE,
                        "⛔ توقف: دکمهٔ «توقف» موقع ارسال.",
                        "👤 فقط ادمین‌های ست‌شده می‌تونن دستور بدن.",
                        "📦 محتوا و اکانت رو از PV ربات تنظیم کن.",
                        "🟢 آماده‌ی ارسال!",
                    ]), buttons=_group_menu())
                    await _log_group_event("✅ BOT INSTALLED IN GROUP", cfg, [])
                else:
                    await _log_group_event("⚠️ BOT ADDED TO UNCONFIGURED GROUP",
                                           {"group_id": gid, "customer_id": "?"}, [])
        elif event.user_kicked or event.user_left:
            if (me.id in (event.user_ids or [])
                    or getattr(event, "user_id", None) == me.id) and cfg:
                db.set_group_installed(gid, False)
                await _log_group_event("🗑 BOT REMOVED FROM GROUP", cfg, [])
    except Exception as e:  # noqa: BLE001
        print(f"[group chat-action] {e}")


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def setup(shared_bot, run_send=None, active_jobs=None, stop_flags=None,
          pending_send=None, customer_active_account=None):
    """Register group handlers. Called once from customer_bot.amain().
    run_send/active_jobs/stop_flags/pending_send are customer_bot references
    (so we REUSE the proven send engine instead of duplicating it)."""
    global bot, _run_send, _active_jobs, _stop_flags, _pending_send
    global _customer_active_account
    global rb, worker, account_conn
    bot = shared_bot
    _run_send = run_send
    _active_jobs = active_jobs
    _stop_flags = stop_flags
    _pending_send = pending_send
    _customer_active_account = customer_active_account

    import rubika_client as _rb
    import worker as _w
    import account_conn as _ac
    rb, worker, account_conn = _rb, _w, _ac

    add = bot.add_event_handler
    # group text/commands (non-private only)
    add(_group_msg_router, events.NewMessage(func=lambda e: not e.is_private))
    # group inline buttons (g_*)
    add(_group_cb_router, events.CallbackQuery(pattern=b"g_(menu|send|stop|status|"
                                               b"accounts|content|settings|help|toggle|"
                                               b"go_\\d+)"))
    # bot added/removed
    add(_chat_action_router, events.ChatAction())
    print("[group] Group section wired up.")
    return True
