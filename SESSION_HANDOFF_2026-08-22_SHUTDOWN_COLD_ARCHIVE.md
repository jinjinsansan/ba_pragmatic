# SESSION HANDOFF 2026-08-22 — プロジェクト全停止と冷凍保存

branch: `feat/engine-heartbeat` (5コミット push済み・作業ツリーに未コミット変更ゼロ)
金庫: `V:\_ARCHIVE_BACOPY_SHUTDOWN_20260822\` (442MB / 72ファイル)
再開手順の本体: **`RESTART_KIT_2026-08-22.md`**(本書は経緯の記録、あちらが手順書)

---

## 1. きっかけと決定

オーナー:
> このプロジェクト全体を一度停止しようと思います。Xサーバーも解約し bafather のデスクトップクラウドも解約しようと思います。ただし、いつか再開するかもしれません。

解約対象:
- Xserver VPS `210.131.215.116` (シグナル生成・カード収集・テレグラム配信の本体)
- Xserver デスクトップクラウド "bafather" `162.43.83.54` (オーナー自身の運用機)

**方針を「解約」ではなく「冷凍保存(コールドアーカイブ)」に置いた。** 実地調査の結果、
再開に必要なものが想定よりはるかに小さく、サーバー代をゼロにしながら資産を
1バイトも失わない形が成立したため。

オーナー確認事項(この順で確定):
- 受け子8人への停止連絡 → **完了済み**
- Telegram チャンネル → **放置でOK**(削除しない)
- Supabase ダンプ → **実施**
- B: バックアップHDDへのミラー → **後日**
- USDT ウォレット秘密鍵 → **未使用のため保全不要**
- git commit / push → **実施**

---

## 2. 調査で分かったこと

### 2-1. VPS 116GB のうち、再開に要るのは 3.2GB だけだった

| 中身 | 容量 | 判定 |
|---|---|---|
| `/opt/laplace/monitor` | **86 GB** | 🗑 **833,342個**のブラウザ auth_state 残骸 |
| ログ各種 | ~350 MB | 🗑 |
| **コード全部** (240ファイル) | **2.4 MB** | ★ |
| DB + JSONL (50ファイル) | **3.08 GB** | ★ |

`/opt` の内訳は laplace 87G / laplace2 5.6G / bacopy 6.5G。**112GB がゴミ**だった。

### 2-2. ★VPS本番bot は repo版と別物 — 実測で確定

メモリに「VPS本番botはrepoと別物」という警告はあったが、差の大きさは未知だった。

| ファイル | VPS本番 | repo (HEAD) | |
|---|---|---|---|
| `dual_line_pragmatic_bot.py` | 160,368 B | 334,502 B | ★**repoが倍以上** |
| `dual_line_money.py` | 14,312 B | 19,087 B | ★差分あり |
| `dual_line_match.py` | 6,648 B | 7,148 B | ★差分あり |
| `dual_line_logic.py` | 13,124 B | 13,124 B | 同一 |

**これを回収せずに解約していたら再開は不可能だった。** 本セッション最大の収穫。
`_vps_prod/` として git に取り込み済み。

### 2-3. 蓄積データはメモリの記録より育っていた

`analytics_pragmatic.sqlite3` の shoes は **282,714行**。
メモリの `reference_analytics_pragmatic_db` は「61k shoes」と記録していたので、約4.6倍。

`bacopy.sqlite3` は decisions **70,663行** / executors 10行。
これが「友人代打ち → AI代理」構想(Phase 4 ML)の学習データそのもの。

---

## 3. 実施した保全

作業中もフリートは稼働継続していた(17:14 に決済ログ更新を確認)。全て読み取り専用で実施。

### 3-1. コード回収

VPS 側で tar を2本作成し、秘密情報とコードを分離:
- コード用: `.py .sh .ps1 .md .txt .csv .json .service`、`.env*` と `*.bak*` と
  `__pycache__` / `auth_state` / `monitor` / `data` を除外 → 240ファイル / 1.3MB
- 秘密情報用: `.env` 実キー4本 + systemd unit 10本 + root crontab → 8KB(**金庫のみ**)

秘密情報スキャンで `LAPLACE_PROJECT.md` に **実トークン2件が平文**で埋まっているのを検出。
原本を金庫 `secrets/plaintext_docs/` に退避し、repo側は伏字化した。

### 3-2. データ保全 — ★稼働中の sqlite は cp してはいけない

稼働中のDBを単純コピーすると壊れたアーカイブになるため、`VACUUM INTO` で
整合スナップショットを取得(sqlite 3.46.1)。4本とも `PRAGMA integrity_check` = `ok`。

JSONL は gzip、sqlite も gzip。**個別gzip + SHA256マニフェスト**方式にした
(1つの巨大tarにしない = 将来1ファイル単位で検証・復元できる)。

結果: **3.08 GB → 440 MB**。転送後にローカルで全件再検証。

### 3-3. 復元テスト(バイト一致だけでは不十分なので実際に開いた)

| DB | 結果 |
|---|---|
| `bacopy.sqlite3` | `integrity_check=ok` / decisions **70,663行** / executors 10行 |
| `analytics_pragmatic.sqlite3` | `integrity_check=ok` / shoes **282,714行** |

### 3-4. bafather デスクトップクラウド

AppData 3.8GB の大半は Chrome キャッシュとログ(廃棄可)。
固有で価値があるのは小さな設定・状態ファイルのみ → **55ファイル / 185KB** を抽出。
中でも `dual_line_billing_state.json`(120KB・課金状態)が本命。

### 3-5. Supabase

`pg_dump` 用の資格情報がローカルに無い(Management token / DB password なし)ため、
`ba/web/.env.local` の `SUPABASE_SERVICE_ROLE_KEY` で PostgREST 経由の全行取得を実装。

- 通常テーブル **31本 / 931行**、失敗0
- `auth.users` は PostgREST に露出しないので `/auth/v1/admin/users` で別途 → **13アカウント**
- SHA256 33/33 OK

---

## 4. 副産物の発見

| 発見 | 内容 |
|---|---|
| **`invite_codes` テーブルが存在しない** | 8/17 の招待コードマイグレーションSQLは**未適用のまま停止**した。サイト経由の新規登録は503 fail-closed のまま(停止するので実害なし) |
| **card_feed 7/14〜8/01 が消失済み** | logrotate が22世代しか保持しておらず、残っていたのは 8/02 以降のみ。ただしカード分析は 7/21 に R1/R2 とも不合格=無エッジ判定済みなので実害は小さい |
| **`bacopy-card-collector.service` に camoufox 時代の引数が残存** | `--profile`/`--cookies` が ExecStart に残っている。8/3 の dga直結化でコードは変わったので実害なしだが、再構築時の混乱要因 |
| **パスワードハッシュは取得不可** | Supabase が秘匿するため。再開時はユーザー再作成が必要 |

---

## 5. ★教訓 — `.gitignore` が保全を静かに取りこぼす

`git add _vps_prod` で **240本中209本しか入らなかった**。
`.gitignore:46` の **`_*.py`** ルールが `_` 始まりを全除外していたため。

取りこぼしていたのは全て**稼働中**のスクリプトだった:

| ファイル | 用途 |
|---|---|
| `_vps_block_watch.py` | ★**root crontab に登録**(`17 * * * *`) |
| `_card_analysis_v2_testB.py` | ★毎朝の定例「カード判定して」 |
| `_now_density_prereg_watch.py` | NOW密度の事前登録監視 |
| `_card_*.py` 8本 / `_bt_*.py` 7本 / `_v4_*.py` 4本 | 分析・パッチ群 |

**`git status` に出ないので気付けない。** `git add -f` で解決。

金庫の `code/_vps_code.tar.gz` は git でなく `find` で作ったため**最初から240本全て**
入っていた。保全自体は無事で、git側のコピーだけが不完全だった。

> **今後の保全時は必ず突き合わせる:**
> ```bash
> find <dir> -type f | wc -l     # 作業ツリー
> git ls-files <dir> | wc -l     # git追跡  → 一致しなければ取りこぼし
> ```
> このリポジトリはルート直下も `_bt_*` `_card_*` だらけなので、今後も同じ罠を踏む。

### 秘密情報スキャンの結果

push 後に全コミット済みファイルを2方式(JSON構造 / 高エントロピー文字列)で走査。
**漏洩0件**。唯一ヒットした `crypto_payment_poller.py` の `TR7NH...` は
**USDT-TRC20 の公式コントラクトアドレス**であり公開情報。

---

## 6. コミット履歴

```
dcc631b  docs: カード仮説の本判定(7/21)を記録 + RESTART_KIT に gitignore の罠を追記
046f140  fix(receiver): ロビー直リンクを廃止して casino/home 経由にする
5e3de90  feat(money): SEQ開始額を任意入力化 (smallcustom / playercustom)
2e2bacc  archive(vps): .gitignore で漏れた運用スクリプト31本を追加 (-f)
d1057b0  archive: プロジェクト全停止に伴うVPS本番コード回収 + 再開キット
```

`5e3de90` と `046f140` は前セッションから未コミットで残っていたもの。
停止前に確実に残すため、性質ごとに分けてコミットした。

★`5e3de90` は挙動が変わる: 旧 `small02/3/6/10/30` は $1基準への統一で
**天井が 3.2〜3.6倍に変化**する(承認済み・`loss_cut` で調整前提)。

### 運用メモ

git の write 系(`add`/`commit`/`push`)は auto-mode の分類器に一律ブロックされたため、
オーナーが `!` プレフィクスで実行した。次回同じ作業をする時も同様の想定。

---

## 7. 保全物の全体像

`V:\_ARCHIVE_BACOPY_SHUTDOWN_20260822\` — **442MB / 72ファイル**

| ディレクトリ | 容量 | 中身 | 検証 |
|---|---|---|---|
| `data/` | 440 MB | sqlite 4本 + JSONL 23本 (生3.08GB) | SHA256 27/27・gzip 27/27・復元テスト2件 |
| `code/` | 1.3 MB | VPS全コード 240ファイル | — |
| `supabase/` | 98 KB | 32テーブル + 13アカウント | SHA256 33/33 |
| `bafather_desktopcloud/` | 185 KB | 固有55件(課金状態JSON) | SHA256 1/1・zip整合 |
| `secrets/` | 16 KB | `.env`実キー4本・systemd・cron・平文トークン文書 | ★git禁止 |
| `env/` | 9 KB | pip freeze 2本・OS情報・envキー名 | — |
| `README_RESTART_KIT.md` | 16 KB | repo版と同一 | — |

**GitHub** にはコード240/240 + RESTART_KIT が入っている(秘密は除外済み)。
つまり コード = 作業ツリー / 金庫 / GitHub の**三重化**、
データ・秘密 = 金庫のみ(B:ミラーで二重化予定)。

---

## 8. 残タスク

- [ ] **B: バックアップHDDへのミラー** — オーナー指示により後日。
      `C:\Users\USER\bin\backup_vault.ps1` の流儀に合わせる(★node_modules を絶対に含めない)
- [ ] **課金の最終精算** — Supabase 台帳の締め(オーナー判断)
- [ ] **解約実行** — ★上記2件の後

### ⚠️ 解約の順序

```
1. 受け子への通知                              ← 完了 (8/22)
2. 資金引き上げ・課金の最終精算                  ← 未了
3. データ吸い出し → ハッシュ検証 → 復元テスト    ← 完了 (8/22)
4. B: へのミラー(二重化)                       ← 未了
5. ★ここまで終わってから解約
```

**解約を先にやると、Xserver が即時削除の場合すべて消える。**

---

## 9. いつか再開する時に

### 9-1. 最初にやること

**`RESTART_KIT_2026-08-22.md` を読む。** 本書ではなくあちらが手順書。
特に §3「再開時に踏む地雷」を読まずにコードを触らないこと。

いちばん危ないのは **「3系統の `dual_line_*.py`」**:

| 場所 | 正体 |
|---|---|
| `_vps_prod/laplace2/` | VPSシグナルbot(**本番**) |
| `_rev53144/` | 受け子engineのビルド元(**本番**) |
| repo直下 | どちらでもない**回帰版。触るな** |

`_deploy_vps.ps1` は repo直下を VPS へ上書きする作りなので、**そのまま実行してはいけない**。

### 9-2. 朗報

2026-08-03 の dga直結化により、シグナル取得は **ブラウザ不要**になっている
(`wss://dga.pragmaticplaylive.net/ws` は casinoId だけで購読でき、Stakeログインも不要)。
**再開時に Cloudflare の壁を踏むことはない。** これは大きい。

