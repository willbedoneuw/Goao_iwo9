"""
owner_bot.py — the CENTRAL PANEL bot (owner only).
==================================================

Only the configured OWNER may use it. It manages the whole service:

  * dashboard stats (customers, accounts, sends, revenue, overall health)
  * per-customer profile + customer search + customer ranking
  * add a customer manually + add/subtract subscription time + instant block /
    unblock
  * broadcast a message to every customer
  * worker management + worker logs (reused worker subsystem)
  * maintenance mode toggle + on-demand system backup

The owner panel is the admin: it reads/writes BOTH the operational customer DB
(db.py) and the owner-only central DB (central_db.py). Broadcasts are delivered
THROUGH the customer bot's chats — the owner bot can message a customer because
Telegram lets a bot DM any user who has started it; here we DM via this panel
bot, and customers are also notified inside their own customer-bot chat by the
customer bot's own loops. Privileged actions are logged to the central group
and the audit log.
"""
import asyncio
import os

from telethon import TelegramClient, events, Button

import backup
import central_db
import config
import crypto_util
import db
import logbus
import worker

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

bot = TelegramClient(os.path.join(DATA_DIR, "owner_bot"),
                     config.API_ID, config.API_HASH)

LINE = logbus.LINE
state: dict = {}


def now() -> str:
    return config.now_str()


def card(title, rows):
    return logbus.card(title, rows)


def is_owner(event) -> bool:
    return config.OWNER_ID and event.sender_id == config.OWNER_ID


async def safe_edit(event, text, buttons=None):
    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        try:
            await bot.send_message(event.sender_id, text, buttons=buttons)
        except Exception:
            pass


def main_menu():
    return [
        [Button.inline("📊 داشبورد", b"dash"),
         Button.inline("👥 مشتری‌ها", b"customers")],
        [Button.inline("🔎 جستجوی مشتری", b"search"),
         Button.inline("🏆 رتبه‌بندی", b"ranking")],
        [Button.inline("➕ افزودن مشتری", b"addcust"),
         Button.inline("📣 پیام همگانی", b"broadcast")],
        [Button.inline("🛠 ورکرها", b"workers"),
         Button.inline("🧰 تعمیر/بکاپ", b"sys")],
    ]


WELCOME = "🎛 پنل مرکزی روبیکا تولز\nیکی از گزینه‌ها رو انتخاب کن:"


@bot.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    if not is_owner(event):
        await event.respond("⛔ این پنل فقط برای مالکه.")
        return
    state.pop(event.sender_id, None)
    await event.respond(WELCOME, buttons=main_menu())


@bot.on(events.CallbackQuery(data=b"home"))
async def home_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, WELCOME, buttons=main_menu())


@bot.on(events.CallbackQuery(data=b"cancel"))
async def cancel_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, "لغو شد.", buttons=main_menu())


# --------------------------------------------------------------------------- #
# Dashboard.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"dash"))
async def dash_cb(event):
    if not is_owner(event):
        return
    s = db.stats()
    workers = db.list_workers()
    healthy = sum(1 for w in workers if w.get("status") == "ok")
    health_pct = (s["active_accounts"] / s["accounts"] * 100) if s["accounts"] else 0
    await safe_edit(event, card("📊 داشبورد", [
        f"👥 مشتری‌ها : {s['customers']}  (فعال: {s['active_subs']} | مسدود: {s['blocked']})",
        f"📱 اکانت‌ها : {s['accounts']}  (فعال: {s['active_accounts']})",
        f"🚀 کل ارسال : {s['sends']}",
        f"💰 درآمد : {s['revenue']:g} USDT",
        f"🩺 سلامت کلی اکانت‌ها : {health_pct:.0f}%",
        f"🛠 ورکرها : {len(workers)} (سالم: {healthy})",
        f"🕒 {now()}",
    ]), buttons=[[Button.inline("🔄 تازه‌سازی", b"dash")],
                 [Button.inline("🔙 بازگشت", b"home")]])


# --------------------------------------------------------------------------- #
# Customers list / profile.
# --------------------------------------------------------------------------- #
def _cust_buttons(customers):
    rows = []
    for c in customers[:30]:
        d = db.days_left(c["telegram_id"])
        tag = "⛔" if c.get("blocked") else ("🟢" if d > 0 else "🔴")
        label = f"{tag} {c.get('name') or c['telegram_id']} ({d}d)"
        rows.append([Button.inline(label, f"cust_{c['telegram_id']}".encode())])
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    return rows


