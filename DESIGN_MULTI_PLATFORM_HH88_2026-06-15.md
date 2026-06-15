# 設計書：マルチプラットフォーム対応（hh88 への移植）— 2026-06-15

> **目的**: 現行の Pragmatic Play 自動BETシステム（Stake.com 向け）を、第2のカジノ
> プラットフォーム **hh88（hh88vip5.com）** でも同じGUI/エンジンで稼働させる。
> 本書は調査結果（実証済み）・アーキテクチャ分析・具体的な変更点・実装計画を記録する。
> **次にこの作業をする人は本書を最初に読む。**

---

## 0. 結論（先に要点）

- **hh88 は Stake と“同一の Pragmatic マルチバカラ・バックエンド”を使っている**（実測で確定）。
- **賭けの心臓部（lpbet / betsopen / gameresult / bc=1/0 / dga）は無改修で動く**。実際に hh88 で
  **2 HKD のバンカー賭けがサーバに受理・決済された**（`{"win":{"gameId":...,"nwb":"-2.0",...}}`）。
- よって **別GUIは作らない**。現行エンジン/GUIに **「プラットフォーム抽象層」** を足し、カジノ固有の
  **ラッパー層（ログイン・ロビー到達ナビ・残高/損益ソース・通貨）だけ** を設定で切り替える。
- 主なコード作業は **StakeにハードコードされたロビーURL/判定/ナビの一般化**。賭け・シグナル・決済の
  プロトコル層は触らない。

---

## 1. 調査結果（2026-06-15・実証済み）

### 1.1 hh88 の Pragmatic 統合形態
- ログイン: `https://www.hh88vip5.com/en_hk/login`（アカウント=会員制）。言語切替あり（日本語可）。
- ゲーム起動: `https://www.hh88vip5.com/en_hk/game-iframe-v2?name=PP&targetUrl=https://<aggregator>/gs2c/playGame.do?key=token%3D...`
  - カジノのラッパーページ → アグリゲータ(`*.net`) 経由 → **Pragmatic 本体クライアントを iframe で読み込む**。
  - `key=token%3D...` は**一回限り/期限付きの起動トークン**。この URL を直接リロードすると失敗しうる
    （＝エンジンの「lobby goto でリロード」をそのまま使えない理由）。
- 実際に読み込まれる iframe: **`https://client.pragmaticplaylive.net/desktop/multibaccarat/`**
  → **Stake と全く同一の Pragmatic マルチバカラ・クライアント**。

### 1.2 プロトコル一致（CDP実測）
- 送信(lpbet): `<command channel="table-cbcf6qas8fscb224"><lpbet gm="mtb_desktop" gId="..." uId="ppc..." ck="..."><bet amt="2" bc="1" ck="..."/></lpbet></command>`
  - `gm="mtb_desktop"`・`bc` 数値（**Banker=1 / Player=0 の旧エンコード**。06のような10/11ではない）・チャネルホスト `cbcf6qas8fscb224`（**Stakeと同一の卓ホスト**）。
- 受信(feed・チャネルホストWS): `betsopen` / `betsclosed` / `betsclosingsoon`（Stakeと同一フォーマット）。
  卓ID(qpid)も Stake と同じ（`bc392chromabc392`, `spto21bctorobc21`, `cbcf6qas8fscb224` …）。
- 結果/決済（チャネルホストWSでも届く）:
  - `{"gameresult":{...,"result":"player","score":"6",...,"id":"<gid>","table":"<tile>"}}`
  - `{"win":{"gameId":"<gid>","nwb":"-2.0","win":"0.0","rewardtype":"CASH","table":"<tile>","seq":...}}`
    ← **1賭けごとの純損益(`nwb`)が直接返る**（受理＋決済の決定的証拠／billingに使える）。
- 通貨: **HKD**。`amt` はアカウント通貨の直値（**2 HKD ≒ $0.2**）。`winners` フレームに `"currency":"HKD"`。
- 結果フィード `dga.pragmaticplaylive.net` は **Pragmatic 自身のサーバ**（カジノ非依存）→ Stake同様に使える見込み。

