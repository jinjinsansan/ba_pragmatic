# セッション引継ぎ書 — デュアルライン v4(10パターン) 別モード実装

作成: 2026-05-30 / ブランチ: `feat/dual-line` / 前任 Claude → 次任 Claude Code 向け

---

## 0. 最初に読むもの（順番厳守）

1. **本ファイル**（最新の正確な状態 = 最優先）
2. `PLAN_DUAL_LINE_V4_10PATTERN.md`（全体計画・5フェーズ・**安全なデプロイ順序**）
3. メモリ `project_dual_line_forward_oos_2026-05-22`（v3 はエッジ維持中の本番候補。**v4 を「no-edge」と混同するな**）
4. メモリ `project_phase3a_deploy_handoff_2026-05-30`（参考。ただし誤記あり→**本ファイルが正**）

> ⚠️ **前任の重大な反省（次任は鵜呑みにするな）**: メモリ `project_phase3a_deploy_handoff_2026-05-30.md` に「Phase 3b を 18:15 デプロイ完了・ASAR_SWAP_OK・app.asar=326830」と書いた箇所があるが **すべて誤り**。実際は **Phase 3b は bafather 未デプロイ**。前任は並列ツール呼び出しの連鎖キャンセルを「成功」と誤認した。**bafather 実測 = app.asar 203913 bytes / 2026-05-28（＝旧GUIのまま）**。数値は本ファイルか実機確認のみ信用すること。

---

## 1. プロジェクト概要（30秒）

- **bacopy** = バカラ「デュアルライン」自動BET。友人の罫線予想ロジック(`SPEC_DUAL_LINE_MATCHING.md` / 実装 `dual_line_logic.py`←`dual_line_match.py`)で Stake Pragmatic Play のライブをWS観察→パターン一致でBET。
- 構成: **VPS**(210.131.215.116, dry-run並走bot+API) + **bafather**(162.43.83.54, 本番GUI = Electron + Python engine EXE)。
- **v3 = 6パターン** = 実績ある本番候補(forward OOS 勝率51.7%, Z=+4.08, エッジ維持中)。**絶対汚さない**。
- **v4 = 10パターン** = 分母(BET数)増の**別モード候補**。backtest選定済・forward未検証。**採用判断はユーザがする**(前任が「運用するな」と示唆して叱られた。判断はユーザの領分)。

---

## 2. ゴールと全体像（5フェーズ）

現行v3を温存したまま、v4を別モード並走→ライブ比較→**ユーザが採用判断**できる状態を作る。

| Phase | 内容 | 状態 |
|---|---|---|
| 1 | VPS v4 dry-run並走(BETなし・別state・Telegram) | ✅ 完了・VPS稼働中 |
| 2 | VPSが v4 decision を master API へ mode=v4 配信(env gated) | ✅ **有効化済(5-30 17:55, BACOPY_V4_PUBLISH=1, bot稼働)** |
| 3a | engine に mode フィルタ(`set_dual_mode` IPC / `BACOPY_DUAL_MODE` env 既定v3) | ✅ コード+**bafatherデプロイ済** |
| 3b | Electron GUI に「PATTERN MODE v3/v4」プルダウン | ✅ **完了(5-30 17:29 デプロイ→GUI再起動→プルダウン目視確認済)** |
| 4 | SEQ開始額UI($1/$3/$6) | ❌ 未着手 |
| 5 | forward検証(1000-2000シグナル)→採用判定 | ❌ 未着手 |

---

## 3. コミット履歴（`feat/dual-line`・全てローカル・**未push**）

```
a120da8 Update v4 plan: Phase 3a deployed, Phase 3b code done (not built)
7d0c2c9 Phase 3b: GUI pattern-mode dropdown (v3/v4) for dual-line
fd0284e Phase 2: add env-gated v4 (10-pattern) decision publish patch (default OFF)
10053d2 Update v4 plan: Phase 3a done, safe deploy order documented
5ae2ea5 Phase 3a: GUI bot dual-mode filter (v3/v4), default v3 (safe)
51c6c78 Add v4 (10-pattern) definition + plan + VPS shadow-tracking patch (Phase 1)
```
- `fd0284e` のメッセージ先頭に余計な `@` 混入(無害)。
- `copytrade_gui/src/` はクリーン(全コミット済)。未追跡の大量ファイルは本件無関係のログ/レポート類。

