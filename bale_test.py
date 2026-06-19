#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bale_test.py — aiobale capability probe (run MANUALLY on the server).
=====================================================================

این یه اسکریپتِ تستِ مستقله — هیچ‌جای رباتو دست نمی‌زنه. هدفش اینه که قبل از
ساختِ «پنل بله»، مطمئن شیم کتابخونه‌ی aiobale این کارها رو درست انجام می‌ده:

  1) لاگین (همون مسیری که ربات لازم داره: start_phone_auth -> validate_code
     -> در صورت نیاز validate_password)  + ذخیره‌ی سشن در فایل.
  2) پایداریِ سشن: بازخوانی از فایل بدون لاگین مجدد.
  3) خواندنِ مخاطبین (contacts / دوطرفه‌ها).
  4) خواندنِ دیالوگ‌ها و جدا کردنِ «پیوی (PV)» از «گروه‌ها».
  5) ارسالِ تست (به خودت = Saved Messages) و در صورت تأیید، به یک مخاطب/گروه.
  6) رفتارِ محدودیت (rate-limit) با چند ارسالِ پیاپی به خودت.

همه‌چیز هم روی کنسول، هم در فایلِ bale_test.log لاگ می‌شه (مثل روبیکا).

اجرا:
    ./venv/bin/pip install aiobale        # یک‌بار، اگه نصب نیست
    ./venv/bin/python bale_test.py