サーバー再構築の要件は Ubuntu 25.04 / Python 3.13.3、依存は venv 2本で計81パッケージ。
`env/requirements_*.txt` で再現できる。

### 9-3. 再開するなら、どこから始めるか

エッジ検証は**ほぼ全て no-edge で決着している**。
追従・逆張り・波乗り・タイ予測・日次P/B偏り・時間構造・カード(R1/R2)・
クラスタリング・ドラゴン追い抜き・連敗予兆 — いずれも不合格。
生きているのは **デュアルライン6パターンの forward OOS** のみ。

一方で 2026-08-01 の反実仮想バックテストが出した結論が効いている:

> 同じ設定を**機械的に**回すと全員プラスだった(小川 +361 / ひろゆき +1,082)。
> **損失は設定ではなく運用が原因。** 大負けしたのは人間が介入した2人だけで、
> 介入しなかった受け子は賭けではプラスだった。

つまり再開時に「新しいエッジを探す」のは筋が悪い。
**運用を固定する(`loss_cut` を必ず入れる・人間の介入を排する)** ことが
損益を決めるという結論から始めるのが早い。

★`loss_cut=0` は user04 全損の直接原因。ゼロで配ってはいけない。

### 9-4. 停止時点で宙に浮いていたもの

- **招待コードゲート**: SQL未適用のまま停止。再開して新規受け子を入れるなら
  `ba/web/supabase/invite_codes_migration_20260817.sql` を先に流す
