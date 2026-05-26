# Dual-Line Manual Assist Phase 0 Baseline

作成日: 2026-05-26

## 目的

manual assist 実装に入る前に、現在の作業ツリーの状態を固定して記録する。

このファイルは計画書ではなく、実装開始前の基準点メモである。今後 manual assist を進める際、auto-bet experimental 側の差分と manual assist 側の新規差分が混ざらないようにする。

## Git 基準

最新コミット:

```text
c3f47c7 Add decoy-tile resubscribe recovery for stuck Turbo Baccarat panels
```

最新コミット時点の変更概要:

```text
dual_line_live_executor.py | 192 +++++++++++++++++++++++++++++++++++++--------
1 file changed, 160 insertions(+), 32 deletions(-)
```

## 現在の tracked 未コミット差分

```text
copytrade_gui/src/main.js
dual_line_live_executor.py
dual_line_pragmatic_bot.py
```

差分規模:

```text
copytrade_gui/src/main.js  |  30 +++++---
dual_line_live_executor.py | 185 ++++++++++++++++++++++++++++++++++-----------
dual_line_pragmatic_bot.py |  67 ++++++++++++++--
3 files changed, 219 insertions(+), 63 deletions(-)
```

## 未追跡ファイルについて

作業ツリーには多数の未追跡ファイルが存在する。manual assist 実装では、明示的に必要な新規ファイル以外は触らない。

今回追加した manual assist 関連ファイル:

```text
DUAL_LINE_MANUAL_ASSIST_PLAN.md
DUAL_LINE_MANUAL_ASSIST_PHASE0_BASELINE.md
```

## 差分分類

### copytrade_gui/src/main.js

分類:

```text
GUI起動/リセット補助
```

主な内容:

- old engine exit が観測できない場合でも 2.5 秒後に replacement engine を開始する fallback
- new session 時の削除対象に dual-line state を追加
  - `dual_line_pragmatic_state.json`
  - `dual_line_money_state.json`

manual assist への扱い:

- manual assist でも新規開始/リセットは重要なので、基本的には再利用候補
- ただし既存モードの resume/reset 挙動に影響するため、Step 1 で mode 分離とあわせて確認する

### dual_line_live_executor.py

分類:

```text
auto-bet experimental / table assist reusable
```

主な内容:

- multi-lobby focus JS に absolute qpid scan を追加
- preposition scroll 探索量と待機時間の調整
- fallback join の busy 判定追加
- exact BET window open だけで queued switch を消して先にBETする経路を抑制
- `prepared != target` かつ antenna NG の状態で `antenna_ok=True` に偽装して直接クリックする処理を削除
- game-id mismatch 時の decoy resubscribe を早める

manual assist への扱い:

- scroll / focus / visible hold / chip preselect は再利用候補
- Player / Banker の最終クリック、game-id sync、BET confirmed plumbing は auto-bet experimental 専用として隔離する
- manual assist mode から `_click_place_bet` / `_js_click_bet_in_container` 相当へ到達しないようにする

注意:

- このファイルは最近の auto-bet 修正が多いため、manual assist で大きく書き換えない
- 最初は「呼ばない」分離を優先し、削除しない

### dual_line_pragmatic_bot.py

分類:

```text
dual-line state / result settlement補助 / manual assist shared候補
```

主な内容:

- money state path を `MONEY_STATE_PATH` として定数化
- `--reset` 時に dual-line money state も削除
- multi-lobby result feed の numeric table id と qpid 差を、exact game id + table name で補助照合
- BET成功済みdecisionが結果待ちの間は remote snapshot もsettle入力として使用
- remote snapshot の最新 `gameId` を hands に保持

manual assist への扱い:

- VPS decision / preposition 受信は shared として再利用
- money / SEQ 管理は manual result entry で再利用
- auto-bet向けの settlement / confirmed bet 照合は manual assist では主経路にしない

注意:

- manual assist では結果確定入口を GUI の Win / Lose / Tie に限定する
- remote snapshot settle は auto-bet experimental 側または結果補助として残すが、manual result と混ぜない

## 作業方針

次のフェーズは Step 1: モード分離。

最初の実装ゴール:

```text
manual assist mode で起動したら、VPS signal / preposition は受けるが、
Player / Banker の自動BETクリック経路には絶対に入らない。
```

Step 1 で触る候補:

```text
bacopy_engine.py
copytrade_gui/src/main.js
dual_line_pragmatic_bot.py
```

Step 1 では原則として以下に触らない:

```text
dual_line_live_executor.py の最終クリック処理
renderer の大規模UI
money / SEQ result entry
```

## Step 1 の完了条件

- manual assist と auto-bet experimental を起動モードとして区別できる
- manual assist 起動時に自動BETしない
- manual assist 起動時にログで mode が確認できる
- 既存 auto-bet experimental は削除されていない
- 既存モードの起動引数を壊していない

## 禁止事項

- 未追跡ファイルを整理・削除しない
- 既存 auto-bet 差分を勝手に戻さない
- auto-bet の guard を緩めない
- manual assist 実装前に GUI side panel / result entry / money progression を一気に実装しない
- README / docs は触らない

