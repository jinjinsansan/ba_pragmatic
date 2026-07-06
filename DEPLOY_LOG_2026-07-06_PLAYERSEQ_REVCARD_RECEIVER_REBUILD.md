# デプロイ記録 2026-07-05〜06: プレイヤーSEQ配備 → 逆張りカード「−」バグ根治 → 受け子02-10リビルド

> 1セッションで完結した3本立ての記録。
> ①元祖SEQ(大学ノート版)を「プレイヤーSEQ」5変種としてGUIに実装しbafatherへ配備
> ②bafather実機で発覚した「逆張り拾NOW率カードが−のまま」の真因特定と両側修正
> ③最新engine+GUIで受け子02-10のインストーラ9本を再ビルド(未配布)
> 実装仕様の詳細は `PLAYER_SEQ_ORIGINAL_2026-07-05.md` を参照。

---

## 1. プレイヤーSEQシリーズの実装と配備

### 何を作ったか
- 元祖SEQ =「大学の〇✖ゲーム」ノート版(`〇×ロジック仕様書元祖.txt`)の **48段・$1 start・天井$250** の階段を、mode key `player1` として実装
- さらに $1 基準×開始額の比例展開で4変種追加(small2=small1×2 と同流儀):

| mode | 倍率 | 開始 | 天井 |
|---|---|---|---|
| player02 | ×0.2 | $0.20 | $50 |
| player04 | ×0.4 | $0.40 | $100 |
| player1 | ×1(元祖そのまま) | $1 | $250 |
| player2 | ×2 | $2 | $500 |
| player3 | ×3 | $3 | $750 |

- 進行ロジックは従来スモールSEQと同じ `MaruBatsuTracker`(=元祖gameLogic.tsの忠実移植)を共用。**階段だけが違う**
- **型(攻撃/バランス/守備)はプレイヤーSEQには適用しない**(元祖忠実がコンセプト)。エンジンは`PLAYER_SEQ_MODES`で shape 置換前に return、GUIは player* 選択時に型ドロップダウンを隠す
- ★注意: 元祖は **48段**(当初49段と数え間違えた。`len(marubatsu_strategy.SEQ)==48`)

### commit
- `751e002` feat(money): add PLAYER SEQ series(_rev53144/dual_line_money.py + copytrade_gui renderer 2ファイル)
- GUI側の落とし穴: `_loadMoneyModeUI` の SEQ 判定が `mode.indexOf('small')===0` 前提だったため、`indexOf('player')===0` の追加が必須だった

### bafather配備(07-06 00:29 自然停止後にswap)
- 手順: 停止検知モニタ(SSH 60秒ポーリング)→ プロセス0確認 → ハッシュガード → `.bak_20260706_002947` 退避 → swap
- engine **68741736**(bafatherでPyInstallerビルド・ソース同期前にリモートとローカルHEAD~1のMD5一致を確認してから上書き)
- asar **324125**(稼働中デプロイasar 322488 を取得→中身がrepo HEAD~1と完全一致[index.htmlはCRLF差のみ]を確認→app.js/index.htmlの2ファイルだけ注入→再パック→差分2ファイルのみをdiffで実証)

### 実機検証(07-06 朝)
- player02 でLIVE稼働: BET $0.20→(負け後)$0.40 と元祖階段どおり、seq_turn/overshoot/turns 進行正常、一晩で PnL **+$5.41**
- オーナーの運用実態: **START直後に手動で逆張りONにするのが常態**(00:31/00:47両セッションとも起動数秒で`[REVERSE] ENABLED`・本人確認済)

---

## 2. 逆張り拾NOW率カード「−」固定バグ(真因特定→両側修正)

### 症状(オーナー報告)
GUIの拾NOW率3カード(追従込/追従なし/逆張り)が全て「−」のまま。他のパネルは動いている。

### 切り分けの経緯(再利用価値の高い診断経路)
1. エンジンstate(`C:\BACOPYRECEIVER_user01\resources\engine\dual_line_pragmatic_state.json`)を直読み → `caught_rev=67W/50L/20T` が蓄積している=**エンジン側は健全**
2. エンジンログ → 全拾NOWがrevタグ(逆張りON中のため)。`[REVERSE] VPS decision side B->P` が現在進行形=逆張り実BET中
3. **決定打**: GUIが受けた生パイプのキャプチャ `%APPDATA%\bacopy-copytrade-gui\logs\engine_cli_capture.log` に `caught_stats`(rev=57.6%入り)が55回記録 → 「エンジンは送った・GUI mainは受けた」まで確定
4. main.js は全JSONを無条件で renderer へ転送(ホワイトリスト無し)→ 残るは renderer の描画ロジック
5. コード精読で真因発見

