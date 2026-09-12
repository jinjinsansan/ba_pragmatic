# RESTART KIT — bacopy 全停止と再開手順 (2026-08-22)

> **これは「未来の自分」宛の文書。** サーバーを解約した後、半年後・一年後に
> 再開しようとした時に最初に読む1本。ファイルを置いただけでは再開できないので、
> 「どこに何があるか」より **「何を踏むと事故るか」** を先に書いてある。

---

## 0. TL;DR — 3行

1. 保全物は **金庫 `V:\_ARCHIVE_BACOPY_SHUTDOWN_20260822\` に442MB** だけ(VPS・デスクトップクラウド・Supabase の全て)。全件SHA256検証済み・復元テスト済み。
2. **VPS本番bot は repo版と別物**(半分以下のサイズ)。再開時は必ず `_vps_prod/` を正とする。repo直下の `dual_line_*.py` で起動すると挙動が変わる。
3. ドメイン `bafather.uk` / Telegram ch / GitHub は**解約しない**。これらは失うと回復不能。

---

## 1. なぜ止めたか (2026-08-22)

オーナー判断でプロジェクト全体を停止。Xserver VPS (`210.131.215.116`) と
Xserver デスクトップクラウド "bafather" (`162.43.83.54`) を解約する。
**再開の可能性は残す**ため、解約ではなく「冷凍保存」の方針を取った。

受け子8人への停止連絡は 8/22 時点で完了済み。Telegram チャンネルは削除せず放置(無料で維持される)。

---

## 2. 保全物 — 全部で 442 MB

`V:\_ARCHIVE_BACOPY_SHUTDOWN_20260822\`

| ディレクトリ | 容量 | 中身 | 検証 |
|---|---|---|---|
| `data/` | 440 MB | sqlite 4本 + JSONL 23本 (生 3.08 GB を gzip 圧縮) | SHA256 27/27 OK・gzip -t 27/27 OK・復元テスト2件で `integrity_check=ok` |
| `code/` | 1.3 MB | VPS 全コード tar (240ファイル) | repoの `_vps_prod/` と同一物 |
| `secrets/` | 16 KB | `.env` 実キー4本・systemd unit 10本・root crontab・平文トークン入り文書1本 | ★git禁止 |
| `env/` | 9 KB | pip freeze 2本・OS情報・env キー名一覧(値は伏字) | — |
| `bafather_desktopcloud/` | 185 KB | デスクトップクラウド固有ファイル 55件 | zip整合 OK |
| `supabase/` | 98 KB | 全32テーブル(31 + `auth.users`)・931行 + 13アカウント | SHA256 33/33 OK・gzip整合 OK |

### data/ の中身と価値

| ファイル | 生サイズ | 中身 | 再取得可能性 |
|---|---|---|---|
| `analytics_pragmatic.sqlite3.gz` | 1.6 GB | **shoes 282,714行** + shoes_extra 282,714行 | ❌ 数ヶ月の収集が必要 |
| `baccarat.db.gz` | 589 MB | LAPLACE(Evolution)側データ | ❌ |
| `decisions.jsonl.gz` | 227 MB | 決定ログ(監査証跡・全BET判断) | ❌ **友人代打ちPJの本体資産** |
| `bacopy.sqlite3.gz` | 166 MB | **decisions 70,663行** / executors 10行 | ❌ Phase4 MLの学習データ |
| `analytics.sqlite3.gz` | 105 MB | LAPLACE analytics | ❌ |
| `card_feed.jsonl*.gz` ×22 | 531 MB | 8デッキカード研究コーパス | ❌ |

---

## 3. ★再開時に踏む地雷 (ここが最重要)

### 3-1. VPS本番bot ≠ repo版 — 実測で確認済み

| ファイル | VPS本番 | repo (HEAD) | |
|---|---|---|---|
| `dual_line_pragmatic_bot.py` | 160,368 B | 334,502 B | ★**repoが倍以上** |
| `dual_line_money.py` | 14,312 B | 19,087 B | ★差分あり |
| `dual_line_match.py` | 6,648 B | 7,148 B | ★差分あり |
| `dual_line_logic.py` | 13,124 B | 13,124 B | 同一 |

**repo版で起動するとシグナル生成が変わる。** 再開時は `_vps_prod/laplace2/` を正とすること。
`_deploy_vps.ps1` は repo直下のファイルを VPS に上書きする作りなので、**そのまま実行してはいけない**。

なお **engine(受け子側)のビルド元は `_rev53144/`** であって repo直下ではない。これも別物。
つまりこのリポジトリには「3系統の dual_line_*.py」が存在する:
- `_vps_prod/laplace2/` … VPSシグナルbot(本番)
- `_rev53144/` … 受け子engineのビルド元(本番)
- repo直下 … **どちらでもない。回帰版。触るな**

### 3-2. 朗報 — Cloudflare 問題は再発しない

2026-08-03 に VPS の headless camoufox が Cloudflare に弾かれて全配信停止した。
恒久対策として **dga lobby WS 直結**(`wss://dga.pragmaticplaylive.net/ws`・casinoId のみで購読可・
Stakeログイン不要・**ブラウザ不要**)に切り替え済み。
再開時にこの問題を再度踏むことはない。詳細は `INCIDENT_VPS_CLOUDFLARE_DGA_DIRECT_20260803.md`。

