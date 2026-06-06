# デュアルライン 全自動WS BET 完成記録 + トラブルシュート (2026-06-06)

> **これは何**: NOW→自動WS BET→自動決済→SEQ/GUI進行 という「全自動コピートレード」を実機で完成させた記録。
> 将来どこかが壊れたら、**まず本書の該当セクション(症状→原因→確認コマンド→修正点)を見れば直せる**ように書いてある。
> 対象機: bafather (162.43.83.54) / 稼働インストール = `C:\BACOPYRECEIVER_user01\`

---

## 0. 全体像（データフロー）

```
VPS master (master.bafather.uk) ── NOW decision ──┐
                                                   ▼
受け子 engine (dual_line_pragmatic_bot.py)  ── place_bet → send_bet
                                                   ▼
  [オート時] WS transport: _ws_send → ws_proxy → ホストWS(cbcf6qas8fscb222) に <lpbet> 送信
                                                   ▼
  Stake が受理 (stake_delta / GAME-BET-CONFIRM) → [BET-CONFIRMED] 確定台帳に登録
                                                   ▼
  勝敗: executor の直結dga WS が全卓 gameResult(winner/gameId) を受信
        → bot _on_dga_frame → _dga_vps_settle_q → _settle_confirmed_decision_from_hand
        → money.apply_result → GUI resolution(○×/SEQ/ダランベール/PnL)