---

## 4. 実装詳細（コードの場所）

### Phase 1（完了・VPS稼働中）
- `dual_line_match.py`: `LIVE_SIGNAL_PATTERNS_V4`(10パターン frozenset)。
- `_v4_patch.py`: VPS bot へ v4 shadow 並走を注入する**冪等パッチ**。`_v4_track()` を足し、6パターン経路非干渉で別state(`dual_line_v4_state.json`)に独立集計+Telegram。**VPS適用済・稼働中**。
- v4 の10パターン: sansan|telecho|P / niconico|niconico|B / telecho|niconico|P / niconico|nikoichi|B / telecho|niconico|B / niconico|dragon|B / niconico|nikoichi|P / telecho|nikoichi|B / telecho|telecho|B / telecho|nikoichi|P
- ※v3の `niconico|dragon|P`(50.5%基準未満)は v4 から外れる(単純上位集合ではない)。

### Phase 2（コードのみ・未デプロイ・未有効化）
- `_v4_publish_patch.py`: VPS bot に `_publish_v4_decision()` を足す**冪等パッチ**。`_v4_track` の signal確定直後に呼ぶ。
  - `BACOPY_V4_PUBLISH=1` の時のみ master `/api/decisions` へ配信(`source=dual_line_vps_v4_10pattern` / `mode=v4` / `did=dl_v4_*`)。
  - **env未設定/≠1 なら即return=従来と完全同一挙動**。v3の `_publish_live_decision`(source=dual_line_vps_whitelist6)に非干渉。
  - ローカルで inject+冪等+py_compile+Phase1未適用ガード 検証済。**VPS未適用**。
- ⚠️ 有効化(順序②)は Phase 3a デプロイ後でないと危険(v4の5パターンはv3と共有、mode無視の旧engineに流すと暴発)→Phase 3aは済なので前提クリア。

### Phase 3a（完了・bafatherデプロイ済）
- `dual_line_pragmatic_bot.py`: `V4_PATTERNS`(L127) / `self.dual_mode`(L435, env `BACOPY_DUAL_MODE` 既定"v3") / stdin IPC `set_dual_mode`(L1007)→`_set_dual_mode()`(L1021) / `_handle_decision`(L2844)で `decision.mode` が選択モード一致時のみ該当whitelistでBET(mode未指定=v3後方互換)。
- 既定v3で挙動不変。bafather engine = 新EXE 224546475 bytes デプロイ済。

### Phase 3b（★次作業 — コード+ローカルビルド完了、bafather未デプロイ）
3ファイル計13行(commit `7d0c2c9`):
- `copytrade_gui/src/renderer/index.html`(L296付近 dualLineMoneyGroup先頭): `#inputDualMode`(v3/v4)プルダウン。
- `copytrade_gui/src/renderer/app.js`: `dual_mode` を4箇所 — DEFAULT_SETTINGS(L702 既定'v3') / モーダル読込(L1021) / startBotFlow config(L464) / 保存ハンドラ(L1162)。
- `copytrade_gui/src/main.js`(L733付近): dual-line spawn時のみ `childEnv.BACOPY_DUAL_MODE=(dualMode==='v4')?'v4':'v3'`。
- 全て `node --check` 通過。既定v3で挙動不変。

---

## 5. ✅ Phase 3b の bafather デプロイ = 完了(5-30 17:29) → GUI再起動+目視検証待ち

### デプロイ実績（2026-05-30 17:29 実施・実機確認済）
| 場所 | 対象 | 値 | 意味 |
|---|---|---|---|
| bafather | engine EXE | 224546475 / 16:38 | ✅ Phase 3a 済(無干渉) |
| bafather | app.asar | **204214 / 17:27** | ✅ **3bデプロイ済** |
| bafather | バックアップ | `app.asar.bak_20260530_172916`(203913) | ✅ 旧GUI退避(ロールバック可) |
| bafather | プロセス | GUI=0, engine=0 | ⏸ 停止中=RDP再起動待ち |
| ローカル | ビルド済app.asar | 204214 / 17:09 | ✅ 3b入り(検証済) |