⚠️ ただし `bacopy-card-collector.service` の ExecStart には **camoufox 時代の引数が残っている**
(`--profile .../camoufox_profile_cardfeed --cookies .../stake_cookies.json`)。
実際のコードは直結化済みなので引数は無視されるが、再構築時に混乱しないよう注意。

### 3-3. 受け子側の既知の罠(再配布するなら必読)

- ロビー直リンクは Chrome 冷間起動で「Failed to start third party session」が**必発**。
  `BACOPY_LOBBY_URL=https://stake.com/ja/casino/home` にする(2026-07-13 全受け子共通で確定)
- `dist/` 内のインストーラは古いビルドの可能性 → 導入時に手動パッチが要る
- SEQ の `loss_cut=0` は **user04 全損の直接原因**。ゼロで配るな
- 受け子ごとの `bc` BETコード違い(Banker10/Player11)は env 化済み

### 3-4. `.gitignore` の `_*.py` が保全を取りこぼす

`.gitignore:46` に **`_*.py`** ルールがある。`_` 始まりのファイルを全て除外するため、
`git add _vps_prod` では **240本中209本しか入らなかった**(2026-08-22に発覚→`git add -f` で解決)。

取りこぼしていたのは全て稼働中のスクリプトだった:
- `_vps_block_watch.py` … ★root crontab に登録 (`17 * * * *`)
- `_card_analysis_v2_testB.py` … ★毎朝の定例「カード判定して」で使用
- `_now_density_prereg_watch.py` … NOW密度の事前登録監視
- `_card_*.py` 8本 / `_bt_*.py` 7本 / `_v4_*.py` 4本 ほか

このリポジトリはルート直下も `_bt_*.py` `_card_*.py` だらけなので、
**今後 `_vps_prod` 以外を保全する時も同じ罠を踏む**。`git status` に出ないので気付けない。
確認方法:
```bash
find <dir> -type f | wc -l          # 作業ツリー
git ls-files <dir> | wc -l          # git追跡  → 一致しなければ取りこぼし
```
なお金庫の `code/_vps_code.tar.gz` は git ではなく `find` で作ったので **最初から240本全て**入っている。

### 3-4. 未コミットのまま止めた作業

ブランチ `feat/engine-heartbeat` に **SEQ任意開始額**の実装が未コミットで残っている
(`SEQ_CUSTOM_START_2026-08-05.md` 参照・6ファイル)。
GUI のSEQ選択を「15択のプルダウン」から「種類2択 + 開始額自由入力」に変えるもの。
★旧 `small02/3/6/10/30` は $1基準統一により**天井が 3.2〜3.6倍に変わる**(承認済・`loss_cut` で調整前提)。

---

## 4. 復元手順

### 4-1. サーバー再構築

