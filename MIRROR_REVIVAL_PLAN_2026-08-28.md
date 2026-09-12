# ミラートレード版 復活計画 (2026-08-28)

> 知人からの依頼:「マスター画面で卓を移動 → BANKER/PLAYER を押すと、受け子GUIも卓移動して
> 同じ側に BET する」版を復活させたい。
>
> 前提: 2026-08-22 に Xserver VPS・デスクトップクラウドとも解約済み(`RESTART_KIT_2026-08-22.md`)。
> ドメイン `bafather.uk` と GitHub は保持。

---

## 0. TL;DR — 3行

1. **ミラー版は消えていない。** 受け子アプリは1本で2モードを持ち、`executor-pragmatic` が
   ミラー版、`dual_line` が梶原さん式(SEQ・6/10パターン)。旧コミットに戻す必要はない。
2. **ローカル実証済み (8/28)**: マスターAPI起動 → `/master` 描画 → decision投入 → 受け子がack、
   まで今日のコードで一周した。壊れていない。
3. 足りないのは **VPS 1台と DNS だけ**。しかもミラー版が要求するのは
   **Python 2プロセス(ブラウザ不要)**。旧構成の1/10以下の規模で足りる。

---

## 1. 構成 (実装済みのもの)

```
[マスター画面]  bacopy_master_ui.py → bacopy_api.py が /master で配信
   卓グリッド(罫線つき)から卓を選ぶ → SWITCH_TABLE
   PLAYER / BANKER 大ボタン          → BET
        │  POST /api/decisions
        ▼
[マスターAPI]  bacopy_api.py   port 8010 / 標準ライブラリのみ / sqlite
        │  GET /api/decisions/wait  (ロングポーリング)
        ▼
[受け子GUI]  copytrade_gui (BACOPYRECEIVER・Electron)
        └─ engine: bacopy_engine.exe = bacopy_executor_pragmatic_ws_live.py
             実Chrome を :9222 CDP接続 → ロビーから卓をクリックして移動
             → lpbet XML を WS 直送で BET → ack / result をマスターへ返す
```

### 卓グリッドの供給元

マスター画面の卓一覧と罫線は `/api/snapshots` が供給する。これを埋めるのが
`collector_pragmatic.py`。**2026-08-03 の Cloudflare 対策で dga 直結モード(ブラウザ不要)が
入っている**(`_vps_prod/laplace2/collector_pragmatic.py:468-540`)。
`wss://dga.pragmaticplaylive.net/ws` に casinoId だけで購読でき、Stake ログインも不要。

→ **VPS に載るのは `bacopy_api.py` と `collector_pragmatic.py` の2本だけ。**
   シグナルbot(dual_line)・カード収集・LAPLACE・Camoufox は**すべて不要**。

---

## 2. 2026-08-28 に確認したこと (ローカル実証)

| 確認項目 | 結果 |
|---|---|
| `bacopy_api.py` が現行 Python (3.12) で起動するか | ✅ `/api/health` `{"ok":true}` |
| `/master` 画面が描画されるか | ✅ 117KB・PLAYER/BANKER ボタン・「配信先 GUI」カウンタあり |
| decision の投入 → 受け子の取得 → ack | ✅ SWITCH_TABLE と BET を投入し、`status=processing` + ack記録まで到達 |
| DB スキーマの自動生成 | ✅ `decisions` / `executors` / `bet_guard` を新規作成 |

再現コマンド (実弾・カジノに一切触れない):
```bash
BACOPY_DB_PATH=/tmp/smoke.sqlite3 BACOPY_API_KEY=devkey BACOPY_MASTER_PASSWORD=devpw \
  BACOPY_ENABLE_AUTO_BET=0 python bacopy_api.py --host 127.0.0.1 --port 8010
BACOPY_API_URL=http://127.0.0.1:8010 BACOPY_API_KEY=devkey python bacopy_executor_dryrun.py
```

---

## 3. ★最優先で潰した危険 (対応済み)

