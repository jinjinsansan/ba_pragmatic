---
name: bacopy プロジェクト計画書 (Copytrade + Friend→AI)
description: ba(Evolution既存GUI)とは別系統で、友人の判断をコピー実行→ログ蓄積→学習→代理判断まで到達するための実装計画。新しいDroidが最初に読む前提の全体メモ。
type: project
createdAt: 2026-04-17
---

# 0. TL;DR（新しいDroid向けの最短理解）

- このWSL内には **2つの別プロジェクト**がある。
  - **`/mnt/e/dev/Cusor/ba`**：既存の最初のGUI（Evolution中心 / LAPLACE）。**これは残す**。既存ユーザーも使うので、破壊的変更を避ける。
  - **`/mnt/e/dev/Cusor/ba_pragmatic`**：当初はPragmatic移植のために作られたGUI。これを **コピー取引（友人判断）＋学習（Friend→AI）**の本体に転用する。
- 近々 `ba_pragmatic` は **ディレクトリ名を `bacopy` に変更**する（ユーザー要望）。ただし **この計画書は現時点で `ba_pragmatic/docs/` に置く**（rename後は `bacopy/docs/` へ移動）。
- 既存のVPS運用（laplace-api / collector / DBなど）は **基本的に `ba` 系に存在**しており、当面は `ba` を止めずに `bacopy` を独立に構築する。

この計画書のゴールは：
1) 友人が **Master画面**で「どのテーブルで、BET/LOOK/side/amount」を判断  
2) システムがそれを **Executor**として正確に執行（ACK確認、二重BET防止、異常時停止）  
3) 全決定を **決定時点のWSスナップショット＋結果**としてログ化（学習データ化）  
4) 蓄積データで **友人の判断を模倣→評価→段階的自動化**し、友人不在でも回せる状態へ

---

# 1. ディレクトリ/リポジトリ構成（重要）

## 1.1 既存プロジェクト（残す）：`ba`

- パス：`/mnt/e/dev/Cusor/ba`
- 役割：最初に出来た既存GUI（Evolution中心）。既存ユーザー利用があるため現状維持が基本。
- 既に実装済みの主なもの（過去セッション要約から）：
  - VPS API（例：`laplace_api.py`）とクライアント（例：`laplace_client.py`）
  - データ収集（`monitor/run_data_collector.py` 等）
  - crowd(bettingStats) 収集パイプライン（`crowd_events` テーブル、`/api/sessions/{user_id}/crowd` など）
  - 既存バックテスト群、レポートHTML
- VPS運用が絡むものは原則 `ba` 側が source-of-truth。

## 1.2 新プロジェクト（本計画の対象）：`ba_pragmatic` → `bacopy`

- 現パス：`/mnt/e/dev/Cusor/ba_pragmatic`
- 将来パス（ユーザー要望）：`/mnt/e/dev/Cusor/bacopy`
- 役割：コピー取引（Friend Proxy Betting System）＋ Friend→AI 学習パイプラインの本体。
- ここには Pragmatic のWSやDOM調査・収集系が既にある（例：`collector_pragmatic.py`, `capture_pragmatic*.py`, `ws_raw_log.jsonl` 等）。これを土台にする。

---

# 2. VPS/サーバーの現状（ba側が主）

## 2.1 現行の運用（既存LAPLACE）

過去作業の文脈では、VPSは概ね以下：
- VPS：`210.131.215.116`
- デプロイ先：`/opt/laplace`（`ba` ベース）
- systemd：`laplace-api` / `laplace-data-collector` /（状況により）`laplace-crowd-collector`

⚠️ 注意：この計画書は「現時点の把握」をまとめたもので、実際のVPS状態は随時変わり得る。新しいDroidは開始時に `systemctl status` やDBファイル位置などを確認すること。

## 2.2 bacopy 側の方針

- 当面：`ba` のVPS運用を壊さない（既存ユーザーが使う）。
- `bacopy` は **別サービス名 / 別DB / 別ポート** で独立稼働させるのが安全。
  - 例：`bacopy-api`（FastAPI）、`bacopy-executor`（常駐WS executor）
  - DB：`analytics_bacopy.sqlite3` / `bacopy_logs.sqlite3`（命名は後で確定）

---

# 3. bacopy のプロダクト定義

## 3.1 何を「コピー」するか

Master（友人）が出す指示：
- table選択（provider / table_id / table_name）
- action：`LOOK` / `BET`
- side：`PLAYER` / `BANKER`（必要なら `TIE` も）
- amount：$ or chip unit
- optional：メモ（友人の理由、シグナル名）

Executor（システム）が保証すること：
- 指示が「実際に打てたか」をACKで確定し、Master UIへフィードバック
- BET window内で確実に処理（遅延時はLOOKに倒す）
- 二重送信・重複BET・セッション切れを安全に扱う

## 3.2 ログ（学習データ）で絶対に必要なもの

**決定ロガーは初期実装から必須**（MDでも強調）。