```
OS      : Ubuntu 25.04
Python  : 3.13.3
venv    : /opt/bacopy/.venv (26 pkgs) / /opt/laplace/.venv (55 pkgs)
          → env/requirements_*.txt で再現
主要pkg : websockets 17.0.1 / requests 2.33.1 / numpy 2.4.4
          camoufox 0.4.11・playwright 1.58.0 は dga直結化後は不要のはず
```

1. `code/_vps_code.tar.gz` を `/opt/` に展開 → `laplace2/` `bacopy/` `laplace/` が復元される
2. `secrets/_vps_secrets.tar.gz` から `.env` 4本を各ディレクトリに戻す
   (ファイル名 `envfile__opt_laplace2_.env` = 元 `/opt/laplace2/.env`)
3. systemd unit 10本を `/etc/systemd/system/` へ、`crontab_root.txt` を `crontab -` で復元
4. `data/*.gz` を `gzip -d` して各所へ戻す
   - `analytics_pragmatic.sqlite3` → `/opt/laplace2/`
   - `bacopy.sqlite3` `decisions.jsonl` → `/opt/bacopy/data/`
   - `baccarat.db` → `/opt/laplace/data/` 、`analytics.sqlite3` → `/opt/laplace/`
   - `card_feed.jsonl*` → `/opt/laplace2/`

### 4-2. 稼働していたサービス

```
bacopy-api.service                 master API (/master UI)
bacopy-card-collector.service      カード収集 (dga lobby WS, read-only)
bacopy-hourly-stats-watch.service  hourly_stats.json 鮮度監視 (安全モード)
bacopy-pragmatic-collector.service スナップショット収集
laplace-api.service                LAPLACE Logic API (FastAPI)
```

root crontab (8本):
```
0 */6 * * *  clustering_test.py
0 *    * * *  hourly_report.py          ← テレグラム勝率レポート
*/10 * * * *  hourly_stats_json.py
5 9    * * *  pattern_prereg_watch.py
*/10 * * * *  winrate_history.py
*     * * * *  winrate_trend.py
17 *   * * *  _vps_block_watch.py
@reboot       run_dual_line_bot.sh      ← シグナルbot本体
```

★**crontab は root だけではない。`laplace` ユーザーにも4本ある**(2026-08-22に発覚)。
`crontab -l` だけ見ると取りこぼす。復元時は両方戻すこと。

laplace crontab (4本):
```
0 6    * * *  auto_update_tables.py      (cd /opt/laplace・LAPLACE_API_KEY を行内に持つ)
*      * * * *  run_crypto_poller.sh     ← 毎分。.env.crypto を毎回 source する
5 10   * * *  dow_winrate_report.py
*/10   * * * *  run_wallet_watch.sh      ← bafather.uk /api/cron/wallet-watch を叩くだけ
```
両方の完全な内容は金庫 `secrets/crontab_ALL_USERS.txt`。

### 4-3. 検証コマンド

```bash
cd V:/_ARCHIVE_BACOPY_SHUTDOWN_20260822/data
sha256sum -c SHA256SUMS.txt      # → 27件すべて OK になること
```

---

## 4-4. ★停止時にロックダウンを掛けてある — 復旧しないと何も動かない

2026-08-22 19:34、解約前の措置として**全ユーザー + 全Telegramチャンネル**をロックした。
**サーバーを復元しただけでは配信もBETも再開しない。**以下を戻すこと。

### (a) Supabase — 全13アカウント `bot_paid=false`

ロック前は9名が `true` だった(`black0213black` / `sarah_ttotto` / `milliongold999` /
`hikata11462662` / `lselfloveself` / `hiroyuki2626123` / `shojihashimoto0922` /
`masakatsu.ogawa` / `skjpcoltd`)。

```bash
python _billing_lockdown.py --restore _billing_lockdown_backup_20260822_193406.json
```
バックアップJSONは repo直下。★`--all` フラグは 2026-08-22 に追加したもので、
KEEP(過去の部分ロックダウン用の許可リスト)を無効化して全員を止める。

