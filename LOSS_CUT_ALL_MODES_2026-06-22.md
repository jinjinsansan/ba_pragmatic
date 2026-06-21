# 損切り(loss_cut)をGUIに搭載・全資金管理モード共通 — 2026-06-22

> ダランベール×セット/SEQ等で「飛ばない保険」として loss_cut が必須だが、**GUIに損切り入力欄が無く**、
> dual-line の起動設定で `loss_cut:0` がハードコードされていて**事実上ずっと無効**だった。
> エンジン(BetManager)は元から全モードで loss_cut を適用する設計なので、**GUIに欄を足して配線するだけ**で全モード共通の損切りが有効化される。

---

## 1. 何だったか（バグ/欠落）
- **エンジンは loss_cut を全 money mode に適用済み**：`BetManager._check_limits` が `apply_result` 内で mode 問わず `session_pnl <= -loss_cut` で `limit_reached`(停止)。CLI `--loss-cut` 有り・`status_dict` も emit。
- **main.js** も `if (config.loss_cut) args.push('--loss-cut', ...)` で渡す配線済み。
- **欠落＝GUI**：dual-line 設定に損切り入力欄が無く、`app.js` の起動 config で `loss_cut: 0` をハードコード → **どのモードでも損切りが効かない**状態だった（利確 `inputDualProfitTarget` はあるのに損切りだけ無かった）。

## 2. 修正（GUIのみ・エンジン無変更）
- **index.html**：利確の隣に「損切り」入力 `inputDualLossCut`（0=無効・全モード共通・目安ユニット×300の注記）。
- **app.js**：起動 config の `loss_cut: 0` → `isDualLine ? (parseFloat(#inputDualLossCut)||0) : 0`。設定の load/save も配線（再起動で復元）。
- **エンジン再ビルド不要**（loss_cut は既に全モード対応）。GUI(asar)のみ。
- ＝**flat / SEQ / Kelly / 1-2-3×セット / ダランベール×セット どれでも共通で損切りが効く**。到達でセッション停止(on_limit=stop)。

## 3. 推奨値（先行検証より・[[DALEMBERTSET_RISK_LOSSCUT]]）
- **loss_cut ＝ ユニット × 300 ／ 元本 ＝ ユニット × 1000**（loss_cut≒残高30%）。
- 谷より浅いと「戻れる所で切る」逆効果。$300は普段の谷($134級)は切らず、稀な最悪の並びだけ封印。
- 連続運用だと$300は守備寄り(週の~21%を切る)＝もっと復活を残すなら元本を厚く(ユニット比を下げる)。

## 4. デプロイ
- **bafather**：asar `296830`(損切り欄入り)swap済(GUI停止中・旧295826をbackup)・起動して損切り欄表示を確認済。engine `26BDA93B`(68,730,505)**無変更**(loss_cut対応済)。
- **受け子02-10**：NSIS全9本リビルド(`_build_user0210.ps1` #3)。同梱＝engine 26BDA93B＋asar(損切り＋bet123set/dalembertset＋勝率レンジ＋安全モード撤去の全部入り)・bc=user06のみ10/11。配布＝オーナーが手動転送。
- ＝この1本で受け子は「7ハンドダランベール・1-2-3×セット・全モード共通損切り・勝率レンジチャート」を全部入手。

## 5. 関連
- 損切り適正値の検証＝`DALEMBERTSET_RISK_LOSSCUT_2026-06-22.md` / `[[project_dalembertset_losscut_2026-06-22]]`
- 新資金管理2種＝`MONEY_SET_MODES_2026-06-21.md` / `[[project_money_set_modes_2026-06-21]]`
- 勝率レンジチャート＝`WINRATE_RANGE_CHART_2026-06-21.md` / `[[project_winrate_range_chart_2026-06-21]]`
- ビルド手順＝`DISTRIBUTION_BUILD_RUNBOOK.md`
