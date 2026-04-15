"""バカラモニター + 自動BETシステム設定"""
import os
import configparser
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# --- Paths ---
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
AUTH_STATE_DIR = BASE_DIR / "auth_state"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
DB_PATH = DATA_DIR / "baccarat.db"
CONFIG_INI_PATH = BASE_DIR / "config.ini"

DATA_DIR.mkdir(exist_ok=True)
AUTH_STATE_DIR.mkdir(exist_ok=True)
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# --- config.ini 読み込み ---
_ini = configparser.ConfigParser()
if CONFIG_INI_PATH.exists():
    _ini.read(str(CONFIG_INI_PATH), encoding="utf-8")


def _ini_get(section: str, key: str, fallback: str = "") -> str:
    return _ini.get(section, key, fallback=fallback)


def _ini_getint(section: str, key: str, fallback: int = 0) -> int:
    return _ini.getint(section, key, fallback=fallback)


def _ini_getfloat(section: str, key: str, fallback: float = 0.0) -> float:
    return _ini.getfloat(section, key, fallback=fallback)


def _ini_getbool(section: str, key: str, fallback: bool = False) -> bool:
    return _ini.getboolean(section, key, fallback=fallback)


# --- Profile (複数プロセス同時実行の分離用) ---
PROFILE_NAME = os.getenv("PROFILE_NAME", _ini_get("monitor", "profile_name", "default"))

# --- Stake.com ---
STAKE_USERNAME = os.getenv("STAKE_USERNAME", "")
STAKE_PASSWORD = os.getenv("STAKE_PASSWORD", "")
STAKE_URL = _ini_get("casino", "url", "https://stake.com")
BACCARAT_LOBBY_URL = _ini_get(
    "casino", "lobby_url",
    f"{STAKE_URL}/casino/games/evolution-baccarat-lobby",
)

# --- Telegram (optional) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Monitor ---
HEADLESS = _ini_getbool("monitor", "headless", False)
POLL_INTERVAL = _ini_getint("monitor", "poll_interval", 5)
MAX_RETRIES = _ini_getint("monitor", "max_retries", 10)
RETRY_DELAY = _ini_getint("monitor", "retry_delay", 30)
REPORT_INTERVAL = _ini_getint("monitor", "report_interval", 3600)
WS_SILENCE_THRESHOLD = _ini_getint("monitor", "ws_silence_threshold", 300)
TARGET_TABLE = os.getenv(
    "TARGET_TABLE",
    _ini_get("casino", "target_tables", "Japanese Baccarat"),
)
VIDEO_QUALITY = os.getenv("VIDEO_QUALITY", _ini_get("monitor", "video_quality", "auto")).lower()
VIDEO_VIEWPORT_WIDTH = int(os.getenv("VIDEO_VIEWPORT_WIDTH", _ini_getint("monitor", "video_viewport_width", 1280)))
VIDEO_VIEWPORT_HEIGHT = int(os.getenv("VIDEO_VIEWPORT_HEIGHT", _ini_getint("monitor", "video_viewport_height", 720)))

# --- BET ---
BET_ENABLED = _ini_getbool("bet", "enabled", False)
BET_MIN = _ini_getfloat("bet", "min_bet", 1.0)
BET_MAX = _ini_getfloat("bet", "max_bet", 10.0)
DAILY_LOSS_LIMIT = _ini_getfloat("bet", "daily_loss_limit", 100.0)
DAILY_PROFIT_TARGET = _ini_getfloat("bet", "daily_profit_target", 50.0)
BET_STRATEGY = _ini_get("bet", "strategy", "yokonagare")
BET_DEMO_MODE = _ini_getbool("bet", "demo_mode", True)

# --- Strategy ---
STRATEGY_CONFIG = {
    "strategy": BET_STRATEGY,
    "min_regularity_score": _ini_getint("strategy.yokonagare", "min_regularity_score", 60),
    "max_consecutive_loss": _ini_getint("strategy.yokonagare", "max_consecutive_loss", 3),
}

# --- Humanize ---
HUMANIZE_CONFIG = {
    "enabled": _ini_getbool("humanize", "enabled", True),
    "mouse_speed_min": _ini_getint("humanize", "mouse_speed_min", 200),
    "mouse_speed_max": _ini_getint("humanize", "mouse_speed_max", 600),
    "bet_interval_min": _ini_getint("humanize", "bet_interval_min", 2),
    "bet_interval_max": _ini_getint("humanize", "bet_interval_max", 8),
    "session_minutes_min": _ini_getint("humanize", "session_minutes_min", 25),
    "session_minutes_max": _ini_getint("humanize", "session_minutes_max", 40),
    "break_minutes_min": _ini_getint("humanize", "break_minutes_min", 5),
    "break_minutes_max": _ini_getint("humanize", "break_minutes_max", 10),
    "skip_bet_probability": _ini_getfloat("humanize", "skip_bet_probability", 0.07),
}

# --- Executor ---
EXECUTOR_CONFIG = {
    "demo_mode": BET_DEMO_MODE,
    "max_shoes_per_table": _ini_getint("bet", "max_shoes_per_table", 3),
}

# --- Notification ---
NOTIFY_EVERY_BET = _ini_getbool("notification", "notify_every_bet", False)
NOTIFY_SESSION_SUMMARY = _ini_getbool("notification", "notify_session_summary", True)
NOTIFY_SHOE_COMPLETE = _ini_getbool("notification", "notify_shoe_complete", True)
