# 引継ぎ書 2026-06-10 夕方 — dual-line 速度問題 全解決 → 次は「リバートで失った良い機能の足し戻し」

> **次セッション最初に読む。** 前セッションで dual-line の「決済(光り)が遅い・追従ミス」を**完全解決**した。
> 次タスク = **53144e3+skip-refocus の良い土台に、リバートで失った良い修正を1つずつ足し戻して"速い＋全機能入り"の決定版を作る**こと。
> 詳細な調査経緯は `SESSION_HANDOFF_2026-06-10_REVERT_TO_0606_FAST.md`（必読・本書はその続編の作業指示）。

---

## 0. 今どうなっているか（30秒サマリ）

- **bafather (162.43.83.54) = engine `68699438` で稼働中・速い・安定**（追従が間に合い、取り逃しBET 47分間0件＝実証済）。
- この engine = **`53144e3`（06-07の良い土台）＋ skip-refocus修正 ＋ tick計測(無害)**。
- git: **commit `c5cae44`**（skip-refocus修正＋引継ぎMD）, **tag `dual-line-skip-refocus-2026-06-10`**, branch `feat/dual-line`。**未push**。
- 速度問題は**2層**で、両方解決済み:
  1. **毎回遅い** = SEQ-Supabase回帰(06-08 `fa66b96`/`95ce0ab`) → **53144e3に戻して除去**。
  2. **たまに赤/青枠stuck・追従ミス** = `_perform_switch`(予告卓切替FOCUS)が同一卓へ繰り返し5-19秒freeze → **skip-refocus修正で70-85%減・取り逃し0**。
- ⚠️ **branch HEAD(`1d323f2`)はSEQ-Supabase回帰を含む**。だからデプロイは53144e3ベース。HEADから素直にビルドすると遅い。

---

## 1. ★次タスク = 失った良い機能の足し戻し

53144e3(06-07)に戻したことで、**06-08以降と今日の変更が engine から消えている**。良いものだけ戻したい。

### 復活対象の棚卸し（HEADにあって 53144e3 に無い）
| commit | 機能 | 判定 |
|---|---|---|
| `1d323f2` 今日 | **HOLD freeze全網羅**（手動user02の黄色枠スクロール不具合修正） | ✅ 戻す（手動user向け・優先1） |
| `fa66b96` 06-08(一部) | **SEQ recovery floor**（回収中$1戻り解消・overshoot>0→next_idx≥1）＋ **HOLDスクロール修正** | ✅ 戻す（資金管理・優先2） |
| `9379162` 今日(一部) | **決済時NOW-LOCK即解除**（赤/青枠を決済時に消す） | ✅ 戻す（優先3） |
| `3e72e64` 06-07 | **v4パターン再定義**（pline/bline 5+/≤1, bline\|telecho\|B） match.py | ⚠️ 影響確認（v4信号はVPS生成なので受け子match.pyの影響は限定的か要確認・優先4） |
| `fa66b96`/`95ce0ab` 本体 | **SEQ-Supabase永続化**（プロファイル破損時のSEQ復元） | ⚠️ **回帰の犯人。そのまま入れない**。決済ホットパスに一切触れない形(完全別スレッド/起動時GET復元のみ)でしか入れない・最後・別途慎重に |
| `c6387b7` 今日 | DOM-center deferral | ❌ 対症療法・skip-refocusで不要 |
| `e1aca4d` 今日 | lpbet_fast | ❌ 対症療法・53144e3が速いので不要 |
| `27cd1db` 今日 | game_id確証(空振り `game=-`) | ❌ 不要 |

> ※ログローテーション(`9379162`本体)は**asar側なので失っていない**（234275で生存）。

