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

### 4-3. 検証コマンド

```bash
cd V:/_ARCHIVE_BACOPY_SHUTDOWN_20260822/data
sha256sum -c SHA256SUMS.txt      # → 27件すべて OK になること
```

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
- [ ] **B: バックアップHDDへのミラー** — オーナー指示により後日。`V:` 単独では二重化になっていない。
      `C:\Users\USER\bin\backup_vault.ps1` の流儀に合わせる(★node_modules を絶対に含めない)
- [ ] **`_vps_prod/` と本ファイルの git commit** — 作業ツリーには存在するが未コミット
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
