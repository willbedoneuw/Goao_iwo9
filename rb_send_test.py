# -*- coding: utf-8 -*-
"""
rb_send_test.py — direct, single-connection Rubika send test (no bot, no churn).

Run on the machine that holds THIS account's .rb session (the worker it's
assigned to, or the master if it's local). STOP that machine's bot/worker
service first so ONLY this test connects to the session:

    # on the worker that has the account:
    systemctl stop goao-worker      # or whatever the worker service is named
    cd <repo>
    ./venv/bin/python rb_send_test.py 989227458187
    # (optional args: <phone> <marker> <limit> <delay>)

What it does:
  1) opens ONE clean connection (no prep+send double-connect),
  2) finds the marked message in Saved Messages,
  3) forwards it to the first <limit> recipients, one by one,
  4) on the FIRST failure prints the EXACT Rubika error + whether it's
     classified as an auth error (so we see AUTH_FROM_ANOTHER / INVALID_AUTH /
     too_requests / NOT_REGISTERED at message ~8).

If it STILL dies at ~8 with this single clean connection => it's NOT the bot's
double-connect; the account is being used elsewhere (phone/another worker) or
Rubika is flood-limiting. If it does NOT die => the bot churn was the cause.
"""
import asyncio
import sys
import traceback

import config
import rubika_client as rb
import account_conn


PHONE = sys.argv[1] if len(sys.argv) > 1 else "989227458187"
MARKER = sys.argv[2] if len(sys.argv) > 2 else config.FORWARD_MARKER
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 25
DELAY = float(sys.argv[4]) if len(sys.argv) > 4 else float(
    getattr(config, "SEND_DELAY", 2) or 2)


async def main():
    import os
    sp = rb.session_path(PHONE)
    print("phone        :", PHONE)
    print("marker       :", repr(MARKER))
    print("session path :", sp)
    print("session here :", any(os.path.exists(sp + ext) or os.path.exists(sp)
                                 for ext in ("", ".rb", ".session")))
    print("delay        :", DELAY, "| limit:", LIMIT)
    print("-" * 40)

    client = rb.open_client(PHONE)
    try:
        await rb.connect_ready(client)
    except Exception as e:
        print("CONNECT FAILED ->", type(e).__name__, e)
        print("=> session probably not on THIS machine, or already dead.")
        traceback.print_exc()
        return
    try:
        print("connected. self_guid:", await rb.get_self_guid(client))
    except Exception as e:
        print("get_self_guid FAILED ->", type(e).__name__, e)

    try:
        saved_guid, mid = await rb.find_marked_message(client, MARKER)
    except Exception as e:
        print("find_marked_message FAILED ->", type(e).__name__, e)
        traceback.print_exc()
        await _close(client)
        return
    print("marker found :", bool(mid), "| mid:", mid)
    if not mid:
        print("=> no message with this marker in Saved Messages. Set the marker"
              " or pass it as arg2.")
        await _close(client)
        return

    try:
        ordered, _stats = await rb.get_ordered_recipients(client)
    except Exception as e:
        print("get_ordered_recipients FAILED ->", type(e).__name__, e)
        traceback.print_exc()
        await _close(client)
        return
    recips = [r["guid"] for r in ordered][:LIMIT]
    print("recipients   :", len(recips), "(forwarding one by one)")
    print("-" * 40)

    ok = fail = 0
    for i, g in enumerate(recips, 1):
        try:
            await asyncio.wait_for(
                rb.forward_message(client, saved_guid, g, mid),
                timeout=config.SEND_TIMEOUT)
            ok += 1
            print(f"[{i:>3}] {g} -> OK")
        except Exception as e:
            fail += 1
            print(f"[{i:>3}] {g} -> FAIL: {type(e).__name__}: {e}")
            try:
                print("       is_auth_error :", account_conn.is_auth_error(e))
            except Exception:
                pass
            traceback.print_exc()
            print("       (^ this is the EXACT Rubika error at message", i, ")")
            # keep going a couple more to see if it's per-recipient or session-wide
            if fail >= 3:
                print("3 failures — stopping the test.")
                break
        await asyncio.sleep(DELAY)

    print("-" * 40)
    print(f"DONE ok={ok} fail={fail}")
    await _close(client)


async def _close(client):
    try:
        await client.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
