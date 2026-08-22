"""LAPLACE ユーザー別 EXE ビルド用のサポートトンネル情報をプロビジョニング。

各エンドユーザー向けに:
  1. ed25519 鍵ペアを生成
  2. VPS 踏み台 (laplace_support@210.131.215.116) の authorized_keys に公開鍵を登録
  3. ユーザー固有の固定ポート番号を割り当て
  4. EXE にバンドルする .env と秘密鍵を gui/user_build/<slug>/ に配置

配布時のフロー:
  python scripts/provision_user_build.py --email alice@example.com
  → gui/user_build/alice_at_example_com/ が生成される
  → 後続の電子ビルドで extraResources に指定してパッケージング

Usage:
  # 新規プロビジョニング (自動で次の空きポート割当)
  python scripts/provision_user_build.py --email alice@example.com

  # ポート指定
  python scripts/provision_user_build.py --email alice@example.com --port 20042

  # 既存ユーザーを再発行 (鍵ローテーション) — 既存鍵は上書き
  python scripts/provision_user_build.py --email alice@example.com --rotate

  # 登録一覧を確認 (VPS の authorized_keys を表示)
  python scripts/provision_user_build.py --list

前提条件:
  - ローカルに ssh-keygen があること
  - VPS (laplace@210.131.215.116) に SSH で sudo NOPASSWD 可能なこと
  - 秘密鍵 ~/.ssh/laplace_vps があること
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import padding
except ImportError:
    print("[error] cryptography library required. Install: pip install cryptography", file=sys.stderr)
    sys.exit(1)

# --- 設定 ---
REPO_ROOT = Path(__file__).resolve().parent.parent
USER_BUILD_DIR = REPO_ROOT / "gui" / "user_build"
REGISTRY_CSV = REPO_ROOT / "scripts" / "user_ports.csv"  # 管理者ローカルの台帳
# 環境変数で上書き可
_BASTION_HOST = os.environ.get("LAPLACE_BASTION_HOST", "210.131.215.116")
_BASTION_USER = os.environ.get("LAPLACE_BASTION_USER", "laplace")
VPS_SSH = f"{_BASTION_USER}@{_BASTION_HOST}"
VPS_KEY = Path(os.environ.get("LAPLACE_BASTION_KEY", str(Path.home() / ".ssh" / "laplace_vps")))
SUPPORT_USER_ON_VPS = os.environ.get("LAPLACE_SUPPORT_USER", "laplace_support")
SUPPORT_HOST = f"{SUPPORT_USER_ON_VPS}@{_BASTION_HOST}"
PORT_RANGE_START = 20001
PORT_RANGE_END = 29999

# authorized_keys 行のテンプレート (restrict で最小権限、permitopen で当該ポートのみ許可)
AUTHORIZED_KEY_TEMPLATE = (
    'restrict,port-forwarding,permitlisten="127.0.0.1:{port}",'
    'command="/bin/echo tunnel-only" {pubkey}'
)

# AES暗号化用の固定ソルト (プロジェクト固有、環境変数で上書き可)
ENCRYPTION_SALT = os.environ.get("LAPLACE_KEY_SALT", "laplace-support-v1-2026").encode('utf-8')

# bafather.uk ライセンス API
BAFATHER_URL = os.environ.get("LAPLACE_SITE_URL", "https://www.bafather.uk").rstrip("/")
LAPLACE_API_KEY = os.environ.get("LAPLACE_API_KEY", "")


def verify_license(email: str, strict: bool = True) -> bool:
    """bafather.uk にライセンス登録があるか確認。
    strict=True: 登録なしなら SystemExit で中断。
    API 到達不能時は警告のみで継続 (管理者の運用を止めない)。
    """
    if not LAPLACE_API_KEY:
        print(f"[warn] LAPLACE_API_KEY 未設定 — ライセンス検証をスキップ")
        return True
    url = f"{BAFATHER_URL}/api/auth/license"
    body = json.dumps({"email": email, "api_key": LAPLACE_API_KEY}).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "LAPLACE-provision/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read().decode("utf-8"))
    except Exception as e:
        print(f"[warn] ライセンスAPI 到達不能 ({e}) — 検証スキップして継続")
        return True
    if data.get("ok"):
        print(f"[ok] License verified for {email}")
        return True
    reason = data.get("reason", "unknown")
    if strict:
        raise SystemExit(f"[abort] {email} is not a registered license: {reason}\n"
                         f"  bafather.uk で事前にライセンス登録してください。")
    print(f"[warn] License not found for {email}: {reason} — 継続")
    return False


def slugify_email(email: str) -> str:
    """email を安全なディレクトリ名に変換 (alice@example.com → alice_at_example_com)"""
    s = email.lower().strip()
    s = s.replace("@", "_at_")
    s = re.sub(r"[^a-z0-9_-]", "_", s)
    return s


def _derive_key(email: str) -> bytes:
    """ユーザーメールから AES-256 鍵を導出 (PBKDF2 100,000 iterations)"""
    return hashlib.pbkdf2_hmac('sha256', email.lower().encode('utf-8'), ENCRYPTION_SALT, 100000)


def encrypt_private_key(plaintext: bytes, email: str) -> str:
    """秘密鍵を AES-256-CBC で暗号化し、Base64エンコードして返す。
    
    フォーマット: base64(iv + ciphertext)
    """
    key = _derive_key(email)
    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    
    # PKCS7 パディング
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode('ascii')


def decrypt_private_key(encrypted_b64: str, email: str) -> bytes:
    """暗号化された秘密鍵を復号 (GUI側で使用)"""
    key = _derive_key(email)
    data = base64.b64decode(encrypted_b64)
    iv = data[:16]
    ciphertext = data[16:]
    
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    
    # パディング除去
    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext


def run_local(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def run_vps(remote_cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """VPS (laplace@) で任意のコマンドを実行"""
    cmd = [
        "ssh",
        "-i", str(VPS_KEY),
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        VPS_SSH,
        remote_cmd,
    ]
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def load_registry() -> list[dict]:
    if not REGISTRY_CSV.exists():
        return []
    with REGISTRY_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_registry(rows: list[dict]) -> None:
    """CSV を原子的に書き換える (tmp → rename)。同時実行の競合を最小化。"""
    REGISTRY_CSV.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["email", "port", "slug", "registered_at"])
        w.writeheader()
        w.writerows(rows)
    os.replace(str(tmp), str(REGISTRY_CSV))


def next_free_port(registry: list[dict]) -> int:
    used = {int(r["port"]) for r in registry if r.get("port")}
    for p in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if p not in used:
            return p
    raise RuntimeError("No free port in range")


def generate_keypair(user_dir: Path, comment: str, email: str) -> tuple[Path, Path]:
    """鍵ペア生成後、秘密鍵を AES-256 で暗号化して保存。
    
    Returns: (暗号化済み秘密鍵パス, 公開鍵パス)
    """
    priv_tmp = user_dir / "support_key.tmp"  # 一時的な平文鍵
    priv_enc = user_dir / "support_key"      # 暗号化済み
    pub = user_dir / "support_key.pub"
    
    if priv_enc.exists() or pub.exists():
        raise FileExistsError(f"Key already exists: {priv_enc}. Use --rotate to regenerate.")
    
    user_dir.mkdir(parents=True, exist_ok=True)
    
    # 一時ファイルに鍵生成
    run_local(["ssh-keygen", "-t", "ed25519", "-f", str(priv_tmp), "-N", "", "-C", comment])
    
    # 秘密鍵を読み込んで暗号化
    plaintext = priv_tmp.read_bytes()
    encrypted_b64 = encrypt_private_key(plaintext, email)
    
    # 暗号化済みを保存
    priv_enc.write_text(encrypted_b64, encoding='utf-8')
    
    # 一時平文ファイルを削除
    priv_tmp.unlink()
    
    # Windows では chmod が効かないが試みる
    try:
        os.chmod(priv_enc, 0o600)
    except Exception:
        pass
    
    return priv_enc, pub


def register_pubkey_on_vps(pubkey_content: str, port: int, email: str) -> None:
    """laplace_support の authorized_keys に行追加 (重複チェック付き、flock で排他制御)"""
    line = AUTHORIZED_KEY_TEMPLATE.format(port=port, pubkey=pubkey_content.strip())
    escaped_line = line.replace("'", "'\\''")  # シェル安全化
    
    # flock で排他ロックを取得して authorized_keys を原子的に更新
    # LOCKFILE: /var/lock/laplace_authkeys.lock
    update_cmd = f"""