### 1.3 実証（PoC）
- リロードせず、ページの認証済み WebSocket を CDP で掴み、`<lpbet ... amt="2" bc="1">` を1発送出。
- サーバ応答（同一ソケット）で全ライフサイクル → `gameresult result=player` → **`win nwb=-2.0`**（バンカー賭けが負け、2 HKD減）。
- **カスタマーサポート/拒否なし＝完全受理**。スクリプト: `_hh88_test_bet.py` / 採取: `_hh88_ws_capture.py`（リポジトリ・診断用）。

---

## 2. アーキテクチャ：共通層 vs プラットフォーム固有層

| レイヤ | 内容 | hh88対応 |
|---|---|---|
| **Pragmatic 共通層（無改修）** | lpbet送信・betsopen検知・gameresult/win決済・dga直結・bc=1/0・卓ID・チャネルホスト | **そのまま動く（実証済）** |
| **カジノ固有層（設定で切替）** | ①ログイン/セッション ②Pragmaticバカラへの到達ナビ/URL ③残高/損益の取得元 ④通貨 | **本設計の対象** |
| **GUI層** | 1コードベース維持。プラットフォーム選択を追加 | プルダウン/設定追加 |

---

## 3. 設計：プラットフォーム抽象層

### 3.1 プラットフォーム識別
- `BACOPY_PLATFORM=stake|hh88`（既定 `stake`）を導入。GUI のプルダウン → main.js → childEnv。
- プラットフォームごとに「プロファイル」を持つ（コード内テーブル or env束）。

### 3.2 プラットフォーム・プロファイル（設定項目）
各プラットフォームで以下を定義（env もしくは内部テーブル）：

| 設定キー(案) | Stake | hh88 | 用途 |
|---|---|---|---|
| `lobby_url` | `https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat` | hh88のPragmatic起動ページ（**トークン無しの遷移元**。要特定） | :9222 Chrome起動URL・goto先 |
| `lobby_url_match` | `pragmatic-play-live-lobby-baccarat` | hh88のロビー判定文字列（例: `game-iframe-v2` か Pragmatic iframe の有無） | 「ロビーに居るか」判定 |
| `host_domain` | `stake.com` | `hh88vip5.com` | ページ選択・誤遷移ガード |
| `reach_nav` | （直接URL） | hh88 UI クリック手順 or 起動API | 多くの場合トークン都度発行が必要 |
| `balance_source` | Stake DOM `残高$` | **`win.nwb` 集計** or hh88残高DOM | 残高/損益 |
| `currency` | USDT | HKD | 表示・換算 |
| `bc_banker`/`bc_player` | 1/0 | 1/0 | 既存 `BACOPY_BC_BANKER/PLAYER`（hh88も1/0） |
| `dga_host`/`origin` | dga.pragmaticplaylive.net | 同じ | 既存 env（共通） |

### 3.3 ★最重要設計判断：ロビー到達（reach_nav）
- Stake はロビーURLを `goto` で直接開ける（毎回フレッシュ）。
- **hh88 は起動トークンが都度発行**（`game-iframe-v2?...key=token...`）。固定URLの `goto`/リロードは**トークン消費で失敗しうる**。
- 設計選択肢:
  - **(A) トークン無しの“起動元ページ”をlobby_urlにする**：hh88でPragmaticバカラを開くカジノ内ページ（クリックすると都度トークン発行してiframeを張る）。エンジンはそのページを開き、必要なら「Pragmaticを開く」要素をクリック（Stakeのマルチプレイ・タブ検出と同型のクリック層を hh88 セレクタで実装）。**本命**。
  - **(B) リロードしない方式**：エンジン起動時に goto を一切せず、**ユーザーが手動で multibaccarat を開いた状態**にアタッチして、以後 goto/deadlock-reload を無効化（06で使った `BACOPY_MULTI_DEADLOCK_RELOAD_ENABLE=0` の発展）。手動運用前提なら最小実装。
- → まず **(B) で最小実証**（手動でhh88バカラを開く→エンジンは送信に専念）、その後 **(A)** で自動ナビ化、が安全。

