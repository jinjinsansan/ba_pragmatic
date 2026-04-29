"""
Phase 4: user02 (shojihashimoto0922@gmail.com) billing 復元スクリプト

前提条件:
  1. git push origin main 済み
  2. bafather で build_per_user.ps1 -Start 2 -Count 1 実行済み
  3. user02 に新 EXE 配布・再インストール済み
  4. BET セッションを開始し、Supabase billing.session_state が
     daily_open / current_balance で更新されていることを確認済み

実行方法:
  cd /mnt/e/dev/Cusor/bacopy
  python3 restore_user02_billing.py
"""

import urllib.request
import json
from datetime import datetime, timezone
from pathlib import Path


def load_env(path: str) -> dict:
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env


def main():
    env = load_env('web/.env.local')
    url_base = env['NEXT_PUBLIC_SUPABASE_URL']
    svc_key = env['SUPABASE_SERVICE_ROLE_KEY']
    user_id = 'ad6fdb57-4459-4a1f-9d03-d266bccede37'  # shojihashimoto0922@gmail.com

    # --- 現在の状態確認 ---
    req = urllib.request.Request(
        f'{url_base}/rest/v1/billing?user_id=eq.{user_id}'
        '&select=user_id,balance,is_free,profit_share_rate,suspended,carry_loss',
        headers={'apikey': svc_key, 'Authorization': f'Bearer {svc_key}'},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        current = json.loads(r.read())

    if not current:
        print('ERROR: billing レコードが見つかりません')
        return

    b = current[0]
    print('=== 現在の billing 状態 ===')
    print(f'  is_free           = {b.get("is_free")}')
    print(f'  balance           = {b.get("balance")}')
    print(f'  profit_share_rate = {b.get("profit_share_rate")}')
    print(f'  suspended         = {b.get("suspended")}')
    print(f'  carry_loss        = {b.get("carry_loss")}')

    if not b.get('is_free'):
        print('\n⚠️  is_free がすでに false です。二重実行の可能性があります。中断します。')
        return

    # --- 復元実行 ---
    print('\n=== 復元実行 ===')
    now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    payload = json.dumps({
        'is_free': False,
        'balance': 2000,
        'profit_share_rate': 0.80,
        'suspended': False,
        'updated_at': now_iso,
    }).encode('utf-8')

    req2 = urllib.request.Request(
        f'{url_base}/rest/v1/billing?user_id=eq.{user_id}',
        data=payload,
        headers={
            'apikey': svc_key,
            'Authorization': f'Bearer {svc_key}',
            'Content-Type': 'application/json',
            'Prefer': 'return=representation',
        },
        method='PATCH',
    )
    with urllib.request.urlopen(req2, timeout=10) as r:
        result = json.loads(r.read())

    if result:
        r2 = result[0]
        print(f'  is_free           = {r2.get("is_free")}  ← false に変更')
        print(f'  balance           = {r2.get("balance")}  ← $2000 に復元')
        print(f'  profit_share_rate = {r2.get("profit_share_rate")}')
        print(f'  suspended         = {r2.get("suspended")}')
        print('\n✅ 復元完了。次回 cron/settle (JST 00:05) から課金が再開されます。')
    else:
        print('ERROR: PATCH レスポンスが空です')


if __name__ == '__main__':
    main()
