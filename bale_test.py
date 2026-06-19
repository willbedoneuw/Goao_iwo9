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


async def ainput(prompt: str) -> str:
    """Non-blocking input(): runs the blocking read in a thread so the asyncio
    event loop (and aiobale's keep-alive ping) keeps running while we wait —
    otherwise a long pause at a prompt kills the WebSocket connection."""
    return (await asyncio.to_thread(input, prompt)).strip()


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

# Low-level method classes (used for RAW requests that skip aiobale's pydantic
# models — those crash on bot dialogs with keyboards).
try:
    from aiobale.methods import LoadDialogs, GetContacts
    log("✅ IMPORT", "method classes (LoadDialogs/GetContacts) OK")
except Exception as e:  # noqa: BLE001
    LoadDialogs = GetContacts = None
    log("⚠️ IMPORT", f"method classes وارد نشدن: {e!r}")


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

    code = await ainput("کدی که بله فرستاد رو وارد کن: ")
    log("🔐 LOGIN", "validate_code(...) ...")
    try:
        res = await client.validate_code(code, tx)
    except Exception as e:  # noqa: BLE001
        logx("❌ LOGIN", e)
        return False

    if isinstance(res, AuthErrors):
        if res == AuthErrors.PASSWORD_NEEDED:
            log("🔐 LOGIN", "این اکانت رمز دومرحله‌ای داره.")
            pwd = await ainput("رمز دومرحله‌ای رو وارد کن: ")
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
# RAW request: send a method and return the decoded dict WITHOUT aiobale's
# pydantic model (which crashes on bot dialogs). Lets us read peers safely.
# --------------------------------------------------------------------------- #
async def _raw_request(client: "Client", method, timeout: int = 25):
    await _ensure_started(client)
    sess = client.session
    rid = sess._next_request_id()
    payload = sess.build_payload(method, rid)
    fut = asyncio.get_event_loop().create_future()
    sess._pending_requests[rid] = fut
    await sess.ws.send_bytes(payload)
    resp = await asyncio.wait_for(fut, timeout=timeout)
    if getattr(resp, "error", None):
        raise RuntimeError(f"bale error: {resp.error}")
    return resp.result  # the raw decoded dict