### 3.4 残高/損益（billing）設計
- Stake は `_poll_bet_history_billing`（Stakeのベット履歴DOM）と残高DOMに依存 → **hh88では使えない**。
- **hh88は `win` フレームの `nwb`（1賭けごとの純損益）が最良ソース**：
  - 各 `win{gameId,nwb,table}` を自分の発注 gid と突き合わせて損益累積 → `daily_bet_pnl` 相当を生成。
  - これは**入出金非依存で純粋**（週次課金システムにそのまま乗る）。Stakeの daily_bet_pnl と同列の「優先度1ソース」になる。
- **通貨換算**：hh88はHKD。課金/表示はUSD基準なので **HKD→USD換算レート** を設定（`BACOPY_CURRENCY=HKD` + `BACOPY_FX_HKD_USD`）。`win.nwb`(HKD) × レート = USD損益。
- 残高表示も `win` 累積 or hh88残高DOMセレクタ（任意）。

### 3.5 ログイン/セッション
- Stake同様 **手動ログイン**（:9222 Chrome に人がログイン）。エンジンはログイン済みChromeにCDPアタッチするだけ。
  → **コード変更なし**（プラットフォームに依存しない）。
- 受け子配布時は「hh88にログインしてからSTART」を手順化。

---

## 4. 変更が必要な具体的ハードコード箇所（Stake固定 → 一般化）

### `_rev53144/dual_line_live_executor.py`
- **L36-39** `PRAGMATIC_BACCARAT_LOBBY_URL`（env `BACOPY_PRAGMATIC_LOBBY_URL`、既定Stake）→ プラットフォーム別に。
- **L2131-2135** `lobby_url = PRAGMATIC...` ＋ `"stake.com" not in cur_url` 判定 → `host_domain`/`lobby_url_match` で一般化。
- **L2249-2252** `"pragmatic-play-live-lobby-baccarat" in cur_url` ＋ `goto(PRAGMATIC...)` → `lobby_url_match`/`lobby_url`。
- **L3086 / L3117 / L4822** lobby goto（dga setup・test path）→ `lobby_url`。
- **L3899-3900** test-bet の卓探索 `"stake.com" in cur_url` ＋ lobby判定 → 一般化。
- **L5338-5468** 受理確認（Stake残高delta / GAME-BET-CONFIRM）→ **hh88は `win.nwb` も受理確証に使う**（経路追加）。
- （無改修でOK）**L137-138** dga host/origin（env・共通）、**L492** `/desktop/multibaccarat` 検出（共通）、**L1614** gm（env）、**L1615-1624** bc（env済）。

### `_rev53144/dual_line_pragmatic_bot.py`
- **L6212 / L6216** ページ選択 `"stake.com"` / `"pragmatic-play-live-lobby-baccarat"` → `host_domain`/`lobby_url_match`。
- **L6260-6262 / L6291 / L6381 / L6437** `bet_page.goto(cp.LOBBY_URL)` → プラットフォームの `lobby_url`。
  - ★`cp.LOBBY_URL`（collector_pragmatic 内・現状env非対応）を **env/プロファイル駆動に変更**するのが鍵。
- **L6270-6299** `_ensure_pragmatic_lobby` の `"pragmatic-play-live-lobby-baccarat" in cur` → `lobby_url_match`。
  - hh88は**トークン都度発行**のため、ここを単純 goto にせず §3.3 の方式で（reach_nav or no-reload）。

### `copytrade_gui/src/main.js`
- `ensureCdpChrome`（L567-595）起動URL=`BACOPY_LOBBY_URL`（env済）→ プラットフォーム別既定。
- `buildSpawnSpec`：`BACOPY_PLATFORM` と関連env（lobby/host/currency/fx）を childEnv に設定。
- 残高表示（renderer）：`win.nwb` 由来の損益 or hh88残高に対応。

### マルチエリア・タブ検出
- `_MULTI_LOBBY_ENSURE_TAB_JS`（Stakeのマルチプレイ・タブをクリック）→ hh88のUIは異なるので、
  hh88用セレクタ or §3.3(B) no-reload 運用で回避。

---

## 5. hh88 プラットフォーム・プロファイル（確定値）