⚠️ **解除してもエンジンは自動再始動しない。** `bot_paid=true` に戻ると GUI の
ライセンス表示が戻って START が押せるようになるだけ。各受け子の手動START操作が要る。

### (b) Telegram — 全宛先を空文字にしてある

`_tg_lockdown.sh lock` は**使っていない**(あれはミラーchへ付け替える実装で、
今回の「全停止」には合わない)。`.env` を直接空にした。

| ファイル | 空にした変数 |
|---|---|
| `/opt/laplace2/.env` | `TELEGRAM_CHAT_ID` `DUAL_LINE_CHAT_ID` `DUAL_LINE_RESULT_CHAT_ID` `DUAL_LINE_EXTRA_CHAT_IDS` `WINRATE_CHAT_ID` |
| `/opt/bacopy/.env` | `DUAL_LINE_RESULT_CHAT_ID` |
| `/opt/laplace/.env` | `TELEGRAM_CHAT_ID` `ADMIN_CHAT_ID` |
| `/opt/laplace2/.env.crypto` | `PAYMENTS_TELEGRAM_CHAT_ID` |

復旧は `*.bak_tgfullstop_20260822_*` から戻す(4ファイルとも同名規則でバックアップ済み)。
★これらのバックアップは**VPS上にしかない**。解約前に金庫へ回収すること。
なお金庫 `secrets/_vps_secrets.tar.gz` には**ロック前**の `.env` が入っているので、
そちらから復元しても同じ結果になる(こちらの方が確実)。

### (c) ロック時に分かった実装上の罠

- `hourly_report.py` の宛先は `WINRATE_CHAT_ID` → `DUAL_LINE_CHAT_ID` → `TELEGRAM_CHAT_ID`
  の**フォールバック**。1つでも値が残っていると配信が続く
- `_send_telegram` はメインの chat_id が空だとミラーにも送らない実装。
  だから「全部空」で完全停止になる
- 全宛先が空なら `hourly_report.py` は `dry` モードに落ちるだけでクラッシュしない
- ハードコードされた chat_id は VPS 上に存在しない(全スクリプト走査で確認)

### (d) bot の再起動方法(★事故防止)

```bash
ps -eo pid,ppid,etime,cmd --no-headers | grep dual_line_pragmatic_bot   # PIDを目視
kill <その python の PID>        # wrapper が約20秒で再起動する
```
★**`pgrep` / `pkill -f` を ssh 越しに使ってはいけない。** パターンが ssh の
`bash -c` 引数自身にマッチして**自セッションを殺す**(2026-08-17に実際に発生)。
`[c]` の括弧トリックを付けても、ssh の引数に文字列が入る限り危険。

---

## 5. 意図的に捨てたもの / 失ったもの

| 対象 | 容量 | 理由 |
|---|---|---|
| `/opt/laplace/monitor/` | **86 GB** | 833,342個のブラウザ auth_state 残骸。完全なゴミ |
| 各種 `*.log` | ~350 MB | 再現不要 |
| `*.bak*` / `__pycache__` | — | ノイズ |
| **card_feed 7/14〜8/01 分** | ~450 MB | ⚠️ **logrotate で既に消失していた**(保持22世代)。ただしカード分析は 7/21 に R1/R2 とも不合格=無エッジ判定済みなので実害は小さい |
| bafather の Chrome キャッシュ等 | 3.8 GB | 再ログインで済む |

---

## 6. 解約しない資産 — 失うと回復不能

| 資産 | 状態 | 理由 |
|---|---|---|
| ドメイン `bafather.uk` | **維持** | 手放すと第三者に取られ再取得不能。年数千円 |
| Telegram チャンネル群 | **放置** | 削除=復旧不能。7/30 に番号変更で3ch失った前科あり。放置なら無料で残る |
| GitHub `jinjinsansan/ba` `bacopy` | **維持** | 無料 |
| Supabase | **ダンプ済** | 無料枠は90日非アクティブで自動停止するが、`supabase/` に全件退避済みなので停止して構わない |
| Vercel `bafather.uk` | 要判断 | 停止してもソースは `ba` repo にある |
| USDT ウォレット秘密鍵 | **対象外** | オーナー確認済み=当該ウォレットは未使用のため保全不要 |