ログ単位（1 decision event）に最低限必要：
- `captured_at`（決定時刻、UTC ISO8601）
- `provider`（Evolution/Pragmatic）
- `table_id`, `table_name`
- `game_id` / `round_id`（WS由来のユニークID）
- `phase`（BET window open/close など）
- `snapshot`（決定時点で取得できるWS情報一式）
  - bead road / big road等（WS JSONで取れるもの）
  - players_online / watchers / bettingStats 等（取れる範囲で）
- `friend_action`（LOOK/BET/side/amount）
- `ack`（BET受理確認：受理有無、受理時刻、受理payload）
- `result`（次の結果：P/B/T）と、その確定時刻
- `execution`（実際に置けた額、partial betの有無、残高スナップショット）

重要原則：
- **look-ahead（未来情報）混入禁止**：snapshotは「ボタンを押した瞬間に得られたデータ」のみ。
- ログは「再学習」だけでなく「監査（本当に押した？打てた？）」にも使う。

---

# 4. アーキテクチャ方針（Evolution/Pragmatic両対応）

## 4.1 まずは“同一I/F”で抽象化

Provider差（Evolution/Pragmatic）を吸収するため、Executor側で共通I/Fを定義する：
- `ProviderSession`：
  - `connect()` / `refresh_tokens()` / `subscribe(table)` / `get_snapshot()` / `place_bet(side, amount)` / `wait_result(game_id)`
- `Snapshot`：WSイベントから生成
- `BetReceipt`：ACK確認の統一表現

## 4.2 実装の順序（安全に進める）

最初から「完全WS直送」に寄せるより、段階でリスクを潰す。

推奨の段階：
1. **Logger first**：WS監視＋snapshot保存＋結果追跡（まだBETしない）  
2. **指示伝達**：Master→API→Executor で decision を受け取る（まだBETはdry-run可）  
3. **実BET（最初はブラウザでも可）**：ACK/二重BET防止/残高整合を実戦で固める  
4. **WS直送に置換（Pragmatic優先）**：BETコマンドXML確定、token refresh、安定運用  
5. **Shadow mode**：モデル推奨を出すが自動BETしない（一致率/勝率測定）  
6. **段階的自動化**：少額→一部自動→全自動

---

# 5. 既知の運用リスクと対策（設計に必須）

## 5.1 Session Expired / WS切断 / 状態不整合

- MasterがLOOK中心だとセッションが死ぬ（既知問題）。
- 対策は「検知→復旧」より、まず **“押せない状態では押せないUI”**が最重要。

必須：
- `table_connected` / `ws_connected` / `bettable` の3状態を常時提示
- 1つでもNGならBETボタン無効化（LOOKのみ）
- BET後は必ずACK待ち（ACK無なら「打ててない」扱い）

## 5.2 二重BET/重複決定

- Masterの連打、ネットワーク再送、APIリトライで二重BETが起きる。
- 対策：`decision_id`（UUID）＋ `game_id` で冪等化し、同一roundへのBETは1回だけ許可。

## 5.3 無限残高シミュの罠

- ノートのシミュ等で「無限残高」前提だと破綻が観測されない。
- bacopy では現実運用として、残高不足時は必ず停止/LOOKに倒す。

---

# 6. “bacopy” へのリネーム方針（ユーザー要望）

ユーザー方針：
- `ba` は残す（既存GUI、既存ユーザー用）
- `ba_pragmatic` を **`bacopy` にして、コピー取引＋学習プロダクトにする**

実施手順（作業時）：
1. WSL上でディレクトリ名変更（git repoを壊さないように慎重に）
2. パス依存スクリプト（バッチ、systemd、相対パス）を更新
3. 既存 `docs/friend_proxy_betting_system.md` と本計画書を `bacopy/docs/` に残す

---

# 7. 直近の実装TODO（次のDroidがすぐ動ける粒度）

## Step 0（準備）
- `ba_pragmatic` の現状を「コピー取引プロダクト」として整理
  - 実行エントリポイントを決める（例：`bacopy_api.py`, `bacopy_executor.py` など）
  - DB名/保存先を決める（`data/` 配下推奨）

## Step 1（決定ロガー最小）
- WSから取れる snapshot を1ファイルに統合（Evolution/Pragmaticで差分は許容）
- ログ出力先：まずは `data/decisions.jsonl`（append-only）＋必要ならsqlite

## Step 2（Master→Executor API）
- FastAPIで `POST /decisions`（friend_actionを受け取る）
- `GET /status`（接続状態、現在テーブル、直近ACK、直近結果）

## Step 3（Executor：dry-run→live）
- まずは dry-run（BETしない）で全フローを通す
- liveは少額で、ACKと実額が一致することを確認

## Step 4（WS直送）
- Pragmatic：BET XMLコマンドを1回の実BET観測で確定（最大ブロッカー）
- token refreshジョブ（45分間隔など）と手動フォールバック導線

## Step 5（学習の第一歩）
- Shadow mode：友人決定をラベルとして、モデルが同じ決定を出せるか（精度）を測る
- “利益”より先に、まず **模倣一致率** と **条件付き勝率** の再現性を確認

---

# 8. 関連ドキュメント

- `docs/friend_proxy_betting_system.md`：全体構想（本計画書の上位コンセプト）
