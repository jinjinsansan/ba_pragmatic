# プレイヤーSEQ シリーズ（元祖SEQ・5開始額）を money mode に追加 — 2026-07-05

> 大学ノート版（`〇×ロジック仕様書元祖.txt`）の元祖SEQ階段を、GUIの新モード
> **「プレイヤーSEQ」シリーズ（0.2$/0.4$/1$/2$/3$ スタート）** として追加。
> 進行ロジック（7ターン=1セット・overshoot・斜線・次単位3段階ルール）は従来スモールSEQと
> 同じ `MaruBatsuTracker`（=元祖忠実移植）を共用し、**階段だけが元祖48段（×開始額）**になる。

## 仕様

- **mode keys**: `player02` / `player04` / `player1` / `player2` / `player3`
- **基準階段（player1）**: `marubatsu_strategy.SEQ` から import（`SEQ_PLAYER1 = list(SEQ)`）＝旧MaruBatsu系と同一値を保証・重複定義なし
  - `1,2,3,5,7,9,11,13, 16,19,22,25,28,31, 35,39,43,47,51,55, 60..100(+5), 106..160(+6), 170..250(+10)` — **48段・$1 start・天井$250（250x）**
- **変種は $1 基準 × 開始額の比例展開**（small2=small1×2 と同流儀）。元祖が全段整数なので全変種の全段が最小チップ$0.2の倍数（コードで検証済み）:

| モード | 倍率 | 開始 | 天井 |
|---|---|---|---|
| player02 | ×0.2 | $0.20 | $50 |
| player04 | ×0.4 | $0.40 | $100 |
| player1 | ×1（元祖そのまま） | $1 | $250 |
| player2 | ×2 | $2 | $500 |
| player3 | ×3 | $3 | $750 |
- **型（攻撃/バランス/守備）は適用しない**（元祖忠実がコンセプト・全変種共通）
  - エンジン: `_resolve_seq()` で `PLAYER_SEQ_MODES` は shape 置換より先に return（env `BACOPY_SEQ_SHAPE` を無視）
  - GUI: `player*` 選択時は型ドロップダウンを非表示＋説明ノート表示
  - 起動ログ `[SEQ-SHAPE]` は `shape=original` と出る
- ターン制（セット長 5/7）は既存機構どおり選択可（元祖は7。デフォルト7）
- loss_cut / profit_stop / state永続化 / seq7_sets のGUI管理表描画は既存スモールSEQと完全共通（コード変更ゼロで乗る）

## スモールSEQとの違い（比較の結論）

| | 元祖=player1 | small1（攻撃型） |
|---|---|---|
| 段数 | 48段 | 28段 |
| 天井 | $250（250x） | $333（333x） |
| 勾配 | 緩い（+1〜+10刻み） | 急（少段数で333xまで） |
| 型切替 | なし（常に元祖） | 攻撃/バランス/守備 |

進行アルゴリズム自体は同一（`marubatsu_strategy.py` = 元祖gameLogic.tsの忠実移植）。
唯一のロジック差=既存のovershoot>0時idx0回帰禁止ガード（`marubatsu_strategy.py:139-144`、
元祖の穴の修正）は player1 にもそのまま適用される。

## 変更ファイル（ソース）

| ファイル | 内容 |
|---|---|
| `_rev53144/dual_line_money.py`（エンジン真ソース） | `SEQ_PLAYER1`（marubatsu_strategyからimport）+`SEQ_PLAYER02/04/2/3`（比例展開）・`PLAYER_SEQ_MODES`タプル・`SMALL_SEQ_MODES`に連結・`BET_MODES`5件・attack map・`_resolve_seq()`のshape除外・`[SEQ-SHAPE]`ログ=original |
| `copytrade_gui/src/renderer/index.html` | SEQタイプdropdownに「プレイヤーSEQ 0.2$/0.4$/1$/2$/3$」5行＋`#player1Note`説明・隠しselect `#inputDualMoneyMode` に5キー |
| `copytrade_gui/src/renderer/app.js` | `ALLOWED_BET_MODES`に5キー・`_loadMoneyModeUI`のisSeq判定=`indexOf('player')===0`・`_applyMoneyTypeVisibility`でplayer*時に型グループ非表示＋ノート表示・SEQタイプchange時に可視性再適用 |

- ルートの旧 `dual_line_money.py` は従来どおり**触っていない**（small14/24追加時と同じ方針）。
- エンジンbuildは `marubatsu_strategy` を既に同梱済み（`MaruBatsuTracker` import経由）なので spec 変更不要。

## 検証（ローカル実測・scratchpad `test_player1.py` / `test_player_variants.py`）

1. 全5変種: `current_seq` が元祖×倍率と完全一致・48段・期待天井・**全段$0.2チップグリッド上**・tracker生成 ✅
2. `BACOPY_SEQ_SHAPE=balance/defense` でも player* は元祖のまま／small1 は従来どおり置換（回帰なし）✅
3. player1: 3勝4敗セット→セット中$1固定→overshoot=1→次$2、5勝2敗→斜線・$1回帰 ✅
4. player02: $0.2固定→負けセット→$0.4→回収→$0.2・斜線 ✅／player3: $3固定→2勝5敗(OS=3)→$6 ✅
5. TIEはターン消費しない・セット長5の配線 ✅
6. `node --check app.js` OK・py動作テスト全PASS

## デプロイ（未実施）

small14/24（commit 0175155）と同一手順:
1. `_rev53144/dual_line_money.py` を bafather `C:\bacopy\` へ同期（同期前に現行とdiffで追加分のみ確認）
2. bafather で PyInstaller → `bacopy_engine.exe.new`
3. asar: デプロイ済安定asarを展開→ `index.html`/`app.js` に player1 追加分のみ外科パッチ→再パック→`app.asar.new`
4. セッション外で swap（`_baf_engine_swap.ps1` / asar swap）→ GUI STARTで「プレイヤーSEQ」各額を選択・`[SEQ-SHAPE] mode=player* shape=original ... steps=48` をログ確認
5. 受け子02-10展開は bafather 検証後（インストーラ再ビルド）
