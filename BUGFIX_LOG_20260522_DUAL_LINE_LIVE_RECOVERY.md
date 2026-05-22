# Dual Line LIVE 復旧ログ（2026-05-22）

## 目的
- デュアルラインモードで「VPSシグナル受信 → 実BET成立」を安定動作させる。
- マルチロビー常駐運用（2歩前スクロール待機→シグナル時即BET）を実現する。

## 3日間の詰まりポイント（要約）
1. **WS送信方式が不安定**
   - Pragmatic の game WS が Worker 側で管理され、通常の `window.WebSocket` 捕捉だけでは送信不能なケースが発生。
2. **DOM対象の取り違い**
   - Stake外側DOMではなく、`client.pragmaticplaylive.net` のフレーム内に実際の BET UI が存在。
3. **シグナル経路の不達**
   - `pending_bet=False` が続き、decisionが bot に届かない。
   - `BACOPY_API_URL/BACOPY_API_KEY` の実行時読み込み経路が混在し、ローカルAPI/リモートAPIの使い分けで詰まり。

## なぜクリックBETへ切り替えたか
- Pragmatic 側の構造上、**実BETボタンはフレーム内UI**で安定しており、セレクタで直接叩けることを確認。
- Worker WS直接送信は環境差・接続状態依存が強く、短い賭け窓で取りこぼしやすかった。
- そのため、multi-lobby では `frame.click()` による **Player/Banker/Tie 直接クリック**を主経路にし、WS送信経路は補助/互換に位置づけた。

## 実施した主な修正

### 1) LIVE実行系（`dual_line_live_executor.py`）
- PragmaticフレームのクリックBET実装（P/B/T）。
- マルチロビーのフォーカス探索を強化（候補名・qpid・スクロール探索）。
- テーブル探索のヒントを再利用する **位置/順序キャッシュ** を追加。
  - `table_focus_cache.json` を profile 配下に保存・読込。
  - 直近 frame index / match index / total nodes を次回探索に反映。

### 2) decision取得系（`dual_line_pragmatic_bot.py`）
- `.env` 読み込み候補を拡張（PyInstaller実行パス/`resources/.env` 系を吸収）。
- decision poll を多重化:
  - `primary`（通常: `BACOPY_API_URL`）
  - `remote_fallback`（`BACOPY_REMOTE_API_URL` または `https://master.bafather.uk`）
- decisionごとに取得元（base/key）を保持し、ACK/RESULTを同じ送信元へ返す。
- dedup（decision_id）を導入し、複数poll先で重複処理しないようにした。

### 3) ビルド/配布
- `scripts/build_bacopy_engine.ps1` の PyInstaller 収集設定を補強し、engine再ビルド後に GUI resources へ再配置。

## 動作確認結果
- ログ確認:
  - `decision poll targets: primary=http://127.0.0.1:8010, remote_fallback=https://master.bafather.uk`
  - multi-table WS 継続受信を確認。
- 手動投入テスト:
  - `manual_test_1779411492` を投入し、`status=done` / `bet_confirm=ws_sent` まで到達。
- 実運用:
  - **2026-05-22 09:58 に実BET（$1）成立**を確認（残高減少で実証）。

## 現在の到達点
- 「3日間0件」から脱却し、**実BETが成立する経路は復旧済み**。
- ただし「完成条件（VPSシグナルで継続的に実BET）」は未達。単発成功から連続安定へ移行中。

## 次の課題（完了条件に対するギャップ）
1. **VPS由来シグナルの連続成立率向上**
   - 連続数十シグナルで `received -> queued -> sent(done)` の落ち率を計測。
2. **processing滞留の解消**
   - 過去decisionに `processing` が残るケースの再送/掃除ポリシーを確立。
3. **BETSOPEN窓の時間最適化**
   - `queued_at -> sent` レイテンシを定量化し、閾値を環境別に調整。
4. **運用監視の明確化**
   - 失敗時に即判別できるログキー（decision_id, table_id, source API, sent age）を固定運用。

## 直近の運用方針
- まずは実運用で「VPSシグナル連続成功」を優先し、挙動を固定化。
- 新規機能追加より、送信成功率と滞留解消の安定化を優先して段階的に仕上げる。
