"""VPS 運用用 Pragmatic Play Collector エントリポイント

/opt/laplace/monitor/run_data_collector.py と同じパターン:
  - monitor/ 直下に配置
  - 独立の auth_state ディレクトリ (auth_state_pragmatic_collector) を使用
  - headless 強制
  - stake_cookies.json から cookie 復元
  - systemd が Restart=always で面倒を見る

Usage (systemd 経由):
  ExecStart=/opt/laplace/.venv/bin/python /opt/laplace2/monitor/run_pragmatic_collector.py
"""
import os
import sys
from pathlib import Path

# LAPLACE2 ルートを import path に
_monitor_dir = Path(__file__).resolve().parent
_root = _monitor_dir.parent
sys.path.insert(0, str(_root))

# LAPLACE の ba/ (Camoufox/scraper依存) も参照可能にする
_ba_root = Path("/opt/laplace")
if _ba_root.exists():
    sys.path.insert(0, str(_ba_root))

from dotenv import load_dotenv  # type: ignore
load_dotenv(_monitor_dir / ".env", override=False)

from collector_pragmatic import Collector  # type: ignore


def main():
    profile_dir = _monitor_dir / "pragmatic_profile"
    cookies_file = _monitor_dir / "auth_state_pragmatic_collector" / "stake_cookies.json"
    c = Collector(headless=True, raw_log=False)
    return c.run(duration=None, profile_dir=profile_dir, cookies_file=cookies_file)


if __name__ == "__main__":
    sys.exit(main())
