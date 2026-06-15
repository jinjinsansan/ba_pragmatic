# 引継ぎ書：次セッション(hh88 仕上げ — オーバーレイ間欠 / 診断engine掃除 / HKD換算)
作成: 2026-06-15 末 / 前提: hh88 実機bet **受理+勝利 達成済み**(bafather)。残るは仕上げ3点。

> ★まず読む: `SESSION_HANDOFF_2026-06-15_HH88_MULTIPLATFORM.md` 冒頭「最新状況」と §11。本書は次にやる事だけを抜き出した実装ガイド。

---

## 0. 現状サマリ(2026-06-15 末)
hh88(hh88vip5.com / 口座 jpa0001)で、現行GUI+エンジンの **自動WS BET が受理され勝利**するところまで到達。Stake は全経路 `IS_HH88` ゲートで無変更。
- 動かすまでに潰した真因3つ(全てデプロイ済): ①route_web_socket→**CDP注入WSブリッジ** ②金額: $0.2が0.2HKDで最低2HKD割れ無言拒否→**hh88はamtをHKDへ×10スケール+最低2HKD** ③uId取り違え(共有チャネルホスト汚染)→**`BACOPY_HH88_UID`(ppc)を`BACOPY_USER_ID`にピン留め**。
- **branch=`feat/dual-line`**(全push済・最新 `124d916`)。repo=`github.com/jinjinsansan/ba_pragmatic`。
- **デプロイ元は `_rev53144/`**(repo-root HEADは別物=回帰込み。必ず `_rev53144` をビルド)。

---

## 1. ★タスクC: オーバーレイ/スクロールが間欠的に出ない(最優先・betは正常)
### 症状
hh88 で黄/青/赤の枠・卓へのスクロールが **出る起動と出ない起動がある**。BET自体は常に受理(別経路=CDP注入)。

### 真因(ログで確定済み)
- `[ASSIST-HOLD] frame not found for center target='...'` / `[MANUAL-ASSIST] overlay clear skipped: frame not found`。
- オーバーレイ/センタリングは **`_find_pragmatic_frame()`(executor L5875)** が Playwright の `page.frames` から `/desktop/multibaccarat` フレームを探す方式。
- hh88 の multibaccarat は **`game-iframe-v2?name=PP` の中に入った cross-origin の入れ子 OOPIF**(`client.pragmaticplaylive.net/desktop/multibaccarat/`)。**Playwright が `page.frames` にこのOOPIFを載せる時と載せない時がある(タイミング依存)** → 見つかった起動だけ枠が出る。
- 対照的に **BET送信は CDP(`/json` のtarget列挙→`Runtime.evaluate`)でフレームを直接掴む**ので常に成功している。

### 修正方針
オーバーレイのフレーム取得を **Playwright `page.frames` 依存からCDP/リトライ方式へ**変える(BETと同じ確実な経路に寄せる)。具体案:
- 案A(軽い): `_find_pragmatic_frame` に **短いリトライ/待機**を入れ、hh88時は `page.frames` を数百ms間隔で数回再走査。OOPIF が遅れて attach されるだけなら拾える可能性。
- 案B(本命): hh88時は **CDPの target 列挙**(`client.pragmaticplaylive.net/desktop/multibaccarat` を持つ target)を真実とし、その frame に対して overlay JS を流す。Playwright frame が無くてもCDP経由でevaluateする(既存の `_HH88_WS_BRIDGE_INIT` 注入と同じ土台)。
  - 既に hh88 は overlay対象のタイルDOM(`TileHeight-{qpid}`)を **Pragmaticクライアント側が持つ**(Stakeでも同じクライアント=だから前回は出た)。問題は「どのフレームに evaluate するか」だけ。
- どちらでも `_center_multi_tile`/`_start_assist_focus_hold`/`clear_manual_assist_overlay`(L6483) が使う frame 取得を直す。

### ★ローカルで再現して検証(本番でいきなり触らない)
1. CDP Chrome 9223 起動 → hh88 ログイン(jpa0001/Aabb1122) → ライブバカラ → **マルチエリア(multibaccarat)** を開く。
2. engine をローカルで hh88 モード起動(§4のenv)。ログで `[ASSIST-HOLD] frame not found` が出る/出ないを観察。
3. `curl http://127.0.0.1:9223/json` で multibaccarat target があるのに `page.frames` に出ない状況を確認 → 案A/Bで frame を掴めるよう修正 → 枠/スクロールが安定して出るまで反復。
4. OK後 §3 でビルド→bafather差替え。

### 関係箇所
`_rev53144/dual_line_live_executor.py`: `_find_pragmatic_frame`(L5875) / overlay JS `applyAssistOverlay`(L894) / `_center_multi_tile`・`_start_assist_focus_hold` / `clear_manual_assist_overlay`(L6483) / `[SWITCH] skip focus (ws transport)`(L4904)。

---