---

## 7. 残タスク (2026-08-22 時点)

- [x] **Supabase 全テーブルのダンプ** — 完了。`supabase/` に 32テーブル(31 + `auth.users`)・931行。
      `pg_dump` 用の資格情報がローカルに無い(Management token / DB password なし)ため、
      `ba/web/.env.local` の `SUPABASE_SERVICE_ROLE_KEY` で PostgREST 経由の全行取得を行った。
      `auth.users` は PostgREST に露出しないので `/auth/v1/admin/users` で別途取得(**13アカウント**)。
      - ⚠️ **パスワードハッシュは返らない**(Supabase側で秘匿)。再開時はユーザー再作成が必要
      - ⚠️ `invite_codes` テーブルは**存在しなかった** = 8/17のマイグレーションSQLは未適用のまま停止した
      - ⚠️ メールアドレスを含むため **git禁止**。金庫のみ
- [x] **B: バックアップHDDへのミラー** — **完了 (2026-08-22 20:5x)**。方式(a)=アーカイブ442MBのみコピー。
      `B:\V\_ARCHIVE_BACOPY_SHUTDOWN_20260822\` へ robocopy(8秒)→ **79/79ファイル ハッシュ完全一致**
      (V: と B: を個別に sha256 して突合)+ 各 `SHA256SUMS.txt` 照合も B: 側で再実行し
      data 27/27・supabase 33/33・lockdown 4/4・bafather 1/1 すべて OK。
      **これで data/secrets/supabase の単一障害点は解消**。フル同期(`backup_vault.ps1` 525GB)は未実施。
      ★B: の実体は `\Device\Harddisk2\Partition1`。Windows が **D:** の文字を割り当てるので
      **エクスプローラーで D: を開かない**(「フォーマットしますか?」= 押すとバックアップ全損)。

  <details><summary>旧記載(未了時の手順・参考)</summary>

      **★これが残っている間のリスク**: コードは 作業ツリー / V: / GitHub の三重化済みだが、
      **`data/` 440MB・`secrets/`・`supabase/` は V: の1箇所にしか無い**
      (メールアドレスと実キーを含むため GitHub に上げられない)。
      しかも V: は 2026-07-31 に **HDD過熱でUSB脱落→マウント破損**を起こした実績がある。

      **やり方は2通り。急ぐなら (a) だけでもリスクは消える:**

      **(a) アーカイブ442MBだけコピー(数十秒・低負荷)**
      B: をマウントして `V:\_ARCHIVE_BACOPY_SHUTDOWN_20260822\` を手動コピーし、
      各 `SHA256SUMS.txt` で照合するだけ。静的で検証済みのファイル群なので
      他セッションが動いていてもロック競合が起きない。

      **(b) フル同期 `C:\Users\USER\bin\backup_vault.ps1`(525GB・長時間)**
      前提: V: / W: / B: が全てマウント済み。node_modules 等の除外はスクリプト内蔵済み
      (★除外なしだと9時間で798GB=2.3倍に膨れて終わらない)。
      注意点:
      - `robocopy /MIR` = **削除も同期する**。実行中にV:側で消したものはB:からも消える
      - `/MT:32` の高負荷 + V: の過熱歴 → **他の作業と同時に走らせない方がよい**
      - `/R:1 /W:1` なので、他プロセスが掴んでいるファイルは1秒で諦めてスキップされる
      - 終了コード 8 以上が失敗。ログは `%TEMP%\backup_vault_*.log`
      - **終わったら B: をアンマウント**(常時マウントはランサムウェア・誤操作がバックアップまで届く)
  </details>
- [x] **`_vps_prod/` と本ファイルの git commit + push** — 完了 (`d1057b0` + `2e2bacc`)。
      240/240 ファイルが `origin/feat/engine-heartbeat` に反映済み。
      秘密情報スキャン実施済み=漏洩0件(`TR7NH...` は USDT-TRC20 の公式コントラクトで秘密ではない)
- [ ] **課金の最終精算** — Supabase 台帳の締め(オーナー判断)
- [ ] **解約実行** — ★上記が完了・検証されてから

**対象外と判断したもの**: USDT ウォレット秘密鍵(未使用のため保全不要・オーナー確認済み 8/22)

### ⚠️ 解約の順序を絶対に守ること

```
1. 受け子への通知              ← 完了 (8/22)
2. 資金引き上げ・課金の最終精算   ← 未了 (オーナー判断)
3. データ吸い出し → ハッシュ検証 → 復元テスト   ← 完了 (Supabase 含む・8/22)
4. B: へのミラー(二重化)   ← 未了 (後日)
5. ★ここまで終わってから解約
```
**解約を先にやると、Xserver が即時削除の場合すべて消える。**

---

## 8. 参照すべき既存ドキュメント

| 文書 | 内容 |
|---|---|
| `INCIDENT_VPS_CLOUDFLARE_DGA_DIRECT_20260803.md` | dga直結化(教訓6項目つき)・障害切り分けの型 |
| `USER_HYPOTHESIS_FRAMEWORK_2026-07-15.md` | オーナーの基本仮説と検証プロトコル。**検証設計前に必読** |
| `USER_COMPARISON_KATSUZAWA_VS_KAJIWARA_2026-08-01.md` | 損失は設定でなく運用が原因と確定した反実仮想BT |
| `SEQ_CUSTOM_START_2026-08-05.md` | 未コミット作業の設計 |
| `SESSION_HANDOFF_2026-08-17_UNKNOWN_SIGNUP_INVITE_GATE.md` | 招待コードゲート(SQL未適用のまま停止) |
| `SESSION_HANDOFF_2026-08-06_BAFATHER_RENEWAL_LAUNCH.md` | サブスク$200/30日モデル |
| `RISK_JP_CDN_BLOCKING_2026-08-06.md` | 総務省→Cloudflare の日本向け451 |

### 再開判断の材料として

エッジ検証はほぼ全て **no-edge** で決着している(追従・逆張り・波乗り・タイ予測・
日次P/B偏り・時間構造・カード・クラスタリング…)。生きているのは
**デュアルライン6パターンの forward OOS** のみ。
再開するなら「新しいエッジを探す」より、**運用(loss_cut・人間の介入を排する)** が
損益を決めるという 8/01 の結論から始めるのが早い。

---

*作成: 2026-08-22 / 保全作業と同一セッション*

---

## 9. ★追記 (2026-08-22 20:33) — 「.env を空にした」が効かない経路があった

§4-4 (b) で「全宛先を空にしたので完全停止」と書いたが、**結果/決済チャンネルへの配信は
その後も 2分おきに継続していた**(オーナーが気付いて発覚)。

### 機序

```
run_dual_line_bot.sh  (PID 1597294 / 8月17日 11:52 起動 ← 一度も再起動していない)
  └─ set -a; source /opt/bacopy/.env ; set +a     ← ★while ループの外。起動時に1回だけ
      └─ while true: python dual_line_pragmatic_bot.py