### 進め方（安全に・1つずつ）
1. 各commitの**差分を `git show <commit> -- <file>` で精査** → skip-refocus/53144e3と競合しないか確認。
2. ✅の修正を **`_rev53144/` のファイルに1つずつ適用**（後述§3の作業ディレクトリ）。
3. bafatherで**ビルド→swap→実機で速度(取り逃し0/freeze低水準)が崩れないか確認**してから次へ。
4. SEQ-Supabaseは最後・別途。
5. 完成したら **user02/03/04(特にuser04=重症)へ配布**。

優先順: `1d323f2` → `fa66b96`一部(SEQ floor+HOLD scroll) → `9379162`一部(NOW-LOCK release) → `3e72e64`(要影響確認) → SEQ-Supabase(最後)。

---

## 2. デプロイ状況（3台）と配布の必要性

| 台 | engine | 状態 |
|---|---|---|
| **bafather(user01)** | `68699438` = 53144e3+skip-refocus | ✅ 速い・安定（本番パイロット） |
| **user02** | `68698219`（古い・SEQ-Supabase回帰込み） | 追従は基本OKだが**光り遅延あり**（freeze軽度）。手動運用あり→HOLD修正欲しい |
| **user03** | `68698219` | 同上(未ヒアリング) |
| **user04** | `68701377`(or古い) | **追従失敗多発・bafatherと同症状(重症)** → **skip-refocus配布の恩恵が最大。最優先配布候補** |

- 配布は「決定版engine完成後」が理想だが、**user04だけ先に skip-refocus入りを入れる**判断もあり(重症のため)。
- 配布方法: インストーラ再ビルド(各160MB)or engine hot-swap。手順は§3と`DISTRIBUTION_BUILD_RUNBOOK.md`。

---

## 3. 操作手順・作業ディレクトリ（再現用）