def _g(d, *keys):
    """Get a key from a dict trying both str and int forms."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d:
            return d[k]
        if str(k) in d:
            return d[str(k)]
    return None


def _extract_dialog_peers(raw: dict):
    """raw['3'] = list of dialogs; each dialog's peer at key '1' = {'1':type,'2':id}.
    Returns list of {'id','type'} skipping the message content that crashes."""
    dialogs = _g(raw, "3", 3) or []
    if isinstance(dialogs, dict):
        dialogs = [dialogs]
    out = []
    for d in dialogs:
        peer = _g(d, "1", 1) or {}
        ptype = _g(peer, "1", 1)
        pid = _g(peer, "2", 2)
        sub = _g(_g(d, "13", 13) or {}, "1", 1)   # subtype: 1=user, 2=group, 4=bot
        if pid is None:
            continue
        out.append({"id": int(pid),
                    "type": int(ptype) if ptype is not None else 0,
                    "ctype": int(sub) if sub is not None else 0})
    return out


# --------------------------------------------------------------------------- #
# 3+4) READ contacts + dialogs (PV vs GROUP, جدا جدا) — RAW, crash-proof
# --------------------------------------------------------------------------- #
async def read_everything(client: "Client"):
    pv, groups = [], []

    # ---- dialogs via RAW request (the reliable path) ----
    if LoadDialogs is not None:
        try:
            raw = await _raw_request(
                client, LoadDialogs(offset_date=-1, limit=200, exclude_pinned=False))
            # dump first 2 raw dialogs so we SEE the real structure
            rd = _g(raw, "3", 3) or []
            if isinstance(rd, dict):
                rd = [rd]
            log("📦 RAW-DIALOGS", f"تعداد خام: {len(rd)} — نمونه‌ی ۲ موردِ اول:")
            for i, d in enumerate(rd[:2], 1):
                log("   raw", f"{i}. {str(d)[:600]}")

            peers = _extract_dialog_peers(raw)
            pv = [p for p in peers if p["type"] == int(PeerType.PRIVATE)]
            groups = [p for p in peers if p["type"] == int(PeerType.GROUP)]
            other = [p for p in peers if p["type"] not in
                     (int(PeerType.PRIVATE), int(PeerType.GROUP))]
            log("💬 DIALOGS", f"کل: {len(peers)}  |  📥 PV: {len(pv)}  "
                             f"👥 GROUP: {len(groups)}  ❓ OTHER: {len(other)}")
            log("📥 PV (پیوی‌ها):")
            for i, p in enumerate(pv[:60], 1):
                log("   •", f"{i}. id={p['id']}  type={p['type']}")
            log("👥 GROUPS (گروه‌ها):")
            for i, p in enumerate(groups[:60], 1):
                log("   •", f"{i}. id={p['id']}  type={p['type']}")
            if other:
                log("❓ OTHER:")
                for i, p in enumerate(other[:30], 1):
                    log("   •", f"{i}. id={p['id']}  type={p['type']}")
        except Exception as e:  # noqa: BLE001
            logx("❌ DIALOGS(raw)", e)
    else:
        log("⏭ DIALOGS", "LoadDialogs وارد نشد — رد شد")

    # ---- contacts via RAW (just to SEE the structure; may differ per account) ----
    if GetContacts is not None:
        try:
            rawc = await _raw_request(client, GetContacts())
            log("📦 RAW-CONTACTS", f"ساختار خامِ مخاطبین: {str(rawc)[:900]}")
        except Exception as e:  # noqa: BLE001
            logx("❌ CONTACTS(raw)", e)

    return pv, groups


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

    # 5b) optional: send to the FIRST REAL private chat (not me, not a bot)
    ans = (await ainput("یه پیامِ تست به اولین پیویِ واقعی بفرستم؟ (y/N): ")).lower()
    real_pv = [p for p in pv if p["id"] != me_id and p.get("ctype") != 4]
    if ans == "y" and real_pv:
        p = real_pv[0]
        try:
            msg = await client.send_message(
                text="🧪 تست ارسال (نادیده بگیر)",
                chat_id=int(p["id"]), chat_type=ChatType.PRIVATE)
            log("✅ SEND-PV", f"به پیویِ واقعی (id={p['id']}) ارسال شد. {dump(msg)}")
        except Exception as e:  # noqa: BLE001
            logx("❌ SEND-PV", e)
    elif ans == "y":
        log("⏭ SEND-PV", "پیویِ واقعی (غیرخودت/غیربات) پیدا نشد")

    # 5c) optional: send to the FIRST group (with confirmation)
    ans = (await ainput("یه پیامِ تست به اولین گروه بفرستم؟ (y/N): ")).lower()
    if ans == "y" and groups:
        p = groups[0]
        try:
            msg = await client.send_message(
                text="🧪 تست ارسال (نادیده بگیر)",
                chat_id=int(p["id"]), chat_type=ChatType.GROUP)
            log("✅ SEND-GROUP", f"به اولین گروه (id={p['id']}) ارسال شد. {dump(msg)}")
        except Exception as e:  # noqa: BLE001
            logx("❌ SEND-GROUP", e)


# --------------------------------------------------------------------------- #
# 6) Rate-limit probe (به خودت، امن): چند ارسالِ پیاپی تا ببینیم بله کِی محدود می‌کنه
# --------------------------------------------------------------------------- #
async def rate_probe(client: "Client"):
    ans = (await ainput("تستِ محدودیت: ۳۰ پیامِ پیاپی به خودت بفرستم؟ (y/N): ")).lower()
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
    raw = await ainput("شماره‌ی اکانت بله (فقط رقم، با کد کشور؛ مثل 989121234567): ")
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