`copytrade_gui/src/main.js` に **解約済みIP `210.131.215.116` がフォールバック先として
ハードコード**されていた。Xserver はこのIPを別契約者へ再割当するため、古いインストーラのままだと
DNS が引けない瞬間に **`BACOPY_API_KEY` 付きのリクエストを見知らぬ第三者のサーバーへ送る**。

→ 2026-08-28 に除去済み。フォールバックが要る場合は `.env` の `BACOPY_API_FALLBACK_IPS` で
その時点の正しいIPを明示する(未設定なら DNS のみで解決)。

**まだ残っている同種の箇所**(復活作業で直す):
- `copytrade_gui/scripts/provision-user-build.js:206` … `BACOPY_SUPPORT_SSH_HOST: 'support@210.131.215.116'`
- `copytrade_gui/scripts/provision-user-build.js:273` … 管理者向け ssh -J の案内文
- `copytrade_gui/main.js:909` … 使われていない旧コピー(entry は `src/main.js`)。削除候補
- **`copytrade_gui/build_staging/.env:2`** … `BACOPY_SUPPORT_SSH_HOST=support@210.131.215.116`
  ★**これがビルド時に各インストーラへ焼き込まれる雛形**。ここを直さないと Phase 3 で再混入する
- `copytrade_gui/dist/win-unpacked/resources/.env:2` … 上記が焼き込まれた実物(ビルド成果物)
- `.env.dist:15` / `.env.template:27` … `LAPLACE_SSH_HOST`。配布用テンプレート
- `analyze_*.py` 12本 … 旧VPSへ ssh する分析スクリプト。実害は小さい(ホスト鍵不一致で接続拒否)が、
  復活後に使うなら新IPへ書き換えが要る
- **既存の `copytrade_gui/dist/BACOPYRECEIVER_user01..11_Setup.exe` は配布禁止**(旧IPを内蔵)

---

## 4. 必要なもの

### VPS 要件 (最小)

| 項目 | 値 |
|---|---|
| OS | Ubuntu 24.04 LTS (旧構成は 25.04) |
| スペック | **1 vCPU / 1GB RAM / 20GB** で足りる |
| Python | 3.12 以上 |
| pip | `websockets` `requests` のみ(★`camoufox` `playwright` は不要) |
| 開放ポート | 443 (Caddy) のみ。8010 は localhost に閉じる |
| 常時起動 | 必須(受け子がロングポーリングで待つため) |

### DNS

`master.bafather.uk` の A レコードを新VPSへ。TLS は Caddy の自動取得で足りる。

### 鍵 (すべて新規発行・旧鍵は使わない)

- `BACOPY_API_KEY` … マスター↔受け子の共通鍵
- `BACOPY_MASTER_PASSWORD` … `/master` のログインパスワード
- 受け子サポート用 SSH 鍵(必要なら)

---

## 5. 手順

### Phase 1 — サーバー構築 (VPS取得後・半日)

1. VPS 契約 → `apt install python3-venv caddy` → venv に `websockets requests`
2. `/opt/bacopy/` に `bacopy_api.py` `bacopy_db.py` `decision_logger.py` `snapshot_store.py`
   `dual_line_match.py`(bacopy_api が import する)を配置
3. `/opt/collector/` に `collector_pragmatic.py` を配置
   - ★`from camoufox.sync_api import Camoufox` が**モジュール冒頭にある**ため、
     dga直結モードでも import エラーになる。**遅延importに直す**(1行修正)
4. systemd 2本: `bacopy-api.service` / `bacopy-pragmatic-collector.service`
5. Caddy で `master.bafather.uk → 127.0.0.1:8010` をリバースプロキシ
6. 検証: ブラウザで `https://master.bafather.uk/master` にログインでき、
   卓グリッドに罫線が出ること(= collector が生きている証拠)

### Phase 2 — 認証ゲートの除去 (半日)

方針: **Supabase / bafather.uk への依存を切る**(オーナー判断 2026-08-28)。

- 受け子GUI のログイン画面(`_supabaseConfig` 系・`copytrade_gui/src/main.js:1035-1260` 付近)を
  バイパスするフラグを追加。`.env` の `BACOPY_API_KEY` だけで動く形にする
