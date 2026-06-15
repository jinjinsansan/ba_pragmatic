# 引継ぎ書：hh88 マルチプラットフォーム移植（次セッション最初に読む）
作成: 2026-06-15 / 対象: 第2カジノ hh88(hh88vip5.com) で現行エンジンを稼働させる作業の続き

---

## 0. 一行サマリ & 次にやること
**hh88 は Stake と同一の Pragmatic マルチバカラ・バックエンド。実BET受理は実証済み。プラットフォーム層も実装済み。
~~残る唯一のブロッカー = エンジンの `route_web_socket(pattern="**")` が hh88 の起動(pusher等)を壊すこと。~~
→ ✅【2026-06-15 続セッションで実装完了】hh88 だけ route_web_socket をやめ、CDP注入WSブリッジに置換した(§11)。
→ 次の作業 = **ローカルE2E検証**(§6 の手順で engine を起動 → betsopen 取込 → 検証BET1発 → `win.nwb` で受理確認)
  → 合格したら §7 でビルド → commit。GUIのプラットフォーム選択UI(main.js)は最後。**

設計の全体像は **`DESIGN_MULTI_PLATFORM_HH88_2026-06-15.md`**（特に §8.5 / §8.6）。本書はその実装ガイド。

> ✅ **実装ステータス(2026-06-15 続セッション)**: §2-3 の CDP注入WSブリッジを `_rev53144` に実装済み（未ビルド・未commit）。詳細は末尾 **§11**。残りは E2E 検証のみ。

---

## 1. 背景（なぜやるか・何が分かっているか）
- オーナーが第2カジノ **hh88(hh88vip5.com)** でも今のGUI/エンジンで自動BETしたい。**別GUIは作らない**。
- hh88 は **Stake と完全に同一の Pragmatic クライアント**(`client.pragmaticplaylive.net/desktop/multibaccarat`)を
  カジノのラッパー(`game-iframe-v2?name=PP`, game_id=808)越しに読み込んでいる。
- **同一**: チャネルホスト(`cbcf6qas8fscb224`等)・卓qpid・`lpbet gm="mtb_desktop"`・**bc=1/0(旧エンコード)**・
  `betsopen`/`betsclosed`/`gameresult`/`win` feed・dga(`dga.pragmaticplaylive.net`)。
- **実証**: hh88 でページの実ソケットから `<lpbet amt=2 bc=1>` 送出 → `{"gameresult":result=player}` →
  **`{"win":{"gameId":...,"nwb":"-2.0"}}`**（2 HKDバンカー負け=受理・決済確定）。カスタマーサポート無し。
- **通貨=HKD**（`amt` 直値・2 HKD≒$0.2）。
- **★オーナー指示(厳守)**: GUIは常に**片側のプラットフォームのみ**(Stake or hh88)。**同時稼働不可**
  (Pragmatic の異変検知回避)。SEQはどちらでも可だが BET は片側のみ。

---

## 2. ★ブロッカーの核心（これが分かれば実装方針が決まる）
エンジンの WS フックは `LiveBetExecutor._setup_ws_proxy`(executor L8406付近) で
**`context.route_web_socket("**", _handle_ws)`** = **全 WebSocket を透過プロキシ**する方式。

- Stake では問題ない（Stake の起動は WS 依存が薄い）。
- **hh88 では起動を壊す**: hh88 は起動に `pusher.com` 等の WS を使い、`"**"` がそれも横取り→
  Playwright の透過プロキシが pusher を壊す→**Pragmatic クライアントが再launchされない**
  (reload 後に CDP で iframe 消失・page のみ、を実測)。
- host 指定パターン(`**pragmaticplaylive**` 等)では **埋め込み multibaccarat ソケットを拾えない**
  (executor L8456-8459 の註記)ため `"**"` 必須 → **pusher を除外できない**。
- ∴ **route_web_socket 方式は hh88 と構造的に非互換**。

---

## 3. ★解決方針（PoCで実証済み）= CDP注入WSブリッジ
PoC `_hh88_test_bet.py`(リポジトリ・ローカル/gitignore) は **route_web_socket を使わず**、
CDP `Runtime.evaluate` で **`WebSocket.prototype.send` をフック**して生ソケットを `window.__bacopyWS` に捕捉し、
`window.__bacopyWS.send(xml)` で lpbet を送って **受理(win)** された。この方式は **pusher 等に一切触らない**。

