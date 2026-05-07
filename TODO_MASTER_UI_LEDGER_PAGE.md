# Master UI に家計簿ページをネイティブ実装する計画

**作成日**: 2026-05-07
**ステータス**: 着手延期 (運用中の Master UI に干渉しないため)
**着手タイミング**: 運用が一段落して Master UI 再起動可能なタイミング

---

## 背景

`bafather.uk/admin/ledger` の FX 運用家計簿ダッシュボードを、Master UI (`master.bafather.uk/master/ledger`) からも見られるようにしたい。

現状は Master UI ヘッダの「📊 家計簿」ボタンが新タブで `bafather.uk/admin/ledger` を開くのみ → 二重ログインが必要で UX が悪い。

## 採用方針: B (Master UI ネイティブ実装)

- bafather に新 API エンドポイントを追加 (LAPLACE_API_KEY 認証)
- Master UI が API から JSON 取得 → Python で HTML 描画
- Master UI のダーク水色テーマで統一表示

→ **マスターUIパスワードのみで完結、二重ログイン不要**

## 実装手順

### ① bafather Web 側 (E:/dev/Cusor/ba/web/)

**新ファイル**: `web/src/app/api/master/ledger/route.ts`

```ts
import { createAdminClient } from '@/lib/supabase-admin'
import { NextRequest, NextResponse } from 'next/server'

export async function GET(req: NextRequest) {
  // LAPLACE_API_KEY (Bearer) 認証
  const auth = req.headers.get('authorization') || ''
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : ''
  const expected = process.env.LAPLACE_API_KEY
  if (!expected || token !== expected) {
    return NextResponse.json({ error: 'unauthorized' }, { status: 401 })
  }

  const admin = createAdminClient()
  const { data: summaries } = await admin
    .from('ledger_investor_summary')
    .select('*')
    .order('investor_name')
  const { data: rules } = await admin
    .from('ledger_distribution_rules')
    .select('*')
    .is('effective_to', null)

  return NextResponse.json({ summaries: summaries || [], rules: rules || [] })
}
```

**ENV**: bafather の Vercel 環境変数 `LAPLACE_API_KEY` は既に設定済 (`bacopy_api.py:78` で使用中)

### ② Master UI Python 側 (E:/dev/Cusor/bacopy/)

**新ファイル**: `bacopy_master_ledger_ui.py`

```python
"""Master UI 家計簿ページ HTML レンダラ"""
from __future__ import annotations
import os
import json
import urllib.request

def fetch_ledger_data() -> dict:
    base = (os.getenv("BACOPY_BAFATHER_URL", "") or "https://www.bafather.uk").rstrip("/")
    api_key = (os.getenv("LAPLACE_API_KEY", "") or "").strip()
    req = urllib.request.Request(
        f"{base}/api/master/ledger",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e), "summaries": [], "rules": []}

def render_master_ledger_page(csrf: str) -> str:
    data = fetch_ledger_data()
    # data["summaries"] と data["rules"] を使って HTML 生成
    # bacopy_master_ui.py と同じ CSS variables で BACOPYMASTER テーマ
    # bafather/admin/ledger と同じレイアウト 4 セクション (左: 投資家情報/分配ルール/口座残高、右: 投資家サマリ/運用者損益/利益所在/未出金/経費受領)
    return _render_html(data, csrf)
```

**bacopy_api.py 修正**: 新ルート追加

```python
# bacopy_api.py do_GET 内の /master 処理直後に追加
if u.path == "/master/ledger":
    s = _get_session(self.headers)
    if not s:
        return _redirect(self, "/master/login")
    from bacopy_master_ledger_ui import render_master_ledger_page
    return _send_html(self, 200, render_master_ledger_page(str(s.get("csrf") or "")))
```

### ③ Master UI ヘッダボタンの差し替え

**ファイル**: `bacopy_master_ui.py:316-318` 付近

現状 (新タブで bafather に飛ばす):
```html
<a href="https://bafather.uk/admin/ledger" target="_blank" rel="noopener noreferrer"
   class="ledger-btn"
   title="FX 運用家計簿 (新しいタブで開く)">📊 家計簿</a>
```

変更後 (内部ページに遷移):
```html
<a href="/master/ledger" class="ledger-btn"
   title="FX 運用家計簿">📊 家計簿</a>
```

### ④ デプロイ手順

1. **bafather web 側のみ先にデプロイ** (新 API エンドポイントだけ追加、既存に影響なし):
   - ba repo に commit & push → Vercel 自動デプロイ
2. **Master UI 再起動**:
   - `bacopy_master_ledger_ui.py` を新規追加
   - `bacopy_api.py` にルート追加
   - `bacopy_master_ui.py` のボタン差し替え
   - bacopy_api を再起動 (運用ダウンタイム数秒)
3. **動作確認**:
   - `master.bafather.uk/master/ledger` にアクセス
   - 数値が `bafather.uk/admin/ledger` と完全一致することを確認

## 注意事項

- ④-2 の Master UI 再起動が必須 (Python module が永続キャッシュされるため)
- 再起動中は executor との通信が一瞬切れるので、運用していない時間帯に行うこと
- bafather web 側のデプロイは無停止 OK

## 参考ファイル

- 仕様: `C:/Users/USER/.claude/projects/E--dev-Cusor-bacopy/memory/project_fx_ledger_spec.md`
- bafather 側ダッシュボード実装: `E:/dev/Cusor/ba/web/src/app/admin/ledger/page.tsx`
- 計算ロジック: `E:/dev/Cusor/ba/web/src/lib/ledger/calc.ts`
- View 定義: `E:/dev/Cusor/ba/web/supabase/ledger_schema.sql`

## 関連: 後続タスク

- [ ] H さん 80% chargeBalance 補充入金機能 (Hさん投資 $96,900 を下回る前)
- [ ] 複数投資家対応 UI (現在は H さん 1 名のみ)
- [ ] 投資家別ロール (投資家ログイン時は自分の数値のみ表示)
