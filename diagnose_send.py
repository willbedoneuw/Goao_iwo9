#!/usr/bin/env python3
"""Standalone SEND diagnostic — does NOT change anything in the app.

Why this exists
---------------
When sending you sometimes get `TooRequests / TOO_REQUESTS`. That is Rubika
rate-limiting (flood) the ACCOUNT, not a bug in the bot. With MAX_ERRORS=3 the
run hits its error cap after only a few throttles, pauses 5 min and eventually
stops — which is why "the bot stops sending".

This script measures, WITHOUT touching the app or the DB:
  1. the machine's public IP + country/ISP  (Rubika throttles non-Iran IPs hard)
  2. how fast we can connect + find the marked message
  3. how many forwards SUCCEED before the first TOO_REQUESTS, and the pattern
     after it (does it recover? always fail?), with per-send latency
  4. (optional) a DELAY SWEEP to find a delay that avoids throttling

IMPORTANT
---------
* Run it on the SAME machine that hosts the account's session:
    - local account  -> on the master:   python3 diagnose_send.py
    - remote account -> inside the worker container:
        docker exec -it v2rubby-worker python3 diagnose_send.py
  (because Rubika sees THAT machine's IP)
* By default it forwards the marked message to your OWN Saved Messages
  (TEST_TARGET=self) so it never spams real contacts. You can delete those
  test messages afterwards. Set TEST_TARGET=contacts for a real-world test.
* Run it when no real send is in progress for that account (shared session).

Configure via environment variables:
  TEST_PHONE        (required) account phone, e.g. 989131528613
  TEST_TARGET       self (default) | contacts
  TEST_COUNT        how many forwards to attempt (default 30)
  TEST_DELAY        seconds between forwards (default = config.DEFAULT_DELAY)
  TEST_MARKER       marker text (default = config.FORWARD_MARKER)
  TEST_SWEEP        0 (default) | 1  -> run the delay sweep instead
  TEST_SWEEP_DELAYS comma list (default "0.5,1,2,3,5")
  TEST_SWEEP_BATCH  forwards per delay in the sweep (default 8)
"""
import asyncio
import os
import time

import config
import rubika_client as rb


def _env(name, default=None):
    v = os.getenv(name)
    return v if (v is not None and v != "") else default


def _is_too_requests(exc) -> bool:
    s = repr(exc).lower()
    return "too_requests" in s or "toorequests" in s


def _line(c="─", n=60):
    print(c * n)


async def show_public_ip():
    """Best-effort public IP + geo. Needs outbound internet on this machine."""
    print("🌐 آی‌پی و موقعیت سرور (همون چیزی که روبیکا می‌بینه):")
    try:
        import httpx
    except Exception as e:  # noqa: BLE001
        print(f"   httpx در دسترس نیست: {e!r}")
        return
    for url in ("http://ip-api.com/json/?fields=query,country,city,isp,org,as",
                "https://ipinfo.io/json"):
        try:
            async with httpx.AsyncClient(timeout=10) as cl:
                r = await cl.get(url)
                r.raise_for_status()
                d = r.json()
            ip = d.get("query") or d.get("ip")
            country = d.get("country")
            city = d.get("city")
            isp = d.get("isp") or d.get("org")
            print(f"   IP      : {ip}")
            print(f"   Country : {country}    City: {city}")
            print(f"   ISP/Org : {isp}")
            if country and str(country).lower() not in ("iran", "ir",
                                                        "islamic republic of iran"):
                print("   ⚠️ این آی‌پی ایران نیست — روبیکا معمولاً آی‌پی غیرایران رو "
                      "شدید ریت‌لیمیت می‌کنه. این می‌تونه علتِ اصلیِ TOO_REQUESTS باشه.")
            return
        except Exception as e:  # noqa: BLE001
            print(f"   ({url} نشد: {e!r})")
    print("   نتونستم آی‌پی عمومی رو بگیرم (شاید این ماشین اینترنت خروجی نداره).")


async def connect_account(phone):
    print(f"🔌 اتصال به اکانت {phone} ...")
    client = rb.open_client(phone)
    t0 = time.monotonic()
    await rb.connect_ready(client)
    dt = (time.monotonic() - t0) * 1000
    print(f"   connect_ready در {dt:.0f}ms")
    return client