@bot.on(events.CallbackQuery(data=b"customers"))
async def customers_cb(event):
    if not is_owner(event):
        return
    customers = db.list_customers()
    if not customers:
        await safe_edit(event, "هنوز مشتری‌ای نداری.",
                        buttons=[[Button.inline("🔙 بازگشت", b"home")]])
        return
    await safe_edit(event, f"👥 مشتری‌ها ({len(customers)}):",
                    buttons=_cust_buttons(customers))


def _profile_text(c) -> str:
    uid = c["telegram_id"]
    d = db.days_left(uid)
    status = "⛔ مسدود" if c.get("blocked") else ("🟢 فعال" if d > 0 else "🔴 منقضی")
    return card("👤 پروفایل مشتری", [
        f"📛 نام : {c.get('name') or '-'}",
        f"🔗 یوزرنیم : @{c['username']}" if c.get("username") else "🔗 یوزرنیم : -",
        f"🆔 آیدی : {uid}",
        f"⭐️ وضعیت : {status}",
        f"⏳ روز باقی‌مانده : {d}",
        f"📅 انقضا : {c.get('expires_at') or '-'}",
        f"📱 اکانت‌ها : {db.count_customer_accounts(uid)}",
        f"🚀 کل ارسال : {c.get('total_sends') or 0}",
        f"💵 مجموع پرداخت : {float(c.get('total_paid') or 0):g} USDT",
        f"📝 یادداشت : {c.get('note') or '-'}",
    ])


def _profile_buttons(uid, blocked):
    rows = [
        [Button.inline("➕۳ روز", f"addt_{uid}_3".encode()),
         Button.inline("➕۷ روز", f"addt_{uid}_7".encode()),
         Button.inline("➕۳۰ روز", f"addt_{uid}_30".encode())],
        [Button.inline("➖۱ روز", f"addt_{uid}_-1".encode()),
         Button.inline("➖۷ روز", f"addt_{uid}_-7".encode()),
         Button.inline("⌨️ زمان دلخواه", f"custt_{uid}".encode())],
    ]
    if blocked:
        rows.append([Button.inline("✅ رفع مسدودی", f"unblock_{uid}".encode())])
    else:
        rows.append([Button.inline("⛔ مسدودسازی فوری", f"block_{uid}".encode())])
    rows.append([Button.inline("📝 یادداشت", f"note_{uid}".encode())])
    rows.append([Button.inline("🔙 بازگشت", b"customers")])
    return rows


@bot.on(events.CallbackQuery(pattern=b"cust_(\\d+)"))
async def cust_profile_cb(event):
    if not is_owner(event):
        return
    uid = int(event.pattern_match.group(1))
    c = db.get_customer(uid)
    if not c:
        await event.answer("مشتری پیدا نشد.", alert=True)
        return
    await safe_edit(event, _profile_text(c),
                    buttons=_profile_buttons(uid, c.get("blocked")))


async def _refresh_profile(event, uid):
    c = db.get_customer(uid)
    if c:
        await safe_edit(event, _profile_text(c),
                        buttons=_profile_buttons(uid, c.get("blocked")))


@bot.on(events.CallbackQuery(pattern=b"addt_(\\d+)_(-?\\d+)"))
async def addtime_cb(event):
    if not is_owner(event):
        return
    uid = int(event.pattern_match.group(1))
    days = int(event.pattern_match.group(2))
    db.ensure_customer(uid)
    new_exp = db.add_days(uid, days)
    central_db.audit("adjust_time", f"{uid}: {days:+d} -> {new_exp}")
    await logbus.to_group(card("🕒 تغییر زمان اشتراک", [
        f"🆔 {uid}", f"{days:+d} روز", f"📅 انقضا : {new_exp}", f"🕒 {now()}"]))
    try:
        await bot.send_message(uid,
            (f"🎁 {days} روز به اشتراکت اضافه شد." if days >= 0
             else f"⏬ {abs(days)} روز از اشتراکت کم شد.") +
            f"\n📅 انقضا: {new_exp}")
    except Exception:
        pass
    await _refresh_profile(event, uid)