"""

import asyncio
import os
import sys
import traceback
from datetime import datetime

# --------------------------------------------------------------------------- #
# Heavy logging: console + file, timestamped (همه‌چیز لاگ می‌شه).
# --------------------------------------------------------------------------- #
BASE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE, "bale_test.log")
SESS_DIR = os.path.join(BASE, "data", "bale_sessions")
os.makedirs(SESS_DIR, exist_ok=True)


def log(tag: str, msg: str = "") -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {tag} {msg}".rstrip()
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def logx(tag: str, exc: Exception) -> None:
    """Log an exception with its full traceback."""
    log(tag, f"{type(exc).__name__}: {exc}")
    for ln in traceback.format_exc().splitlines():
        log("   ", ln)


def dump(obj) -> str:
    """Best-effort structured view of an aiobale object (for discovery)."""
    for attr in ("model_dump",):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return str(fn())
            except Exception:
                pass
    fields = {}
    for k in ("id", "type", "name", "username", "title", "access_hash",
              "peer", "is_contact", "members_count"):
        if hasattr(obj, k):
            try:
                fields[k] = getattr(obj, k)
            except Exception:
                pass
    return str(fields) if fields else repr(obj)


# --------------------------------------------------------------------------- #
# Import aiobale (clear message if missing).
# --------------------------------------------------------------------------- #
try:
    from aiobale import Client
    from aiobale.enums import AuthErrors, ChatType, PeerType
    log("✅ IMPORT", "aiobale imported OK")
except Exception as e:  # noqa: BLE001
    log("❌ IMPORT", f"aiobale وارد نشد: {e!r}")
    log("ℹ️ HINT", "اول نصبش کن:  ./venv/bin/pip install aiobale")
    sys.exit(1)


def _peer_kind(peer) -> str:
    """Map a dialog peer type to a human label (PV vs GROUP)."""
    t = getattr(peer, "type", None)
    if t == PeerType.PRIVATE:
        return "PV"
    if t == PeerType.GROUP:
        return "GROUP"
    return f"OTHER({t})"


def _chat_type_for(peer) -> "ChatType":
    """Best-effort ChatType for send_message, derived from the dialog peer type."""
    t = getattr(peer, "type", None)
    if t == PeerType.GROUP:
        return ChatType.GROUP
    return ChatType.PRIVATE


# --------------------------------------------------------------------------- #
# 1) LOGIN  (the exact programmatic path the bot will use)
# --------------------------------------------------------------------------- #
async def do_login(client: "Client", phone_int: int) -> bool:
    log("🔐 LOGIN", f"start_phone_auth({phone_int}) ...")
    try:
        resp = await client.start_phone_auth(phone_int)
    except Exception as e:  # noqa: BLE001
        logx("❌ LOGIN", e)
        return False

    if isinstance(resp, AuthErrors):
        log("❌ LOGIN", f"start_phone_auth برگردوند: {resp.name}")
        return False
    tx = getattr(resp, "transaction_hash", None)
    log("✅ LOGIN", f"کد ارسال شد. transaction_hash={tx}  | full={dump(resp)}")

    code = input("کدی که بله فرستاد رو وارد کن: ").strip()
    log("🔐 LOGIN", "validate_code(...) ...")
    try:
        res = await client.validate_code(code, tx)
    except Exception as e:  # noqa: BLE001
        logx("❌ LOGIN", e)
        return False

    if isinstance(res, AuthErrors):
        if res == AuthErrors.PASSWORD_NEEDED:
            log("🔐 LOGIN", "این اکانت رمز دومرحله‌ای داره.")
            pwd = input("رمز دومرحله‌ای رو وارد کن: ").strip()
            try:
                res = await client.validate_password(pwd, tx)
            except Exception as e:  # noqa: BLE001
                logx("❌ LOGIN", e)
                return False
            if isinstance(res, AuthErrors):
                log("❌ LOGIN", f"validate_password برگردوند: {res.name}")
                return False
        else:
            log("❌ LOGIN", f"validate_code برگردوند: {res.name}")
            return False

    log("✅ LOGIN", f"ورود موفق! me={dump(getattr(client, 'me', None))}")
    return True


# --------------------------------------------------------------------------- #
# Try a method one-shot; if it fails, start() the client and retry once.
# (this also answers: آیا تک‌شات بدون start() کار می‌کنه؟)
# --------------------------------------------------------------------------- #
_started = {"on": False}


async def _ensure_started(client: "Client") -> None:
    if _started["on"]:
        return
    try:
        await client.start(run_in_background=True, signal_handling=False)
        _started["on"] = True
        log("🔌 START", "client.start(run_in_background=True) OK")
    except Exception as e:  # noqa: BLE001
        logx("⚠️ START", e)


async def call_probe(client: "Client", name: str, coro_factory):
    """Run coro_factory(); on failure, start() and retry once. Logs everything."""
    try:
        res = await coro_factory()
        log(f"✅ {name}", "تک‌شات (بدون start) کار کرد")
        return res
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ {name}", f"تک‌شات نشد: {type(e).__name__}: {e} — با start() دوباره امتحان می‌کنم")
        await _ensure_started(client)
        try:
            res = await coro_factory()
            log(f"✅ {name}", "بعد از start() کار کرد")
            return res
        except Exception as e2:  # noqa: BLE001
            logx(f"❌ {name}", e2)
            return None


# --------------------------------------------------------------------------- #
# 3+4) READ contacts + dialogs (PV vs GROUP, جدا جدا)
# --------------------------------------------------------------------------- #
async def read_everything(client: "Client"):
    # ---- contacts (مخاطبین / دوطرفه‌ها) ----
    contacts = await call_probe(client, "CONTACTS", client.load_contacts)
    if contacts is not None:
        log("📇 CONTACTS", f"تعداد مخاطبین: {len(contacts)}")
        for i, c in enumerate(contacts[:50], 1):
            log("   •", f"{i}. {dump(c)}")
        if len(contacts) > 50:
            log("   …", f"و {len(contacts) - 50} مخاطبِ دیگه")

    # ---- dialogs (چت‌ها) -> جدا کردنِ PV از GROUP ----
    dialogs = await call_probe(
        client, "DIALOGS", lambda: client.load_dialogs(limit=200))
    if dialogs is not None:
        pv, groups, other = [], [], []
        for d in dialogs:
            peer = getattr(d, "peer", None)
            kind = _peer_kind(peer)
            (pv if kind == "PV" else groups if kind == "GROUP" else other).append(d)
        log("💬 DIALOGS", f"کل: {len(dialogs)}  |  PV: {len(pv)}  "
                          f"GROUP: {len(groups)}  OTHER: {len(other)}")
        log("📥 PV (پیوی‌ها):")
        for i, d in enumerate(pv[:40], 1):
            log("   •", f"{i}. {dump(getattr(d, 'peer', d))}")
        log("👥 GROUPS (گروه‌ها):")
        for i, d in enumerate(groups[:40], 1):
            log("   •", f"{i}. {dump(getattr(d, 'peer', d))}")
        if other:
            log("❓ OTHER:")
            for i, d in enumerate(other[:20], 1):
                log("   •", f"{i}. {dump(getattr(d, 'peer', d))}")
        return pv, groups
    return [], []


# --------------------------------------------------------------------------- #
# 5) SEND tests
# --------------------------------------------------------------------------- #
async def send_tests(client: "Client", pv, groups):
    me_id = getattr(client, "id", None)
    log("🆔 ME", f"id={me_id}")

    # 5a) send to SELF (safe — Saved Messages)
    if me_id:
        try:
            msg = await client.send_message(
                text=f"🧪 تستِ aiobale — {datetime.now():%H:%M:%S}",
                chat_id=int(me_id), chat_type=ChatType.PRIVATE)
            log("✅ SEND-SELF", f"به خودت ارسال شد. {dump(msg)}")
        except Exception as e:  # noqa: BLE001
            logx("❌ SEND-SELF", e)

    # 5b) optional: send to the FIRST contact (with confirmation)
    ans = input("یه پیامِ تست به اولین مخاطب بفرستم؟ (y/N): ").strip().lower()
    if ans == "y" and pv:
        peer = getattr(pv[0], "peer", None)
        try:
            msg = await client.send_message(
                text="🧪 تست ارسال (نادیده بگیر)",
                chat_id=int(getattr(peer, "id")), chat_type=_chat_type_for(peer))
            log("✅ SEND-PV", f"به اولین پیوی ارسال شد. {dump(msg)}")
        except Exception as e:  # noqa: BLE001
            logx("❌ SEND-PV", e)

    # 5c) optional: send to the FIRST group (with confirmation)
    ans = input("یه پیامِ تست به اولین گروه بفرستم؟ (y/N): ").strip().lower()
    if ans == "y" and groups:
        peer = getattr(groups[0], "peer", None)
        try:
            msg = await client.send_message(
                text="🧪 تست ارسال (نادیده بگیر)",
                chat_id=int(getattr(peer, "id")), chat_type=_chat_type_for(peer))
            log("✅ SEND-GROUP", f"به اولین گروه ارسال شد. {dump(msg)}")
        except Exception as e:  # noqa: BLE001
            logx("❌ SEND-GROUP", e)


# --------------------------------------------------------------------------- #
# 6) Rate-limit probe (به خودت، امن): چند ارسالِ پیاپی تا ببینیم بله کِی محدود می‌کنه
# --------------------------------------------------------------------------- #
async def rate_probe(client: "Client"):
    ans = input("تستِ محدودیت: ۳۰ پیامِ پیاپی به خودت بفرستم؟ (y/N): ").strip().lower()
    if ans != "y":
        log("⏭ RATE", "رد شد")
        return
    me_id = getattr(client, "id", None)
    if not me_id:
        log("⏭ RATE", "id خودت نامعلومه — رد شد")
        return
    ok = 0
    for i in range(1, 31):
        try:
            await client.send_message(text=f"rate-test {i}", chat_id=int(me_id),
                                      chat_type=ChatType.PRIVATE)
            ok += 1
            log("➡️ RATE", f"{i}/30 ارسال شد (ok={ok})")
        except Exception as e:  # noqa: BLE001
            log("🛑 RATE", f"در ارسالِ {i} متوقف شد — این همون محدودیتیه که باید مدیریت کنیم:")
            logx("   ", e)
            break
        await asyncio.sleep(0.7)
    log("🏁 RATE", f"پایان. موفق: {ok}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
async def main():
    log("=" * 8, "BALE PROBE START " + "=" * 8)
    raw = input("شماره‌ی اکانت بله (فقط رقم، با کد کشور؛ مثل 989121234567): ").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        log("❌ INPUT", "شماره نامعتبر")
        return
    phone_int = int(digits)
    sess_path = os.path.join(SESS_DIR, f"{digits}.bale")
    log("📁 SESSION", f"مسیر سشن: {sess_path}  (هست؟ {os.path.exists(sess_path)})")

    # ---- create client bound to a per-phone session file ----
    client = Client(session_file=sess_path)

    had_token = bool(getattr(client, "_Client__token", None) or
                     getattr(client, "token", None))
    log("🔑 TOKEN", f"سشنِ قبلی توکن داشت؟ {had_token}")

    try:
        if not had_token:
            if not await do_login(client, phone_int):
                log("🛑 STOP", "لاگین ناموفق — پایان")
                return
        else:
            log("✅ SESSION", "از سشنِ قبلی استفاده می‌شه (بدون لاگین مجدد)")
            log("🔎 SESSION", f"me={dump(getattr(client, 'me', None))}")

        # 2) session persistence: reload from file in a fresh client
        try:
            client2 = Client(session_file=sess_path)
            tok2 = bool(getattr(client2, "_Client__token", None) or
                        getattr(client2, "token", None))
            log("♻️ RELOAD", f"کلاینتِ دوم از فایل ساخته شد. توکن داره؟ {tok2}")
        except Exception as e:  # noqa: BLE001
            logx("⚠️ RELOAD", e)

        # 3+4) read contacts + dialogs (PV / GROUP)
        pv, groups = await read_everything(client)

        # 5) send tests
        await send_tests(client, pv, groups)

        # 6) rate-limit probe
        await rate_probe(client)

    except Exception as e:  # noqa: BLE001
        logx("💥 FATAL", e)
    finally:
        try:
            await client.stop()
            log("🔌 STOP", "client.stop() OK")
        except Exception as e:  # noqa: BLE001
            log("🔌 STOP", f"stop ignored: {e!r}")
        log("=" * 8, "BALE PROBE END " + "=" * 8)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("🛑 STOP", "توسط کاربر متوقف شد")