```
platform        = hh88
host_domain     = hh88vip5.com
game_client     = client.pragmaticplaylive.net/desktop/multibaccarat   (Stakeと同一)
channel_host    = cbcf6qas8fscb224 等（動的検出・Stakeと同一空間）
bc_banker / bc_player = 1 / 0
gm              = mtb_desktop
dga             = dga.pragmaticplaylive.net（共通）
currency        = HKD（amt直値・2 HKD≒$0.2）
settlement_src  = win フレーム nwb（gIdごとの純損益）＋ dga gameResult
lobby_url       = （要特定：hh88のPragmaticバカラ起動ページ。トークンは都度発行）
launch_path     = /en_hk/game-iframe-v2?name=PP&targetUrl=<aggregator>/gs2c/playGame.do?key=token...（都度変化）
login           = 手動（会員アカウント）
```

---

## 6. 実装計画（フェーズ）

### Phase 0（完了）
- 調査・プロトコル一致確認・**実BET受理の実証**（本書 §1）。

### Phase 1：最小実証（no-reload 運用で1台を自動BET）
- `BACOPY_PLATFORM=hh88` を導入し、**goto/deadlock-reload を無効化**してアタッチ専念モードを追加。
- 受理確証に **`win.nwb` 経路を追加**（Stake残高DOM非依存）。
- ユーザーが手動でhh88マルチエリアを開く→エンジンが betsopen 検知→lpbet 自動送信→`win` で決済。
- これで「現行エンジンがhh88で自動BET＋決済」を完成（手動ナビ前提）。

### Phase 2：自動ナビ（reach_nav）
- hh88のPragmaticバカラ起動ページURL/クリック手順を特定し、エンジンが自動到達（都度トークン）。
- ロビー判定/ページ選択を `host_domain`/`lobby_url_match` で一般化。

### Phase 3：billing/通貨/週次
- `win.nwb`(HKD) → USD換算 → 既存の `daily_pnl_log`/`weekly_pnl`/請求書システムに接続。
- GUIにプラットフォーム選択＋通貨表示（HKD/USD）。

### Phase 4：配布
- hh88受け子インストーラ（identity＋`BACOPY_PLATFORM=hh88`）。bafather/受け子は Stake のまま共存。

---

## 7. リスク・未解決事項
- **起動トークンの都度発行**：固定URLリロード不可。Phase 2のナビ特定が要（Phase 1は手動回避）。
- **通貨換算レート**：HKD↔USD の固定/変動の運用方針（請求の公平性）。
- **`win.nwb` の符号/通貨**：HKD建て。pair等のサイドベットは別フレーム（メイン bc=0/1 のみ対象）。
- **アグリゲータ経由ドメイン**（`*.net`）：将来変わりうる（トークンURL内）。Pragmatic本体(client/dga)は安定。
- **bc符号の将来変化**：Stake同様hh88でも将来 10/11 化の可能性（envで即対応可・既存の仕組み）。
- **利用規約/アカウント**：自動化の可否はオーナー判断（既存Stake運用と同じ前提）。

## 8. PoC 証拠（hh88・2026-06-15）
- 送信: `<command channel="table-b0jf7rlboleibnap"><lpbet gm="mtb_desktop" gId="14499909321" uId="ppc1735343648462" ck="..."><bet amt="2" bc="1" ck="..."/></lpbet></command>`
- 受理/決済: `{"gameresult":{"result":"player","score":"6","id":"14499909321",...}}` →
  `{"win":{"gameId":"14499909321","nwb":"-2.0","win":"0.0","rewardtype":"CASH","table":"b0jf7rlboleibnap"}}`
- 診断スクリプト（リポジトリ・ローカル用）: `_hh88_ws_capture.py`（採取）/ `_hh88_test_bet.py`（1発送出＋応答監視）。
- CDP Chrome: ポート **9223**・プロファイル `C:\Users\USER\_hh88_cdp_profile`（Stakeの9222と分離）。

---

## 8.5 ★実装メモ（2026-06-15 追記・重要な方針確定）

### リロード可を実測確認 → Option A（Stake機構流用）で確定
- hh88 の起動ページ `game-iframe-v2?name=PP`（`game_id=808`/`isGameUrlApiCalled=true`）は
  **読込ごとにサーバAPIで新トークンを取得**して Pragmatic を張り直す。**`Page.reload` で再launch成功**を実測。
- リロード後は **Stakeと同じ Pragmatic `client.pragmaticplaylive.net/desktop/lobby2/`** に着地
  → そこから multibaccarat へ（**同一クライアント＝エンジンの multi-area ナビ流用可**）。