```

**鍵となる事実**
- マルチプレイ48卓は **共有ホストWS 1本(`cbcf6qas8fscb222`)** が全卓の betsopen も BET も運ぶ（`channel="table-<qpid>"` で宛先指定）。**常時接続＝入場/ローディング不要**。
- BET額は `<bet amt="...">` に直書き → **チップクリック不要＝多チップ問題が構造的に存在しない**。
- 勝敗は **executor 自前の直結dga WS**（headless TLS, Stakeセッション非依存）で全卓取得。マルチプレイ/Speed卓は `_on_new_hand` に winner が来ない（betsopen の hand-end しか来ない）ため、**この dga feed が決済の唯一の勝者源**。

---

## 1. WS BET 送信（多チップ/ローディング問題の解）

### 1.1 真因だった2バグ（封印解除の核心）
過去 WS transport は「`WS send did not prove real Stake balance acceptance`」で封印されていた。真因は **送信ペイロードの2バグ**（単一卓用builderの流用）：

| 項目 | 誤(封印の原因) | 正(実機キャプチャ 2026-06-06) |
|---|---|---|
| `gm`(game module) | `baccarat_desktop` | **`mtb_desktop`**(マルチテーブル) |
| `bc`(賭け先) | 文字 `B`/`P` | **数値 Player=`0` / Banker=`1`** |
| lpbet開きタグ末尾 | `ck="..."  >`(空白2) | `ck="...">`(空白なし) |

### 1.2 正しい lpbet（バイト一致テンプレート）
実機が送る/受理される形（`<bet>`の`amt`はドル直値）:
```xml
<command channel="table-{qpid}"><lpbet gm="mtb_desktop" gId="{gameId}" uId="{userId}" ck="{ms}"><bet amt="{dollars}" bc="{0|1}" ck="{ms}"/></lpbet></command>
```
実例(受理・残高+$1):
```
<command channel="table-spto12bctorobc12"><lpbet gm="mtb_desktop" gId="15051119720" uId="ppc1735318303555" ck="1780730939659"><bet amt="1" bc="1" ck="1780730939659"/></lpbet></command>
```

### 1.3 実装場所
- `dual_line_live_executor.py` → `_build_lpbet_xml()`: gm=`mtb_desktop`(env `BACOPY_LPBET_GM`で上書き可)、bc を `_bc in (B,BANKER,1)→"1" / (P,PLAYER,0)→"0"` に数値化、空白なし。
- `dual_line_live_executor.py` → `_ws_send(table_id, payload)`: 4経路(ws_proxy / worker_bridge / open_send / page_bridge)。chrome_attachでは **`ws_proxy_server.send`(proxy_global)** が成功し、payload は **ホストWS(cbcf6qas8fscb222)** に乗る。成功ログ=`[WS-SEND] ws_proxy_server.send OK` / `_ws_send result={'ok':True,'mode':'ws_proxy'}`。
- `dual_line_live_executor.py` → `send_bet()` の WS transport 分岐: `BACOPY_MULTI_BET_TRANSPORT=ws` かつ `_is_multi_table_ws or ws_proxy` のとき。窓検査(`TRY-BET-WS-CHECK window_open`)後に送信。

### 1.4 検証フック(任意・実BET1回)
`_maybe_fire_ws_test_bet()`(tick先頭, env `BACOPY_WS_BET_TEST_ONCE=1`/`_AMT`/`_SIDE`): money/SEQに触れず `_ws_send` を直接1回だけ叩く。1回で自動disarm。`[WS-BET-TEST] FIRING` → `_ws_send result` で経路と成否を確認。**本番では使わない(env未設定)**。

### 1.5 トラブルシュート: 「BETが着弾しない/受理されない」
1. 生ログ(`engine_cli_capture.log`)で `WS-SENT-RAW tid=cbcf6qas8fscb222 ... <lpbet ...>` を確認。**手動BETでもこのフレームが出る**ので、まず手動で1回打って実機テンプレを採取し、`gm`/`bc`/空白を比較。
2. `_ws_send result` が `{'ok':False,...}` → proxy未確立。`[WS-SEND-PATH] proxy=SET/NONE` を確認。NONEなら multi-lobby 未確立(マルチエリア未入場)。
3. 受理確認は `[BET-CONFIRMED] type=stake_delta` or `[GAME-BET-CONFIRM] amount=N`。出なければ Stake拒否(窓閉じ/額/署名)。`bc`が文字のままだと拒否される(再発時はここを疑う)。

---

## 2. 自動決済（結果ループ＝SEQ/○× を回す）

### 2.1 何を解いたか
オート時、BETは着弾するが **money model(SEQ/ダランベール/○×) が進まない** 問題があった。
- 真因: マルチプレイ/Speed卓は **winner が game-WS の `_on_new_hand` に届かない**（betsopen の hand-end しか来ない）→ `_resolve_prediction`/`apply_result` が一度も呼ばれない。
- 手動アシストは **人間の WIN/LOSE タップが勝者判定を代行** していたので露呈しなかった。
- `billing`(n/PnL/残高=/me/realtime) は Stake残高から独立決済なので動く（**money model とは別系統**。ここが「billingは動くがSEQが凍る」の理由）。

### 2.2 配線（dga勝者 → VPS-NOW確定BETの決済）
`dual_line_pragmatic_bot.py`:
- `_maybe_register_dga_callback()`: **`manual_assist_auto_click`(オート) or env `BACOPY_DGA_SETTLE_FEED`** のとき executor の `set_dga_result_callback(_on_dga_frame)` を呼ぶ（`BACOPY_DGA_LOCAL_SIGNAL` off でも登録）。`_dga_settle_only=True`(signal計算/dga発注はせず勝者→決済のみ)。ログ=`[DGA-LOCAL] dga result callback registered (settle_only=True dga_on=False)`。
- executor `set_dga_result_callback` → `_start_dga_direct_source` → `_dga_direct_loop`(dual_line_live_executor.py): **headless 直結 TLS WS** を `dga.pragmaticplaylive.net` に張り、全卓 gameResult を `_on_dga_frame` に転送(実測164結果/60s/61卓)。ログ=`[DGA-DIRECT] connected + subscribed`。env `BACOPY_DGA_DIRECT_SOURCE`(既定1)。
- `_on_dga_frame()`: winner `c`(P/B/T)+`gid` を取り出し、**pending decision があれば** `_dga_vps_settle_q` に `{tid,qpid,gid,outcome,name}` を enqueue。settle_only時は signal計算をスキップ(`continue`)。
- `_dga_vps_settle_pump()`(メインループで毎回drain、dga mode非依存): `_settle_vps_from_dga()` を呼ぶ。
- `_settle_vps_from_dga()`: `get_confirmed_bet` で pending に confirmed_bet を補完→ `new_hand={"winner":outcome,"gameId":gid}` と `_DgaHandBuf(table_name,qpid_table_id,table_id)` を作り `_settle_confirmed_decision_from_hand(qpid or tid, buf, new_hand, outcome)` を呼ぶ。成功ログ=`[DGA-VPS-SETTLE] settled via dga winner ... gid=... outcome=...` + `[DECISION] local bet settled: ... result=WIN/LOSE/TIE`。
- `_settle_confirmed_decision_from_hand()`(既存): `_decision_matches_hand` で **confirmed_bet の game_id と勝者の gameId が厳密一致**(誤着弾ガード)＋卓ID/名一致 → `apply_result` → `_post_decision_settlement` → GUI `resolution`(decision_id一致で○×)。

### 2.3 効く前提（重要）
- **dga gameResult の gameId と betsopen/bet の game_id は同一 Pragmatic 名前空間**（VPS NOW の signal_game_id 照合が成立しているのが証拠）。だから厳密一致決済が通る。
- confirmed_bet が決済前に pending に付いている必要がある（`_flush_pending_decision_results` の `get_confirmed_bet` + `_settle_vps_from_dga` の自己補完で担保）。

### 2.4 トラブルシュート: 「オートでBETするがSEQ/○×が進まない」
1. `[DGA-LOCAL] dga result callback registered (settle_only=True...)` が出ているか → 出てなければ登録条件(auto_bet_enabled)未成立。`manual_assist_mode auto_bet_enabled=true` を確認。
2. `[DGA-DIRECT] connected + subscribed (N tables)` が出ているか → 出てなければ直結dga WS不通(`BACOPY_DGA_DIRECT_SOURCE=0`になってない確認／ネット)。
3. `[DGA-STAT] gr_frames=...` が増えているか(勝者feed生存。`results_added=0`はsettle_onlyの仕様で無害)。
4. BET後に `[DGA-VPS-SETTLE] settled` が出ているか → 出てなければ `_decision_matches_hand` 不一致(gidズレ等)。`[DECISION] local bet settled` も無ければ未決済。
5. money_status `total_bets` が増えるか（権威データ＝`C:\BACOPYRECEIVER_user01\resources\engine\dual_line_money_state.json` を直読み）。

---

## 3. GUI（BET MODE プルダウン / WIN-LOSE出し分け / LIVE撤去 / DAILY TOTAL）

### 3.1 BET MODE プルダウン（手動↔自動の唯一の切替）
- `index.html` `#inputBetMode`: `dual_line`(デュアルラインアシスト) / `dual_line_auto`(デュアルラインオート)。
- `app.js`: `dual_line_auto` は `isDualLineAssistBetMode=false` → `config.mode='dual_line_auto'`。`dual_line` → `config.mode='dual_line_assist'`。
- `main.js` `buildSpawnSpec`: `isDualLineAuto = mode==='dual_line'||'dual_line_auto'`、`isDualLineAssist = 'dual_line_assist'||'dual_line_manual'`。
  - **両モードとも `--manual-assist`** を push(manual-assistエンジン+オーバーレイ)。
  - **オート**: `BACOPY_MANUAL_ASSIST_AUTO_CLICK=1` + `BACOPY_MANUAL_NO_AUTOCLICK=0` + `BACOPY_MULTI_BET_TRANSPORT=ws` + `BACOPY_ALLOW_WS_BET_TRANSPORT=1` + `BACOPY_ENABLE_WS_REAL_BET=1`。
  - **アシスト**: `BACOPY_MANUAL_NO_AUTOCLICK=1` + `BACOPY_MULTI_BET_TRANSPORT=click` + `BACOPY_MANUAL_CHIP_BASE=1`。
