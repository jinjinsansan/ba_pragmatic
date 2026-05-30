# 計画書: デュアルライン v4（10パターン）別モード実装

最終更新: 2026-05-30 / ブランチ: feat/dual-line

## 0. 目的とゴール

**分母（BET回数）を増やす**ため、現行6パターン（v3）とは**別モード**として、勝率≥51%かつ$/BET>0かつサンプル妥当な**10パターン（v4）**を新設する。

- 現行 v3（6パターン）モードは **実績ありの本番候補**。**一切汚さず温存**する。
- v4 は **forward live で~52%を保つか検証してから採用**（backtest単独は信用しない方針＝ユーザの哲学）。
- 6 vs 10 を**同一期間・同一方式でライブ比較**できる状態を作る。

## 1. v4 = 10パターン（確定）

選定基準: **勝率≥51% AND $/BET>0 AND サンプル≥200**（バックテストレポート全データ・T込み方式）。

| # | パターン | 勝率 | $/BET | v3との関係 |
|---|---|---|---|---|
| 1 | sansan\|telecho\|P | 58.5% | +0.1546 | v3継続 |
| 2 | niconico\|niconico\|B | 54.0% | +0.0453 | v3継続 |
| 3 | telecho\|niconico\|P | 52.8% | +0.0508 | v3継続 |
| 4 | niconico\|nikoichi\|B | 52.7% | +0.0242 | 🆕 |
| 5 | telecho\|niconico\|B | 52.1% | +0.0148 | 🆕 |
| 6 | niconico\|dragon\|B | 51.8% | +0.0083 | 🆕(高頻度) |
| 7 | niconico\|nikoichi\|P | 51.7% | +0.0324 | 🆕 |
| 8 | telecho\|nikoichi\|B | 51.7% | +0.0072 | 🆕(高頻度) |
| 9 | telecho\|telecho\|B | 51.6% | +0.0062 | v3継続 |
| 10 | telecho\|nikoichi\|P | 51.2% | +0.0213 | v3継続 |

**重要**: v4 は v3 の単純な上位集合ではない。v3 の `niconico|dragon|P`(50.5%, 基準未満)は **v4 から外れる**。除外: `niconico|telecho|B`/`bline|telecho|B`($/BET負)、`sansan|niconico|*`(サンプル極小)。

## 2. 不変条件（全フェーズ共通の鉄則）

1. **v3（6パターン）の dry-run / GUI モードは触らない**（実績ベースライン保護）。
2. **モードごとに統計stateを完全分離**（混ぜると会計破綻）。`logic_version` で区別。
3. **money経路（実BET）の変更は dry-run 検証後・env ガード付き・段階導入**。
4. **T処理は v3 と同一方式を踏襲**（ライブと不一致を新たに作らない。T処理の正規化は別タスク）。
5. 各変更は backup + `py_compile` + 自動復帰ループ前提で安全に。

## 3. フェーズ計画

### Phase 1: VPS v4 dry-run 並走（最優先・GUI変更なし・BETなし）
- `dual_line_match.py` に `LIVE_SIGNAL_PATTERNS_V4`（10パターン）を追加。
- VPS dry-run bot に **v4 並走トラッキング**を追加（既存6パターンの観測・予想・解決フローに相乗り、**別の v4 pending/stats 構造**で独立集計）。
- v4 シグナルは **0から計測**、**別state file**（`dual_line_v4_state.json`）に保存。
- Telegram通知に **`[v4]` タグ**を付けて送信（通知増は許容）。
- 既存6パターンの挙動・state・通知は**完全に不変**。
- **成果物**: 6（既存#2316継続）と 10（v4・0から）が同一観測でライブ並走。

### Phase 2: VPS が v4 decision を master API に mode タグ付き配信
- `_publish_live_decision` を mode（`v3`/`v4`）対応に。decision に `mode`/`logic_version` を付与。
- v4 シグナルも GUI が受け取れるよう publish（GUI側で mode フィルタ）。
- v3 の配信は不変。

### Phase 3: GUI モード選択プルダウン
- bafather GUI のデュアルラインアシストに **モード選択 `v3 (6) / v4 (10)`** プルダウン追加。
- 選択モードの decision のみ拾って BET（mode フィルタ）。
- 設定の永続化（config）。v3 が既定。