```

1. ラッパーは **8/17 の値**をシェル環境に取り込んだまま常駐していた
2. 19:35 に `.env` を空にし、19:36 に **python の子プロセスだけ**を再起動した
3. 子プロセスは**ラッパーの古い環境を継承**する → `DUAL_LINE_RESULT_CHAT_ID=-1003981572727,-1004424762109` が生きたまま
4. bot 側は `load_dotenv(_env_path, override=False)` — **既に環境にある値が .env より優先**。
   空の `.env` では上書きできない

証跡: `/proc/3392407/environ` に旧 chat_id が残存。ログ上は
`_send_telegram`(メイン系)= `send skipped` 245件で停止できていたのに、
`_send_telegram_result` 経由の `[V4-SIGNAL-TELEGRAM] firing #35939` は 20:30 まで継続していた。

### 教訓(再開時にも同じ罠を踏む)

- **`.env` を書き換えたら、それを source した「親」を再起動しないと反映されない。**
  子プロセスの再起動では不十分。`/proc/<pid>/environ` で実際に確認すること
- `load_dotenv(..., override=False)` は **継承環境が勝つ**。「.env が正」だと思い込まない
- 「全スクリプト走査でハードコード chat_id なし」は正しかったが、**問題はハードコードではなく
  プロセスのメモリ上に残った環境変数**だった。走査対象がファイルだけでは検出できない
