# Valhalla II — 次回開発TODO

## 完了済み
- [x] Electron GUI基盤 (コントロールパネル、Settings、ログ)
- [x] 近未来デザイン (シアンアクセント、ガラスモーフィズム、グロー)
- [x] agent_api.py — BET実行フロー (scraper.py + executor.py + marubatsu_bet.py統合)
- [x] Camoufoxブラウザ別窓方式 (WS傍受が確実に動く)
- [x] ログイン検出 + バカラロビー自動遷移 + テーブル入場
- [x] Dry Runモード (ランダム結果生成、Telegram通知無効)
- [x] 勝敗フラッシュ (緑/赤で画面全体が光る)
- [x] LIVE FEED (スクロール式 W/L/T ドット、セット完了でリセット)
- [x] SHOE HISTORY (完了セットの〇✕並びと損益表示)
- [x] アクションバー (ブラウザの現在動作をリアルタイムテキスト表示)

## 次回優先 (BET本番稼働に必要)
1. **LIVE BETテスト** — Dry Runを外して$1チップで実際にBET。テーブル入場→BETフェーズ待ち→チップ配置→結果取得の全フロー確認
2. **EPIPEエラー修正** — STOP時にPlaywrightが閉じた後のpipe write error。main.jsのpythonProcess.stdinにerrorハンドラ追加
3. **STOP時のクリーンシャットダウン** — ブラウザを確実に閉じてからPythonプロセスを終了
4. **BET結果のGUI反映確認** — 実際のBETで残高変化、P&L、フラッシュエフェクトが正しく表示されるか

## GUI改善
5. **勝率表示** — stats-rowに勝率(Win Rate)を追加
6. **セッション時間表示** — 開始からの経過時間
7. **音声フィードバック** — 勝ち/負けの効果音 (オプション)
8. **ウィンドウ位置記憶** — 閉じた位置で再起動

## ロジック秘匿化 (配布前に必須)
9. **Python → EXE化** — PyInstaller/Nuitkaでmarubatsu_strategy.pyを含むPythonコードをバイナリ化
10. **ログからSEQ/ターン情報除去** — agent.logから戦略パラメータを消す
11. **IPC通信の難読化** — stdout JSONから `turns_display`, `current_unit` 等のロジック情報を削除/暗号化
12. **変数名・関数名の難読化** — marubatsu, SEQ, overshoot等の名前をランダム化

## 配布準備
13. **electron-builder設定** — Windows EXE (.exe) ビルド
14. **ライセンスキーシステム** — サーバーAPIでキー認証、有効期限管理
15. **自動アップデート** — electron-updater でGitHub Releasesから自動更新
16. **管理ダッシュボード** — ユーザー管理、ライセンス発行、利用状況モニタリング

## 既知の問題
- Evolution WSがロビーで数分サイレントになる (90秒リロード方式で対応中、テーブル入場後は不要)
- Camoufoxのフィンガープリント偽装でStakeが`Game is disabled`を返す場合がある (現在は動作中)
- 初回起動時にbrowser_dataロックでChromium起動失敗することがある (Camoufox方式では発生しない)
