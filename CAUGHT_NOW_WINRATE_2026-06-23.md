# 拾NOW率（各GUIが拾ったNOWの勝率・取りこぼし込み・2欄）  2026-06-23

## 要望
梶原さん発：各GUI別に「そのGUIが拾ったNOW」の勝率を出したい。**実際にBETしたもの（WIN RATE）ではなく、拾ったNOW**（受信した初回シグナル）の勝率。**取りこぼし（拾ったが賭けられなかった分）も含める**。最終的にユーザー案で**2欄（追従込み／追従なし）**に決定。

## 実装（engine `F142EDB6` / commit `c00f2e7`）
### 集計2バケツ
- **拾NOW率 追従込**（`caught_*`）＝全拾NOW（初回＋追従）
- **拾NOW率 追従無**（`caught_now_*`）＝初回NOWのみ（追従除外＝シグナルの素の質）
- WIN RATE（`wins`/`losses`＝BET成立のみ）とは**別物**。

### 仕組み
1. **登録**：NOW捕捉の全経路（VPS駆動の手動アシスト`5291`/`5458`・dga・追従`3278`）で、予想方向を**台の全IDをエイリアス集合**（table_id/qpid/table_name）で登録。`is_follow`で2バケツ振り分け。
2. **解決**：その台の**次の完了ハンド**で勝敗。★受け子の結果は`buf.hands`でなく**dga `gameResult`フィード経由**（`buf.hands`はsnapshot専用で成長しない）。`_on_dga_frame`の結果ループ（全卓・新gameId毎・BET有無に無関係）に`_resolve_caught_now`をフック＝**取りこぼしも観測**。照合はエイリアス集合の積（ID不一致でも当たる）。
3. **送信**：解決毎に`type:caught_stats`＋periodic statusに両rate。`[CAUGHT-NOW]`診断ログ。
4. **永続化**：stateに保存・復元（wins/lossesと同じlifecycle）。

### 挙動（ユーザーQ&A）
- **GUI再起動**：継続(resume=true)なら復元・継続／新規セッション(--reset)なら0（WIN RATEと同じ）。
- **SEQリセット**：state全体削除なら拾NOW率も0。★**手動LOSEWINでSEQ復元しても拾NOW率は不変**（手動ボタンは決済操作でNOW捕捉でない）。拾NOW率が動くのは「実際にNOWを拾って台の結果が出た時」だけ。
- **GUI停止期間**：拾わない・観測しない＝最後の値で凍結。停止中のNOWは非カウント。保留中（結果待ち）はごく少数失われる（保留は非永続）。

### GUI（index.html/app.js）
上部ステータス行に2カード（`#caughtWinRate` 追従込／`#caughtNowWinRate` 追従無）。`updateCaughtWinRate()`が`caught_stats`/`status`で更新。表示＝`XX.X% (◯W/◯L)`。

## 検証（bafather実機）
- `registered`→`resolved`が回り、`追従込=2W/1L` vs `追従無=2W/0L`＝**2欄が正しく差**（追従の負けが込みのみに反映）。GUI表示も確認。
- **光りへの干渉＝無し**：`LOOP-SLOW`412回は全部`executor.tick`（センタリング）で**caught関連SLOW=0**。私の追加はマイクロ秒級・try/except・決済ホットパス無変更。

## ★デバッグの教訓（捕捉点と結果経路が想定と違った）
- **捕捉点**：VPS駆動auto-betの初回NOWは`5291`(手動アシストfocus)/`5458`(🎯 via VPS)。最初に入れた`2542`等は別モード用で空振り（registered=0）。`total_signals += 1`を全部洗い出して特定。
- **結果経路**：受け子の決済は`buf.hands`成長でなく**dga `gameResult`フィード**（自動BET時は決済専用で起動）。最初`_process_table_frame`にフックして空振り（resolved=0）→`_on_dga_frame`の結果ループに移して解決。
- **ID不一致**：捕捉(qpid/table_id)と結果(tid/qpid/name)でID体系が違う→エイリアス集合＋extra_keysで吸収。
- **追従が大半**：v4テストでsignals 120中118が`follow_`。追従除外だと数字がほぼ動かない→2欄(込/無)案で解決。
- **干渉診断**：LOOP-SLOWは`executor.tick`センタリング由来（48卓＝新鮮でも1.9GBで重い）・v4はNOW連発でセンタリング多発。v3なら稀＝速い。拾NOW率は無罪。

## 配置
- bafather：engine`F142EDB6`＋2カードasar・swap済・検証済。
- 受け子02-10：リビルド済（同engine＋2カードsrc）・配布=オーナー手動。