- 検証は「設定を変えた」ではなく **「送信が止まったログ」** で確認する

### 20:33 時点で実施した完全停止

| 対象 | 操作 | 備考 |
|---|---|---|
| `run_dual_line_bot.sh` (1597294) | kill | ★先にこれ。python だけ殺すと20秒後に古い環境で復活する |
| `dual_line_freeze_watchdog.py` (1597297) | kill | `pkill -f` で bot を殺してラッパーに再起動させる作りなので先に停止 |
| `dual_line_pragmatic_bot.py` (3392407) + playwright driver | kill | SIGTERM で正常終了。`Final: signals=16473 resolved=16465 pnl=$-3095.00` |
| `/tmp/dual_line_bot.lock` / `..._freeze_watchdog.lock` | 削除 | |
| root crontab 8本 / laplace crontab 4本 | `crontab -r` | バックアップ= VPS `/root/crontab_{root,laplace}_backup_20260822_fullstop.txt`。★`@reboot run_dual_line_bot.sh` があったので、消さないと再起動で復活していた |
| systemd 5本 (bacopy-api / card-collector / hourly-stats-watch / pragmatic-collector / laplace-api) | stop + **disable** | disable まで実施済み=再起動しても上がらない |
| **孤児 uvicorn (1377594)** | kill | ★**systemd 管理外**。5/20 に `nohup ... &` で手動起動された `laplace_api:app --port 8765`。受け子がポーリングする VPS API 本体。`systemctl stop` だけでは絶対に止まらない |

停止確認: 8765 / 8010 ともに LISTEN なし、camoufox / playwright 残存プロセスなし。

**★`systemctl` と `crontab` だけ見ても全部は止まらない。** 手動 `nohup` 起動の孤児が
3ヶ月間走っていた。再開時も「起動方法が3種類ある」(systemd / cron / 手動nohup)前提で見ること。

### 9-2. デスクトップクラウド bafather (162.43.83.54) の停止 — 20:38

接続: `ssh -i ~/.ssh/laplace_vps Administrator@162.43.83.54`(Windows Server 2022)。
★SSH越しの PowerShell は **Base64 EncodedCommand** で渡すこと(`|` や `"` がエスケープで壊れる)。

停止前の状態: **GUI/engine(electron)は既に動いていなかった**。生きていたのは以下:

| 対象 | 状態 | 操作 |
|---|---|---|
| タスク `bacopy_watchdog` | Running | Stop + **Disable** ★これが GUI を再起動させる本体 |
| タスク `BACOPY-SupportTunnel` | Running | Stop + **Disable**(VPS が落ちた今は無意味) |
| タスク `bacopy_gui_start` | Ready | **Disable**(ログオン/起動時トリガ) |
| `chrome` 13プロセス(8/19 21:19 起動) | 稼働 | kill(賭け用CDPブラウザ) |
| `ssh.exe`(トンネルクライアント) | 稼働 | タスク停止に伴い終了 |

停止確認: chrome 0 / ssh クライアント 0 / electron・python・node 0 / タスク3本とも `Disabled`。
9222・8010・8765 の LISTEN なし。

★**再開時は3つのタスクを `Enable-ScheduledTask` で戻す**(`bacopy_watchdog` `bacopy_gui_start`
`BACOPY-SupportTunnel`)。Disable のままだと「GUIが自動で上がらない」原因になり、
プロセスを直接起動しても watchdog が居ないので落ちたら復旧しない。
なお sshd は落としていない(落とすと自分の接続経路が消えるため)。

**これで VPS・デスクトップクラウドとも稼働ゼロ。** 残タスクは §7 のとおり
①B:ミラー → ②鍵の失効 → ③フォーマット → ④解約(+課金の最終精算)。