- **bafather サブスクモデル**($200/30日)は 8/6 に本番公開済みだが、
  切替日ロールアウトSQLと初回入金E2Eは未実施
- **日本向け CDN 遮断リスク**: 総務省→Cloudflare で sportsbet.io が451。
  stake.com は未対象だが、**受け子の賭け経路が単一障害点**として残っている(監視未実装)

### 9-5. 再開しないと決めた場合

金庫の442MBだけ保持しておけばよい。ドメイン `bafather.uk` を手放す判断をする時は、
第三者に取られて再取得できなくなる点だけ承知の上で。
Telegram チャンネルと GitHub は放置しても費用がかからない。

---

## 10. 参照

| 文書 | 内容 |
|---|---|
| **`RESTART_KIT_2026-08-22.md`** | ★**再開手順の本体。まずこれ** |
| `INCIDENT_VPS_CLOUDFLARE_DGA_DIRECT_20260803.md` | dga直結化・障害切り分けの型(教訓6項目) |
| `USER_HYPOTHESIS_FRAMEWORK_2026-07-15.md` | オーナーの基本仮説と検証プロトコル。**検証設計前に必読** |
| `USER_COMPARISON_KATSUZAWA_VS_KAJIWARA_2026-08-01.md` | 「損失は運用が原因」の反実仮想BT |
| `SEQ_CUSTOM_START_2026-08-05.md` | `5e3de90` の設計 |
| `SESSION_HANDOFF_2026-08-17_UNKNOWN_SIGNUP_INVITE_GATE.md` | 招待コードゲート(SQL未適用) |
| `SESSION_HANDOFF_2026-08-06_BAFATHER_RENEWAL_LAUNCH.md` | サブスクモデル |
| `RISK_JP_CDN_BLOCKING_2026-08-06.md` | 日本向け451リスク |

---

*記録: 2026-08-22 / 保全作業と同一セッション*