## 2. タスク: 診断engine → クリーン版へ
現 bafather engine は **診断ビルド(MD5 `BC066E2`)**: `[HH88-FIRE-XML]`(ボット送信xml) と `[HH88-SENT-LPBET]`(WS.sendフックで`<lpbet>`採取=手動BET生フレーム) の冗長ログ入り。動作はOK。
- C修正と同時に、ログ冗長を抑える(必要なら `[HH88-SENT-LPBET]` は env ゲートで残す)。
- **amt scaling / uId pin は維持**。

---

## 3. ビルド & bafather デプロイ手順(確立済み)
### エンジン(lean ~98MB・必ず `_rev53144` をビルド)
```
cd /e/dev/Cusor/bacopy
cp _rev53144/dual_line_live_executor.py _rev53144/dual_line_money.py _rev53144/dual_line_pragmatic_bot.py ./
python -m PyInstaller build/bacopy_engine.spec --noconfirm --distpath dist --workpath build   # spec の excludes は維持(numpyは残す/pandas等除外=~98MB)
git checkout -- dual_line_live_executor.py dual_line_money.py dual_line_pragmatic_bot.py
certutil -hashfile dist/bacopy_engine.exe MD5
```
※ spec `build/bacopy_engine.spec` の `excludes=['pandas','scipy','numba','pyarrow','matplotlib','IPython','tbb','llvmlite']`(★numpyは camoufox が必要なので残す)。これが無いと224MBに膨れる(現環境にnumpy/pandas等あり)。spec は **git管理外**。

### GUI asar
```
cd copytrade_gui && npx electron-builder --dir --config.win.signAndEditExecutable=false
# 出力: copytrade_gui/dist/win-unpacked/resources/app.asar
```

### bafather 差替え(★SSH越しインラインPSは `$` が食われる→必ず .ps1 を scp + `-File`)
- SSH: `ssh -i C:\Users\USER\.ssh\laplace_vps -o IdentitiesOnly=yes Administrator@162.43.83.54`
- engine: `dist/bacopy_engine.exe` を scp → `C:\bacopy\bacopy_engine.exe.new` → `_baf_engine_swap.ps1` を scp+`-File`実行(GUI/engine停止→backup→swap→size検証)。
- asar: app.asar を scp → `C:\bacopy\app.asar.new` → `_baf_swapasar_u01.ps1` を `-File`実行。
- 差替え後は **ユーザーが RDP で GUI 再起動**(session0からGUI起動不可)。

---

## 4. ローカルE2E 起動env(参考)
```
BACOPY_PLATFORM=hh88 BACOPY_BROWSER=chrome_attach BACOPY_CHROME_CDP_URL=http://127.0.0.1:9223 \
BACOPY_MULTI_LOBBY_MODE=1 BACOPY_MULTI_BET_TRANSPORT=ws BACOPY_ALLOW_WS_BET_TRANSPORT=1 BACOPY_ENABLE_WS_REAL_BET=1 \
BACOPY_MANUAL_ASSIST_AUTO_CLICK=1 BACOPY_MULTI_DIAGNOSTIC_ONLY=0 BACOPY_DUAL_MODE=v3 \
BACOPY_USER_ID=ppc1735343648462 PYTHONIOENCODING=utf-8 \
dist/bacopy_engine.exe dual-line --live --manual-assist --no-vps-poll --money-mode flat --money-unit 0.2 > _hh88_e2e.log 2>&1
```
※ `--money-unit 0.2` + hh88スケールで amt=2(HKD)。`BACOPY_USER_ID=ppc...`(ppc形式)を渡せば uId 学習不要。

---

## 5. bafather 現状 / 参照
- engine = 診断ビルド `BC066E2` / GUI asar = 最新(uId pin+URLボタン+永続化) / `.env`: `BACOPY_PLATFORM=hh88`, `BACOPY_HH88_UID=ppc1735343648462`。
- engineログ: `C:\Users\Administrator\AppData\Roaming\bacopy-copytrade-gui\logs\engine_cli_capture.log`(grepは `_baf_loggrep.ps1` を scp+`-File`)。
- GUI asarパス: `C:\BACOPYRECEIVER_user01\resources\app.asar` / engineパス: `...\resources\engine\bacopy_engine.exe`。
- ロールバック: engine `bacopy_engine.exe.bak_2026...` / asar `app.asar.bak_2026...`(各swapで生成)。
- 正しい uId = `ppc1735343648462`(jpa0001固定・今朝ローカル/今夜bafather一致)。間違いuId(他人/キャッシュ)= `ppc1620028910255` / `...67253234`。

## 6. 既知の制約(Phase3)
- 表示「DAILY TOTAL」/課金 daily_bet_pnl は **$建てのまま**(実betは×10のHKD)。bafather口座 `lselfl` は is_free=課金スキップなので実害なし。正確化は HKD↔USD 換算層(`BACOPY_FX_HKD_USD` 等)で別途。

## 7. オーナー指示(厳守)
- GUIは常に **片側のプラットフォームのみ**(Stake or hh88)・**同時BET不可**(Pragmatic異変検知回避)。STOP→dropdown切替→START で運用。
- bafather は **本番Stake機**。hh88変更は `IS_HH88`/`_plat=="hh88"` ゲートで Stake を壊さない。
