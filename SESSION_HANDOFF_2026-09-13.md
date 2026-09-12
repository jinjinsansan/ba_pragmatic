# 引継ぎ書 — 2026-09-13 (明日やること)

> **★これが最新の引継ぎ書。** 「最新の引継ぎ書を読んで」と言われたらこれを開く。
> 前提知識ゼロでも、この1本を読めば続きから再開できるように書いてある。

---

## 0. 30秒で分かる現在地

2026-08-22 に全停止したバカラ支援システムを、**3チーム分**同時に復活させている。

| | チーム | 何をする | 状態 |
|---|---|---|---|
| ① | **梶原**(日本) | 元通り。テレグラム4ch + GUI + SEQ | 未着手 |
| ② | **韓国** | 梶原ロジックを技術を盗ませずに貸す | 未着手 |
| ③ | **田辺**(日本) | マスター画面(卓移動 + B/P/T)→ 受け子が追随BET | ★**明日ここ** |

**9/12(昨日)に到達したところ**: サーバー構築・マスター画面稼働・受け子実装・ミラー版engineビルド、まで完了。
**残っているのは「実機で本当に BET できるか」だけ。**

---

## 1. ★明日やること(1行)

**Xserver のデスクトップクラウドを契約 → 田辺版受け子を設置 → マスターから実際に BET させる。**

---

## 2. まず最初に — 生きているか確認する

```bash
# マスターAPI (これが返れば土台は生きている)
curl -s https://master.bafather.uk/api/health
# → {"ok": true}

# VPS の全サービス
ssh -i ~/.ssh/bacopy_vps_2026 root@160.251.211.181 \
  'for s in bacopy-api bacopy-collector caddy bacopy-origin-check.timer; do printf "%-26s %s\n" $s $(systemctl is-active $s); done'
# → 全部 active
```

**2026-09-12 23:10 時点の実測値**(比較用):

```
bacopy-api / bacopy-collector / caddy / origin-check.timer  → 全て active
現在のオリジン : https://stake.ac  (候補21本)
卓データ       : 60卓 / 罫線あり 59卓
ディスク       : 5.8G / 99G (7%)
メモリ         : 436 / 1966 MB
```

---

## 3. 接続情報(すべてここに集約)

| 対象 | 値 |
|---|---|
| **マスター画面** | `https://master.bafather.uk/master` |
| マスターのPW | **`tanabe`** |
| **VPS** | `ssh -i ~/.ssh/bacopy_vps_2026 root@160.251.211.181` (鍵のみ・PW認証は無効) |
| VPS事業者 | ConoHa VPS 2GB / Ubuntu 24.04.3 / 東京 |
| **APIキー取得** | `ssh -i ~/.ssh/bacopy_vps_2026 root@160.251.211.181 'grep BACOPY_API_KEY /opt/bacopy/.env'` |
| DNS | Cloudflare。`master.bafather.uk` A → `160.251.211.181`(★**DNSのみ。プロキシにしない**) |
| リポジトリ | `V:\dev\Cusor\bacopy` / ブランチ `feat/engine-heartbeat`(9/12 に push 済み) |

★**締め出された時の逃げ道**: ConoHa のコンソール(VNC)から
`/etc/ssh/sshd_config.d/00-bacopy-hardening.conf` を消して `systemctl restart ssh`。

★**マスターのログインを何度も間違えると15分ブロックされる**(5回失敗で発動)。
解除は `systemctl restart bacopy-api`(状態はメモリのみ)。

---

## 4. ★明日の最大のリスク — ここを最初に確かめる

**ミラー版のブラウザは Camoufox であり、実Chrome(:9222 CDP)ではない。**
8/28 の計画書の記述は誤りで、9/12 に実測で判明した。

```
grep -c                                 connect_over_cdp   chrome_attach
  bacopy_executor_pragmatic_ws_live.py         0                0     ← ミラー版はこれ
  dual_line_pragmatic_bot.py                   1               12     ← 梶原版はこれ
```

**なぜ今まで気付かなかったか**: 8/28 と 9/12 の「一周実証」はどちらも
`bacopy_executor_dryrun.py`(ブラウザに触れないツール)で行った。
decision の往復と結果観測は確かに通ったが、**BET の実行経路は未検証のまま**。

