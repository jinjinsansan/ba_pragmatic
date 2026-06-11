# 計画: フリート監視ダッシュボード（bafather.uk admin/users 拡張）

> 目的: admin/users で各受け子の **稼働状態 / オート・手動 / 追従 / 資金法(SEQ・フラット) / NEXT BET額 / SEQ段数・負け越し / 当日W-L** を一覧表示。運用を「誰が今どう動いているか」一目で把握できるフリート監視盤にする。
> 状態: 計画段階（未実装）。2026-06-11 立案。

---

## 0. 前提（なぜ簡単か）
- 配管は既存: `受け子エンジン → POST /api/session-state → Supabase billing.session_state → admin表示`（60秒ごと）。
- エンジンは欲しい値を**全部すでに持っている**（`money.status_dict()` と bot カウンタ）。新規計算ゼロ。
- 既存表示（残高 / 当日PnL / liveランプ）と同じ仕組みに**1オブジェクト相乗りさせるだけ**。

## 1. 確定済みデータソース（実在確認済み）
`dual_line_money.py status_dict()` が返す:
`mode, unit, next_bet(別関数), seq_level, seq_turn, seq_overshoot, total_wins, total_losses, total_ties, win_rate, loss_cut, profit_stop, limit_reached, limit_reason`
bot (`dual_line_pragmatic_bot.py`):
`manual_assist, manual_assist_auto_click, _follow_enabled, _follow_active, _follow_chain, total_signals, wins, losses, total_resolved`

## 2. 要決定: 「負け越し個数」の定義（ユーザー確認）
3案あり、すべてエンジンが保持:
- **(A) seq_overshoot**（マルバツの負け超過＝SEQ増額を駆動する正味の負け数）★推奨＝「今どれだけ回収待ちの穴にいるか」を最も正確に表す
- (B) seq_level（SEQ配列の現在段数＝次にいくら賭けるかの位置）
- (C) セッション純負け数（losses − wins）＝当日の勝ち負け
→ **推奨: (A)を「負け越し」として主表示、(C)を「当日W-L」として併記、(B)はNEXT BETに内包**。

## 3. スキーマ（session_state に追加する bot_status）
```jsonc
"bot_status": {
  "running": true,
  "mode": "auto" | "manual",          // not manual_assist or manual_assist_auto_click → auto
  "follow": true,                      // _follow_enabled
  "follow_active": false,              // 現在追従チェーン中か
  "follow_chain": 0,                   // 現在の連勝追従数
  "money_mode": "small3" | "flat" ...,// money.mode
  "unit": 100,
  "next_bet": 12.0,                    // money.next_bet()
  "seq_level": 3,                      // SEQ段数
  "seq_overshoot": 5,                  // ★負け越し個数(推奨)
  "loss_cut": 0,                       // 0=無制限（赤表示の判断に使える）
  "wins": 219, "losses": 204,          // 当日
  "win_rate": 51.7,
  "updated_at": "<iso>"
}
```
※ 課金フィールド(daily_bet_pnl 等)とは**別オブジェクト**。精算cronに一切影響しない。

## 4. Phase 1 — エンジン（受け子）
- `dual_line_pragmatic_bot.py` `_sync_billing_session_state()` の state 構築部に `state["bot_status"] = {...}` を追加。
  - 値は `self.money.status_dict()` と self.* から組む。`next_bet` は `self.money.next_bet()`（side指定不要の現額）。
  - mode判定: `"auto" if (not self.manual_assist or self.manual_assist_auto_click) else "manual"`。
- **適用先: `_rev53144/dual_line_pragmatic_bot.py`（デプロイ元）＋ repo-root 両方**（教訓: エンジンは_rev53144からビルド [[project_dual_line_daily_total_aligned_2026-06-11]]）。
- 工数: 実コード ~30分。py_compile。

## 5. Phase 2 — Web（`ba` リポジトリ）
- **表示元は `ba` リポの `web/src/app/admin/users/`**（bacopy/web ではない。教訓 [[project_billing_mult_misparse_2026-06-11]]）。
- UI: TIME RATEパネルと同じ**行クリック展開式**。`session_state.bot_status` を読んでバッジ表示:
  - 稼働: 🟢稼働中 / ⚪停止（既存 isLive = last_balance_at<90s を流用）
  - モード: `AUTO` / `手動`（色分け）
  - 追従: `追従ON🔗`（active中は chain 数も）
  - 資金法: `SEQ small3` / `FLAT $11`
  - NEXT: `$12`
  - 負け越し: `▼5`（seq_overshoot、閾値超で赤）/ loss_cut=0 は⚠️
  - 当日: `219W/204L (51.7%)`
- 工数: ~1-2時間。Vercel自動デプロイ。

## 6. Phase 3 — 配布（本当のコスト）
- 新フィールドを出すには**全受け子のエンジン更新が必要**（再ビルド＋各自インストール/hot-swap）。
- ★**次回エンジン配布に相乗りさせる**のが最効率（単独配布はロスが大きい）。倍率ガード配布(engine 68699601)と同梱推奨。

## 7. 検証
1. エンジン更新後、`GET /api/session-state?email=...&api_key=...` に `bot_status` が乗るか。
2. admin で展開表示が出るか（user03=ageagetony で auto/follow/SEQ small3/next=$12/overshoot が見えること）。
3. 課金（daily_bet_pnl）と settle cron が無変化なこと。

## 8. 注意点（リスク）
- **60秒スナップショット**（リアルタイムではない）。SEQ段数/NEXTは緩変なので監視十分。
- **2リポ触る**: エンジン（全台配布）＋ `ba`（Vercel）。
- **billing本体に触れない**（別オブジェクト追加のみ）。
- 既存 session_state を壊さない（追加のみ・後方互換）。

## 9. シーケンス / 工数まとめ
| Phase | 作業 | 工数 |
|---|---|---|
| 1 | エンジン bot_status 追加(_rev53144+repo) | ~30分 |
| 2 | ba admin/users 展開表示 | ~1-2時間 |
| 3 | 全受け子へエンジン再配布 | 次回配布に相乗り |

## 10. ユーザー確認事項（着手前に1つだけ）
- **「負け越し個数」の定義** = (A)seq_overshoot 推奨でよいか？（A主表示＋当日W-L併記の構成）
- それさえ決まれば Phase1/2 を即書ける。配布は次のエンジン更新時にまとめる。