- マスターUI の「承認ユーザー一覧」は `LAPLACE_API_KEY` 未設定なら fail-soft で空になる
  (`bacopy_api.py:_fetch_approved_users`)。**そのまま放置してよい**
- 課金・ロックダウン(`_billing_lockdown.py`)は**使わない**

### Phase 3 — 受け子ビルド (1日)

1. `copytrade_gui/scripts/provision-user-build.js` の VPS ホストを新IPへ
2. engine EXE を PyInstaller で再ビルド(`bacopy_executor_pragmatic_ws_live.py` → `bacopy_engine.exe`)
3. `npm run build:installer` で 8〜11人分を生成(`_build_users_0710.ps1` が雛形)
4. ★**exe の検証を文字列検索でやらない**。`calc` サブコマンド等の実行で確かめる
   (`SEQ_CUSTOM_START_2026-08-05.md` の教訓)

### Phase 4 — 実機テスト (1人で先行)

1. 受け子1台に導入 → 専用Chrome プロファイル・:9222 を確認
2. マスターから SWITCH_TABLE → 卓が動くか
3. `$0.20` で BET → **ack と result がマスターに返るか**(ここが確証)
4. 問題なければ残りへ展開

### Phase 5 — 展開

8〜11台へ配布。1台ずつ ack が返ることを確認してから次へ。

---

## 6. 既知の罠 (最初から入れる)

| 罠 | 対処 |
|---|---|
| Chrome 冷間起動で「Failed to start third party session」が必発 | `BACOPY_LOBBY_URL=https://stake.com/ja/casino/home`(ロビー直リンク禁止) |
| 個人Chrome と :9222 が競合して GUI がクラッシュ | 賭け用は専用プロファイル(`cdp_chrome_profile`)で起動 |
| 受け子ごとに BET コードが違う (Banker=10 / Player=11) | `.env` で `bc` を指定(env化済み) |
| 賭けChrome の膨張(2GB+ でクリック遅延) | GUI のバッジで監視。解消は OS再起動か :9222 kill のみ |
| 古い Chrome だとサポートモーダル連発 | Chrome 149 以降 |
| `loss_cut=0` | ミラー版では SEQ を使わないので基本無関係。使うなら 0 で配らない |

---

## 7. 未検証のリスク (実機で確かめるまで断定できない)

1. **Stake / Pragmatic 側が 8/22 以降も同じか**。lpbet WS の受理、ロビーDOM、卓ID(qpid)は
   相手側の都合で変わる。Phase 4 の $0.20 テストが唯一の確認手段
2. **dga 直結フィードが今も通るか**。8/03 時点では公開フィードだったが、6ヶ月経っている
3. **日本からの接続**。`sportsbet.io` は総務省→Cloudflare 通知で日本向け451。
   `stake.com` は対象外だが、同じ経路で将来止まる可能性は残る(`RISK_JP_CDN_BLOCKING_2026-08-06.md`)
4. **旧鍵の失効が未実施**(`CREDENTIAL_REVOCATION_CHECKLIST_2026-08-22.md` の🔴3件が未着手)。
   復活とは別に、Stake パスワード変更と ANTHROPIC_API_KEY revoke は先に済ませるべき

---

## 8. 参照

| 文書 | 内容 |
|---|---|
| `RESTART_KIT_2026-08-22.md` | 停止の全体像・金庫の中身・3系統の dual_line_*.py の罠 |
| `docs/friend_proxy_betting_system.md` | ミラー版の設計思想(=この方式の原点) |
| `docs/bacopy_project_plan.md` | Master / Executor の役割分担 |
| `DISTRIBUTION_BUILD_RUNBOOK.md` | 受け子インストーラのビルド手順 |
| `CREDENTIAL_REVOCATION_CHECKLIST_2026-08-22.md` | 旧鍵の失効(未着手) |

---

*作成: 2026-08-28*

---

## 9. セッション記録 (2026-08-28) — 現在地と次アクション

### このセッションで確定したこと