### ローカル作業ディレクトリ
- `E:\dev\Cusor\bacopy\_rev53144\` = **デプロイ中engineのソース**（53144e3の4ファイル + skip-refocus + tick計測）。**ここを編集して足し戻す**。
  - `git show 53144e3:<file>` で取り出した bot/executor/money/match に、skip-refocus(executor)とLOOP-SLOW(bot)とTICK-SLOW(executor)を追加済。
- `E:\dev\Cusor\bacopy\_rev0606\` = 純06-06(1c3e3a7)の4ファイル（参考・速い基準）。

### bafatherでのビルド→デプロイ
1. ローカルで `_rev53144/` の.pyを編集。
2. `scp -i ~/.ssh/laplace_vps _rev53144/dual_line_*.py Administrator@162.43.83.54:C:/bacopy/`
3. ビルド: `ssh ... "powershell -File C:\bacopy\_build_engine_handend.ps1"`（~3分・`bacopy_engine.exe.new`生成・size/mtime出力）。
4. **必ず `.new` の mtime が新しいことを確認**（早とちり防止＝過去に古い.newをswapした事故あり）。
5. **bafather停止後**(ユーザに依頼) swap: `ssh ... "powershell -File C:\bacopy\_baf_swapeng.ps1"`（GUI/engine停止→lock削除→backup→差替→size検証→`ENGINE_SWAP_OK`）。
6. **ユーザがRDPでGUI再起動**（session0/SSHからChrome起動不可）。
7. 検証: 後述の監視スクリプト。

### 監視スクリプト（bafather `C:\bacopy\`・ローカルにも同名あり）
- `_baf_skipchk.ps1` = skip-refocus発火数 / 実FOCUS数 / TICK-SLOW _perform_switch数・max。
- `_baf_watch.ps1` = TICK-SLOW内訳 + bet/settle/follow活動 + **missed/discarded bets数**（追従取り逃しの主指標）。
- `_baf_tickline.ps1` = tick-block per分(count/max)。
- `_baf_at.ps1 -re ' 11:0[0-9]:'` = 指定時刻のbet/settle/switch/freeze時系列（ユーザ報告時刻と突合用）。
- `_baf_sysres.ps1`/`_baf_chromeage.ps1`/`_baf_defender.ps1` = CPU/RAM/Chrome年齢/Defender除外（CPU逼迫調査用）。
- ローカルの周回監視は `_*_run.sh`（`run_in_background`・sleep間隔×N回で` _*.txt`に追記）。

### ハマりどころ（必ず守る）
- **SSH越し inline PowerShell は `$_`/`$e`/`$c` 等が bash に食われて構文エラー** → **必ず.ps1をscpして `-File` 実行**。今セッションで何度も踏んだ。
- swap前に `.new` の mtime/size 確認（古い.newをデプロイする事故防止）。
- engine genealogy（今日のbafather）: 68698219(53144eベースで一度デプロイ) / 68696844(純06-06) / 68698192(53144e3+LOOP-SLOW) / 68696193(53144e3+TICK-SLOW計測) / **68699438(53144e3+skip-refocus＝現行)**。backupは`bacopy_engine.exe.bak_<stamp>`。
- **本番(bafather)で実験しない**。壊したら即ロールバック。前セッションは実験版を投入しまくってユーザに混乱と恐怖を与えた(反省)。
- Defenderプロセス除外に **chrome.exe/bacopy_engine.exe/node.exe を追加済**(CPU 83→47%・ただしfreezeの主因ではなかった=skip-refocixが本丸)。

---

## 4. 重要な技術的事実（次セッションの判断材料）

- **WS BETにDOM FOCUSは不要**（共有ホストWS `cbcf6qas8fscb222` 1本で全卓BET・`<bet amt>`直書き）。だから予告の卓切替FOCUSは"視覚のためだけ"でスキップ可能（skip-refocusの根拠）。詳細=`DUAL_LINE_AUTO_WS_BET_COMPLETE_2026-06-06.md`。
- **追従(follow)は各受け子のローカル計算**（自分の勝敗でP→B→P継続/終了）、**SEQ額もローカル**。だから2台同時オートでも**BETするハンド・利益が異なるのは正常**（初回NOWはVPS共通だがfreezeで取り逃すとズレる）。
- 否定済みの仮説（freezeの主因ではない）: centering頻度(recenter=30無効)・Chrome経時劣化(新品でもfreeze)・Defender CPU(除外でCPU半減もfreeze継続)・auto/manual(user02もオート)。**真因は`_perform_switch`の同一卓繰り返しFOCUS**で確定。
- **残freeze**: 新卓への"初回"FOCUS(6-18s)とFOCUS失敗卓の再試行は残る（取り逃しは0なので実害小）。さらに詰めるなら「WSモードで予告FOCUS自体を省略」or「FOCUS失敗卓の時間throttle」。

---

## 5. 関連ドキュメント
- `SESSION_HANDOFF_2026-06-10_REVERT_TO_0606_FAST.md` — 速度問題の完全な調査経緯(§3失った機能/§4.5-4.7真因特定/§5操作)。**最も詳しい**。
- `DUAL_LINE_AUTO_WS_BET_COMPLETE_2026-06-06.md` — 全自動WS BETの仕様本体(データフロー/env/トラブルシュート)。
- メモリ `project_dual_line_slowflash_is_0606_regression_2026-06-10` — 2層の遅さ・skip-refocus修正の要約。
- `DISTRIBUTION_BUILD_RUNBOOK.md` — 受け子インストーラ再ビルド/配布手順。

---

## 6. 次セッション最初の一手（推奨）
1. 本書 + `REVERT_TO_0606_FAST.md`§3 を読む。
2. `git show 1d323f2 -- dual_line_live_executor.py` で HOLD freeze修正の差分を見る。
3. `_rev53144/dual_line_live_executor.py` に適用 → ビルド → swap(ユーザ停止依頼) → 再起動依頼 → `_baf_watch.ps1`で「取り逃し0/freeze低水準が維持されているか」確認。
4. OKなら次のcommit(`fa66b96`一部)へ。1つずつ。
