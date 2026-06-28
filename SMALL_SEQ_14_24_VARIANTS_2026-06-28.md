# スモールSEQ $1.40 / $2.40 変種を追加（攻撃/バランス/守備 各3型）— 2026-06-28

> 既存 small1/small2 の間を埋める開始額 $1.40・$2.40 のSMALL SEQを新設。
> 設計は `small2 = small1 × 2` と同流儀＝`small14 = small1 × 1.4` / `small24 = small1 × 2.4`。
> エンジン＋asarは **ステージ済・未スワップ**（bafatherライブセッション中のため swap は次回）。

## 何を作ったか
- `small14`：**$1.40 start**・28段・天井 $466.2（333x）
- `small24`：**$2.40 start**・28段・天井 $799.2（333x）
- 各々 **攻撃/バランス/守備** の3型が選択可（型は既存 `BACOPY_SEQ_SHAPE` 機構で自動派生）。

| モード | 攻撃(attack) | バランス(balance) | 守備(defense) |
|---|---|---|---|
| small14（$1.4） | 28段・天井$466.2（333x） | 31段・天井$350（250x） | 30段・天井$280（200x） |
| small24（$2.4） | 28段・天井$799.2（333x） | 31段・天井$600（250x） | 30段・天井$480（200x） |

全段が **$0.2（最小チップ）の倍数** ＝丸め不要・チップ妥当（コードで検証済み）。

## 設計根拠
- `small1` の全段が整数。これに 1.4(=7×0.2) / 2.4(=12×0.2) を掛けると、整数×(0.2の倍数)＝必ず0.2の倍数になりチップ床に綺麗に乗る。
- 天井倍率を333xに揃え、small02〜small6の攻撃ファミリーと同じリスク特性に統一。
- バランス/守備は `$1基準テンプレ（SEQ_SHAPE_BALANCE/DEFENSE）× 開始額` で展開（`start = attack[0]`）＝新変種でも追加実装不要で3型が出る。

## 変更ファイル（ソース）
| ファイル | 内容 |
|---|---|
| `_rev53144/dual_line_money.py`（エンジン真ソース） | `SEQ_SMALL14`/`SEQ_SMALL24` 配列・`SMALL_SEQ_MODES`・`BET_MODES`・attack map・ヘッダ。`ALLOWED_MODES`は`BET_MODES.keys()`から自動 |
| `copytrade_gui/src/renderer/index.html` | 「SEQタイプ」ドロップダウン2箇所に 1.4$ / 2.4$ を追加 |
| `copytrade_gui/src/renderer/app.js` | `ALLOWED_BET_MODES` 検証セットに `small14`/`small24` 追加 |

- ルートの旧 `dual_line_money.py`（small2すら無い旧版）は**触っていない**（混乱回避）。
- エンジン/executor/bot は `dual_line_money` から `BET_MODES`/`ALLOWED_MODES` を import するだけで、SEQ定義の重複なし。executor のチップ計画は額から動的生成＝モード非依存で追従不要。

## ビルド＆ステージ（未スワップ）
bafather ライブセッション中につき **swap（GUI/engine停止→差替→再起動）は実行せず**、直前までを実施：
1. 編集済 `dual_line_money.py` を bafather `C:\bacopy\` へ同期（稼働engineはコードをメモリ保持済＝ディスク上書きは無害）。同期前に bafather現行と diff し、差分が追加分のみ＝クロバー無しを確認。
2. **asar はローカルで外科パッチ**：デプロイ済 07安定asar（313668）を展開→`index.html`/`app.js` に **small14/small24 の行だけ** 追加→再パック。
   - ★重要：ローカル `copytrade_gui/app.js` には依頼外の **Chrome膨張バッジ（renderChromeBloatBadge）** が入っていたため丸ごとコピーは不採用。展開した安定asarに追加分のみ当てて混入回避（バッジ=0確認）。
   - 結果 `app.asar.new` = **317181**（313668 + 追加分のみ）。
3. **エンジンは bafather で PyInstaller**（`build\bacopy_engine.spec`・稼働中ビルド可＝dist出力のみ）→ `bacopy_engine.exe.new` = **68735858**（旧68733831 +2027B）。`undetected_playwright` 欠落警告は従来同様の非致命・`Build complete`。

### ステージ済み成果物（bafather `C:\bacopy\`）
- `bacopy_engine.exe.new` = 68735858
- `app.asar.new` = 317181

## 次回スワップ手順（セッション終了後）
1. エンジン差替：`_baf_engine_swap.ps1`（`.new`→`C:\BACOPYRECEIVER_user01\resources\engine\bacopy_engine.exe`・旧は`.bak_<stamp>`）
2. asar差替：`_baf_swap_asar_user01.ps1`（`.new`→`...\resources\app.asar`・旧は`.bak_<stamp>`）
3. GUI START → SEQタイプで「1.4$ / 2.4$」+ 型を選択して検証
- 受け子02-10展開は bafather 検証後（要インストーラ再ビルド）。

## 検証ログ
- `_resolve_seq()` を6通り（2変種×3型）実行＝全て期待天井・全段チップ妥当（chipOK=True）。
- asar 再パック後フル再展開で `index.html` に small14/small24 が4行survive・`app.js` にバッジ混入0。