async def show_recipient_stats(client):
    try:
        t0 = time.monotonic()
        ordered, stats = await rb.get_ordered_recipients(client)
        dt = (time.monotonic() - t0) * 1000
        print("👥 وضعیت مخاطبین (با ترتیب فعلی):")
        print(f"   کل مخاطب : {stats.get('contacts')}   "
              f"آنلاین : {stats.get('online')}   "
              f"چت‌دار : {stats.get('with_chat')}   "
              f"گروه : {stats.get('groups')}")
        print(f"   ساخت لیست در {dt:.0f}ms")
        return ordered
    except Exception as e:  # noqa: BLE001
        print(f"   نتونستم لیست مخاطبین رو بسازم: {e!r}")
        return []


async def do_forwards(client, saved_guid, mid, targets, delay, label):
    """Forward the marked message to each target, NEVER aborting, recording the
    outcome + latency of each one. Returns a result dict."""
    ok = too = other = 0
    first_too_at = None
    latencies = []
    print(f"\n▶️ تست ارسال [{label}] — تعداد={len(targets)}  تاخیر={delay}s")
    _line()
    for i, guid in enumerate(targets, 1):
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                rb.forward_message(client, saved_guid, guid, mid),
                timeout=config.SEND_TIMEOUT)
            dt = (time.monotonic() - t0) * 1000
            latencies.append(dt)
            ok += 1
            print(f"  #{i:>3}  ✅ ok        {dt:>6.0f}ms")
        except Exception as e:  # noqa: BLE001
            dt = (time.monotonic() - t0) * 1000
            if _is_too_requests(e):
                too += 1
                if first_too_at is None:
                    first_too_at = i
                print(f"  #{i:>3}  🚫 TOO_REQ    {dt:>6.0f}ms")
            else:
                other += 1
                print(f"  #{i:>3}  ❌ {repr(e)[:60]}")
        await asyncio.sleep(delay)
    _line()
    avg = sum(latencies) / len(latencies) if latencies else 0
    print(f"  جمع‌بندی [{label}]: ✅{ok}  🚫TOO_REQUESTS={too}  ❌خطای‌دیگر={other}"
          f"  | اولین throttle در #{first_too_at}  | میانگین تاخیر موفق‌ها {avg:.0f}ms")
    return {"ok": ok, "too": too, "other": other,
            "first_too_at": first_too_at, "avg_ms": avg, "n": len(targets)}