### 次セッションの実装（hh88 限定・Stake は route_web_socket のまま無変更）
1. **`_setup_ws_proxy` を hh88 で無効化**（`if IS_HH88: return` 等）。代わりに hh88 用ブリッジを起こす。
2. **hh88用 CDP/JS ブリッジ**（add_init_script + page.evaluate で、reload しなくても効くように）:
   - `WebSocket.prototype.send` をフック → 送信フレームに `channel="table-` を含むソケットを
     `window.__bacopyWS` に捕捉、`uId="..."` も拾う。
   - 受信フレームを JS 側で蓄積 or `window.__bacopyRecv` 配列に push（betsopen/win/gameresult）。
     ※または CDP `Network.webSocketFrameReceived` を engine が直接購読(PoC `_hh88_ws_capture.py` 方式)。
   - 送信関数 `window.__bacopyFire(xml)` を用意（PoCと同じ）。
3. **`_ws_send` の hh88 経路**: `page.evaluate("window.__bacopyFire(arguments[0])", xml)` に置換
   （既存の ws_proxy_server.send の代わり）。
4. **reload しない**（フックが既存ソケットを掴むので不要）。`BACOPY_PLATFORM_NO_RELOAD` を hh88 で ON に戻す
   （今 OFF にしてある。理由は §5）。reload しないので §5 の reload-if-on-host も hh88 では使われない。
5. **betsopen / win の取り込み**: engine の tick で `window.__bacopyRecv` を吸い上げ、
   既存の `_on_ws_message` 相当処理（betsopen→table_states、win→`_win_by_gid`/確証）に流す。
6. **検証**: `BACOPY_WS_BET_TEST_ONCE` 相当を CDP注入経路で1発 → `win.nwb` で受理確認。

### 注意
- PoC は **手動で開いた multibaccarat** に対して動いた。エンジンも **ユーザーが手動で hh88 マルチエリアを開いた状態**に
  アタッチする前提（no-reload）。GUI 配布時は「hh88 マルチエリアを開いてから START」を手順化。
- もし将来 reload したい場合でも、CDP注入なら reload 後に再フックすればよい（route_web_socket は使わない）。

---

## 4. すでに実装済み（commit `5026811` on `feat/dual-line`・全て Stake 安全）
`_rev53144/dual_line_live_executor.py`:
- L36-65 付近: `PLATFORM`/`IS_HH88`/`PLATFORM_LOBBY_URL`/`PLATFORM_LOBBY_MATCH`(hh88=`game-iframe-v2`)/
  `PLATFORM_HOST`(hh88=`hh88vip5.com`)/`PLATFORM_NO_RELOAD`(**今OFF**・§5)/`WIN_SETTLE_WAIT_SEC`(hh88=95s)。
  `PLATFORM_LOBBY_URL` 指定時に `PRAGMATIC_BACCARAT_LOBBY_URL` を差替。
- 判定文字列を一般化: `"stake.com"`→`PLATFORM_HOST`、`"pragmatic-play-live-lobby-baccarat"`→`PLATFORM_LOBBY_MATCH`
  (setup/recover/test-path/dga-setup 各所)。
- `{"win":{gameId,nwb}}` パーサ(`_on_ws_message` 内)→ 自分の gId 一致で `[WIN-CONFIRM]`、`_win_by_gid` に保存。
- `bet_server_validated` に win 確証分岐 + `has_win_for_game(gid)` メソッド。
- `_recover_pragmatic_lobby` / `_ensure_multi_area` フォールバックの hh88 ゲート(Stake `_join_table` を hh88 で skip)。
`_rev53144/dual_line_pragmatic_bot.py`:
- ページ選択前に `_plat`/`_no_reload`/`_host`/`_lobby_match`/`_lobby_url` を定義し、選択ロジックを一般化。
- 初回ナビ: `_plat!="stake"` かつ host 一致なら reload、それ以外は goto(_lobby_url)。`_ensure_pragmatic_lobby` の判定も一般化。
- PHANTOM-GUARD(`_settle_confirmed_decision_from_hand` 付近 L3110)に hh88 grace
  (`has_win_for_game` を最大~4s ポーリングしてから drop 判定)。
