# BUGFIX LOG — AUTO BET 実装・障害対応（2026-05-10）

## 概要
- 対象: `bacopy_master_ui.py`（AUTO BET 制御）
- 目的: `$1 AUTO BET` の連続稼働安定化、誤停止防止、UI改善
- 反映先: VPS `bacopy-api`（`/opt/bacopy/bacopy_master_ui.py`）

## 発生した主な事象
1. AUTO開始後、数回でBETが止まる  
2. `$200 到達` トーストが誤発火（実際は$1運用）  
3. AUTOボタン押下時に反応しないタイミングがある  
4. hashimoto 側フリーズ復帰後にAUTOトリガーが止まりやすい  

## 根本原因
1. **gameIdトリガー依存**
   - `snap.last_hand.gameId` の更新タイミングがBET窓とズレ、送信漏れ/多重化が発生。
2. **APIレスポンスのネスト構造差分**
   - `total_bets/wins/losses` は `e.gui.*` に存在。`e.total_bets` 参照は無効。
3. **refresh順序不整合**
   - `_autoBetCheck()` が `_state.executors` 更新前に走り、`count=0` で停止。
4. **$200判定の集計範囲ミス**
   - 全executor最大値を参照し、過去/別GUIの高額bet_amountで誤停止。
5. **単一executor追跡の脆さ**
   - 先頭1台の `total_bets` だけ監視していたため、片系停止/復帰時にAUTO連鎖が止まる。

## 実装した修正
1. **AUTOトリガー再設計**
   - gameId検知 → `total_bets` ベースへ変更。
2. **正しいフィールド参照**
   - `e.gui.total_bets / wins / losses / ties` を利用。
3. **refresh順序修正**
   - `_state.executors` を先に更新してから `_autoBetCheck()` 実行。
4. **$200停止判定の対象限定**
   - selected table 上の対象GUIのみを判定対象に限定。
5. **合計値追跡へ強化**
   - selected table 対象GUIの合計 `total_bets/wins/losses/ties` で進行判定。
   - `_autoBet.lastTies` を追加。
6. **LOOK補完維持**
   - LOOK時は `total_bets` が増えないため、40秒タイムアウト + snapshot補完を維持。
7. **UI改善**
   - AUTOボタン押下時の凹み（`:active`）を追加。
   - AUTO/セッションボタンの視認性向上（前段修正）。
8. **運用補助**
   - executorカードに削除ボタン（`DELETE /api/executors/{id}`）追加済み（同日対応）。

## デプロイ/運用対応
- `bacopy_master_ui.py` を複数回VPS反映、`bacopy-api`再起動実施。
- `bacopy-pragmatic-collector` はメモリ肥大化確認時に再起動（4.7GB→379MB）実施。
- 最終的にユーザー指示で collector 停止（`failed`=停止状態）。

## 監視結果（5分）
- AUTO `$1` は継続送信・実行を確認。
- queueは安定（`pending=0`、`processing` は短時間で解消）。
- `warakaji` / `user02` とも `SIGNAL BET` と `total_bets` 増加を確認。
- `BrokenPipeError` はブラウザ切断系で、今回のAUTO停止の直接原因ではない。

## 関連コミット（同日）
- `7c2af40` AUTOトリガー再設計（初版）
- `cf79d55` `e.gui.*` 参照修正
- `4259356` executor削除機能 / $200判定修正 / デバッグ除去
- `e451f5b` executors更新順序修正 / AUTOボタン押下感
- （未コミット差分）selected table 対象GUI合計でのAUTO追跡強化

## 現在ステータス
- AUTO `$1` は再現監視で安定稼働を確認。
- collectorはユーザー要望で停止中（必要時のみ再起動）。
