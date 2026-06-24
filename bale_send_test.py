# -*- coding: utf-8 -*-
"""
bale_send_test.py — standalone end-to-end test for the Bale section.

Run from the repo root with the project's venv:

    cd /root/Goao_iwo9
    systemctl stop goao-customer        # avoid session conflict
    ./venv/bin/python bale_send_test.py
    systemctl start goao-customer

It does NOT touch any project module. It uses the EXISTING .bale session under
data/bale_sessions/ and:
  1) loads the session/token (login check),
  2) reads contacts (GetContacts),
  3) reads PV + groups (LoadDialogs),
  4) sends a TEST text to the first 5 contacts (raw path that bypasses
     aiobale's broken connection-less response parser).

This is a throwaway diagnostic file; delete it whenever you want.
"""
import asyncio
import glob
import traceback

import aiohttp
from aiobale import Client
from aiobale.enums import ChatType, PeerType
from aiobale.methods import SendMessage, GetContacts, LoadDialogs
from aiobale.types import MessageContent, TextMessage
from aiobale.utils import add_header, clean_grpc

try:
    from aiobale.utils import generate_id
except Exception:  # pragma: no cover
    import random
    def generate_id():
        return random.randint(1, 2 ** 31)

TEST_TEXT = "🤖 پیام تستِ سیستم — لطفاً نادیده بگیرید (automated test)."
N = 5
DELAY = 2.0


def g(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d:
            return d[k]
        if str(k) in d:
            return d[str(k)]
    return None


def extract_peers(raw):
    dialogs = g(raw, "3", 3) or []
    if isinstance(dialogs, dict):
        dialogs = [dialogs]
    out = []
    for d in dialogs:
        peer = g(d, "1", 1) or {}
        ptype = g(peer, "1", 1)
        pid = g(peer, "2", 2)
        sub = g(g(d, "13", 13) or {}, "1", 1)
        if pid is None:
            continue
        out.append({
            "id": int(pid),
            "type": int(ptype) if ptype is not None else 0,
            "ctype": int(sub) if sub is not None else 0,
        })
    return out


async def raw_request(client, method, timeout=30):
    """Fire a method over plain HTTP and return the RAW decoded dict (this is
    exactly how the working READ path in bale_panel.py talks to Bale, and it
    sidesteps aiobale's response-model validator that needs a started client)."""
    sess = client.session
    if sess.session is None or sess.session.closed:
        sess.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), proxy=sess.proxy)
    token = getattr(client, "_Client__token", None)
    headers = {
        "User-Agent": sess.user_agent,
        "Origin": "https://web.bale.ai",
        "content-type": "application/grpc-web+proto",
    }
    try:
        headers.update({k[0].upper() + k[1:]: v for k, v in sess._get_meta().items()})
    except Exception:
        pass
    if token:
        headers.update(sess._build_headers(token))
    url = f"{sess.post_url}/{method.__service__}/{method.__method__}"
    data = method.model_dump(by_alias=True, exclude_none=True)
    req = await sess.session.post(url=url, headers=headers,
                                  data=add_header(sess.encoder(data)))
    content = await req.read()
    gm = req.headers.get("grpc-message")
    if gm is not None:
        raise RuntimeError(f"grpc: {gm}")
    return sess.decoder(clean_grpc(content))


async def send_text(client, cid, text):
    chat = client._build_chat(int(cid), ChatType.PRIVATE)
    peer = client._resolve_peer(chat)
    call = SendMessage(peer=peer, message_id=generate_id(),
                       content=MessageContent(text=TextMessage(value=text)),
                       chat=chat)
    return await raw_request(client, call)


async def main():
    sf = (glob.glob("data/bale_sessions/*.bale") or [None])[0]
    print("session file:", sf)
    if not sf:
        print("NO .bale SESSION FOUND under data/bale_sessions/ — login first.")
        return

    c = Client(session_file=sf)
    print("\n=== 1) LOGIN / SESSION ===")
    print("token loaded:", getattr(c, "_Client__token", None) is not None,
          "| account id:", getattr(c, "id", None))
    me = int(getattr(c, "id", 0) or 0)

    print("\n=== 2) READ CONTACTS ===")
    contacts = []
    try:
        rawc = await raw_request(c, GetContacts())
        clist = g(rawc, "3", 3) or []
        if isinstance(clist, dict):
            clist = [clist]
        for x in clist:
            cid = g(x, "1", 1)
            if cid is not None and int(cid) != me:
                contacts.append(int(cid))
        print("contacts found:", len(contacts))
    except Exception as e:
        print("READ CONTACTS FAILED:", repr(e))
        traceback.print_exc()

    print("\n=== 3) READ DIALOGS (PV / GROUPS) ===")
    pv, groups = [], []
    try:
        raw = await raw_request(
            c, LoadDialogs(offset_date=-1, limit=500, exclude_pinned=False))
        for p in extract_peers(raw):
            if p["type"] == int(PeerType.PRIVATE) and p["ctype"] != 4 and p["id"] != me:
                pv.append(p["id"])
            elif p["type"] == int(PeerType.GROUP):
                groups.append(p["id"])
        print("pv found:", len(pv), "| groups found:", len(groups))
    except Exception as e:
        print("READ DIALOGS FAILED:", repr(e))
        traceback.print_exc()

    targets = (contacts or pv)[:N]
    print(f"\n=== 4) SEND TEST to {len(targets)} targets ===")
    print("targets:", targets)
    ok = fail = 0
    for i, cid in enumerate(targets, 1):
        try:
            await asyncio.wait_for(send_text(c, cid, TEST_TEXT), timeout=30)
            ok += 1
            print(f"[{i}] {cid} -> OK")
        except Exception as e:
            fail += 1
            print(f"[{i}] {cid} -> FAIL: {type(e).__name__}: {e}")
        await asyncio.sleep(DELAY)

    print(f"\n=== DONE === ok={ok} fail={fail} (total {len(targets)})")
    try:
        s = c.session
        if s and not s.is_closed():
            await s.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