- 既定 `_no_reload=OFF`(§5)。

---

## 5. ★重要な注意（ハマりどころ）
- **`PLATFORM_NO_RELOAD` は今 OFF**（reload 方式を試した名残）。だが §2-3 で reload 方式は hh88 不可と判明。
  **次の実装で hh88 は no-reload + CDP注入に戻す**（`PLATFORM_NO_RELOAD` 既定を hh88=ON に、かつ no-reload 時に
  CDP注入ブリッジを起こす）。
- **Stake 後方互換は厳守**: 既定 `BACOPY_PLATFORM=stake` で全挙動が従来通り。hh88 分岐は必ず `IS_HH88`/`_plat=="hh88"` で
  ゲートする。Stake の bafather は本番稼働機なので壊さない。
- **排他**: GUI は片側のみ。`BACOPY_PLATFORM` で完全分離。GUI のプラットフォーム選択UI(main.js buildSpawnSpec の
  childEnv 配線)は **未実装**（次セッションで。E2E は env 直指定で可）。
- **bc=1/0**(hh88)。`BACOPY_BC_BANKER/PLAYER` 既定でOK(hh88は10/11ではない)。
- **通貨 HKD**: `win.nwb` は HKD。USD課金/週次へ繋ぐ時は HKD→USD 換算(`BACOPY_FX_HKD_USD` 等・Phase3)。
- **cp932**: Windows コンソールで `—`(em-dash)等の非ASCIIを print するとクラッシュ。スクリプトは ASCII か
  `PYTHONIOENCODING=utf-8 python -X utf8` で実行。
- **本番=bafather(クラウド・9222)**。GUIはローカルで動かさない。ローカルはビルド/検証用。

---

## 6. ローカルE2Eの再現手順（次セッションの検証環境）
1. CDP Chrome 起動(ローカル・Stakeの9222と分離):
   `chrome.exe --remote-debugging-port=9223 --user-data-dir=C:\Users\USER\_hh88_cdp_profile <hh88 login URL>`
2. hh88 ログイン: `https://www.hh88vip5.com/en_hk/login`（ユーザー `jpa0001` / PW `Aabb1122`）→
   Pragmatic Play → ライブバカラ → **マルチエリア(multibaccarat)** を開く。
3. CDP確認: `curl -s http://127.0.0.1:9223/json` で `client.pragmaticplaylive.net/desktop/multibaccarat/` iframe があること。
4. PoC送信テスト: `PYTHONIOENCODING=utf-8 python -X utf8 _hh88_test_bet.py`（2 HKDバンカーを1発・winで確認）。
5. エンジンE2E(CDP注入実装後): 下記envで engine 起動 → betsopen → 検証BET → win確証。

### エンジン起動コマンド（参考・現状はroute_web_socketで起動が壊れる→CDP注入実装後に有効）
```
BACOPY_PLATFORM=hh88 BACOPY_BROWSER=chrome_attach BACOPY_CHROME_CDP_URL=http://127.0.0.1:9223 \
BACOPY_MULTI_LOBBY_MODE=1 BACOPY_MULTI_BET_TRANSPORT=ws BACOPY_ALLOW_WS_BET_TRANSPORT=1 BACOPY_ENABLE_WS_REAL_BET=1 \
BACOPY_MANUAL_ASSIST_AUTO_CLICK=1 BACOPY_MANUAL_NO_AUTOCLICK=0 BACOPY_MULTI_DIAGNOSTIC_ONLY=0 BACOPY_DUAL_MODE=v3 \
BACOPY_WS_BET_TEST_ONCE=1 BACOPY_WS_BET_TEST_AMT=2 BACOPY_WS_BET_TEST_SIDE=B BACOPY_EXECUTOR_ID=hh88test PYTHONIOENCODING=utf-8 \
dist/bacopy_engine.exe dual-line --live --manual-assist --no-vps-poll --money-mode flat --money-unit 2 > _hh88_e2e.log 2>&1
```
（ログ追走: `grep -ivE "AUTO-PROBE|now_probe|DEP0169|dga-msg|GAME-ID|BETSOPEN-SKIP" _hh88_e2e.log | grep -iE "PLATFORM|MULTI-AREA|is_multi_ws|DGA|WS-BET-TEST|WIN-CONFIRM|<lpbet|Stage 1|stake.com|Error"`）

