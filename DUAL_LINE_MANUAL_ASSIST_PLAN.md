# Dual-Line Manual Assist 実装計画

作成日: 2026-05-26

## 目的

BACOPY のデュアルラインモードを、自動BET主系から manual assist mode 主系へ移行する。

manual assist mode では、システムは人間のオペレーターを補助するが、Player / Banker の最終BETクリックは行わない。既存の dual-line auto-bet は experimental として残し、後から検証できる状態を保つ。

## 背景

現在の dual-line auto-bet は、以下の複数要素が同時に成立しないと安定しない。

- 対象タイルへのスクロール到達
- タイル描画完了
- active panel / subscription 更新
- hand id / game id 同期
- チップ選択と複数クリック速度
- BET成功確認
- 結果settle
- GUI / SEQ 反映

特に最終クリック周辺は Pragmatic 側の描画・購読更新・game_id同期に依存し、実運用で安定させるにはリスクが高い。

一方で、これまで実装した以下は manual assist として再利用価値が高い。

- VPS シグナル受信
- 2歩手前 / 1歩手前の preposition
- multi-lobby の table focus / scroll
- 対象卓の highlight / center keep
- chip preselect
- money / SEQ / signal 状態管理
- GUI の既存 status / shoe_history / resolution 表示基盤

## 方針

auto-bet を削除しない。manual assist 用の主系を追加し、auto-bet 専用の最終クリック・game-id同期・BET確認処理から責務を分離する。

基本方針:

- manual assist を dual-line の main/default 運用モードにする
- auto-bet は experimental / selectable として残す
- shared: signal detection / queue / table assist / money preview
- auto-bet only: final Player/Banker click / game-id sync / bet confirmation plumbing
- manual-assist only: operator queue / taken item / manual Win-Lose-Tie entry / manual money progression
- 既存モードや既存IPCスキーマを壊さない
- README / docs / 未追跡ファイルには明示依頼なしに触れない

## 実装ステップ

### Step 0: 現状固定

目的: これ以上の混乱を防ぐ。

- `git status` を確認
- 最新コミットと未コミット差分を把握
- auto-bet experimental 現状を作業ログに記録
- 未追跡ファイルは触らない
- 必要なら作業ブランチを切る

完了条件:

- どの差分を前提に manual assist を始めるか説明できる
- auto-bet 側を誤って戻したり削ったりしていない

### Step 1: モード分離

目的: manual assist を主系として起動できる入口を作る。

候補:

- `dual-line-assist`
- `dual-line-manual`
- 既存 `dual-line` は manual assist に向ける
- auto-bet は `dual-line-auto` または `dual-line-experimental` にする

実装対象候補:

- `bacopy_engine.py`
- `copytrade_gui/src/main.js`
- GUI mode selection 周辺
- `dual_line_pragmatic_bot.py`

注意:

- 既存 auto-bet ファイルを大量削除しない
- auto-bet の hand/game-id guard は緩めない
- manual assist では最終BETクリック関数に到達しない構造にする

完了条件:

- GUIから manual assist と auto-bet experimental を区別して起動できる
- manual assist 起動時に自動BETしないことがログで分かる

### Step 2: Manual Queue IPC

目的: GUIに手動オペレーター向けキューを出す。

bot から renderer へ新しいIPCイベントを送る。

候補イベント:

- `manual_assist_item`
- `manual_assist_update`
- `manual_assist_queue`

item の最小フィールド:

- `id`
- `decision_id`
- `table_id`
- `qpid`
- `table_name`
- `side`
- `amount`
- `pattern_key`
- `status`: `READY` / `NOW` / `TAKEN` / `EXPIRED` / `MISSED` / `SETTLED`
- `created_at`
- `expires_at`
- `signal_game_id` if available
- `seq_at_predict` if available

完了条件:

- pre-alert / preposition で `READY` がGUIに表示される
- actionable decision で `NOW` がGUIに表示される
- 複数itemが同時に見える
- NOWが最も目立つ

### Step 3: Side Panel UI

目的: 既存 signal panel とは別の manual assist panel を追加する。

表示内容:

- 優先キュー
- READY / NOW / EXPIRED / MISSED / TAKEN
- table name
- side: Player / Banker
- next bet amount
- remaining seconds
- pattern
- current SEQ state summary

UI原則:

- NOWを最優先で視認できるようにする
- 既存GUI表示を壊さない
- 既存 signal panel の意味を変更しない
- compactで運用中に見やすい表示にする

完了条件:

- manual assist panel が表示される
- 既存の signal panel / LIVE表示 / status 表示が壊れていない

### Step 4: Table Assist

目的: 人間がBETしやすい状態まで画面を持っていく。

再利用対象:

- `dual_line_live_executor.py` の multi-lobby focus / scroll
- table highlight
- visible hold / center keep
- chip preselect

禁止:

- manual assist mode で Player / Banker のBETエリアを自動クリックしない
- `_click_place_bet` / `_js_click_bet_in_container` 相当の最終クリック経路に入らない

操作:

- READY / NOW の対象をアクティブ化
- 対象テーブルへスクロール
- 対象テーブルを中央付近に保持
- 対象テーブルをhighlight
- 可能ならチップのみ事前選択

完了条件:

- item選択またはNOW昇格で対象卓へスクロールする
- 対象卓が目視しやすくhighlightされる
- チップpreselectが可能な場合だけ実行される
- BETエリアはクリックされない

### Step 5: Taken State

目的: 人間が実際にBETした対象だけ結果入力できるようにする。

導入状態:

- `active_manual_item`
- `taken_signal`
- `manual_bet_taken`

GUI操作候補:

- `Take / Mark Bet Placed`
- `Cancel`
- `Missed`

ルール:

- `NOW` item を operator が `TAKEN` にする
- `TAKEN` item だけ Win / Lose / Tie を受け付ける
- `EXPIRED` / `MISSED` item には結果入力不可
- operator が実際にBETしていない限り money / SEQ を進めない

完了条件:

- stale item に結果入力できない
- expired signal でSEQが勝手に進まない
- active/taken item と result が1対1で紐づく

### Step 6: Manual Result Entry

目的: manual assist mode における唯一の結果確定入口を作る。

GUIに追加:

- Win
- Lose
- Tie

処理:

- active/taken item を検証
- side / amount / table / decision_id と紐づけ
- `dual_line_money.BetManager` に結果を適用
- `status` / `shoe_history` / 必要なら `resolution` をGUIへ送信
- manual result log を残す

注意:

- Win/Lose は prediction side に対する結果として扱う
- Tie はSEQを進めるか現行BetManagerの仕様に従う
- auto-bet settlement と混ぜない

完了条件:

- Win / Lose / Tie で money / SEQ が進む
- GUIの next bet / current turn / WLT / pnl が更新される
- 誤ったitemへの結果入力が拒否される

### Step 7: Validation

必須:

- 触ったPythonファイルの `python -m py_compile`
- 触ったJSファイルの構文チェック
- GUI起動確認
- manual assist の end-to-end 状態遷移確認

確認シナリオ:

1. pre-alert が来る
2. GUI panel に READY が出る
3. decision が来る
4. NOW に昇格する
5. 対象卓へscroll/highlightされる
6. operator が Take を押す
7. operator が Win/Lose/Tie を押す
8. money / SEQ / GUI が更新される
9. expired item では結果入力できない

## リスク

- manual assist と auto-bet のIPCが混ざる
- result入力が stale item に入る
- existing signal panel の表示を壊す
- chip preselect がBETエリアクリックへ誤って進む
- auto-bet experimental のguardを壊す
- 状態がGUI側とengine側で二重管理になり不整合を起こす

## 禁止事項

- manual assist mode で自動BETしない
- Player / Banker BETエリアを自動クリックしない
- auto-bet の hand/game-id guard を緩めない
- 既存 auto-bet ファイル群を大量削除しない
- README / docs を明示依頼なしに編集しない
- 未追跡ファイルを触らない
- manual assist に無関係な大規模整理をしない
- 複数原因を一度に修正しない

## 最初の実装単位

最初のコミット候補は以下だけに絞る。

- manual assist mode の起動入口
- manual assist queue の最小IPC
- GUI side panel のREADY/NOW/EXPIRED表示
- 自動BETしないことのログ

この段階ではまだWin/Lose/Tie入力やchip preselectは接続しない。

理由:

- まず「signalが安全にGUIへ見える」ことを確認する
- auto-betとの分離を先に保証する
- money/SEQ更新は次段階で検証可能にする

## 完成条件

- manual assist mode が主系として起動できる
- pre-alert で READY が表示される
- actionable signal で NOW が表示される
- 対象卓までscroll/highlightされる
- next bet amount / money / SEQ 状態が表示される
- operator の Win/Lose/Tie 入力で money/SEQ が進む
- missed / expired では money/SEQ が進まない
- auto-bet は experimental として残る
- 既存GUI表示を壊さない

