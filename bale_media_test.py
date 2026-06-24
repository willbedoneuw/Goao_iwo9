# -*- coding: utf-8 -*-
"""
bale_media_test.py — prove the PHOTO/FILE send path for Bale (raw).

Run from the repo root with the project's venv:

    cd /root/Goao_iwo9
    systemctl stop goao-customer
    ./venv/bin/python bale_media_test.py
    systemctl start goao-customer

It uses the EXISTING .bale session, generates a tiny 1x1 PNG, UPLOADS it with
aiobale's upload_file (which works connection-less), then sends it AS A PHOTO to
your OWN account (saved messages) via the raw path that bypasses aiobale's
broken response parser. Throwaway diagnostic file; delete whenever.
"""
import asyncio
import base64
import glob
import os
import tempfile
import traceback

import aiohttp
from aiobale import Client
from aiobale.enums import ChatType, SendType
from aiobale.methods import SendMessage
from aiobale.types import (MessageContent, DocumentMessage, MessageCaption,
                           DocumentsExt, PhotoExt, FileInput)
from aiobale.utils import add_header, clean_grpc

try:
    from aiobale.utils import generate_id
except Exception:  # pragma: no cover
    import random
    def generate_id():
        return random.randint(1, 2 ** 31)

# a minimal valid 1x1 PNG
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")

CAPTION = "🤖 تست عکس Bale — نادیده بگیرید (automated test)."


async def raw_request(client, method, timeout=60):
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


async def main():
    sf = (glob.glob("data/bale_sessions/*.bale") or [None])[0]
    print("session file:", sf)
    if not sf:
        print("NO .bale SESSION FOUND.")
        return

    tmp = os.path.join(tempfile.gettempdir(), "bale_probe.png")
    with open(tmp, "wb") as f:
        f.write(_PNG_1x1)
    print("test image:", tmp, os.path.getsize(tmp), "bytes")

    c = Client(session_file=sf)
    me = int(getattr(c, "id", 0) or 0)
    print("account id:", me)

    try:
        print("\n=== 1) UPLOAD FILE ===")
        fi = await c.upload_file(file=FileInput(tmp), chat_id=me,
                                 chat_type=ChatType.PRIVATE, send_type=SendType.PHOTO)
        print("uploaded:", "file_id=", getattr(fi, "file_id", None),
              "size=", getattr(fi, "size", None), "name=", getattr(fi, "name", None),
              "mime=", getattr(fi, "mime_type", None),
              "access_hash=", getattr(fi, "access_hash", None))

        print("\n=== 2) BUILD + RAW SEND (photo to self) ===")
        chat = c._build_chat(me, ChatType.PRIVATE)
        peer = c._resolve_peer(chat)
        caption = MessageCaption(content=CAPTION)
        ext = DocumentsExt(photo=PhotoExt(w=1000, h=1000))
        document = DocumentMessage(
            file_id=fi.file_id, size=fi.size, name=fi.name,
            mime_type=fi.mime_type, access_hash=fi.access_hash,
            caption=caption, thumb=None, ext=ext)
        content = MessageContent(document=document)
        call = SendMessage(peer=peer, message_id=generate_id(),
                           content=content, chat=chat)
        r = await raw_request(c, call)
        print("PHOTO SEND OK ->", str(r)[:250])
    except Exception as e:
        print("MEDIA TEST FAILED ->", type(e).__name__, e)
        traceback.print_exc()
    finally:
        try:
            s = c.session
            if s and not s.is_closed():
                await s.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
