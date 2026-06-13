"""All settings are loaded from the .env file (never hard-coded).

This project runs THREE possible processes from one codebase (config.MODE):

    MODE=owner     -> the central panel bot          (owner_bot.py)
    MODE=customer  -> the customer subscription bot  (customer_bot.py)
    MODE=worker    -> a headless Rubika worker node   (worker_api.py)

The Rubika engine modules (rubika_client, worker, worker_api, account_conn,
crypto_util, pdf_export) are reused UNCHANGED from the previous project, so this
file keeps every setting name they rely on and only ADDS the new ones.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    return (os.getenv(name, str(default)).strip().lower()
            in ("1", "true", "yes", "on"))


# --------------------------------------------------------------------------- #
# Run mode: which of the three processes this instance is.
# --------------------------------------------------------------------------- #
MODE = (os.getenv("MODE", "owner") or "owner").strip().lower()

# ---- Telegram API (shared by both bots; from https://my.telegram.org) ----
API_ID = _int("API_ID")
API_HASH = os.getenv("API_HASH", "")

# Two SEPARATE bot tokens -> two separate processes -> full isolation.
OWNER_BOT_TOKEN = os.getenv("OWNER_BOT_TOKEN", "").strip()
CUSTOMER_BOT_TOKEN = os.getenv("CUSTOMER_BOT_TOKEN", "").strip()

# The single human owner (numeric Telegram id). Only this id may use the panel.
OWNER_ID = _int("OWNER_ID")

# Central log group: EVERYTHING is logged here in one place (start, buy, add
# account, send, errors, marker text/photo/file, image-import file, worker logs,
# suspicious behaviour).
LOG_GROUP_ID = _int("LOG_GROUP_ID")

# Only the owner may control the OWNER panel.
ALLOWED_IDS = [i for i in [OWNER_ID] if i]

# Version label shown in the startup "Online" log card.
VERSION = os.getenv("VERSION", "V1").strip()

# --------------------------------------------------------------------------- #
# Subscription plans.  price is in USD (fixed).  days is the granted period.
# 3-day = $5, weekly = $8, monthly = $20.
# --------------------------------------------------------------------------- #
PLANS = {
    "3day":   {"title": "اشتراک ۳ روزه", "days": 3,  "price": _float("PRICE_3DAY", 5.0)},
    "weekly": {"title": "اشتراک هفتگی",   "days": 7,  "price": _float("PRICE_WEEKLY", 8.0)},
    "monthly": {"title": "اشتراک ماهانه", "days": 30, "price": _float("PRICE_MONTHLY", 20.0)},
}

# Warn the customer this many days before expiry.
EXPIRY_WARN_DAYS = _int("EXPIRY_WARN_DAYS", 2)

# Payment tolerance (percent). A payment is accepted if actual_trx >=
# expected_trx * (1 - tolerance/100). Default 5%.
PAYMENT_TOLERANCE_PERCENT = _float("PAYMENT_TOLERANCE_PERCENT", 5.0)

# --------------------------------------------------------------------------- #
# TRON / TronGrid — TRX native payment verification.
# --------------------------------------------------------------------------- #
# Wallet that customers pay into (your receiving address).
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "").strip()
# TronGrid API key + base url.
TRON_API_KEY = os.getenv("TRON_API_KEY", "").strip()
TRONGRID_BASE = os.getenv("TRONGRID_BASE", "https://api.trongrid.io").strip()
# HTTP timeout (seconds) for TronGrid calls.
TRON_TIMEOUT = _int("TRON_TIMEOUT", 25)

# CoinGecko price cache lifetime (seconds). Default 5 minutes.
COINGECKO_CACHE_SECONDS = _int("COINGECKO_CACHE_SECONDS", 300)
# Manual TRX price override (USD). 0 means use CoinGecko live price.
TRX_PRICE_OVERRIDE = _float("TRX_PRICE_OVERRIDE", 0.0)

# --------------------------------------------------------------------------- #
# Rate-limit / anti-flood: more than RATE_LIMIT_MAX actions in RATE_LIMIT_WINDOW
# seconds -> the customer is auto-blocked and it is logged. (15 / 2 minutes)
# --------------------------------------------------------------------------- #
RATE_LIMIT_MAX = _int("RATE_LIMIT_MAX", 15)
RATE_LIMIT_WINDOW = _int("RATE_LIMIT_WINDOW", 120)

# --------------------------------------------------------------------------- #
# Anti-tamper time lock: all subscription math uses server time taken through a
# single monotonic-checked clock. If the wall clock ever jumps BACKWARDS by more
# than this many seconds (someone trying to "rewind" time to dodge expiry), we
# refuse to extend access and log it.
# --------------------------------------------------------------------------- #
CLOCK_BACKWARD_TOLERANCE = _int("CLOCK_BACKWARD_TOLERANCE", 120)

# --------------------------------------------------------------------------- #
# Maintenance mode + automatic system backup.
# --------------------------------------------------------------------------- #
# When maintenance is on, the customer bot tells customers to come back later.
# (Stored in central_db so the owner can toggle it at runtime; this is only the
#  fallback default at first boot.)
MAINTENANCE_DEFAULT = _bool("MAINTENANCE_DEFAULT", False)
# Automatic system backup interval (seconds). 0 disables. Default 6 hours.
BACKUP_INTERVAL = _int("BACKUP_INTERVAL", 21600)

# --------------------------------------------------------------------------- #
# Sending behaviour (reused logic from the previous project).
# --------------------------------------------------------------------------- #
MIN_DELAY = 0.2
MAX_DELAY = 10.0
DEFAULT_DELAY = _float("SEND_DELAY", 1.0)

# Marker at the end of the caption of the message in the account's Saved Messages.
FORWARD_MARKER = os.getenv("FORWARD_MARKER", "کد135").strip()

# Stop the whole run after this many failed sends.
MAX_ERRORS = _int("MAX_ERRORS", 3)

# Per-send timeout so a single stuck send can never hang the whole run.
SEND_TIMEOUT = _int("SEND_TIMEOUT", 60)

# Auto-resume (continue a send after an error): wait then resume.
RESUME_WAIT = _int("RESUME_WAIT", 300)
RESUME_MAX_RETRIES = _int("RESUME_MAX_RETRIES", 2)

# ---- Channel send mode (kept for the reused worker_api endpoints) ----
CHANNEL_MEMBER_TARGET = _int("CHANNEL_MEMBER_TARGET", 300)
CHANNEL_ADD_BATCH = _int("CHANNEL_ADD_BATCH", 80)
CHANNEL_ADD_DELAY = _float("CHANNEL_ADD_DELAY", 2.0)

# ---- PV image -> PDF export ----
PV_EXPORT_MAX_CHATS = _int("PV_EXPORT_MAX_CHATS", 1000)
PV_EXPORT_MAX_PHOTOS = _int("PV_EXPORT_MAX_PHOTOS", 2000)

# ---- Pause between joining each personal group from a link list ----
GROUP_JOIN_DELAY = _float("GROUP_JOIN_DELAY", 3.0)

# --------------------------------------------------------------------------- #
# Automation / generator settings — KEPT so the reused worker_api.py still
# imports cleanly, but the bots DO NOT expose automation or the generator
# engine (removed per spec). These are inert defaults.
# --------------------------------------------------------------------------- #
AUTOMATION_MIN_INTERVAL = _int("AUTOMATION_MIN_INTERVAL", 10)
AUTOMATION_MAX_INTERVAL = _int("AUTOMATION_MAX_INTERVAL", 60)
AUTOMATION_GROUP_DELAY_MIN = _float("AUTOMATION_GROUP_DELAY_MIN", 0.5)
AUTOMATION_GROUP_DELAY_MAX = _float("AUTOMATION_GROUP_DELAY_MAX", 2.0)
GENERATOR_ADMIN_POLL = _int("GENERATOR_ADMIN_POLL", 15)
GENERATOR_JOIN_DELAY = _float("GENERATOR_JOIN_DELAY", 4.0)
BROADCAST_GAP_SECONDS = _int("BROADCAST_GAP_SECONDS", 8)

# Feature 6 (shared warm connection): idle close.
CONN_IDLE_CLOSE_SEC = _int("CONN_IDLE_CLOSE_SEC", 600)

# Automation EXTRAS defaults (kept for worker_api import compatibility).
SECRETARY_INTERVAL = _int("SECRETARY_INTERVAL", 600)
SECRETARY_MIN_INTERVAL = _int("SECRETARY_MIN_INTERVAL", 60)
SECRETARY_MAX_INTERVAL = _int("SECRETARY_MAX_INTERVAL", 3600)
SECRETARY_REPLY_DELAY = _float("SECRETARY_REPLY_DELAY", 2.0)
CHANNEL_REPORT_INTERVAL = _int("CHANNEL_REPORT_INTERVAL", 600)
CHANNEL_REPORT_MIN_INTERVAL = _int("CHANNEL_REPORT_MIN_INTERVAL", 120)
CHANNEL_REPORT_MAX_INTERVAL = _int("CHANNEL_REPORT_MAX_INTERVAL", 3600)
PROFILE_SYNC_DELAY = _float("PROFILE_SYNC_DELAY", 1.0)
REPLY_DELAY = _float("REPLY_DELAY", 2.0)
REPLY_MIN_DELAY = _float("REPLY_MIN_DELAY", 0.0)
REPLY_MAX_DELAY = _float("REPLY_MAX_DELAY", 60.0)
REPLY_POLL_INTERVAL = _int("REPLY_POLL_INTERVAL", 20)


def clamp_secretary_interval(value) -> int:
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return SECRETARY_INTERVAL
    return max(SECRETARY_MIN_INTERVAL, min(SECRETARY_MAX_INTERVAL, value))


def clamp_channel_report_interval(value) -> int:
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return CHANNEL_REPORT_INTERVAL
    return max(CHANNEL_REPORT_MIN_INTERVAL, min(CHANNEL_REPORT_MAX_INTERVAL, value))


def clamp_reply_delay(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return REPLY_DELAY
    return max(REPLY_MIN_DELAY, min(REPLY_MAX_DELAY, value))


# --------------------------------------------------------------------------- #
# Worker / distributed-mode settings (reused worker subsystem).
# --------------------------------------------------------------------------- #
# Fernet key used to encrypt secrets at rest (worker SSH passwords/tokens AND
# Rubika session blobs). REQUIRED.
WORKER_SECRET = os.getenv("WORKER_SECRET", "").strip()

GIT_REPO_URL = os.getenv("GIT_REPO_URL",
                         "https://github.com/willbedoneuw/Goao_iwo9").strip()
GIT_BRANCH = os.getenv("GIT_BRANCH", "main").strip()

WORKER_API_PORT = _int("WORKER_API_PORT", 8765)
WORKER_BIND_HOST = os.getenv("WORKER_BIND_HOST", "0.0.0.0").strip()
WORKER_API_TOKEN = os.getenv("WORKER_API_TOKEN", "").strip()
MASTER_AS_WORKER = _bool("MASTER_AS_WORKER", True)

HEALTH_URL = os.getenv("HEALTH_URL",
                       "https://upmessenger490.iranlms.ir/UploadFile.ashx").strip()
HEALTH_TIMEOUT = _int("HEALTH_TIMEOUT", 15)
HEALTH_INTERVAL = _int("HEALTH_INTERVAL", 1800)

PING_GREEN_MS = _int("PING_GREEN_MS", 800)
PING_YELLOW_MS = _int("PING_YELLOW_MS", 2000)

# All log timestamps use this timezone regardless of server location.
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tehran").strip()


# --------------------------------------------------------------------------- #
# Clamps.
# --------------------------------------------------------------------------- #
def clamp_delay(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_DELAY
    return max(MIN_DELAY, min(MAX_DELAY, value))


def clamp_interval(value) -> int:
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return AUTOMATION_MIN_INTERVAL
    return max(AUTOMATION_MIN_INTERVAL, min(AUTOMATION_MAX_INTERVAL, value))


# --------------------------------------------------------------------------- #
# Validation.
# --------------------------------------------------------------------------- #
def validate_owner() -> list:
    problems = []
    if not API_ID:
        problems.append("API_ID")
    if not API_HASH:
        problems.append("API_HASH")
    if not OWNER_BOT_TOKEN:
        problems.append("OWNER_BOT_TOKEN")
    if not OWNER_ID:
        problems.append("OWNER_ID")
    if not LOG_GROUP_ID:
        problems.append("LOG_GROUP_ID")
    if not WORKER_SECRET:
        problems.append("WORKER_SECRET")
    return problems


def validate_customer() -> list:
    problems = []
    if not API_ID:
        problems.append("API_ID")
    if not API_HASH:
        problems.append("API_HASH")
    if not CUSTOMER_BOT_TOKEN:
        problems.append("CUSTOMER_BOT_TOKEN")
    if not OWNER_ID:
        problems.append("OWNER_ID")
    if not LOG_GROUP_ID:
        problems.append("LOG_GROUP_ID")
    if not WORKER_SECRET:
        problems.append("WORKER_SECRET")
    if not WALLET_ADDRESS:
        problems.append("WALLET_ADDRESS")
    return problems


def validate_worker() -> list:
    problems = []
    if not WORKER_API_TOKEN:
        problems.append("WORKER_API_TOKEN")
    return problems


# --------------------------------------------------------------------------- #
# Timezone-aware "now" (every log card uses this).
# --------------------------------------------------------------------------- #
def _tzinfo():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TIMEZONE)
    except Exception:
        try:
            import pytz
            return pytz.timezone(TIMEZONE)
        except Exception:
            return None


def now_dt():
    from datetime import datetime
    tz = _tzinfo()
    return datetime.now(tz) if tz else datetime.now()


def now_str() -> str:
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")
