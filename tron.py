"""
tron.py — USDT (TRC20) payment verification via TronGrid.
=========================================================

A customer pays the configured wallet in USDT (TRC20) and sends us the
transaction hash. We confirm the payment ENTIRELY on-chain, with no trust in
the customer's claim:

  1) the transaction exists and is CONFIRMED (contractRet == SUCCESS and packed
     into a block with a SUCCESS receipt),
  2) it is a TRC20 transfer of the official USDT contract,
  3) the destination address equals OUR wallet,
  4) the transferred amount equals the plan price (within a tiny tolerance).

Anti-fraud "each hash only once" is enforced by db.record_payment (the tx hash
column is UNIQUE), so even concurrent submissions cannot double-credit.

We decode the transfer straight from the raw transaction `data` field
(method id a9059cbb = transfer(address,uint256)), which avoids relying on
TronGrid's event-indexing lag. Address (base58 <-> hex) conversion is done
locally so there is no extra dependency.
"""
from __future__ import annotations

import hashlib

import config

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# --------------------------------------------------------------------------- #
# base58check <-> hex TRON address helpers.
# --------------------------------------------------------------------------- #
def _b58decode(s: str) -> bytes:
    num = 0
    for ch in s:
        num = num * 58 + _B58_ALPHABET.index(ch)
    full = num.to_bytes((num.bit_length() + 7) // 8, "big")
    # restore leading zero bytes (each leading '1' == one 0x00 byte)
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + full


def base58_to_hex(addr: str) -> str:
    """Convert a base58check TRON address (T...) to its 41-prefixed hex form."""
    raw = _b58decode(addr)
    if len(raw) != 25:
        raise ValueError("bad TRON address length")
    payload, checksum = raw[:-4], raw[-4:]
    digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if digest != checksum:
        raise ValueError("bad TRON address checksum")
    return payload.hex()  # starts with '41'


def hex_to_base58(hex_addr: str) -> str:
    """Convert a 41-prefixed hex TRON address to base58check (T...)."""
    payload = bytes.fromhex(hex_addr)
    digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    raw = payload + digest
    num = int.from_bytes(raw, "big")
    out = ""
    while num > 0:
        num, rem = divmod(num, 58)
        out = _B58_ALPHABET[rem] + out
    pad = 0
    for b in raw:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + out


# --------------------------------------------------------------------------- #
# TronGrid calls.
# --------------------------------------------------------------------------- #
def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if config.TRON_API_KEY:
        h["TRON-PRO-API-KEY"] = config.TRON_API_KEY
    return h


async def _get_tx(client, tx_hash: str) -> dict:
    url = f"{config.TRONGRID_BASE}/wallet/gettransactionbyid"
    r = await client.post(url, json={"value": tx_hash}, headers=_headers())
    r.raise_for_status()
    return r.json() or {}


async def _get_tx_info(client, tx_hash: str) -> dict:
    url = f"{config.TRONGRID_BASE}/wallet/gettransactioninfobyid"
    r = await client.post(url, json={"value": tx_hash}, headers=_headers())
    r.raise_for_status()
    return r.json() or {}


def _decode_transfer(data_hex: str):
    """Decode a TRC20 transfer(address,uint256) data blob.
    Returns (to_hex_41, raw_amount_int) or (None, None) if not a transfer."""
    data_hex = (data_hex or "").lower()
    if not data_hex.startswith("a9059cbb") or len(data_hex) < 8 + 64 + 64:
        return None, None
    to_arg = data_hex[8:8 + 64]            # 32-byte address arg (left-padded)
    amount_arg = data_hex[8 + 64:8 + 128]  # 32-byte uint256 amount
    to_hex = "41" + to_arg[-40:]           # last 20 bytes + TRON 0x41 prefix
    try:
        raw_amount = int(amount_arg, 16)
    except ValueError:
        return None, None
    return to_hex, raw_amount


class VerifyResult:
    def __init__(self, ok: bool, reason: str = "", amount: float = 0.0,
                 to_address: str = "", confirmed: bool = False):
        self.ok = ok
        self.reason = reason
        self.amount = amount
        self.to_address = to_address
        self.confirmed = confirmed


async def verify_usdt_payment(tx_hash: str, expected_amount: float) -> VerifyResult:
    """Verify ONE USDT (TRC20) transfer on-chain.

    Returns VerifyResult(ok=True, ...) only when the transaction is a confirmed
    USDT transfer of EXACTLY `expected_amount` into the configured wallet.
    `ok=False` carries a human-readable Persian `reason`.
    """
    tx_hash = (tx_hash or "").strip().lower().replace("0x", "")
    if not tx_hash or len(tx_hash) < 60:
        return VerifyResult(False, "هش تراکنش نامعتبره.")
    if not config.WALLET_ADDRESS:
        return VerifyResult(False, "آدرس ولت مقصد تنظیم نشده.")

    try:
        import httpx
    except ImportError:
        return VerifyResult(False, "httpx نصب نیست.")

    try:
        wallet_hex = base58_to_hex(config.WALLET_ADDRESS).lower()
        usdt_hex = base58_to_hex(config.USDT_CONTRACT).lower()
    except Exception:
        return VerifyResult(False, "آدرس ولت/قرارداد نامعتبره.")

    try:
        async with httpx.AsyncClient(timeout=config.TRON_TIMEOUT) as client:
            tx = await _get_tx(client, tx_hash)
            if not tx or "raw_data" not in tx:
                return VerifyResult(False, "تراکنش پیدا نشد (هنوز ثبت نشده؟).")

            # 1) confirmed + successful
            ret = (tx.get("ret") or [{}])[0]
            if str(ret.get("contractRet", "")).upper() != "SUCCESS":
                return VerifyResult(False, "تراکنش روی شبکه موفق/تأییدشده نیست.")

            contracts = tx.get("raw_data", {}).get("contract", [])
            if not contracts:
                return VerifyResult(False, "محتوای تراکنش نامعتبره.")
            c0 = contracts[0]
            if c0.get("type") != "TriggerSmartContract":
                return VerifyResult(False, "این تراکنش انتقال TRC20 نیست.")

            value = c0.get("parameter", {}).get("value", {})
            contract_addr = str(value.get("contract_address", "")).lower()
            if contract_addr != usdt_hex:
                return VerifyResult(False, "این تراکنش مربوط به قرارداد USDT نیست.")

            to_hex, raw_amount = _decode_transfer(value.get("data", ""))
            if to_hex is None:
                return VerifyResult(False, "تراکنش یک انتقال استاندارد USDT نیست.")
            to_hex = to_hex.lower()

            if to_hex != wallet_hex:
                return VerifyResult(
                    False, "مقصد تراکنش با آدرس ولت ما یکی نیست.",
                    to_address=hex_to_base58(to_hex))

            amount = raw_amount / (10 ** config.USDT_DECIMALS)

            # 2) double-check it is packed into a block with a SUCCESS receipt
            info = await _get_tx_info(client, tx_hash)
            confirmed = bool(info.get("blockNumber"))
            receipt = (info.get("receipt") or {}).get("result")
            if receipt and str(receipt).upper() != "SUCCESS":
                return VerifyResult(False, "رسید تراکنش موفق نیست.")
            if not confirmed:
                return VerifyResult(False, "تراکنش هنوز در بلاک تأیید نشده.",
                                    amount=amount,
                                    to_address=config.WALLET_ADDRESS)

            # 3) exact amount (within tolerance)
            if abs(amount - float(expected_amount)) > config.PAYMENT_AMOUNT_TOLERANCE:
                return VerifyResult(
                    False,
                    f"مبلغ تراکنش ({amount:g} USDT) با مبلغ پلن "
                    f"({float(expected_amount):g} USDT) برابر نیست.",
                    amount=amount, to_address=config.WALLET_ADDRESS,
                    confirmed=True)

            return VerifyResult(True, "", amount=amount,
                                to_address=config.WALLET_ADDRESS, confirmed=True)
    except Exception as e:  # noqa: BLE001
        return VerifyResult(False, f"خطا در ارتباط با TronGrid: {repr(e)[:140]}")