**実施ログ**: SCP(古い5/24残骸169751を204214で上書き)→`.new`サイズ204214検証→`_swap_asar_phase3b.ps1`(204214ガード付)実行→`AFTER_STOP gui=0`→`BACKUP=...172916 203913`→`DEPLOYED=204214`→`ASAR_SWAP_OK`→再確認で resources/app.asar=204214。
**残**: ① ユーザRDPでGUI起動 ② 設定画面に「PATTERN MODE v3/v4」プルダウン目視 ③ 既定v3で挙動不変確認。

### (参考)デプロイ前の状態
- bafather app.asar は 203913 / 2026-05-28(旧GUI)だった。`C:\bacopy\app.asar.new` に5/24の古い残骸(169751)が居たため新ビルドで上書き必須だった(対応済)。

### ローカル成果物（検証済・そのまま使える）
- パス: `E:\dev\Cusor\bacopy\copytrade_gui\dist\win-unpacked\resources\app.asar`（204214 bytes, 2026-05-30 17:09）
- 検証: asarバイナリを grep し `BACOPY_DUAL_MODE` と `inputDualMode` を検出済=3b確実に内包。asar内パスは `\src\main.js` `\src\renderer\app.js` `\src\renderer\index.html`(バックスラッシュ区切り)。
- 再ビルドする場合: `cd copytrade_gui; npx electron-builder --dir --config.win.signAndEditExecutable=false`（package.jsonの`build`は前段に`python scripts/minify_js.py --inplace`があるがminifyは任意。直接呼べばソース改変なし）

### デプロイ手順（GUI停止必要・app.asarのみ差替・engine EXEは触るな）

**Step 1: SCP（無干渉）** ※Bashツールで `dangerouslyDisableSandbox: true` 必須
```
scp -i /c/Users/USER/.ssh/laplace_vps -o BatchMode=yes \
  /e/dev/Cusor/bacopy/copytrade_gui/dist/win-unpacked/resources/app.asar \
  "Administrator@162.43.83.54:C:/bacopy/app.asar.new"
```

**Step 2: 差替スクリプトをローカル作成→SCP→`-File`実行**。スクリプト内容:
```powershell
$ErrorActionPreference = 'Stop'
$asar = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources\app.asar"
$new  = "C:\bacopy\app.asar.new"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
if (-not (Test-Path -LiteralPath $new)) { Write-Output "ABORT: .new missing"; exit 1 }
$expected = (Get-Item -LiteralPath $new).Length
Get-Process BACOPYRECEIVER -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Get-Process bacopy_engine -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3
$gc = @(Get-Process BACOPYRECEIVER -ErrorAction SilentlyContinue).Count
Write-Output ("AFTER_STOP gui=" + $gc)
if ($gc -gt 0) { Write-Output "ABORT: GUI still running"; exit 1 }
$bak = $asar + ".bak_" + $stamp
Copy-Item -LiteralPath $asar -Destination $bak -Force
Write-Output ("BACKUP=" + $bak + " " + (Get-Item -LiteralPath $bak).Length)
Copy-Item -LiteralPath $new -Destination $asar -Force
$i = Get-Item -LiteralPath $asar
Write-Output ("DEPLOYED=" + $i.Length)
if ($i.Length -ne $expected) { Write-Output "WARN size mismatch"; exit 1 }
Write-Output "ASAR_SWAP_OK"
```
SCP: `scp -i <key> -o BatchMode=yes _swap_asar_phase3b.ps1 "Administrator@162.43.83.54:C:/bacopy/_swap_asar_phase3b.ps1"`
実行: `ssh -i <key> -o BatchMode=yes Administrator@162.43.83.54 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\bacopy\_swap_asar_phase3b.ps1'`
（前任は実行出力がpipeに出ず確認に手間取った。出力が出なくても BACKUP / DEPLOYED 行が出れば成功。別途 read-only スクリプトで app.asar サイズ=204214 を確認するのが確実）

**Step 3: ユーザにGUI再起動を依頼**（RDPで BACOPYRECEIVER 起動。SSHから起動は不可）。

**Step 4: 検証** — 設定画面 dual-line セクションに「PATTERN MODE: v3/v4」プルダウンが出るかユーザ目視。