### 真因
- 周期 `status` emit(`dual_line_pragmatic_bot.py`)に `caught_wins`/`caught_now_*` はあるが **`caught_rev_*` が無い**(07-05 の c201612 は caught_stats emit と state 保存/復元にだけ rev を追加し、status emit に入れ忘れ)
- GUI は status 受信のたびに `updateCaughtWinRate(msg)` で3カードを再描画 → rev はフィールド欠落=0件扱い → **「−」で上書き**
- 2〜3分毎の `caught_stats` で一瞬 57.6% が入っても、数秒後の status で「−」に戻されていた(昨日のengine 68741066から存在した潜在バグ)

### 正常だった点(誤解しないこと)
- **追従込/なしの「−」は仕様どおり**。逆張りON中は順方向カウンタを完全凍結する隔離設計(c201612)であり、bafatherは起動6秒からONなので順方向サンプル0件=「−」が正しい
- 逆張りOFFで運用した時に初めて順方向カウンタが貯まり始める(2つの帳簿は排他)

### 修正(commit `5d6c6a1`・二重防御)
1. engine: 周期 status emit に `caught_rev_wins/losses/ties/win_rate` を追加
2. GUI: `_setCaughtCard` に「フィールド欠落のメッセージではカードを触らない」ガード(どちらか片方でも直る)

### 配備(07-06 09:45 自然停止後にswap)
- engine **68743068**(MD5 `8EFD10E3F7AF7C9DCADA98B3CDA46919`)
- asar **324437**(MD5 `B043CF06…`・土台324125にapp.jsのみ差替)
- 旧 = `.bak_20260706_094534`
- **実機確認済**: 再起動後の status に `caught_rev_win_rate: 56.7` が載り、GUIカードに「56.7% (68W/52L)」表示。カウンタはswapを跨いで正しく復元

---

## 3. 受け子02-10 インストーラ再ビルド(9本・未配布)

### 手順(DISTRIBUTION_BUILD_RUNBOOK.md 準拠)
1. bafatherの `bacopy_engine.exe.new`(68743068)を `copytrade_gui/build_staging/engine/bacopy_engine.exe` へ取得・MD5一致確認
2. 前提確認: `copytrade_gui/src` = コミット済HEADと一致 / `web/.env.local` 鍵 / `support_keys/` / package.json author
3. `_build_users_0210_playerseq.ps1`(staged engineサイズガード付き)で provision→electron-builder ×9

### ★事故と教訓: インストーラ418MB肥大
- 1回目のビルドが **418MB/本**(前回バッチ160MB)
- 真因: `build_staging/engine/` に過去engineの `.bak_*` 4本(6/24〜6/28の差替残骸・計260MB)が溜まっており、extraResources のディレクトリ丸ごと同梱で全インストーラに混入
- 対処: `.bak_*` を `copytrade_gui/_engine_baks/` へ退避(削除せず)→ 再ビルドで **160.1MB/本** に復帰
- **教訓: ビルド前に `build_staging/engine/` が exe 1本だけであることを確認する**

### 成果物(07-06 10:25 完成)
`copytrade_gui/dist/BACOPYRECEIVER_userNN_Setup.exe`(NN=02〜10・各160.1MB)

検証済み:
- 同梱engine MD5 = `8EFD10E3…`(bafather稼働中と同一)
- asar実体抽出: `ALLOWED_BET_MODES`にplayer5種 / カード上書き防止ガード / index.htmlにプレイヤーSEQ5行
- ポート焼き込み: 02=2277 / 03=2255 / 04=2291 / 05=2241 / 06=2268 / 07=2222 / 08=2248 / 09=2250 / 10=2290

### この9本に入っている主な差分(受け子の現行旧版比)
- 罫線T込み読み修正(★最大級バグ・01f0f66)+v3=6パターン
- 逆張りモード+逆張り専用拾NOW率カウンタ+カード(63b8a86/c201612/5d6c6a1)
- プレイヤーSEQ 5種+スモールSEQ small14/24(751e002/0175155)
- 安全モード(好調のみBET)、勝率レンジチャート等 6月末までの全機能

---

## 4. 現状と残タスク

- ✅ bafather: engine 68743068 / asar 324437 で稼働・逆張りカード表示確認済
- ✅ commits: `751e002`(プレイヤーSEQ) `5d6c6a1`(revカードfix) on `feat/engine-heartbeat`(未push)
- ⬜ **受け子02-10へ配布**(Setup.exe各1本・ユーザー取り違え厳禁・SmartScreen→INSTALL ON THIS PC→UAC周知)
- ⬜ 配布後の各受け子動作確認(SSH経由: プレイヤーSEQ選択肢・T込みパターン・逆張りカード)

## 5. 関連ファイル
- `PLAYER_SEQ_ORIGINAL_2026-07-05.md` — プレイヤーSEQ実装仕様の詳細
- `〇×ロジック仕様書元祖.txt` — 元祖(大学ノート版)の原典仕様
- `DISTRIBUTION_BUILD_RUNBOOK.md` — 配布ビルドの正規手順
- `copytrade_gui/_build_users_0210_playerseq.ps1` — 今回のバッチスクリプト
