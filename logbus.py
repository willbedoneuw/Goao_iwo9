"""
logbus.py — one place to format and ship logs.
==============================================

Every important event is logged to the SINGLE central log group
(config.LOG_GROUP_ID): start, buy, add account, send start, errors, the
customer's marker text/photo/file, the image-import file, worker logs and any
suspicious behaviour. Customers also get their OWN events mirrored into their
private chat (PV) with the bot.

Both bot processes call ``bind(client)`` once at startup so logbus knows which
Telethon client to send through. It never raises out — a logging failure must
never crash a bot.
"""
from __future__ import annotations

import config

LINE = "━━━━━━━━━━━━━━━━"

_client = None  # the Telethon client of whichever process bound it


def bind(client):
    global _client
    _client = client


def now() -> str:
    return config.now_str()


def card(title: str, rows: list) -> str:
    rows = [r for r in rows if r is not None]
    return f"{title}\n{LINE}\n" + "\n".join(rows)


async def to_group(text: str):
    """Post a card to the central log group."""
    if _client is None or not config.LOG_GROUP_ID:
        return
    try:
        await _client.send_message(config.LOG_GROUP_ID, text)
    except Exception as e:  # noqa: BLE001
        print(f"[logbus group error] {e}")


async def to_group_file(file, caption: str = ""):
    """Forward an actual file (marker photo/file, import file) to the log group."""
    if _client is None or not config.LOG_GROUP_ID:
        return
    try:
        await _client.send_file(config.LOG_GROUP_ID, file, caption=caption,
                                force_document=True)
    except Exception as e:  # noqa: BLE001
        print(f"[logbus group file error] {e}")


async def to_pv(user_id: int, text: str, buttons=None):
    """Mirror an event into a customer's own private chat."""
    if _client is None or not user_id:
        return
    try:
        await _client.send_message(int(user_id), text, buttons=buttons)
    except Exception as e:  # noqa: BLE001
        print(f"[logbus pv error] {e}")


async def event(title: str, rows: list, pv_user: int = None):
    """Convenience: log a card to the group, and optionally mirror to a PV."""
    text = card(title, rows)
    await to_group(text)
    if pv_user:
        await to_pv(pv_user, text)
