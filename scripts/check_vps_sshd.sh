#!/bin/bash
# VPS側 sshd 設定とリソース上限を確認するスクリプト
#
# Usage:
#   bash scripts/check_vps_sshd.sh
#
# 確認項目:
#   1. MaxSessions / MaxStartups の設定値
#   2. ulimit -n (ファイルディスクリプタ上限)
#   3. sshd プロセス数とメモリ使用量
#   4. laplace_support ユーザーの authorized_keys 行数

set -euo pipefail

VPS_HOST="${LAPLACE_BASTION_HOST:-210.131.215.116}"
VPS_USER="${LAPLACE_BASTION_USER:-laplace}"
VPS_KEY="${LAPLACE_BASTION_KEY:-$HOME/.ssh/laplace_vps}"
SUPPORT_USER="${LAPLACE_SUPPORT_USER:-laplace_support}"

echo "=== VPS SSH Configuration Check ==="
echo "VPS: $VPS_USER@$VPS_HOST"
echo ""

# 1. sshd_config の MaxSessions / MaxStartups
echo "[1] sshd_config settings:"
ssh -i "$VPS_KEY" -o StrictHostKeyChecking=no "$VPS_USER@$VPS_HOST" \
  "sudo grep -E '^(MaxSessions|MaxStartups)' /etc/ssh/sshd_config || echo 'Not configured (using defaults)'"
echo ""

# 2. ulimit -n (ファイルディスクリプタ上限)
echo "[2] File descriptor limit (ulimit -n):"
ssh -i "$VPS_KEY" -o StrictHostKeyChecking=no "$VPS_USER@$VPS_HOST" "ulimit -n"
echo "   (Recommended: >= 65536 for 1000+ concurrent connections)"
echo ""

# 3. sshd プロセス数とメモリ
echo "[3] Current sshd processes and memory usage:"
ssh -i "$VPS_KEY" -o StrictHostKeyChecking=no "$VPS_USER@$VPS_HOST" \
  "ps aux | grep 'sshd:' | grep -v grep | wc -l | xargs -I{} echo '   Active sshd sessions: {}'"
ssh -i "$VPS_KEY" -o StrictHostKeyChecking=no "$VPS_USER@$VPS_HOST" \
  "ps aux | grep 'sshd:' | grep -v grep | awk '{sum+=\$6} END {printf \"   Total memory: %.2f MB\\n\", sum/1024}'"
echo ""

# 4. laplace_support authorized_keys 登録数
echo "[4] laplace_support authorized_keys entries:"
ssh -i "$VPS_KEY" -o StrictHostKeyChecking=no "$VPS_USER@$VPS_HOST" \
  "sudo wc -l /home/$SUPPORT_USER/.ssh/authorized_keys 2>/dev/null | awk '{print \"   Total lines: \" \$1}' || echo '   File not found'"
ssh -i "$VPS_KEY" -o StrictHostKeyChecking=no "$VPS_USER@$VPS_HOST" \
  "sudo grep -c '^restrict,port-forwarding' /home/$SUPPORT_USER/.ssh/authorized_keys 2>/dev/null | awk '{print \"   Registered users: \" \$1}' || echo '   No entries'"
echo ""

# 5. システムリソース
echo "[5] System resources:"
ssh -i "$VPS_KEY" -o StrictHostKeyChecking=no "$VPS_USER@$VPS_HOST" "free -h | grep Mem"
ssh -i "$VPS_KEY" -o StrictHostKeyChecking=no "$VPS_USER@$VPS_HOST" "df -h / | tail -1"
echo ""

echo "=== Check Complete ==="
echo "For detailed logs: ssh $VPS_USER@$VPS_HOST 'sudo journalctl -u sshd -n 50'"