@bot.on(events.CallbackQuery(pattern=b"custt_(\\d+)"))
async def custtime_cb(event):
    if not is_owner(event):
        return
    uid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_custtime", "uid": uid}
    await safe_edit(event,
                    "⌨️ چند روز اضافه/کم بشه؟ یک عدد بفرست (مثلاً 14 یا -5):",
                    buttons=[[Button.inline("🔙 لغو", f"cust_{uid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"block_(\\d+)"))
async def block_cb(event):
    if not is_owner(event):
        return
    uid = int(event.pattern_match.group(1))
    db.set_blocked(uid, True)
    central_db.audit("block", str(uid))
    await logbus.to_group(card("⛔ مسدودسازی فوری", [f"🆔 {uid}", f"🕒 {now()}"]))
    try:
        await bot.send_message(uid, "⛔ حساب شما توسط مدیریت مسدود شد.")
    except Exception:
        pass
    await _refresh_profile(event, uid)


@bot.on(events.CallbackQuery(pattern=b"unblock_(\\d+)"))
async def unblock_cb(event):
    if not is_owner(event):
        return
    uid = int(event.pattern_match.group(1))
    db.set_blocked(uid, False)
    db.rate_reset(uid)
    central_db.audit("unblock", str(uid))
    await logbus.to_group(card("✅ رفع مسدودی", [f"🆔 {uid}", f"🕒 {now()}"]))
    try:
        await bot.send_message(uid, "✅ مسدودی حساب شما برداشته شد.")
    except Exception:
        pass
    await _refresh_profile(event, uid)


@bot.on(events.CallbackQuery(pattern=b"note_(\\d+)"))
async def note_cb(event):
    if not is_owner(event):
        return
    uid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_note", "uid": uid}
    await safe_edit(event, "📝 یادداشت این مشتری رو بفرست:",
                    buttons=[[Button.inline("🔙 لغو", f"cust_{uid}".encode())]])


# --------------------------------------------------------------------------- #
# Add customer manually.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"addcust"))
async def addcust_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_addcust"}
    await safe_edit(event,
                    "➕ آیدی عددیِ مشتری رو بفرست (اختیاری: بعدش با فاصله، تعداد روز).\n"
                    "مثال: `123456789 30`",
                    buttons=[[Button.inline("🔙 لغو", b"home")]])


# --------------------------------------------------------------------------- #
# Search + ranking.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"search"))
async def search_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_search"}
    await safe_edit(event, "🔎 بخشی از آیدی/نام/یوزرنیم مشتری رو بفرست:",
                    buttons=[[Button.inline("🔙 لغو", b"home")]])


@bot.on(events.CallbackQuery(data=b"ranking"))
async def ranking_cb(event):
    if not is_owner(event):
        return
    customers = db.list_customers()
    ranked = sorted(customers, key=lambda c: (float(c.get("total_paid") or 0),
                                              int(c.get("total_sends") or 0)),
                    reverse=True)[:15]
    rows = []
    for i, c in enumerate(ranked, 1):
        rows.append(f"{i}. {c.get('name') or c['telegram_id']} — "
                    f"{float(c.get('total_paid') or 0):g}$ / {c.get('total_sends') or 0} ارسال")
    await safe_edit(event, card("🏆 رتبه‌بندی مشتری‌ها", rows or ["— خالی —"]),
                    buttons=[[Button.inline("🔙 بازگشت", b"home")]])


# --------------------------------------------------------------------------- #
# Broadcast.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"broadcast"))
async def broadcast_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_broadcast"}
    await safe_edit(event, "📣 متنِ پیام همگانی رو بفرست (به همه‌ی مشتری‌ها ارسال می‌شه):",
                    buttons=[[Button.inline("🔙 لغو", b"home")]])


async def do_broadcast(event, text):
    customers = db.list_customers()
    ok = 0
    fail = 0
    for c in customers:
        try:
            await bot.send_message(c["telegram_id"], f"📣 {text}")
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    central_db.record_broadcast(text, ok, fail)
    central_db.audit("broadcast", f"ok={ok} fail={fail}")
    await logbus.to_group(card("📣 BROADCAST", [
        f"✅ {ok}   ❌ {fail}   👥 {len(customers)}", f"🕒 {now()}"]))
    await event.respond(f"📣 ارسال شد. ✅ {ok} / ❌ {fail}", buttons=main_menu())


# --------------------------------------------------------------------------- #
# System: maintenance toggle + backup now.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"sys"))
async def sys_cb(event):
    if not is_owner(event):
        return
    m = central_db.get_maintenance()
    last = central_db.get_last_backup() or "—"
    await safe_edit(event, card("🧰 تعمیر و بکاپ", [
        f"🛠 حالت تعمیر : {'روشن 🔴' if m else 'خاموش 🟢'}",
        f"💾 آخرین بکاپ : {last}",
    ]), buttons=[
        [Button.inline("🛠 تغییر حالت تعمیر", b"maint_toggle")],
        [Button.inline("💾 بکاپ فوری", b"backup_now")],
        [Button.inline("🔙 بازگشت", b"home")]])


@bot.on(events.CallbackQuery(data=b"maint_toggle"))
async def maint_toggle_cb(event):
    if not is_owner(event):
        return
    new = not central_db.get_maintenance()
    central_db.set_maintenance(new)
    central_db.audit("maintenance", "on" if new else "off")
    await logbus.to_group(card("🛠 MAINTENANCE", [
        ("روشن شد 🔴" if new else "خاموش شد 🟢"), f"🕒 {now()}"]))
    await sys_cb(event)


@bot.on(events.CallbackQuery(data=b"backup_now"))
async def backup_now_cb(event):
    if not is_owner(event):
        return
    await event.answer("در حال ساخت بکاپ ...")
    ok = await backup.run_backup(notify_user=event.sender_id)
    if not ok:
        await bot.send_message(event.sender_id, "هنوز چیزی برای بکاپ نیست.")


# --------------------------------------------------------------------------- #
# Workers (reused worker subsystem).
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"workers"))
async def workers_cb(event):
    if not is_owner(event):
        return
    workers = db.list_workers()
    rows = []
    for w in workers:
        emoji = worker.status_emoji(w)
        tag = w["tag"] + (" (مستر)" if w.get("is_master") else "")
        rows.append([Button.inline(f"{emoji} {tag} — {w.get('status')}",
                                   f"w_{w['id']}".encode())])
    rows.append([Button.inline("➕ افزودن ورکر", b"w_add"),
                 Button.inline("🩺 بررسی همه", b"w_checkall")])
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event, "🛠 مدیریت ورکرها:", buttons=rows)


@bot.on(events.CallbackQuery(data=b"w_checkall"))
async def w_checkall_cb(event):
    if not is_owner(event):
        return
    await event.answer("در حال بررسی ...")
    results = await worker.check_all()
    rows = []
    for r in results:
        rows.append(f"{r.get('tag')} : {r.get('status')} ({r.get('ping_ms')}ms)"
                    + (f" — {r.get('detail')}" if r.get("detail") else ""))
    await logbus.to_group(card("🛠 STATU WORKER ALL", rows + [f"🕒 {now()}"]))
    await safe_edit(event, card("🛠 وضعیت ورکرها", rows or ["— ورکری نیست —"]),
                    buttons=[[Button.inline("🔙 بازگشت", b"workers")]])


@bot.on(events.CallbackQuery(pattern=b"w_(\\d+)"))
async def w_menu_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        await event.answer("ورکر پیدا نشد.", alert=True)
        return
    accs = db.count_accounts_on_worker(wid)
    rows = [
        f"🏷 {w['tag']}" + (" (مستر)" if w.get("is_master") else ""),
        f"🌐 IP : {w.get('ip')}",
        f"⭐️ وضعیت : {w.get('status')} ({w.get('ping_ms')}ms)",
        f"📁 فایل : {worker.file_label(w)}",
        f"📱 اکانت‌ها : {accs}",
        f"🔌 فعال : {'بله' if w.get('enabled') else 'خیر'}",
    ]
    btns = [[Button.inline("🩺 بررسی", f"wchk_{wid}".encode())]]
    if not w.get("is_master"):
        toggle = "⏸ غیرفعال" if w.get("enabled") else "▶️ فعال"
        btns.append([Button.inline(toggle, f"wtog_{wid}".encode()),
                     Button.inline("🗑 حذف", f"wdel_{wid}".encode())])
    btns.append([Button.inline("🔙 بازگشت", b"workers")])
    await safe_edit(event, card("🛠 ورکر", rows), buttons=btns)


@bot.on(events.CallbackQuery(pattern=b"wchk_(\\d+)"))
async def wchk_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        await event.answer("ورکر پیدا نشد.", alert=True)
        return
    await event.answer("در حال بررسی ...")
    r = await worker.check_worker(w)
    await logbus.to_group(card("🛠 WORKER CHECK", [
        f"{r.get('tag')} : {r.get('status')} ({r.get('ping_ms')}ms)"
        + (f" — {r.get('detail')}" if r.get("detail") else ""), f"🕒 {now()}"]))
    await w_menu_cb(event)


@bot.on(events.CallbackQuery(pattern=b"wtog_(\\d+)"))
async def wtog_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        await event.answer("ورکر پیدا نشد.", alert=True)
        return
    db.set_worker_enabled(wid, not w.get("enabled"))
    await w_menu_cb(event)


@bot.on(events.CallbackQuery(pattern=b"wdel_(\\d+)"))
async def wdel_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        await event.answer("ورکر پیدا نشد.", alert=True)
        return
    try:
        await worker.teardown_worker(w)
    except Exception:
        pass
    db.delete_worker(wid)
    await logbus.to_group(card("🗑 WORKER REMOVED", [f"🏷 {w['tag']}", f"🕒 {now()}"]))
    await workers_cb(event)


@bot.on(events.CallbackQuery(data=b"w_add"))
async def w_add_cb(event):
    if not is_owner(event):
        return
    if not crypto_util.is_configured():
        await event.answer("اول WORKER_SECRET رو توی .env تنظیم کن.", alert=True)
        return
    state[event.sender_id] = {"step": "w_ip", "w": {}}
    await safe_edit(event, "🌐 IP سرور ورکر رو بفرست:",
                    buttons=[[Button.inline("🔙 لغو", b"workers")]])


async def _worker_add_flow(event, st):
    step = st["step"]
    txt = event.raw_text.strip()
    w = st["w"]
    if step == "w_ip":
        w["ip"] = txt
        st["step"] = "w_port"
        await event.respond("🔌 پورت SSH (پیش‌فرض 22):")
    elif step == "w_port":
        w["port"] = txt or "22"
        st["step"] = "w_user"
        await event.respond("👤 یوزرنیم SSH:")
    elif step == "w_user":
        w["user"] = txt
        st["step"] = "w_pass"
        await event.respond("🔑 پسورد SSH:")
    elif step == "w_pass":
        w["pass"] = txt
        state.pop(event.sender_id, None)
        msg = await event.respond("⏳ در حال راه‌اندازی ورکر (نصب Docker و سورس) ...")

        async def progress(s):
            try:
                await msg.edit(s)
            except Exception:
                pass
        prov = await worker.provision_worker(w["ip"], w["port"], w["user"],
                                             w["pass"], on_progress=progress)
        if not prov.get("ok"):
            await msg.edit(f"❌ راه‌اندازی ناموفق: {prov.get('error')}",
                           buttons=[[Button.inline("🔙 ورکرها", b"workers")]])
            return
        wid = await worker.register_provisioned(w["ip"], w["port"], w["user"],
                                                w["pass"], prov)
        await logbus.to_group(card("🛠 ADDED WORKER", [
            f"🏷 {prov['tag']}", f"🌐 {w['ip']}", f"🕒 {now()}"]))
        new_w = db.get_worker(wid)
        try:
            await worker.check_worker(new_w)
        except Exception:
            pass
        await msg.edit(f"✅ ورکر {prov['tag']} اضافه شد.",
                       buttons=[[Button.inline("🛠 ورکرها", b"workers")]])


# --------------------------------------------------------------------------- #
# Free-text router.
# --------------------------------------------------------------------------- #
@bot.on(events.NewMessage(func=lambda e: e.is_private and not (e.raw_text or "").startswith("/")))
async def text_router(event):
    if not is_owner(event):
        return
    st = state.get(event.sender_id)
    if not st:
        return
    step = st.get("step")
    if step == "await_addcust":
        await _handle_addcust(event)
    elif step == "await_custtime":
        await _handle_custtime(event, st)
    elif step == "await_note":
        await _handle_note(event, st)
    elif step == "await_search":
        await _handle_search(event)
    elif step == "await_broadcast":
        state.pop(event.sender_id, None)
        await do_broadcast(event, event.raw_text.strip())
    elif step in ("w_ip", "w_port", "w_user", "w_pass"):
        await _worker_add_flow(event, st)


async def _handle_addcust(event):
    state.pop(event.sender_id, None)
    parts = event.raw_text.strip().split()
    if not parts or not parts[0].lstrip("-").isdigit():
        await event.respond("آیدی نامعتبره.", buttons=main_menu())
        return
    uid = int(parts[0])
    days = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 0
    db.ensure_customer(uid)
    if days:
        db.add_days(uid, days)
    central_db.audit("add_customer", f"{uid} +{days}d")
    await logbus.to_group(card("➕ ADD CUSTOMER (دستی)", [
        f"🆔 {uid}", f"⏳ {days} روز", f"🕒 {now()}"]))
    c = db.get_customer(uid)
    await event.respond("✅ مشتری اضافه شد.", buttons=main_menu())
    await bot.send_message(event.sender_id, _profile_text(c),
                           buttons=_profile_buttons(uid, c.get("blocked")))


async def _handle_custtime(event, st):
    uid = st["uid"]
    state.pop(event.sender_id, None)
    txt = event.raw_text.strip()
    if not txt.lstrip("-").isdigit():
        await event.respond("عدد نامعتبره.")
        return
    days = int(txt)
    new_exp = db.add_days(uid, days)
    central_db.audit("adjust_time", f"{uid}: {days:+d} -> {new_exp}")
    await logbus.to_group(card("🕒 تغییر زمان اشتراک", [
        f"🆔 {uid}", f"{days:+d} روز", f"📅 {new_exp}", f"🕒 {now()}"]))
    try:
        await bot.send_message(uid, f"📅 زمان اشتراکت به‌روزرسانی شد. انقضا: {new_exp}")
    except Exception:
        pass
    c = db.get_customer(uid)
    await event.respond("✅ انجام شد.", buttons=main_menu())
    await bot.send_message(event.sender_id, _profile_text(c),
                           buttons=_profile_buttons(uid, c.get("blocked")))


async def _handle_note(event, st):
    uid = st["uid"]
    state.pop(event.sender_id, None)
    db.set_note(uid, event.raw_text.strip())
    c = db.get_customer(uid)
    await event.respond("✅ یادداشت ذخیره شد.", buttons=main_menu())
    await bot.send_message(event.sender_id, _profile_text(c),
                           buttons=_profile_buttons(uid, c.get("blocked")))


async def _handle_search(event):
    state.pop(event.sender_id, None)
    results = db.search_customers(event.raw_text.strip())
    if not results:
        await event.respond("چیزی پیدا نشد.", buttons=main_menu())
        return
    await event.respond(f"🔎 {len(results)} نتیجه:", buttons=_cust_buttons(results))


# --------------------------------------------------------------------------- #
# Worker health report loop + entrypoint.
# --------------------------------------------------------------------------- #
async def worker_report_loop():
    interval = int(config.HEALTH_INTERVAL or 0)
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            workers = db.list_workers()
            remotes = [w for w in workers if not w.get("is_master")]
            if not remotes:
                continue
            results = await worker.check_all(workers)
            rows = [f"{r.get('tag')} : {r.get('status')} ({r.get('ping_ms')}ms)"
                    for r in results]
            await logbus.to_group(card("🛠 STATU WORKER ALL", rows + [f"🕒 {now()}"]))
        except Exception as e:  # noqa: BLE001
            print(f"[worker report] {e}")


async def amain():
    problems = config.validate_owner()
    if problems:
        raise SystemExit("تنظیمات ناقصه (.env): " + ", ".join(problems))
    db.init()
    central_db.init()
    worker.ensure_master_worker()
    await bot.start(bot_token=config.OWNER_BOT_TOKEN)
    logbus.bind(bot)
    await logbus.to_group(card("🎛 OWNER PANEL ONLINE", [
        f"🏷 Version : {config.VERSION}", f"🕒 {now()}"]))
    asyncio.create_task(worker_report_loop())
    asyncio.create_task(backup.backup_loop())
    print("owner bot running")
    await bot.run_until_disconnected()