---

## 7. エンジンのビルド手順（hh88変更を反映する時）
```
cp _rev53144/dual_line_live_executor.py _rev53144/dual_line_money.py _rev53144/dual_line_pragmatic_bot.py ./   # repo-rootへ一時上書き
python -m PyInstaller build/bacopy_engine.spec --noconfirm --distpath dist --workpath build                  # dist/bacopy_engine.exe(224MB)
git checkout -- dual_line_live_executor.py dual_line_money.py dual_line_pragmatic_bot.py                      # root復元
certutil -hashfile dist/bacopy_engine.exe MD5
```
※ デプロイ元は `_rev53144/`。repo-root HEAD は別物(回帰込み)なので **必ず `_rev53144` をビルド**。

---

## 8. 診断/PoC資産（ローカル・gitignore `_*.py`）
- `_hh88_ws_capture.py`: CDPで hh88 multibaccarat の WS フレーム採取(送受信・betsopen/win/lpbet)。
- `_hh88_test_bet.py`: **CDP注入で1発 lpbet 送信＋応答監視**（=次の実装の雛形・実証コード）。
- CDP Chrome: ポート **9223**・プロファイル `C:\Users\USER\_hh88_cdp_profile`(Stakeの9222と分離)。

## 9. 関連
- 設計書: `DESIGN_MULTI_PLATFORM_HH88_2026-06-15.md`(§8.5 残り編集 / §8.6 WS方式転換)
- bc個体差の前例: `USER06_BC_BETCODE_FIX_2026-06-15.md`(hh88はbc=1/0なので該当せず)
- WS BET本体仕様(Stake): `DUAL_LINE_AUTO_WS_BET_COMPLETE_2026-06-06.md`
- メモリ: `project_multiplatform_hh88_2026-06-15`
- commit: `5026811`(hh88 WIP) / branch `feat/dual-line` / repo `github.com/jinjinsansan/ba_pragmatic`

## 10. このセッションで完了した別件（hh88とは独立・参考）
- user06 BET拒否の真因=bc符号違い(10/11) を解明・修正・配布(別メモリ `project_user06_bc_betcode_resolved_2026-06-15`)。
- bafather.uk: 無料ユーザーのチャージ解放 / 請求書システム(admin発行→ダッシュボード→ハイブリッド決済) /
  請求・入金履歴UI＋Telegram通知 / 週次課金システム(純PnL・キャリー可視化) を実装・本番反映(`ba`リポ)。
- 全受け子SSH点検: 全台健全。新クライアント(bc=10/11)は現状06のみ。

---

## 11. ★CDP注入WSブリッジ 実装完了（2026-06-15 続セッション・未ビルド/未commit）
§2-3 の方針どおり、hh88 を route_web_socket から CDP注入WSブリッジへ置換。**全変更は `IS_HH88`/`_plat=="hh88"` でゲート → Stake は一切無変更（後方互換）**。

### 変更ファイル（デプロイ元 `_rev53144/`）
**`dual_line_live_executor.py`**
1. `PLATFORM_NO_RELOAD` 既定を **hh88=ON**（reload で pusher を壊さない）。
2. 新 JS 定数 **`_HH88_WS_BRIDGE_INIT`**（PoC `_hh88_test_bet.py` 方式）:
   - `WebSocket.prototype.send` をフック → `channel="table-` を送るソケットを `window.__bacopyWS` に捕捉・`uId` を `window.__bacopyUid` に控える。
   - 捕捉ソケットに `message` リスナ → 受信フレームを `window.__bacopyRecv`(上限600のリング) に蓄積。
   - `window.__bacopyFire(xml)` = 直接送信 / `window.__bacopyDrain()` = 受信フレーム＋状態(ws/open/uid)を返してクリア。