### Phase 4: SEQ 開始額 UI（$1 / $3 / $6 開始）
- 資金管理はパターンモードと**独立**（SPEC準拠）。
- GUI に SEQ 開始額（unit）の選択肢が無ければ追加（$1/$3/$6 等）。モードと直交して適用。

### Phase 5: forward 検証 → 採用判定
- v4 dry-run が一定シグナル数（目安 1,000〜2,000）蓄積後、**ライブ勝率を v3 と比較**。
- 採用基準（案）: v4 が forward で**勝率≥51.5% かつ $/BET>0 を維持**、かつ v3 と同等以上。
- 満たせば v4 を実BET運用へ昇格。満たさなければ候補を削る/撤回（v3は無傷）。

## 4. 進捗

- [x] Phase 1: VPS v4 dry-run 並走（commit 51c6c78、稼働中・0から蓄積・V2形式累積Telegram）
- [x] Phase 3a: GUI bot mode フィルタ（commit 5ae2ea5）— `_handle_decision` で選択モードのみBET、既定v3、`set_dual_mode` IPC。**2026-05-30 bafather デプロイ済み**（ローカルPyInstallerビルド→EXEのみ差替, SWAP_OK, app.asar不変, バックアップ有）。
- [x] Phase 3b: Electron UI（モードプルダウン v3/v4）（commit 7d0c2c9）— `index.html` #inputDualMode (v3/v4)、`app.js` 4箇所で dual_mode、`main.js` で dual-line spawn 時 `childEnv.BACOPY_DUAL_MODE=v3|v4`。既定v3で挙動不変。**2026-05-30 17:29 bafather デプロイ済み**（app.asar=204214 差替, ASAR_SWAP_OK, バックアップ `app.asar.bak_20260530_172916`, EXE無干渉）→ **GUI再起動→プルダウン目視確認済**。
- [x] Phase 2: VPS v4 publish（mode="v4" タグ、env gated）— 冪等パッチ `_v4_publish_patch.py`。`_v4_track` 内 signal 確定直後に `_publish_v4_decision` を呼ぶ独立経路。`BACOPY_V4_PUBLISH=1` のとき `/api/decisions` へ `source=dual_line_vps_v4_10pattern`/`mode=v4`/`did=dl_v4_*` で配信。v3(`_publish_live_decision`)非干渉。**2026-05-30 17:55 VPS有効化済**（パッチ適用 py_compile OK→.env に `BACOPY_V4_PUBLISH=1`→wrapper再起動, bot PID 3251587, v3 state signals=2392/+$4830 + v4 shadow 46 保全）。
  - 残課題3点=✅全コード検証済: ① amount は受信側ローカルSEQ(L3295)でサイズ=nominal無視で安全 ② master `wait` は `ORDER BY received_at DESC` の pending リスト返し→新規v3常時配信・v4は実行されず会計汚染ゼロ ③ mode フィルタ(L2860-2863)+poll dedup で v3中v4 drop確認。
- [ ] Phase 5: forward 検証→採用判定（v4 現 46 シグナル → 1000-2000 蓄積後に v3 と勝率比較）

## ⚠️ 安全なデプロイ順序（厳守・暴発防止）
v4の5パターンは v3 と共有。順序を誤ると現行エンジンが mode 無視で共有パターンを実BETする。
```
① bafather に Phase 3a エンジン(mode フィルタ,既定v3)をデプロイ   ← GUI停止+build_dual.ps1
② VPS の v4 publish(Phase 2) を有効化（BACOPY_V4_PUBLISH=1）
③ Electron UI(Phase 3b) をビルド・デプロイ・RDP検証
④ v4 が Phase 5 検証通過 → GUIで v4 選択して実BET開始
```
それまで v4 は dry-run(Phase 1)でライブ蓄積継続。

## 5. 関連
- 6パターン根拠: `dual_line_all_patterns_report.html`
- forward実績(v3): [[project_dual_line_forward_oos_2026-05-22]]
- 安全修正の経緯: [[project_dual_line_chrome_attach_bet_breakthrough_2026-05-29]]
- バックテストはライブを完全再現しない（collector保存シュー≠ライブ観測シュー）ため、**最終判定は forward live**で行う。