- 結論: **CDP注入ブリッジ(§3.3 B)は不要**。hh88 も **Stakeと同じリロード方式**で WS フック可能。
  hh88固有は「起動URL」＋「ロビー判定文字列」＋ログイン＋通貨だけ。

### 実装済み（WIP・全て hh88/env 時のみ作動・Stakeは既定で無変更）
`_rev53144/dual_line_live_executor.py`:
- `PLATFORM`/`IS_HH88`/`PLATFORM_LOBBY_URL`/`PLATFORM_LOBBY_MATCH`(既定 hh88=`game-iframe-v2`)/`PLATFORM_HOST`(hh88=`hh88vip5.com`)/`PLATFORM_NO_RELOAD`(既定OFF)/`WIN_SETTLE_WAIT_SEC`(hh88=95s)。
- `PLATFORM_LOBBY_URL` 指定時は `PRAGMATIC_BACCARAT_LOBBY_URL` を差替（全goto自動追従）。
- `{"win":{gameId,nwb}}` パーサ追加 → 自分のgid一致で `[WIN-CONFIRM]`（受理確証）。
- リカバリgoto(`_recover_pragmatic_lobby`)に no-reload 早期return（フォールバック用）。
`_rev53144/dual_line_pragmatic_bot.py`:
- `_no_reload` 判定＋goto/再初期化のゲート（フォールバック用・既定OFF）。

### 残りの編集（次セッション・機械的）
1. **ロビー判定文字列の一般化**（`"pragmatic-play-live-lobby-baccarat"`/`"stake.com"` → `PLATFORM_LOBBY_MATCH`/`PLATFORM_HOST`）。executor の該当: L2150(`stake.com`), L2264/2278, L3899-3900。bot: `_ensure_pragmatic_lobby` の `"pragmatic-play-live-lobby-baccarat" in cur`、ページ選択 L6212/6216。
2. **bot の lobby URL**: `cp.LOBBY_URL` → `PRAGMATIC_BACCARAT_LOBBY_URL`（=PLATFORM_LOBBY_URL）に差替（bot goto 3箇所）。
3. **hh88 の起動URL確定**: `PLATFORM_LOBBY_URL` に入れる安定URL。候補=`https://www.hh88vip5.com/en_hk/game-iframe-v2?name=PP&gameParams=<game_id:808のJSON>`（targetUrlのワンタイムtokenは除く・ページがAPI再取得）。**E2E前に「この最小URLで再launchするか」を1度検証**。
4. **PHANTOM-GUARD タイムアウト**: hh88は `WIN_SETTLE_WAIT_SEC`(95s) まで待つ／`win`到着で確証（bot側の幻ガード条件に `IS_HH88` 分岐）。
5. **GUI**: `BACOPY_PLATFORM` 選択（排他）＋ `BACOPY_PLATFORM_LOBBY_URL` 等を childEnv へ（main.js buildSpawnSpec）。
6. **ビルド → ローカルE2E（dev機 9223 hh88）→ bafather**。

### hh88 起動URL（実測・参考）
`https://www.hh88vip5.com/en_hk/game-iframe-v2?name=PP&targetUrl=https://<aggr>.net/gs2c/playGame.do?key=token%3D...&gameParams=%7B"code":"Pp","game_id":808,...%7D&jump_type=3&...&userId=<id>`
- 安定要素: `name=PP`・`game_id=808`・`code=Pp`。`targetUrl`内のtokenはワンタイム（リロードでAPI再取得される）。

### 排他制約（オーナー指示）
- **GUIは常に片側のみ**（Stake or hh88）。同時稼働不可（Pragmaticの異変検知回避）。SEQはどちらでも可だが**BETは片側のみ**。
- → GUIのプラットフォーム選択は排他。BACOPY_PLATFORM で完全に分離。

## 9. 関連
- Pragmatic WS BET の本体仕様: `DUAL_LINE_AUTO_WS_BET_COMPLETE_2026-06-06.md`
- bc符号の個体差（Stake user06=10/11）: `USER06_BC_BETCODE_FIX_2026-06-15.md`
- 週次課金/純PnL: `ba` リポ `weekly_pnl_migration_20260615.sql` + `/api/cron/settle-weekly`