→ **「配管は通った」と「BETできる」は別。**

**懸念**: 2026-08-03 に VPS の headless Camoufox が Cloudflare に弾かれて全配信停止した前科がある。
明日は headful(画面あり)なので通る見込みはあるが、**確証はない**。

詳細と失敗時の3案 → **`MIRROR_BROWSER_PATH_2026-09-12.md`**

---

## 5. 明日の手順

### 5-1. 事前(デスクトップクラウド契約より前に)

★**Stake のパスワードを変更する。**
`CREDENTIAL_REVOCATION_CHECKLIST_2026-08-22.md` の🔴6件が未着手のまま。
明日そのアカウントでログインするので、**その前に**変えておくのが順序として自然。

### 5-2. デスクトップクラウド契約

Xserver のデスクトップクラウド(Windows Server)。旧 bafather 機と同じ用途。
契約したら **IPとログイン情報をこの文書の §3 に追記する**。

### 5-3. 受け子の設置

**運ぶもの**

| # | 何を | どこから |
|---|---|---|
| 1 | インストーラ | 下記 5-4 で生成する |
| 2 | Chrome / Camoufox の動く環境 | デスクトップクラウド側 |
| 3 | Stake アカウント | オーナー |

**`.env` に入れる値**

```
BACOPY_API_URL=https://master.bafather.uk
BACOPY_API_KEY=<§3 のコマンドで取得>
BACOPY_EXECUTOR_ID=tanabe01
BACOPY_EXECUTOR_LABEL=田辺01
BACOPY_STANDALONE=1
BACOPY_MONEY_MODES=dalembert,martingale,grand_martingale
```

★**オリジンは書かなくてよい。** マスターの `/api/origin` が配る(現在 `https://stake.ac`)。
★**Supabase / LAPLACE のキーは入れない。** standalone ビルドなら自動で除外される。

### 5-4. インストーラの生成(まだやっていない)

```powershell
# ★1本だけ engine/ に置く。full 版が残っていると梶原ロジックが流出する
powershell -File scripts/stage_engine.ps1 -Variant mirror

cd copytrade_gui
# BACOPY_MONEY_MODES 等を build_staging/.env へ書き込む
$env:BACOPY_STANDALONE="1"
$env:BACOPY_MONEY_MODES="dalembert,martingale,grand_martingale"
$env:BACOPY_STAKE_ORIGIN="https://stake.ac"
node scripts/provision-user-build.js <引数は既存の運用に合わせる>

npm run build:installer
```

ビルド済みの engine:

```
copytrade_gui/build_staging/engine_variants/
    bacopy_engine_mirror.exe  214 MB  sha256 5a06cefc82b5ca6b  ← 田辺用
    bacopy_engine_full.exe     66 MB                            ← 梶原用
```

### 5-5. ★実機テスト(上から順に。1が通らないと以降は無意味)

| # | 確認すること | 失敗したら |
|---|---|---|
| **1** | **Camoufox が起動し、ミラーにログインできるか** | `MIRROR_BROWSER_PATH` §4 の3案へ |
| 2 | Cloudflare の JSチャレンジを通過するか | 同上 |
| 3 | マスターから SWITCH_TABLE → 卓が動くか | ロビーDOM の変更を調査 |
| 4 | `casinoId` が `ppcds00000003709` か | `.env` の `BACOPY_PRAGMATIC_CASINO_ID` で調整(**再ビルド不要**) |
| 5 | **$0.20 の BET が受理されるか** | `lpbet` 形式の変更を疑う |
| 6 | ack と result がマスターへ返るか | ここまで来れば実証完了 |

★**$0.20 でやること。** 開始額は GUI の設定で変えられる。

---

## 6. 昨日までに作ったもの(何が既に動くか)

| 対象 | 状態 |
|---|---|
| マスター画面 | 卓グリッド + 罫線 + **B/P/T** ボタン(TIEは表示のみ・§7参照) |
| 受け子の認証ゲート | 除去済み(`BACOPY_STANDALONE=1` で Supabase 不要) |
| 受け子のオリジン追従 | マスターから取得。**繋がらなくても止まらない**縮退設計 |
| 資金管理3方式 | ダランベール / マーチンゲール / グランドマーチンゲール |
| ミラー自動フェイルオーバー | 30分毎に451を検知して次候補へ切替 |
| 配管の一周 | 本番マスターで実証済み(実弾なし) |