3. `setup()`: hh88 のとき `add_init_script(_HH88_WS_BRIDGE_INIT)`、**`_install_ws_proxy_route` を skip**、no-reload で既存フレームへ再注入(`_hh88_reinject_bridge`)。
4. 新メソッド **`_hh88_reinject_bridge()`**(既存フレームへ冪等再注入) / **`_hh88_drain_recv()`**(tick毎: ドレイン→`_on_ws_message`へ流す＋socket捕捉で `_is_multi_table_ws=True`・`_game_ws_url` placeholder・`_user_id`=uId)。
5. `tick()` 冒頭で hh88 のとき `_hh88_drain_recv()`。
6. `_ws_send()`: hh88 は最優先で `window.__bacopyFire(payload)` 送信(proxy/worker 経路は使わない)。socket未捕捉なら再注入して次機会。

**`dual_line_pragmatic_bot.py`**
- `_no_reload` 既定を **hh88=ON**(executor と一致)。bot は既に no-reload を完全尊重(lobby goto skip・`_ensure_pragmatic_lobby`→True)。

### 設計上のポイント / 注意（次セッションが E2E でハマらないために）
- **受信経路**: hh88 の multibaccarat は cross-origin OOPIF で `page.on("websocket")` が拾えない → JS側 `__bacopyRecv` 蓄積を engine が tick でドレインする方式にした(CDP `Network.webSocketFrameReceived` 直購読は不採用)。
- **送信ゲート**: `send_bet` の WS送信は `_is_multi_table_ws or proxy` を要求 → hh88 は proxy 無しなので、ドレインで socket 捕捉時に `_is_multi_table_ws=True` を立てる。betsopen の窓判定(`bets_open_game_id`/`last_bets_open_at`)は `_on_ws_message` が `__bacopyRecv` 由来フレームで埋める。
- **no-reload 前提**: ユーザーが手動で hh88 マルチエリア(multibaccarat)を開いた状態で START すること。フックは既存ソケットの次の `channel="table-` 送信で捕捉(数秒)。
- **uId**: フックの送信フレームから捕捉(`__bacopyUid`)。hh88 は uId 非検証なので fallback でも可。
- **py_compile 両ファイル OK**。`git diff` = executor +216 / bot +5 行。

### ✅ビルド & E2E合格 & commit済み(2026-06-15 続セッション)
- `dist/bacopy_engine.exe`(224,626,745 bytes) **MD5=`257cdb99d380d71df5724d1b3bdbf7cc`**。`_rev53144`をrootへ一時コピー→PyInstaller→git restore で生成(BUILD_RC=0)。
- **★ローカルE2E合格(CDP 9223・hh88 multibaccarat 手動オープン)**: ログ実測 ↓
  - `[HH88-BRIDGE] re-injected into 3 frame(s)` → `[HH88-BRIDGE] channel-host socket captured → is_multi_table_ws=True`
  - `[WS-BET-TEST] FIRING ... table=speedbca14gesbc2 gid=14502055221 side=B amt=$2.0 uid=...202242`
  - `[WS-SEND] hh88 __bacopyFire OK`(socket無しフレームは`no_socket`でskip→正フレームで送出)
  - `[GAME-BET-CONFIRM] amount=2` → **`[WIN-CONFIRM] own bet settled via win: gId=14502055221 nwb=-2.0 (server-accepted)`**
  - = route_web_socket無し・pusher非破壊で実BET受理・決済確証。PoCと一致。
- **commit `d05dd63`** on `feat/dual-line`(engine 2ファイル+本書)。**未push・未デプロイ**(bafatherはStakeなのでIS_HH88ゲートで無影響)。

### 残タスク
1. ~~ローカルE2E~~ ✅完了(上記)。
2. push(ユーザー判断)。bafather への hh88 投入は別途(Stake本番機なので別個体/別フォルダ推奨)。
2. 合格 → §7 でビルド(`_rev53144`)→ MD5 → commit。
3. GUI: main.js にプラットフォーム選択UI(childEnv へ `BACOPY_PLATFORM` 等を配線)。**排他**(Stake/hh88 同時不可)を担保。
4. HKD→USD 換算(課金/週次連携・Phase3)。
