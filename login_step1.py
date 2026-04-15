"""Stake.com ログイン + 2FA（Email Code）対応

1回の実行でログイン→2FA待機→コード入力→Cookie保存を行う。
Email CodeはTelegram Bot経由で取得する（デフォルト）。

使い方:
    python login_step1.py
    → ログインしてEmail Code画面に到達
    → TelegramBotが「メール認証コードを送ってください」と通知
    → ユーザーがTelegramで6桁コードを送信
    → スクリプトが自動でコードを入力
    → Cookie保存

    # 直接コードを渡す場合
    python login_step1.py --code 123456

    # ファイル経由（レガシー）
    python login_step1.py --file-mode
"""
import sys
import time
import json
import argparse
from pathlib import Path

from camoufox.sync_api import Camoufox

from config import (
    STAKE_USERNAME, STAKE_PASSWORD,
    STAKE_URL, BACCARAT_LOBBY_URL,
    SCREENSHOTS_DIR, AUTH_STATE_DIR,
)
from telegram_auth import ask_email_code

SCREENSHOTS_DIR.mkdir(exist_ok=True)
AUTH_STATE_DIR.mkdir(exist_ok=True)

AUTH_STATE_FILE = AUTH_STATE_DIR / "stake_cookies.json"
CODE_FILE = AUTH_STATE_DIR / "email_code.txt"