---

## 7. ★保留中の項目(明日触れる可能性があるもの)

### TIE の BETコードが未確定

マスター画面の TIE ボタンは**表示されるが押せない**(実行できる受け子が居ないため disabled)。

理由: `B=10 / P=11` は実測値だが、**TIE は既定 `2` の推測値のまま**。
誤ったコードで送ると**意図しない側に賭ける**。

確定させるには受け子1台で `sniff_pragmatic_bet_ws.py` を回して
`BACOPY_PRAGMATIC_BC_TIE` を実測する。その後 engine に `--allow-tie` を付ける。

→ **明日 BET が通ったら、その流れで実測しておくと良い。**

### 旧鍵の失効(🔴6件・未着手)

`CREDENTIAL_REVOCATION_CHECKLIST_2026-08-22.md`。
Stakeパスワード変更 / ANTHROPIC_API_KEY revoke / Telegram bot トークン3本の再発行 など。

---

## 8. 読む順序(困ったとき)

| # | 文書 | 何が書いてあるか |
|---|---|---|
| 1 | **本ファイル** | 明日やること |
| 2 | **`MIRROR_BROWSER_PATH_2026-09-12.md`** | ★実機テスト前に必読。Camoufox の件と失敗時の3案 |
| 3 | `RESUME_3TRACK.md` | **生きた台帳**。完了/未完了・検証コマンド・罠の一覧 |
| 4 | `SESSION_2026-09-12_REVIVAL_DAY1.md` | 昨日の記録。実測事実・踏んだ罠7件・訂正した誤り5件 |
| 5 | `REVIVAL_PLAN_3TRACK_2026-09-12.md` | 3チーム分の設計と全体計画 |
| 6 | `DUAL_LINE_VARIANTS.md` | ★3系統の `dual_line_*.py` のどれが正か |
| 7 | `PHASE0_DGA_BASELINE_2026-09-12.md` | 60卓の qpid 対応表(ミラー検証時の突合先) |
| 8 | `RESTART_KIT_2026-08-22.md` | 停止時の全体像・金庫の中身 |

---

## 9. ★この環境で必ず踏む罠(作業前に読む)

| 罠 | 対処 |
|---|---|
| `grep -r ... \| pipe` が**空を返す** | バッファリングで `timeout` に殺されると結果が出ない。**`git grep` か ripgrep を使う** |
| heredoc に `\b` を書くと**バックスペース(0x08)**になる | 端末で不可視。正規表現が無言で壊れる。**バックスラッシュを含むコードは Write/Edit で書く** |
| このリポジトリは **CRLF 混在** | 複数行の文字列置換は改行を正規化してから当てる |
| `python -c "print('✅')"` が落ちる | `export PYTHONIOENCODING=utf-8`。ASCII 出力が無難 |
| PowerShell 5.1 が `.ps1` の日本語を化けさせる | **UTF-8 BOM** を付ける |
| exe の検証を**文字列検索でやる** | ★必ず**実行**して確かめる。`--help` は遅延importの前に処理されるので検証にならない |
| `build_staging/engine` を整理せずに梱包 | electron-builder が**丸ごと**同梱する。`stage_engine.ps1` で1本だけ置く |
| `_deploy_vps.ps1` を実行する | 直下の回帰版を本番へ上書きする。**ガード済みで即 exit 1 する** |
| 直下の `dual_line_*.py` を「整理」する | `bacopy_api.py` 含む約25本が import している。**移動・削除禁止** |
| `.env` を変えても効かない | **source した親プロセスを再起動**。子だけでは不十分 |

---

## 10. git の状態

```
ブランチ feat/engine-heartbeat
2026-09-12 に 21コミット push 済み (origin との差 0)
追跡ファイルの未コミット: 0
```

★**git に入っていない変更**がある(`.gitignore` で意図的に除外):
`analyze_*.py` 等14本 / `dist_client/` 4本 / `build_staging/.env` / asar スクラッチ13本。
いずれも旧IPの無害化。作業ツリーを失うと消えるが、再適用は1行程度。
詳細は `RESUME_3TRACK.md` §6(b)。

---

*作成: 2026-09-12 深夜 / 対象: 2026-09-13*
