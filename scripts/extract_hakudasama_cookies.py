"""hakudasama Stake cookie 抽出スクリプト.

Camoufox を別プロファイルで起動し、ユーザーが stake.com にログイン完了したら
VPS 配置用に Playwright 互換形式の cookies JSON をエクスポートする。

使い方:
    python scripts/extract_hakudasama_cookies.py

環境変数:
    STAKE_EMAIL  (任意): ログインフォームに事前入力
    STAKE_PASSWORD (任意): ログインフォームに事前入力
    OUTPUT (任意): 出力ファイル (default: data/hakudasama_stake_cookies.json)
    PROFILE_DIR (任意): Camoufox プロファイル保存先
        (default: data/camoufox_profile_hakudasama)

注意:
    - lself の既存ログインには一切触りません（別プロファイル）
    - 認証コードはユーザーが GUI 側で手入力
    - ログイン完了したらターミナルで Enter を押す
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    try:
        from camoufox.sync_api import Camoufox  # type: ignore
    except ImportError:
        print("[ERROR] camoufox が import できません。`pip install camoufox` 済みか確認してください。", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parent.parent
    profile_dir = Path(os.environ.get("PROFILE_DIR") or (root / "data" / "camoufox_profile_hakudasama"))
    output_path = Path(os.environ.get("OUTPUT") or (root / "data" / "hakudasama_stake_cookies.json"))
    email = os.environ.get("STAKE_EMAIL", "").strip()
    password = os.environ.get("STAKE_PASSWORD", "").strip()

    profile_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[info] profile_dir = {profile_dir}")
    print(f"[info] output      = {output_path}")
    print(f"[info] email set   = {bool(email)}")
    print(f"[info] password set= {bool(password)}")

    launch_opts = {
        "headless": False,
        "persistent_context": True,
        "user_data_dir": str(profile_dir),
    }

    with Camoufox(**launch_opts) as ctx:
        page = ctx.new_page() if not ctx.pages else ctx.pages[0]
        print("[info] stake.com へ遷移します...")
        page.goto("https://stake.com/", wait_until="domcontentloaded", timeout=60000)
        print()
        print("========================================")
        print("  Camoufox で以下を実施してください:")
        print("  1. 右上の [Sign in] をクリック")
        print(f"  2. Email:    {email or '(手入力)'}")
        print(f"  3. Password: {password or '(手入力)'}")
        print("  4. 認証コードがメールに届いたら入力")
        print("  ログイン成功を検出するまで最大10分待ちます")
        print("========================================")
        print()

        # ログイン完了を polling で検出: session cookie が出るまで待つ
        max_wait_sec = int(os.environ.get("LOGIN_MAX_WAIT_SEC", "600"))
        poll_interval = 3
        elapsed = 0
        session_val = ""
        while elapsed < max_wait_sec:
            try:
                current = ctx.cookies()
            except Exception as e:
                print(f"[warn] cookies() でエラー: {e}", file=sys.stderr)
                current = []
            s = next((c for c in current if c.get("name") == "session" and "stake.com" in (c.get("domain") or "")), None)
            if s and str(s.get("value") or "").strip():
                session_val = str(s.get("value"))
                print(f"[info] session cookie 検出（{elapsed}秒後）: {session_val[:10]}... (len={len(session_val)})")
                time.sleep(2)  # 他 cookie が揃うのを少し待つ
                break
            if elapsed % 15 == 0:
                print(f"[poll] 待機中... {elapsed}s / {max_wait_sec}s (session cookie 未検出)")
            time.sleep(poll_interval)
            elapsed += poll_interval
        else:
            print(f"[timeout] {max_wait_sec}秒以内にログインが完了しませんでした", file=sys.stderr)
            return 3

        print("[info] cookies を取得中...")
        cookies = ctx.cookies()
        stake_cookies = [c for c in cookies if "stake.com" in (c.get("domain") or "")]
        print(f"[info] 全 cookie = {len(cookies)}, stake.com のみ = {len(stake_cookies)}")

        session_ck = next((c for c in stake_cookies if c.get("name") == "session"), None)
        if not session_ck:
            print("[ERROR] 'session' cookie が見つかりません。保存を中止します。", file=sys.stderr)
            return 4

        output_path.write_text(json.dumps(stake_cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] 書き出し完了: {output_path}")
        print(f"[done] 件数: {len(stake_cookies)}")
        if session_ck:
            sv = str(session_ck.get("value") or "")
            print(f"[done] session cookie: {sv[:10]}... (len={len(sv)})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