### ⚠️ デプロイ時の必須注意（前任が実際に踏んだ罠）
1. **SSH越しPowerShellは複雑なら .ps1 をSCPして `-File` 実行**。Bash経由のダブルクォート `-Command` は引用符が壊れ mojibake エラー(前任は何度も失敗)。単純確認だけ `'powershell -NoProfile -Command "..."'`(全体をシングルクォートで囲む)で時々通るが不安定。**確実なのは .ps1 ファイル方式**。
2. **SCP/SSH は `dangerouslyDisableSandbox: true` 必須**(サンドボックスだと到達しない)。
3. **GUIプロセス名 = `BACOPYRECEIVER`**(package.json productName)。engine=`bacopy_engine`(2個が正常)。
4. **`deploy_bafather_patch.ps1` を使うな** — app.asar と engine EXE を両方 古い `app_new.asar`(5/28) で上書きする罠。app.asarのみ手動差替が安全。
5. **engine EXE は触らない**(Phase 3a で正224MB版差替済)。今回 app.asar だけ。
6. **並列で大量ツールを投げるな** — 1つ失敗で全連鎖キャンセル(前任が4回やらかし、ユーザを怒らせた)。**1ステップずつ**。
7. **cwd が `cd` でずれる罠** — `git -C /e/dev/Cusor/bacopy` など絶対パス使用。前任は cwd が copytrade_gui にずれて全空振りした。

---

## 6. その先（Phase 2 有効化済 → 次は forward 蓄積→採用判定）

ユーザの採用判断のための土台。順序:
1. ✅ **VPS Phase 2 有効化(5-30 17:55 完了)**: `_v4_publish_patch.py` 適用(`_publish_v4_decision`=2, py_compile OK, バックアップ `dual_line_pragmatic_bot.py.bak_phase2_20260530_175238`)→ `/opt/bacopy/.env` に `BACOPY_V4_PUBLISH=1` 追記(バックアップ `.env.bak_phase2_20260530_175249`)→ wrapper再起動(`_restart_dual_bot_phase2.sh`)。bot=PID 3251587, env反映確認, v3 state(signals=2392,pnl=+$4830)+v4 shadow(46) 保全。**v4 decision が master へ `source=dual_line_vps_v4_10pattern`/`mode=v4` で流れ始めた**。
2. GUI で v4 選択時に v4 decision 受信可。v3 選択中は受けても Phase3a mode フィルタで捨てる(=BETしない=安全)。
3. dry-run で v4 を 1000-2000 シグナル蓄積(現 v4=46)。
4. **Phase 5**: forward 勝率を v3 と比較。v4 が勝率≥51.5% & $/BET>0 維持なら**ユーザが採用判断**→実BET運用へ。
5. **Phase 4**(任意): SEQ開始額UI($1/$3/$6)。資金管理はモードと直交。

### Phase 2 有効化前の残課題3点 = ✅ 全てコード検証済(5-30)
- ① amount: 受信側 `dual_line_pragmatic_bot.py` L3295 `bet_amount=self.money.next_bet()` でローカルSEQサイズ。v4 publishの`amount=unit`(nominal)は**無視**=Phase4非依存で安全。
- ② 会計分離/starvation: master `/opt/bacopy/bacopy_api.py` の `/api/decisions/wait` は `get_by_status(...ORDER BY received_at DESC, limit=50)` で**新しい順pendingをリスト返し**。新規v3は常に先頭で配信→未ack v4が溜まっても古い側に沈むだけで**v3をブロックしない**。v3モード中v4は実行/決済されず**会計汚染ゼロ**。残=未ack v4 pending行のDB蓄積(無害cosmetic、必要なら将来 skipped_mode ack をエンジン追加)。
- ③ v3中のv4 drop: L2860-2863 modeフィルタ `if dmode != active_mode: return` + poll の `recent_ids` dedup で暴発も再処理churnもなし。

### ⚠️ VPS bot 操作の罠(本セッションで踏んだ)
- **`pkill -f`/`pgrep -f` 自己マッチ**: SSH `bash -c "...パターン文字列..."` の自分のシェルがマッチして kill され SSH が exit 255 で落ちる。**kill/再起動ロジックは .sh ファイルにSCPして `bash file.sh` で実行**(呼び出しcmdlineにパターンを含めない)。スクリプト内 pgrep は `[d]ual...` bracketトリック必須。`_restart_dual_bot_phase2.sh` が安全な実装。
- bot管理 = systemd外。`/opt/laplace2/run_dual_line_bot.sh`(while-true自動再起動ラッパー, 起動時 `source /opt/bacopy/.env`)。env変更は .env 追記+ラッパー再起動で反映。lock=`/tmp/dual_line_bot.lock`。