| 項目 | 結論 |
|---|---|
| ミラー版はどこにあるか | **HEAD に現存**。受け子アプリは1本で2モード(`executor-pragmatic` = ミラー / `dual_line` = 梶原式)。旧コミットに戻す必要なし |
| 今のコードで動くか | ✅ ローカル一周実証(API起動 → `/master`描画 → decision投入 → 受け子ack) |
| VPS の規模 | Python 2プロセスのみ(`bacopy_api.py` + `collector_pragmatic.py`)。1vCPU/1GB で足りる |
| **Supabase は要るか** | **不要**。3経路すべて確認済み(下記) |
| オーナーの決定 | 新しい格安VPS / 受け子8〜11台 / ライセンス・課金ゲートは外す |

### Supabase 不要の根拠 (3経路すべて追跡済み)

| 経路 | 復活後の扱い |
|---|---|
| 受け子GUIのログイン+課金ゲート(`billingStatus()` = `copytrade_gui/src/main.js:1785`) | **外す**。呼び出し元は ipcMain 1箇所のみ(`:2163`)なので手術は小さい |
| マスター画面の承認ユーザー一覧(`bacopy_api.py:_fetch_approved_users`) | `LAPLACE_API_KEY` 未設定なら空で fail-soft。**放置でよい** |
| エンジン→bafather の残高同期(`bacopy_executor_pragmatic_ws_live.py:259`) | キー未設定なら即 return。**何も起きない** |

失うのは **課金・ロックダウン・Web帳簿**だけ。各受け子の**残高・日次PnL・現在の卓・BET可否**は
ハートビート(`upsert_executor`)経由でマスター画面に出るため、運用に必要な可視性は残る。

### ★仕様で誤解しやすい点 — 賭け額は受け子側が決める

マスターが送るのは **「卓」と「側」だけ**。decision の `friend_action.amount` は
**ACTIONバーの表示にしか使われない**(`bacopy_executor_pragmatic_ws_live.py:6039`)。
実際の BET 額は受け子側の設定(`--chip-base` / money mode / SEQ)で決まる。
→ 「いくらで回すか」は受け子ごとに決めて配る必要がある。

### 完了した作業

- [x] ミラー版の所在・構成の特定
- [x] ローカル実証(スクラッチ領域のみ使用・リポジトリ非汚染・プロセスは停止済み)
- [x] **解約済みIP `210.131.215.116` のハードコード除去** — `copytrade_gui/src/main.js`
      (DNS不通時に APIキー付きリクエストを第三者へ送る経路だった)
- [x] 残存する旧IP箇所の洗い出し(§3 に列挙・**`build_staging/.env` が雛形なので最重要**)
- [x] 本計画書の作成

### 次セッションの最初の3つ (オーナー待ち)

1. **`bafather.uk` の DNS はどこにあるか**(Cloudflare / レジストラ管理画面)
   → 分かり次第 Phase 1(サーバー構築)に入れる
2. **受け子の人数**(前と同じ8人か、知人の新メンバーか) → インストーラの本数が決まる
3. **賭け額の方針**(フラット $X か SEQ 階段か) → Phase 3 までに決まっていればよい

### 次セッションで着手できる作業 (VPS が無くても進む)

- Phase 2: 認証ゲートの除去(`billingStatus()` のバイパスフラグ追加)
- `copytrade_gui/build_staging/.env` ほか残存する旧IPの掃除
- `collector_pragmatic.py` の camoufox import を遅延importに変更(dga直結だけで動かすため)
- `copytrade_gui/main.js`(未使用の旧コピー)の削除判断

### git の状態 (未コミット)

- `MIRROR_REVIVAL_PLAN_2026-08-28.md` … 新規(本ファイル)
- `copytrade_gui/src/main.js` … 旧IP除去(5行)
- `RESTART_KIT_2026-08-22.md` … 前セッションからの未コミット差分(+87行・§9 の停止教訓と B:ミラー完了記録)

※ブランチは `feat/engine-heartbeat`。コミットはオーナーの指示待ち。