- **手動↔自動の継続**: money 状態は永続(`dual_line_money_state.json`)。停止→モード変更→再起動で SEQ 継続(まっさらにしたい時のみ GUI で新規リセット)。

### 3.2 WIN/LOSE/TIE/HOLD の出し分け
- `app.js`: `manual_assist_mode` メッセージの `auto_bet_enabled||manual_assist_auto_click` で `manualAutoBetMode` を立て、`renderManualAssistPanel` で **オート時は WIN/LOSE/TIE/HOLD を `display:none`**、バッジ `#manualAssistMode` を `AUTO`/`ASSIST` 表示。
- **二重決済の保険ガード**(bot `_handle_manual_assist_command`): `action=="result"` かつ `manual_assist_auto_click` のとき拒否(`[MANUAL-ASSIST] result ignored: auto-bet mode`)。オート中に手動WIN/LOSEを押すと **手動apply_result + 自動apply_result の二重計上でSEQが壊れる**ため。HOLD等は許可。

### 3.3 LIVE Mode 撤去（チェック忘れ事故の根絶）
- `index.html`: `#inputDualLive` は非表示+`checked`固定(app.js互換のため要素は残置)。
- `main.js`: dual-line は **常に `--live`(実BET)**。`BACOPY_DUAL_DRYRUN=1`(true/on/yes) の時だけ `--live` を外す（管理者ドライラン専用）。
- 理由: 受け子は常に実BETしたい。チェック外し→`DryRunBetExecutor`で**1件も賭けてない**事故が起きていた。

