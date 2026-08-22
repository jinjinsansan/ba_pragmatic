"""LAPLACE 管理者 CLI

ユーザーPC への SSH 接続・状態確認・登録一覧を簡単コマンドで。

Usage:
  python scripts/laplace_admin.py list                      登録ユーザー一覧
  python scripts/laplace_admin.py status <email>            そのユーザーのトンネル状態
  python scripts/laplace_admin.py status --all              全ユーザー一括状態
  python scripts/laplace_admin.py port <email>              ポート番号表示
  python scripts/laplace_admin.py ssh <email> [user]        ユーザーPCに SSH 接続 (user=Administrator 既定)
  python scripts/laplace_admin.py exec <email> <cmd>        一発コマンド実行

内部動作:
  踏み台 VPS (210.131.215.116) を ProxyJump して、
  リバーストンネル経由でユーザーPC の sshd に接続する。

要件:
  - ~/.ssh/laplace_vps         (踏み台アクセス用)
  - ~/.ssh/laplace_admin       (ユーザーPC ログイン用)
  - scripts/user_ports.csv     (プロビジョニング台帳)
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_CSV = REPO_ROOT / "scripts" / "user_ports.csv"
# 環境変数で上書き可 (運用時VPS移転時等)
VPS_HOST = os.environ.get("LAPLACE_BASTION_HOST", "210.131.215.116")
BASTION_USER = os.environ.get("LAPLACE_BASTION_USER", "laplace")
BASTION_KEY = Path(os.environ.get("LAPLACE_BASTION_KEY", str(Path.home() / ".ssh" / "laplace_vps")))
ADMIN_KEY = Path(os.environ.get("LAPLACE_ADMIN_KEY", str(Path.home() / ".ssh" / "laplace_admin")))
DEFAULT_REMOTE_USER = os.environ.get("LAPLACE_REMOTE_USER", "Administrator")


def load_registry() -> list[dict]:
    if not REGISTRY_CSV.exists():
        raise SystemExit(f"[abort] registry not found: {REGISTRY_CSV}\n  Run provision_user_build.py first.")
    with REGISTRY_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_user(email: str) -> dict:
    for r in load_registry():
        if r["email"].lower() == email.lower():
            return r
    raise SystemExit(f"[abort] {email} not found. Try: laplace_admin list")


def cmd_list() -> None:
    rows = load_registry()
    if not rows:
        print("No users registered.")
        return
    print(f"{'EMAIL':<30} {'PORT':<7} {'SLUG':<30} {'REGISTERED':<25}")
    print("-" * 95)
    for r in rows:
        print(f"{r['email']:<30} {r['port']:<7} {r['slug']:<30} {r.get('registered_at',''):<25}")


def cmd_port(email: str) -> None:
    r = find_user(email)
    print(r["port"])


def _check_tunnel_port(port: int, timeout: int = 5) -> bool:
    """VPS でその port が listen しているかチェック (=トンネル接続中)"""
    cmd = [
        "ssh", "-i", str(BASTION_KEY),
        "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        f"{BASTION_USER}@{VPS_HOST}",
        f"ss -tnlp 2>/dev/null | awk '$4 ~ /:{port}$/'",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+3)
        return bool(res.stdout.strip())
    except Exception:
        return False


def cmd_status(email: str | None, all_users: bool) -> None:
    if all_users:
        rows = load_registry()
    else:
        if not email:
            raise SystemExit("email required (or --all)")
        rows = [find_user(email)]
    print(f"{'EMAIL':<30} {'PORT':<7} {'TUNNEL':<12}")
    print("-" * 52)
    for r in rows:
        active = _check_tunnel_port(int(r["port"]))
        status = "[ONLINE]" if active else "[offline]"
        print(f"{r['email']:<30} {r['port']:<7} {status}")


def _ssh_args(email: str, remote_user: str) -> list[str]:
    r = find_user(email)
    port = r["port"]
    return [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", f"ProxyCommand=ssh -W %h:%p -i {BASTION_KEY} -o StrictHostKeyChecking=no {BASTION_USER}@{VPS_HOST}",
        "-i", str(ADMIN_KEY),
        "-p", port,
        f"{remote_user}@127.0.0.1",
    ]


def cmd_ssh(email: str, remote_user: str) -> None:
    # トンネルが生きてるか先にチェック
    r = find_user(email)
    if not _check_tunnel_port(int(r["port"])):
        print(f"[warn] {email} is currently offline (port {r['port']} not listening)")
        print("  Verify user's GUI is running with Remote Support toggle ON.")
        sys.exit(2)
    args = _ssh_args(email, remote_user)
    print(f"[connecting] {email} via port {r['port']} as {remote_user}")
    os.execvp(args[0], args)


def cmd_exec(email: str, remote_user: str, command: str) -> None:
    r = find_user(email)
    if not _check_tunnel_port(int(r["port"])):
        raise SystemExit(f"[warn] {email} offline (port {r['port']})")
    args = _ssh_args(email, remote_user) + [command]
    res = subprocess.run(args)
    sys.exit(res.returncode)


def cmd_who_is_on() -> None:
    """VPS で listen 中のポートを一括取得して、登録済みユーザーと照合。
    誰が今接続中かを一覧表示 (ユーザーが名乗らなくても識別可能)。"""
    registry = load_registry()
    if not registry:
        print("No registered users.")
        return
    # VPS で ss -tnlp を1回叩いて listen 中ポートを取得
    cmd = [
        "ssh", "-i", str(BASTION_KEY),
        "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        f"{BASTION_USER}@{VPS_HOST}",
        "ss -tnlp 2>/dev/null | awk 'NR>1 {split($4,a,\":\"); print a[length(a)]}' | sort -u",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        listening_ports = {int(p) for p in res.stdout.split() if p.isdigit()}
    except Exception as e:
        raise SystemExit(f"[abort] Failed to query VPS: {e}")
    online = [r for r in registry if int(r["port"]) in listening_ports]
    offline = [r for r in registry if int(r["port"]) not in listening_ports]
    print(f"=== Connected Users ({len(online)}/{len(registry)}) ===")
    if online:
        print(f"{'EMAIL':<30} {'PORT':<7} {'SLUG':<30}")
        print("-" * 70)
        for r in online:
            print(f"{r['email']:<30} {r['port']:<7} {r['slug']:<30}")
    else:
        print("  (no users currently online)")
    if offline:
        print(f"\n=== Offline ({len(offline)}) ===")
        for r in offline:
            print(f"  {r['email']} (port {r['port']})")


def cmd_lookup(port: int) -> None:
    """ポート番号から email を逆引き。
    ログに 'port 20042 から接続' とあった時に誰か即判明。"""
    registry = load_registry()
    for r in registry:
        if int(r["port"]) == port:
            print(f"{r['email']}")
            print(f"  slug:       {r['slug']}")
            print(f"  registered: {r.get('registered_at', '')}")
            # トンネル状態も表示
            active = _check_tunnel_port(port)
            print(f"  tunnel:     {'[ONLINE]' if active else '[offline]'}")
            return
    raise SystemExit(f"[abort] no user registered for port {port}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="LAPLACE admin CLI - connect to user PCs via VPS bastion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List registered users")

    p_status = sub.add_parser("status", help="Check tunnel status")
    p_status.add_argument("email", nargs="?", help="User email")
    p_status.add_argument("--all", action="store_true", help="All users")

    p_port = sub.add_parser("port", help="Print port for user")
    p_port.add_argument("email")

    p_ssh = sub.add_parser("ssh", help="SSH into user's PC")
    p_ssh.add_argument("email")
    p_ssh.add_argument("user", nargs="?", default=DEFAULT_REMOTE_USER,
                       help=f"Remote OS user (default: {DEFAULT_REMOTE_USER})")

    p_exec = sub.add_parser("exec", help="Execute single command on user PC")
    p_exec.add_argument("email")
    p_exec.add_argument("command")
    p_exec.add_argument("--user", default=DEFAULT_REMOTE_USER)

    sub.add_parser("who", help="Who is currently connected (real-time)")

    p_lookup = sub.add_parser("lookup", help="Reverse lookup: port -> email")
    p_lookup.add_argument("port", type=int, help="Port number (e.g. 20042)")

    args = ap.parse_args()

    if args.cmd == "list":
        cmd_list()
    elif args.cmd == "status":
        cmd_status(args.email, args.all)
    elif args.cmd == "port":
        cmd_port(args.email)
    elif args.cmd == "ssh":
        cmd_ssh(args.email, args.user)
    elif args.cmd == "exec":
        cmd_exec(args.email, args.user, args.command)
    elif args.cmd == "who":
        cmd_who_is_on()
    elif args.cmd == "lookup":
        cmd_lookup(args.port)


if __name__ == "__main__":
    main()