flock /var/lock/laplace_authkeys.lock -c '
    AUTHKEYS="/home/{SUPPORT_USER_ON_VPS}/.ssh/authorized_keys"
    sudo sed -i "/# user:{email}$/,/{re.escape(email)}$/d" "$AUTHKEYS" 2>/dev/null || true
    echo "# user:{email}" | sudo tee -a "$AUTHKEYS" > /dev/null
    echo '\''{escaped_line}'\'' | sudo tee -a "$AUTHKEYS" > /dev/null
    sudo chown {SUPPORT_USER_ON_VPS}:{SUPPORT_USER_ON_VPS} "$AUTHKEYS"
    sudo chmod 600 "$AUTHKEYS"
'
"""
    run_vps(update_cmd.strip())


def remove_user_from_vps(email: str) -> None:
    sed_cmd = (
        f"sudo sed -i '/# user:{email}$/,+1d' "
        f"/home/{SUPPORT_USER_ON_VPS}/.ssh/authorized_keys"
    )
    run_vps(sed_cmd, check=False)


def write_user_env(user_dir: Path, email: str, port: int) -> Path:
    """EXE にバンドルする .env (既存 .env と合流するようサポート項目のみ)"""
    env_path = user_dir / "support.env"
    lines = [
        f"# LAPLACE Support Tunnel (per-user, auto-generated)",
        f"# user: {email}",
        f"# generated: {datetime.now(timezone.utc).isoformat()}",
        f"LAPLACE_SUPPORT_ENABLED=1",
        f"LAPLACE_SUPPORT_SSH_HOST={SUPPORT_HOST}",
        f"LAPLACE_SUPPORT_SSH_KEY=./support_key",
        f"LAPLACE_SUPPORT_SSH_KEY_ENCRYPTED=1",
        f"LAPLACE_SUPPORT_USER_EMAIL={email}",
        f"LAPLACE_SUPPORT_REMOTE_PORT={port}",
        f"LAPLACE_SUPPORT_LOCAL_PORT=22",
        "",
    ]
    env_path.write_text("\n".join(lines), encoding="utf-8")
    return env_path


def cmd_provision(email: str, port: int | None, rotate: bool, skip_license: bool = False) -> None:
    # ライセンス検証 (skip_license フラグで回避可能、管理者テスト用)
    if not skip_license:
        verify_license(email, strict=True)
    registry = load_registry()
    slug = slugify_email(email)
    existing = next((r for r in registry if r["email"] == email), None)

    if existing:
        if rotate:
            # 既存鍵を削除して再生成
            user_dir = USER_BUILD_DIR / existing["slug"]
            for f in ("support_key", "support_key.pub"):
                p = user_dir / f
                if p.exists():
                    p.unlink()
            print(f"[rotate] Removed old keys for {email}")
            port_to_use = int(existing["port"])
        else:
            raise SystemExit(
                f"[abort] {email} is already provisioned (port={existing['port']}, slug={existing['slug']}).\n"
                f"Use --rotate to regenerate keys (port stays)."
            )
    else:
        port_to_use = port if port else next_free_port(registry)
        if any(int(r["port"]) == port_to_use for r in registry):
            raise SystemExit(f"[abort] Port {port_to_use} already in use")

    user_dir = USER_BUILD_DIR / slug
    priv, pub = generate_keypair(user_dir, comment=f"laplace_support:{email}", email=email)
    pubkey = pub.read_text(encoding="utf-8").strip()
    print(f"[ok] Keypair generated (encrypted): {priv}")

    register_pubkey_on_vps(pubkey, port_to_use, email)
    print(f"[ok] Registered on VPS laplace_support authorized_keys (port={port_to_use})")

    env_path = write_user_env(user_dir, email, port_to_use)
    print(f"[ok] Env fragment written: {env_path}")

    # 台帳更新
    if existing:
        existing["registered_at"] = datetime.now(timezone.utc).isoformat()
    else:
        registry.append({
            "email": email,
            "port": str(port_to_use),
            "slug": slug,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        })
    save_registry(registry)
    print(f"[ok] Registry updated: {REGISTRY_CSV}")

    print("\n=== Provisioning complete ===")
    print(f"Email: {email}")
    print(f"Slug:  {slug}")
    print(f"Port:  {port_to_use}")
    print(f"Dir:   {user_dir}")
    print("\nNext: include the following in your EXE build extraResources:")
    print(f'  {{ "from": "gui/user_build/{slug}/support_key", "to": "support_key" }}')
    print(f'  {{ "from": "gui/user_build/{slug}/support.env", "to": "support.env" }}')


def cmd_list() -> None:
    registry = load_registry()
    print(f"Registered users ({len(registry)}):")
    for r in registry:
        print(f"  {r['email']:30s} port={r['port']} slug={r['slug']} at={r['registered_at']}")
    print(f"\nVPS authorized_keys:")
    res = run_vps(f"sudo cat /home/{SUPPORT_USER_ON_VPS}/.ssh/authorized_keys 2>&1 | head -n 40")
    print(res.stdout)


def cmd_revoke(email: str) -> None:
    registry = load_registry()
    existing = next((r for r in registry if r["email"] == email), None)
    if not existing:
        raise SystemExit(f"[abort] {email} not found in registry")
    remove_user_from_vps(email)
    registry = [r for r in registry if r["email"] != email]
    save_registry(registry)
    user_dir = USER_BUILD_DIR / existing["slug"]
    print(f"[ok] Revoked {email} (port={existing['port']}).")
    print(f"     User dir still at {user_dir} (manually delete if needed)")


def main() -> None:
    ap = argparse.ArgumentParser(description="LAPLACE support tunnel user provisioning")
    ap.add_argument("--email", help="User email")
    ap.add_argument("--port", type=int, help="Specific port (default: auto-assign)")
    ap.add_argument("--rotate", action="store_true", help="Regenerate keys for existing user")
    ap.add_argument("--list", action="store_true", help="List registered users")
    ap.add_argument("--revoke", action="store_true", help="Revoke user access (requires --email)")
    ap.add_argument("--skip-license", action="store_true",
                    help="bafather.uk ライセンス検証をスキップ (テスト用)")
    args = ap.parse_args()

    if args.list:
        cmd_list()
        return
    if args.revoke:
        if not args.email:
            ap.error("--revoke requires --email")
        cmd_revoke(args.email)
        return
    if not args.email:
        ap.error("--email required (or use --list)")
    cmd_provision(args.email, args.port, args.rotate, skip_license=args.skip_license)


if __name__ == "__main__":
    main()