### 3.4 DAILY TOTAL（/me/realtime と同値）
- 旧: `app.js` がローカル残高差分(現在残高−本日始値)で計算 → 残高DOM読取不安定で **0 のまま**。
- 新: bot `_poll_bet_history_billing()`(6秒・live時)末尾で `send_msg({type:"daily_total", daily_pnl, daily_date, balance, currency, count})` を GUI に送る。値は `self._billing_daily_pnl`(= `[BILLING-SYNC] posted daily_bet_pnl` = /me/realtime と同値、課金=実現日次損益・入出金除外・JST日切替)。
- `app.js`: `case 'daily_total'` → `_engineDailyPnl=daily_pnl` → `updateSessionDisplay()`。`#todayPnl` は **`_engineDailyPnl` があれば最優先**(残高確認に非依存)、無ければ従来のローカル差分。
- **既知の挙動**: エンジンが live になり最初の課金pollを送るまで(=実質最初のBET後)は、GUIに **古いlocalStorage値**が一瞬残る(例: "10")。初回 `daily_total` 到着で正しい値に置換。気になれば「起動直後は`--`表示」に直せる(未実装・軽微)。

### 3.5 トラブルシュート: 「DAILY TOTAL が 0/古い」
1. ログに `"type": "daily_total"` の送信があるか(`daily_pnl=...`)。無ければ engine が live でない or 課金poll未動作。`[BILLING] new bet`/`[BILLING-SYNC] posted daily_bet_pnl` を確認。
2. 値が /me/realtime と違う → engine の `_billing_daily_pnl` ロジック(L3090〜、baccarat判定/JST日切替/seen重複除外)を確認。
3. GUI に出ない → app.js `_engineDailyPnl` / `case 'daily_total'` / `#todayPnl` を確認。

---

## 4. ビルド & デプロイ手順（再発時の作業）

