"""Monitor Stakeログインスクリプト"""
import os
import sys

# monitor/.envを先に読み込む
_dir = os.path.dirname(os.path.abspath(__file__))
_parent = os.path.dirname(_dir)
from dotenv import load_dotenv
load_dotenv(os.path.join(_dir, ".env"), override=True)
sys.path.insert(0, _parent)

# config上書き
import config
config.AUTH_STATE_DIR = config.Path(_dir) / "auth_state"
config.AUTH_STATE_DIR.mkdir(exist_ok=True)
config.STAKE_USERNAME = os.getenv("STAKE_USERNAME", "")
config.STAKE_PASSWORD = os.getenv("STAKE_PASSWORD", "")
config.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
config.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

print(f"Account: {config.STAKE_USERNAME}")
print(f"Cookie dir: {config.AUTH_STATE_DIR}")

# telegram_authのグローバル変数: ログイン時は個人チャットを使う(チャンネルではBotが受信不可)
import telegram_auth
telegram_auth.TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
# 個人受信用 Chat ID (.env の TELEGRAM_PRIVATE_CHAT_ID から読む)
telegram_auth.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_PRIVATE_CHAT_ID", config.TELEGRAM_CHAT_ID)
telegram_auth.API_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

print(f"Telegram chat: {config.TELEGRAM_CHAT_ID}")

# login_step1のmain関数を呼ぶ
import importlib.util
spec = importlib.util.spec_from_file_location("login_step1", os.path.join(_parent, "login_step1.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--code", default="", help="Email 2FAコード")
parser.add_argument("--file-mode", action="store_true")
args = parser.parse_args()
mod.main(args)