async def main():
    phone = _env("TEST_PHONE")
    if not phone:
        print("❌ TEST_PHONE تنظیم نشده. مثال:\n"
              "   TEST_PHONE=989131528613 python3 diagnose_send.py")
        return
    phone = rb.normalize_phone(phone)
    target_mode = (_env("TEST_TARGET", "self") or "self").lower()
    count = int(_env("TEST_COUNT", "30"))
    delay = float(_env("TEST_DELAY", str(config.DEFAULT_DELAY)))
    marker = _env("TEST_MARKER", config.FORWARD_MARKER)
    sweep = _env("TEST_SWEEP", "0") == "1"

    print("=" * 60)
    print("  SEND DIAGNOSTIC  (هیچ‌چیزی از برنامه عوض نمی‌شه)")
    print("=" * 60)
    print(f"🕒 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"⚙️ MAX_ERRORS={config.MAX_ERRORS}  SEND_TIMEOUT={config.SEND_TIMEOUT}s  "
          f"RESUME_WAIT={config.RESUME_WAIT}s  RESUME_MAX_RETRIES={config.RESUME_MAX_RETRIES}")
    print(f"⚙️ delay پیش‌فرض={config.DEFAULT_DELAY}s  marker=«{marker}»  "
          f"target={target_mode}")
    _line("=")

    await show_public_ip()
    _line()

    client = None
    try:
        client = await connect_account(phone)

        # find the marked message in Saved Messages
        print(f"🔎 جستجوی مارکر «{marker}» در Saved Messages ...")
        saved_guid, mid = await rb.find_marked_message(client, marker)
        if not mid:
            print("   ❌ پیام مارکر پیدا نشد. مطمئن شو توی Saved Messages یه پیام با "
                  "این مارکر در انتهای کپشن هست. (بدون مارکر تست ارسال ممکن نیست)")
            return
        print(f"   ✅ پیدا شد. saved_guid={saved_guid}  message_id={mid}")

        ordered = await show_recipient_stats(client)

        # choose targets
        if target_mode == "contacts":
            if not ordered:
                print("❌ مخاطبی برای تست واقعی نیست.")
                return
            targets_all = [r["guid"] for r in ordered]
            print("⚠️ حالت contacts: این ارسالِ واقعی به مخاطب‌های واقعیه!")
        else:
            # SAFE default: forward the marked message back to OWN Saved Messages
            targets_all = [saved_guid]  # repeated below

        if sweep:
            delays = [float(x) for x in
                      (_env("TEST_SWEEP_DELAYS", "0.5,1,2,3,5")).split(",")]
            batch = int(_env("TEST_SWEEP_BATCH", "8"))
            print(f"\n🧪 حالت SWEEP: برای هر تاخیر {batch} ارسال — تا ببینیم کدوم "
                  f"تاخیر throttle نمی‌خوره.")
            results = {}
            for d in delays:
                if target_mode == "contacts":
                    tg = targets_all[:batch]
                else:
                    tg = [saved_guid] * batch
                results[d] = await do_forwards(client, saved_guid, mid, tg, d,
                                               label=f"delay={d}s")
                # small cooldown between sweeps so one doesn't bleed into next
                await asyncio.sleep(3)
            print("\n📊 خلاصهٔ SWEEP:")
            _line()
            for d in delays:
                r = results[d]
                verdict = "خوب ✅" if r["too"] == 0 else f"throttle از #{r['first_too_at']} 🚫"
                print(f"  delay={d:>4}s -> ✅{r['ok']}/{r['n']}  TOO_REQ={r['too']}  {verdict}")
            _line()
            good = [d for d in delays if results[d]["too"] == 0]
            if good:
                print(f"✅ پیشنهاد: با تاخیر ≥ {min(good)}s تو این تست throttle نخورد.")
            else:
                print("⚠️ تو همهٔ تاخیرها throttle خورد — به‌احتمال زیاد مشکل از "
                      "آی‌پی سرور یا محدودیت شدید اکانته، نه از تاخیر.")
        else:
            if target_mode == "contacts":
                targets = targets_all[:count]
            else:
                targets = [saved_guid] * count
            r = await do_forwards(client, saved_guid, mid, targets, delay,
                                  label="run")
            print("\n📊 تحلیل:")
            if r["too"] == 0 and r["other"] == 0:
                print(f"  ✅ هر {r['n']} ارسال موفق بود با تاخیر {delay}s — تو این "
                      "شرایط throttle نخوردی. اگه ارسال واقعی می‌ایسته، احتمالاً "
                      "تعداد واقعی بیشتره یا اکانت قبلاً throttle شده.")
            elif r["too"]:
                print(f"  🚫 بعد از {r['first_too_at'] - 1} ارسال موفق، روبیکا "
                      f"TOO_REQUESTS داد. یعنی سقفِ نرخِ روبیکا حدود همین‌جاست.")
                print(f"  چون MAX_ERRORS={config.MAX_ERRORS}، تو ارسال واقعی فقط بعد "
                      f"از {config.MAX_ERRORS} خطا ربات ۵ دقیقه مکث می‌کنه و بعد "
                      "از چند بار تلاش متوقف می‌شه. برای تستِ تاخیرِ امن: "
                      "TEST_SWEEP=1 بزن.")
            if r["other"]:
                print(f"  ❌ {r['other']} خطای غیرِ throttle هم بود — به متن خطاها "
                      "بالا نگاه کن (مثلاً INVALID_AUTH = سشن پریده).")
    except Exception as e:  # noqa: BLE001
        print(f"\n💥 خطای کلی در تشخیص: {e!r}")
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        print("\n✅ تمام شد. (چیزی در برنامه/دیتابیس تغییر نکرد)")


if __name__ == "__main__":
    asyncio.run(main())