### 4.1 稼働インストールのパス（重要・間違えやすい）
- engine: `C:\BACOPYRECEIVER_user01\resources\engine\bacopy_engine.exe`
- asar:   `C:\BACOPYRECEIVER_user01\resources\app.asar`
- .env:   `C:\BACOPYRECEIVER_user01\resources\.env`
- money:  `C:\BACOPYRECEIVER_user01\resources\engine\dual_line_money_state.json`
- ログ:   `C:\Users\Administrator\AppData\Roaming\bacopy-copytrade-gui\logs\engine_cli_capture.log`
- ※ `AppData\Local\Programs\bacopy-copytrade-gui\` は **旧install(.old)**。間違えない。

### 4.2 engine ビルド（bafather上）
1. ローカルから `dual_line_pragmatic_bot.py` / `dual_line_live_executor.py` を `scp` → bafather `C:\bacopy\`。
2. bafather で `_build_engine_handend.ps1`(=`Set-Location C:\bacopy; python -m PyInstaller --noconfirm build\bacopy_engine.spec` → `C:\bacopy\bacopy_engine.exe.new`)。
3. GUI停止後 `_swap_engine_receiver01.ps1`(BACOPYRECEIVER_user01 へ swap, .bak保存)。

### 4.3 asar ビルド（ローカル）
1. `cd copytrade_gui && npx electron-builder --dir --config.win.signAndEditExecutable=false` → `dist/win-unpacked/resources/app.asar`。
2. `scp` → bafather `C:\bacopy\app.asar.new`。
3. GUI停止後 BACOPYRECEIVER_user01 へ swap。
- ※ app.asar 単体swapで動く(過去実績)。asar integrity は阻害しない。

### 4.4 一括デプロイ
`_deploy_dualauto.ps1`(GUI停止前提): 停止(冪等)→ engine swap → asar swap → .env の手動ハック削除(`BACOPY_MULTI_BET_TRANSPORT|ALLOW_WS|ENABLE_WS|MANUAL_ASSIST_AUTO_CLICK|MANUAL_NO_AUTOCLICK|WS_BET_TEST` を除去 → プルダウンが制御) → 各 `.bak_<stamp>` 保存。デプロイ後は **GUI再起動 + BET MODE選択**。

### 4.5 SSH/作業の注意（ハマりどころ）
- **inline PowerShell over SSH は `$_`/`$e` 等が bash に食われる** → 必ず `.ps1` を scp して `powershell -NoProfile -ExecutionPolicy Bypass -File` で実行。
- ログが超高頻度(betsopen 47卓/秒)。`Get-Content -Tail` の窓は小さいと取りこぼす。**money/SEQは状態ファイル直読みが確実**。
- greenletエラー(`Cannot switch to a different thread`)は **オートモードの周辺Playwright UI操作由来でWS BET/決済には無影響**(時々バースト・ログノイズ)。WS送信/決済は Playwright クリックを使わないので壊れない。

---

## 5. env フラグ一覧

| env | 既定 | 意味 |
|---|---|---|
| `BACOPY_LPBET_GM` | `mtb_desktop` | lpbet の game module |
| `BACOPY_MULTI_BET_TRANSPORT` | `click` | `ws`=WS送信 / `click`=チップクリック |
| `BACOPY_ALLOW_WS_BET_TRANSPORT` | `0` | WS transport 許可ゲート |
| `BACOPY_ENABLE_WS_REAL_BET` | `0` | chrome_attach での WS実BET許可 |
| `BACOPY_MANUAL_ASSIST_AUTO_CLICK` | `0` | NOWで自動BET(オート) |
| `BACOPY_MANUAL_NO_AUTOCLICK` | `0` | 自動クリック強制OFF(アシスト) |
| `BACOPY_DGA_SETTLE_FEED` | (auto時自動) | 決済専用dga勝者feedを起こす |
| `BACOPY_DGA_DIRECT_SOURCE` | `1` | 直結dga WS源 |
| `BACOPY_DGA_LOCAL_SIGNAL` | `off` | dgaローカル信号(別経路。settle feedとは独立) |
| `BACOPY_DUAL_DRYRUN` | (未設定) | 管理者ドライラン(--live外す) |
| `BACOPY_WS_BET_TEST_ONCE` | (未設定) | 検証用ワンショットWS BET |

> **オートは GUI プルダウンが上記を main.js 経由で自動設定するので、.env に手動で書かない**(プルダウンが上書き・main.js childEnv が最終勝者)。

---

## 6. 実機検証エビデンス(2026-06-06)

- WS BET受理: `[GAME-BET-CONFIRM] amount=1` + 残高 133.64→134.64(勝ち+$1)・拒否なし。Speed Baccarat 14/Speed Baccarat 7 等の高速卓でも即着弾。
- 自動決済: `17:57 NOW Speed Baccarat 7 B $1 → WS BET → BET-CONFIRMED → DGA-VPS-SETTLE settled (outcome=P, LOSE) → total_bets 0→1`。GUIに「L」表示。
- SEQ進行: small1 $1 で `seq_turn 1→2`、負け込みで `seq_level 0→2`、勝ちで `2→0` リセット(SEQ正常)。TIE も `result=TIE`(push)で正常処理。
- GUI: AUTO選択で WIN/LOSE/TIE/HOLD 非表示+バッジAUTO、LIVE撤去、DAILY TOTAL=-$4.95(=/me/realtime, BILLING daily_bet_pnl=-4.95)。

## 7. 重要な前提(収益とは別)
**これは「自動化の完成」であって「勝てる(エッジ)」の証明ではない。** 6/10パターンのエッジは薄い(~51%)。SEQは現実元本で破産しうる/フラットは破産しにくいがプラス保証なし。資金管理(モード/額/リスク許容)はユーザ判断。詳細は過去メモリ `project_dual_line_*`。

---

## 9. デュアルラインオート追従 (BACOPY_DUAL_FOLLOW)
BET MODE 3つ目「デュアルラインオート追従」。オート(WS自動BET)＋**勝った時だけ追従**。
- **追従起点**: signal の大路(`pattern_key` china|**BIG**|side の真ん中)が **telecho か dragon** で**勝った**時のみ。それ以外(niconico/nikoichi/sansan)は追従せず通常1回BET。初回が負け→追従せず待機。
- **方向**: telecho=**逆張り**(毎手反対側 P→B→P...) / dragon=**順張り**(同じ側)。開始時の大路で固定(途中再判定なし)。
- **継続/終了**: 勝てば同卓で次手を自動WS BET、**負ければ終了→待機**。**TIEはプッシュ**(同側で再BET・反転しない)。追従中は他卓のNOWを無視。
- **額**: SEQ継続(money model の next_bet)。
- 実装(`dual_line_pragmatic_bot.py`): `_follow_*` state(`BACOPY_DUAL_FOLLOW`で有効), `_follow_big_kind`/`_follow_compute_next`/`_follow_on_settled`(決済後フック・`_settle_confirmed_decision_from_hand`末尾から呼ぶ)/`_follow_place_next`(合成decision `follow_<qpid>_<ms>` で place_bet→既存のdga決済経路に乗る)。`_handle_decision`先頭で追従中はVPS NOWスキップ。メインループに追従タイムアウト監視(`BACOPY_FOLLOW_TIMEOUT_SEC`=180、決済不発で自動reset＋pending掃除)。追従BETはマスターへ投げない(`_post_decision_settlement`/`_flush_pending_decision_results`で`is_follow`スキップ=404防止)。
- GUI: `index.html` option `dual_line_auto_follow`、`app.js` `isDualLineFollowBetMode`＋`dual_follow`config＋**BET MODE変更で即localStorage保存**(保存ボタン押し忘れ防止)、`main.js` auto時 `config.dual_follow`→`BACOPY_DUAL_FOLLOW=1`＋起動ログに `follow=true BACOPY_DUAL_FOLLOW=1`(検証用)。
- **実機検証(2026-06-06)**: `telecho|telecho|B`勝ち→`[FOLLOW] START kind=telecho won_side=B next=P`→`place next side=P`(逆張り)→P負け→`[FOLLOW] END reason=lose`→待機。仕様通り。
- トラブルシュート: 追従しない時は①起動ログ `follow=true BACOPY_DUAL_FOLLOW=1` を確認(=未設定ならGUIで選び直し→即保存される)②勝った手の大路が telecho/dragon か(niconico等は追従しないのが正)③`[FOLLOW]`ログ(START/WIN chain=N/END)を追う。

## 10. Stake「ゲームがありません / failed to start third party session」復旧
症状: 9222専用Chromeで「failed to start third party session」、どの卓も「ゲームがありません」。
- 原因: Stakeの**重複Pragmaticセッション**アンチアビューズ・ブロック。GUI短時間多数回再起動で専用Chromeにセッション/タブ復元状態が重なって残るのが主因(追従/オート等のコードとは無関係)。
- 復旧(`_recover_cdp_session.ps1` を bafather に常備): ①GUI(BACOPYRECEIVER)+engine停止 ②`cdp_chrome_profile`/`9222`のchrome.exe全kill ③`cdp_chrome_profile\Default` の `Sessions\*`・`Session Storage\*`・`Last/Current Session/Tabs` 削除(**ログインcookieは残す**) ④GUI再起動=単一クリーンセッション。
- それでも続く時はStakeサーバ側ブロックが数分残るだけ→3〜5分待って再起動。判定: engineログで `[BETSOPEN]` が現在時刻で流れていれば復旧(ホストWS接続)。
- 予防: 短時間の連続GUI再起動を避ける。

## 8. 関連ファイル(本変更の本体)
- `dual_line_live_executor.py`: `_build_lpbet_xml`(gm/bc/byte), `_ws_send`, `send_bet`(WS分岐), `_maybe_fire_ws_test_bet`, `_dga_direct_loop`/`set_dga_result_callback`(勝者feed)。
- `dual_line_pragmatic_bot.py`: `_maybe_register_dga_callback`(settle feed), `_on_dga_frame`(勝者enqueue), `_dga_vps_settle_pump`/`_settle_vps_from_dga`(決済drain), `_DgaHandBuf`, `_handle_manual_assist_command`(オート時result拒否), `_poll_bet_history_billing`(daily_total送信)。
- `copytrade_gui/src/main.js`: dual_line_auto の env, 常時 --live。
- `copytrade_gui/src/renderer/index.html`: BET MODE プルダウン, LIVE/旧checkbox非表示。
- `copytrade_gui/src/renderer/app.js`: `manualAutoBetMode`(WIN/LOSE非表示), `_engineDailyPnl`/`case 'daily_total'`(DAILY TOTAL)。