def wait_for_code_file(timeout: int = 300) -> str:
    """email_code.txtにコードが書き込まれるのを待つ"""
    # 古いファイルを削除
    if CODE_FILE.exists():
        CODE_FILE.unlink()

    print(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  📧 Email Codeが送信されました！")
    print(f"  メールを確認し、以下のコマンドでコードを入力:")
    print(f"")
    print(f"    echo 123456 > {CODE_FILE}")
    print(f"")
    print(f"  (123456を実際のコードに置き換えてください)")
    print(f"  {timeout}秒以内に入力してください")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if CODE_FILE.exists():
            code = CODE_FILE.read_text().strip()
            if code and len(code) >= 4:
                print(f"\n  コード検出: {code}")
                CODE_FILE.unlink()  # 使用後削除
                return code
        remaining = int(deadline - time.time())
        if remaining % 30 == 0 and remaining > 0:
            print(f"  待機中... 残り{remaining}秒")
        time.sleep(2)

    print("  ⏰ タイムアウト")
    return ""


def main(args):
    print("=== Stake.com ログイン (Camoufox) ===\n")

    # Windows Store版Python対応: realpathでCamoufox実行パスを解決
    launch_opts = {"headless": True}
    try:
        from camoufox.pkgman import get_path, LAUNCH_FILE, OS_NAME
        import os as _os
        path = get_path(LAUNCH_FILE[OS_NAME])
        real = _os.path.realpath(path)
        if real != path and _os.path.isfile(real):
            launch_opts["executable_path"] = real
    except Exception:
        pass

    with Camoufox(**launch_opts) as browser:
        page = browser.new_page()

        # 1. Stake.comにアクセス
        print("[1] Stake.comにアクセス中...")
        page.goto(STAKE_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        page.screenshot(path=str(SCREENSHOTS_DIR / "login_01_home.png"))
        print(f"    タイトル: {page.title()}")

        # 2. Loginボタンクリック
        print("[2] Loginボタンをクリック...")
        login_btn = page.locator(
            'a:has-text("Login"), '
            'button:has-text("Login"), '
            'a:has-text("Sign in"), '
            'button:has-text("Sign in")'
        )
        if login_btn.count() > 0:
            login_btn.first.click()
            time.sleep(3)
        page.screenshot(path=str(SCREENSHOTS_DIR / "login_02_form.png"))

        # 3. メール/ユーザー名入力
        print("[3] 認証情報を入力中...")
        email_input = page.locator('input[name="emailOrName"]')
        if email_input.count() > 0:
            email_input.first.fill(STAKE_USERNAME)
            print(f"    メール入力OK")
        else:
            # フォールバック
            visible_inputs = page.locator("input:visible")
            if visible_inputs.count() > 0:
                visible_inputs.first.fill(STAKE_USERNAME)
                print("    最初のinputに入力")

        time.sleep(0.5)

        # 4. パスワード入力
        pw_input = page.locator('input[type="password"]')
        if pw_input.count() > 0:
            pw_input.first.fill(STAKE_PASSWORD)
            print("    パスワード入力OK")

        time.sleep(0.5)
        page.screenshot(path=str(SCREENSHOTS_DIR / "login_03_filled.png"))

        # 5. Sign Inボタンクリック
        print("[4] Sign Inボタンをクリック...")
        submit = page.locator(
            'button:has-text("Sign In"), '
            'button:has-text("Sign in"), '
            'button[type="submit"]'
        )
        if submit.count() > 0:
            submit.first.click()
            print("    クリック完了")
        # Email Code画面遷移を待つ (最大15秒)
        for _w in range(15):
            time.sleep(1)
            try:
                if page.locator('input[placeholder*="Code" i], input[name*="code" i]').count() > 0:
                    print("    Email Code画面検出")
                    break
            except:
                pass
        page.screenshot(path=str(SCREENSHOTS_DIR / "login_04_after_submit.png"))

        # 6. Email Code入力
        print("\n[5] Email Code画面を確認中...")
        page.screenshot(path=str(SCREENSHOTS_DIR / "login_05_2fa.png"))

        code_input = page.locator(
            'input[placeholder*="Code" i], '
            'input[name*="code" i], '
            'input[placeholder*="code" i]'
        )

        # コードフィールドが見つからない場合、可視テキスト入力を探す
        if code_input.count() == 0:
            # Email Code画面かどうか確認
            body_text = page.locator("body").inner_text()[:500]
            if "email code" in body_text.lower() or "Email Code" in body_text:
                code_input = page.locator('input[type="text"]:visible')

        if code_input.count() > 0:
            # コード取得
            if args.code:
                email_code = args.code
                print(f"  コード(引数): {email_code}")
            elif args.file_mode:
                email_code = wait_for_code_file(timeout=300)
            else:
                # Telegram Bot経由でコードを取得（デフォルト）
                print("  📱 Telegram Botでコードを待機中...")
                email_code = ask_email_code(timeout=300)

            if email_code:
                code_input.first.fill(email_code)
                time.sleep(0.5)

                # Sign inボタンをクリック
                submit2 = page.locator(
                    'button:has-text("Sign in"), '
                    'button:has-text("Sign In"), '
                    'button[type="submit"]'
                )
                if submit2.count() > 0:
                    submit2.first.click()
                    print("    コード送信完了、ログイン待機中...")

                time.sleep(8)
                page.screenshot(path=str(SCREENSHOTS_DIR / "login_06_after_2fa.png"))
            else:
                print("    コードが取得できませんでした")
                cookies = page.context.cookies()
                with open(AUTH_STATE_FILE, "w") as f:
                    json.dump(cookies, f, indent=2)
                return
        else:
            print("    Email Codeフィールドが見つかりません")
            body_text = page.locator("body").inner_text()[:300]
            print(f"    本文: {body_text[:200]}")

        # 7. ログイン状態を確認
        print("\n[6] ログイン状態を確認中...")
        indicators = page.locator(
            '[data-test="balance"], '
            'button:has-text("Wallet"), '
            '[class*="balance"], '
            '[class*="user-menu"]'
        )
        logged_in = indicators.count() > 0
        page.screenshot(path=str(SCREENSHOTS_DIR / "login_07_result.png"))

        if logged_in:
            print("    ✅ ログイン成功！")
        else:
            print("    ⚠️ ログイン状態が不明")
            print(f"    タイトル: {page.title()}")

        # 8. Cookieを保存
        print("\n[7] 認証Cookie保存中...")
        cookies = page.context.cookies()
        with open(AUTH_STATE_FILE, "w") as f:
            json.dump(cookies, f, indent=2)
        print(f"    保存先: {AUTH_STATE_FILE}")
        print(f"    Cookie数: {len(cookies)}")

        # バカラロビーにアクセスしてみる
        if logged_in:
            print("\n[8] バカラロビーテスト...")
            page.goto(BACCARAT_LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(8)
            page.screenshot(path=str(SCREENSHOTS_DIR / "login_08_baccarat.png"))
            print(f"    タイトル: {page.title()}")
            for f in page.frames:
                print(f"    Frame: {f.url[:120]}")

    print("\n=== 完了 ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stake.com ログイン")
    parser.add_argument("--code", default="", help="Email 2FAコード")
    parser.add_argument("--file-mode", action="store_true", help="ファイル経由でコード取得（レガシー）")
    args = parser.parse_args()
    main(args)