---

## 7. 接続情報

- bafather(本番GUI): `Administrator@162.43.83.54`, key `C:\Users\USER\.ssh\laplace_vps`
  - GUI/engine 配置: `C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources\`
  - 一時作業: `C:\bacopy\`
- VPS(dry-run bot+API): `root@210.131.215.116`, 同 key。bot=`/opt/laplace2/`, API=`/opt/bacopy/`
- engineデプロイ実態 = **ローカルPyInstallerビルド→EXE配置**(`python -m PyInstaller --noconfirm --clean build/bacopy_engine.spec` → `dist/bacopy_engine.exe` → bafatherへ)。`_deploy_bafather.ps1` のリモートビルド方式は使われていない(spec名 `bacopy_engine_bafather.spec` 不一致で止まる。前任が誤って作ったがローカルビルドが正しい経路と判明)。

---

## 8. ロールバック

- GUIが起動しなくなったら: bafather `...\resources\app.asar.bak_<timestamp>`(Step2で生成) を `app.asar` に戻す。
- engine: `...\resources\engine\bacopy_engine.exe.bak_20260530_163845`(224MB=Phase3a直前) 等多数。

---

## 9. 絶対原則（メモリより）

- v3(6パターン)の実績経路を汚さない。各変更は backup + py_compile/node --check + 段階導入。
- v4 を「no-edge」と呼ぶな(no-edge結論は友人BET/汎用罫線の話。v4はforward検証前のエッジ候補)。
- **採用/運用の判断はユーザがする。** こちらは安全に選択肢を用意するまで。
- 勝手に休む/区切る提案をしない(ユーザが明示しない限り作業継続)。
- 日本語で応答。

---

## 10. v4 ライブテスト実施 + バグ2件修正 (2026-05-30 夜)

ユーザが v4・最小$1 で実BETライブテスト実施。3症状報告: ①シグナル確定でも間に合わないBET ②黄→赤/青→すぐ黄 ③ハンド終了後も黄のまま〇×進まない/負けても×出ない。

### バグA: v4 決済欠落 (症状②③根本原因) → ✅ 修正・実機検証済
- **原因**: Phase2 publishパッチが「配信」のみで「決済」未実装。v3は VPS(`_publish_live_decision`→`_post_decision_settlement`)が `dl_vps_` 行を settled post→master done→bafather照合pollがGUI〇×更新。v4は publish のみで `dl_v4_` 行が永久 `processing(bet_sent)`→GUI〇×凍結・NOW lock(65s)滞留で後続BET阻害。
- **証拠**: VPS決済post v3=10件/v4=0件。WSは生(GAME-ID活発)。同卓でv3は正常決済。
- **修正**: `_v4_settle_patch.py`(冪等)を VPS に適用。`_v4_track`が publishのdidを `_v4_pending` 保持→結果観測時 `_settle_v4_decision` が `/api/decisions/{did}/result` へ settled(done) をpublishと同env でpost。`mode_tag=v4` 識別。バックアップ `*.bak_settle_20260530_183537`。bot再起動で反映済。
- **検証**: 18:47 `dl_v4_73e784` が processing→**done**(outcome=T)。GUIで T表示・黄色枠解除を目視確認。**症状②③解決**。
- master `mark_result`(bacopy_db.py L325): pending(未BET)の settled は status据え置き=正しい。processing は `has_local_bet_sent`(既存phase=bet_sent)で done遷移。

### バグB: preposition 黄色枠の残留 (症状④) → ✅ 修正・engineデプロイ済
- **原因**: `dual_line_live_executor.py` `_maintain_assist_focus_hold`(黄色preposition overlay)が、NOW-bet hold と違い**期限切れ時に `clear_manual_assist_overlay` を呼ばない**(対称漏れ)。maintain再スタンプでJS-TTL不発→明示clear無しで黄色枠stuck。preposition切替時も前卓overlay未clear。
- **修正(2箇所)**: `_start_assist_focus_hold`(切替時 前卓clear, L5416) + `_maintain_assist_focus_hold`(期限切れ時clear, L5621)。engine EXE再ビルド(224547502)→bafatherにEXEのみ差替(`_swap_engine_yellowfix.ps1`, ENGINE_SWAP_OK 19:36, バックアップ `bacopy_engine.exe.bak_20260530_193658`=224546475)→GUI再起動。app.asar(204214)不変。

### 残課題
- 🔴 **チップ$2過剰着弾バグ(2026-05-30 夜 発覚, 実マネー)**: `planned=$1.00` なのに `observed_stake_delta=$2.00`。ログ `[PARTIAL-BET] amount mismatch: planned=$1.00 observed_stake_delta=$2.00`+`[DECISION] partial local bet amount: ... planned=$1.00 actual=$2.00`。チップクリックが$1のつもりで$2を置く間欠バグ(cached座標の再クリック/二重登録疑い, checkpoint「repeated chip clicks reuse cached coordinates」由来)。SEQ unit/planは正しく$1(mode=small1 unit=1.0)。エンジンは mismatch 検知し actual=$2 を会計反映(取り逃しではない)。**私の修正(決済/黄色枠)とは無関係=既存バグ**。要調査=`dual_line_live_executor.py` のチップクリック数決定ロジック。発覚後ユーザがv4テスト停止。
- **症状①(skipped_not_prepared)**: v4シグナルが preposition外のFast卓(賭け窓~13s)で点灯し窓が閉じてから到着→見送り。マルチ卓・単一executorの固有制約でv4高頻度ゆえ顕著(新規バグではない)。決済修正でNOW lock滞留分は解消。実用化にはpreposition改善要。
- **stuck `dl_v4_2e159333a0dd494f`**: 修正前の実$1 Banker(game 14562498619)が `processing` のまま=未会計の実BET1件。lock期限切れ済で無害。要クリーンアップ。
- **未commit**: `dual_line_live_executor.py` の黄色枠修正(L5416/L5621)はローカル編集済・未commit。`_v4_settle_patch.py` 適用はVPSのみ(ローカルには_v4_trackが無い=Phase1パッチ産物)。

### 学び(操作)
- `pkill -f`/`pgrep -f` 自己マッチ罠→kill/再起動は .sh をSCPして実行。PowerShell `-Command` クォート崩れ多発→**必ず .ps1 を -File 実行**。GUI名=`BACOPYRECEIVER`/engine=`bacopy_engine`、EXE差替はengine停止必須。

---

## 11. チップ$2過剰着弾バグ = 真因確定+④修正デプロイ済 (2026-05-31 未明)

### 真因(実機ログ19:10で確証, game 14563248719)
- `_js_click_bet_in_container`(L5682) の**クリック・フォールバック・カスケードが二重ディスパッチ**。
  text-locator `.click(force=True,timeout=1800)` が**クリック発射後に**1800msタイムアウト→例外。except が次方式へフォールスルー→tagged-locator が**同じBETゾーンを再クリック**。
  → 同一ゲームに `<lpbet>` が**2回**送出(19:10:06,245 と 19:10:07,397)→ stake_delta=$2($1計画)。
  `[CLICK-BET] click=1/1` は計画反復数で物理クリック数ではない(誤認の元)。
- **これが当初の「カスケード二重ディスパッチ」仮説の実体**。①②(過少/ゼロ)は同じtimeoutが「発射前に外れて次も外す」逆向き故障。

### ④修正(commit 7c4903b・feat/dual-line ローカル・未push)
- `_js_click_bet_in_container` に **`_bet_already_landed()` ゲート**追加(4箇所)。各フォールスルー再クリック前に「このgame_idの送出`<lpbet>`がクリック開始後に出たか」を最大`BACOPY_CLICK_BET_LANDED_WAIT_MS`(既定1200ms)待って確認→出ていれば**再クリックせず return True**。
- `expected_game_id` 空なら無効=レガシー/単一卓は挙動不変。マルチロビーは送出lpbet=自分のBETのみ&単一executor1ゲームなのでgid一致で誤検知なし。py_compile OK。
- 黄色枠修正(L5416/L5621)はHEAD在=再ビルドで巻き戻らない事を確認済。

### デプロイ実績(2026-05-31 21:18頃のPCローカル時刻)
- ローカル再ビルド: `dist/bacopy_engine.exe`=**224547652**(PyInstaller 6.19.0, tbb12.dll警告は既存無害)。
- bafather swap: `_swap_engine_doublestakefix.ps1`(=yellowfix版の再利用)。`ENGINE_SWAP_OK`, DEPLOYED=224547652。
- バックアップ: `...\engine\bacopy_engine.exe.bak_20260530_211838`=224547502(旧黄色枠fix版, ロールバック先)。
- swap前状態: engine=0/gui=4(=$2バグ後にengine停止済、BET非進行で安全だった)。

### ④の追加修正: ガード待ち時間 1200→2500ms (commit 5e1df7c, 2026-05-31 未明)
- **デプロイ後もv4ライブで$2が2回再発**(22:15 game14565616419 / 22:51 game14566058619, ともに$1→$2)。
- 原因=**ガードのlpbet待ちが短すぎた(タイミングrace)**。22:15: text-locatorが22:15:45.950でtimeout→ガードが1200ms待って47.172で諦め→**1発目のlpbetは47.486(raiseの1.54s後)に到着**=0.31s遅れで取り逃し→tagged-locatorが再クリック→49.381に2発目lpbet→$2。
- lpbetスニフ遅延は **0.7〜1.54s** とばらつく。1200msでは不足。
- **修正**: `BACOPY_CLICK_BET_LANDED_WAIT_MS` 既定 1200→2500ms (commit 5e1df7c)。**さらに再ビルド不要で即適用**するため bafather `resources\.env` に `BACOPY_CLICK_BET_LANDED_WAIT_MS=2500` 追記済(バックアップ `.env.bak_20260531_014001`)。デプロイ済engine(224547652)が**bot再起動時**にこの値を読む。
- **適用にはGUIでbot停止→起動が必要**(buildSpawnSpec が loadDotEnv を再読込)。

### 4時間ライブ集計(21:18→01:17)= 実態
- クリック到達27 / 確定15 / 失敗(未着弾)15 / **PARTIAL-BET不一致10** / ガード作動40 / Fast窓スキップ52。
- PARTIAL内訳: 22:xx=`$1→$2`過大×2(上記race)、**00:xx=`$2→$1`過少×8**(SEQが$2=$1×2枚に上がり片方が乗らない=③の実証)。
- ガードの "NOT re-clicking" 分岐は00:24/00:33/00:39等で**実際に二重を阻止**(効いてはいる)。

### ★決済根治: VPS結果駆動の決済フォールバック (2026-05-31, commit 1241a65)
- **問題**: bafatherはSEQをローカルのハンド観測(`_settle_confirmed_decision_from_hand`, game_id厳密一致)で決済。マルチロビーで結果取りこぼし→decisionが`settlement_timeout=900s`まで`processing`凍結→**SEQ停止→未追跡実BET累積**(9:32/9:37実例、両方WINなのにSEQ$6段で凍結=過大エスカレーション)。
- **検証**: VPS botログで、詰まった2件含む全decisionを**VPSが~30秒で確実に決済**(`[v4-settle] posted settled ... WIN/LOSE`)。`mark_result`(bacopy_db.py L328-351)でbafatherが`bet_sent`済みならVPS done postで**status=done+outcome付きresultに上書き**=masterが保持。
- **修正(commit 1241a65, dual_line_pragmatic_bot.py)**: `_settle_pending_from_master()`が`GET /api/decisions?status=done`をポーリングし、在庫BET(送信済・未決済)のdidをVPS outcome+**ローカルactual_amount**で決済(`_apply_master_settlement`=ローカル決済をミラー、money.apply_result/GUI resolution更新、**master再postなし**)。`settlement_posted`+popで二重防止。`_flush_pending_decision_results`冒頭で呼ぶ。throttle `BACOPY_MASTER_SETTLE_POLL_SEC=5`、kill-switch `BACOPY_MASTER_SETTLE_ENABLE=1`。詰まり900s→~30sで解消。v3/v4両対応。
- **要engine再ビルド+デプロイ+GUI再起動**。詳細メモリ [[project_dual_line_settlement_freeze_2026-05-31]]。

### ③過少($2→$1)の真因特定 (2026-05-31 01:47, game14568239019, Baccarat 6)
- $2=$1×2枚の多チップBETで、**2クリックなのにlpbet=1回・卓確定amount=1**=$1しか乗らず。
- 内訳: 1枚目=text-locator(信頼click)OK→着弾。**2枚目=cached mouse.click が1枚目の216ms後に発火**(34,155→34,371)。その時1枚目のlpbet(34,398)すら未確定→**2枚目がPragmatic側で吸収/無視**。
- ＝「クリックが外れた」ではなく**連打が速すぎて2枚目が登録されない**。00:xxの過少8件と同型。
- **対処(再ビルド不要)**: チップ間待ち `BACOPY_CLICK_BET_INTER_CLICK_MS` 既定45→**400ms**を bafather `resources\.env` 追記(バックアップ `.env.bak_20260531_021746`)。1枚目確定(lpbet~243ms)後に2枚目を打つ。**bot再起動で適用**。
- ⚠️ これはタイミング仮説。効かなければ次は**コード修正=各チップを毎回trusted text-locatorで打ち直す(cached mouse.click廃止)＋per-click lpbet確認**(L6443/L6471の click_cached_side_once 経路)。要再ビルド。
- 補足: 04:xx近辺の$4(4枚)は "ignore settled result without local bet confirmation"=多チップほど着弾困難。

### ③多チップ過少の真因確定＋修正 (2026-05-31 03:15→04:20, commit 34b07a8)
- 実機$4 BET(game14569312119, $1×4枚): 1枚目=locator.click着弾、**2〜4枚目=`click_cached_side_once`が `frame.page.mouse.click(283.3,347.5)` をframe相対座標で実行**→OOPIFのオフセット(約+147,+100)未加算でベットゾーンを外し**全空振り**→lpbet 1回のみ→$4が$1着弾。**$2→$1/$4→$1の過少すべての真因**。400ms遅延は無関係(タイミングではなく座標)。
- **SEQ自体は正常**(エンジンは$4を正しく計画・送信。GUI/Stake履歴の$1/$2は着弾ミスの結果)。
- 修正3点(commit 34b07a8): ① `click_cached_side_once` でlocator系キャッシュ座標にiframeオフセット加算→page座標化(高速クリック維持・多枚数$8/$85/$333にスケール) ② `_lpbet_count_by_gid`(送出lpbet=着弾チップ数) ③ ループ後に満額検証+不足リトライ(`BACOPY_CHIP_VERIFY_SETTLE_MS`=2500ms待ち→landed<planned なら不足分再クリック。**均一プラン($1×N)のみ自動リトライ**=誤額過大防止、混在プラン($5+$1)はログのみ。`BACOPY_CHIP_VERIFY_RETRY_ROUNDS`=2)。
- engine再ビルド(224549722)→bafather swap(`ENGINE_SWAP_OK` 04:20, バックアップ`bacopy_engine.exe.bak_20260531_042055`=224547652)。env(2500ms/400ms)は`.env`常駐で引継ぎ。

### 残作業(次任)
1. 🔴 **GUI再起動で224549722適用**→ v4ライブで **$2/$4/$6が満額着弾**するか確認。ログ照合: `[CLICK-BET-VERIFY] full amount landed` / lpbet回数=チップ枚数 / `[PARTIAL-BET]`が出ないこと。混在プランで過少が残るなら次は denom 別検証(lpbetに額が載るか要調査)。
2. 🔴 **①②③ 本丸=per-click着弾検証**(過少$2→$1, クリックtimeout/miss根治, 多チップの確信)。**今や主問題**。
   - 候補: ① locator click timeout(`BACOPY_CLICK_BET_LOCATOR_TIMEOUT_MS` 既定1800→延長で空振り/フォールスルー減) ② mouse fallback座標精度(betzone座標を使う,geometry混在排除) ③ チップ1枚ごとlpbet/stake delta確認→外れたら同一クリックのみ再試行,規定回数で全体中止。
3. ① Fast卓の窓切れ(open_to_vps_cap~16.6s > window13s)。対象からFast卓除外 or preposition先行構え拡大。
4. stuck `dl_v4_2e159333a0dd494f` / `dl_v4_0c79e8532bd34e43` クリーンアップ。
5. commit 7c4903b / 5e1df7c は未push。
