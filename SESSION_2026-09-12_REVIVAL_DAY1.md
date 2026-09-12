# セッション記録 — 3方向復活 初日 (2026-09-12)

> 計画立案から VPS 構築・マスター稼働・受け子実装まで一気に進んだ日。
> 進行状況の台帳は `RESUME_3TRACK.md`(こちらが常に最新)。本ファイルはこの日の記録。

---

## 0. この日やったこと (時系列)

| # | 内容 |
|---|---|
| 1 | 3方向復活の計画立案(①梶原 / ②韓国 / ③田辺) |
| 2 | `stake.com` が日本から 451 になっていることを実測で確認 |
| 3 | 旧VPS IP の残骸を42箇所除去、`.env.dist` の実キーも掃除 |
| 4 | ConoHa VPS 契約 → OS更新 → 鍵認証化 → マスターAPI/コレクタ稼働 |
| 5 | DNS を Cloudflare で修正 → TLS 取得 → `https://master.bafather.uk/master` 稼働 |
| 6 | ログイン総当たり対策、Supabase依存の切り離し |
| 7 | ミラー自動フェイルオーバー(`/api/origin`)を実装 |
| 8 | 本番マスターで③の配管を一周実証(実弾なし) |
| 9 | 田辺仕様の確定(B/P/T + 資金管理3方式)と実装 |
| 10 | ミラー版 engine のビルド |

---

## 1. ★実測で分かった重要な事実

### 1-1. `stake.com` は日本から死んでいる

```
stake.com          → HTTP 451 + "Legal Reasons"   = 法的ジオブロック
ミラー121本         → HTTP 403 + "Just a moment"   = Cloudflare JSチャレンジ = 生存
dga.pragmaticplay  → 応答あり・AWS                 = Cloudflare非経由 = 無傷
```

★**「非CDN URL」は誤解。** ミラーも全部 Cloudflare 上にいる。CDN を迂回しているのではなく、
**まだ 451 のブロック対象リストに載っていないだけ**。通知はドメイン単位なので1本ずつ焼かれる。
連番の歯抜け(`stake1022` → `1034`)は焼かれたドメインの墓場で、
`stake3000.com → 301 → stake3015.com` と Stake 自身が転送している。

→ **オリジンを exe に焼かない設計**にした(`/api/origin`)。

### 1-2. `casinoId` と qpid は健在

VPS(日本)から dga 直結フィードを70秒走らせた実測:

```
[DGA-DIRECT] connected (1.1秒)
msgs=625 / shuffles=3 / tables=60 / exit=0
casinoId ppcds00000003709 は有効 (卓一覧に STAKE SPEED BACCARAT あり)
Baccarat 3 の qpid = cbcf6qas8fscb222  ← 6月にBET突破した時と同一値
```

60卓の qpid 対応表は `PHASE0_DGA_BASELINE_2026-09-12.md`。

### 1-3. ★ミラー版のブラウザは Camoufox だった

`MIRROR_REVIVAL_PLAN_2026-08-28.md` の「実Chrome を :9222 CDP接続」は**誤り**。
詳細と明日の手順は **`MIRROR_BROWSER_PATH_2026-09-12.md`**(実機テスト前に必読)。

### 1-4. ★資金管理の経路が2系統あった

```
dual-line (梶原)           --money-mode → dual_line_money.BetManager
executor-pragmatic (田辺)   --bet-mode   → Seq7Session
```

田辺チームが使う後者には **martingale も dalembert も存在しなかった**(grep 0件)。
SEQ配列ベースのモードしか無かったため、連敗ベースの進行をエンジン側に追加した。

★**同じ式が2箇所に存在する状態**になったので、コード内に「片方だけ直すと乖離する」旨を明記。

---

## 2. 作ったもの

| 対象 | 内容 |
|---|---|
| **VPS** | ConoHa 2GB / Ubuntu 24.04.3 / kernel 6.8.0-139 / 鍵認証のみ / ufw 22,80,443 |
| **マスターAPI** | `/opt/bacopy/` + venv(websockets, requests のみ)+ systemd |
| **コレクタ** | `/opt/collector/` dga直結のみ(`BACOPY_DGA_ONLY=1`)・ブラウザ不要 |
| **Caddy** | `master.bafather.uk` → 127.0.0.1:8010 / Let's Encrypt 自動取得 |
| **オリジン監視** | `bacopy-origin-check.timer` が30分毎に451を検知して切替 |
| `stake_origin.py` | 451判定・候補取得・自動ローテーション |
| `stake_origin_client.js` | 受け子側の追従(繋がらなくても止まらない縮退設計) |
| `scripts/build_bacopy_engine_mirror.ps1` | ミラー版 engine(梶原ロジック非同梱) |
| `scripts/stage_engine.ps1` | ★梱包前に engine を1本だけ置く(混入事故の防止) |

**メモリ使用量**: api 29MB + collector 38MB = **計67MB**(1966MB中)。1GBプランで十分だった。

---

## 2-2. ミラー版 engine のビルド結果

```
copytrade_gui/build_staging/engine_variants/
    bacopy_engine_full.exe     66 MB   ← 梶原チーム用 (既存ビルド)
    bacopy_engine_mirror.exe  214.4 MB  ← 田辺チーム用 (今回ビルド)
                                          sha256 5a06cefc82b5ca6b
```

★**検証は実行で行った**(文字列検索では確かめない):

```
ミラー版  dual-line → ModuleNotFoundError: No module named 'dual_line_pragmatic_bot'
full 版   dual-line → import は通り、Chrome(:9222) へ接続を試みて失敗
```

→ 梶原ロジックがミラー版に**物理的に存在しない**ことを実行で確認。
設定で無効化しているのではなく、不在。

★`--help` では検証にならない。argparse が遅延importの**前**に処理して exit 0 するため、
除外できていなくても成功に見える。**importを実際に踏ませる引数**で試すこと。

★**`build_staging/engine` は electron-builder が丸ごと同梱する。**
full 版を置いたまま田辺版を焼くと梶原ロジックが流出するので、
`scripts/stage_engine.ps1 -Variant mirror` で**必ず1本だけ**置いてから梱包する。

---

## 3. ★踏んだ罠(再発しやすいもの)

| 罠 | 内容 |
|---|---|
| **`grep -r \| pipe` が空を返す** | ブロックバッファリングで `timeout` に殺されると結果が出ないまま空に見える。それを「クリーン」と誤読して「旧IP全消去」と誤報告した。**検証には `git grep` か ripgrep を使う** |
| **heredoc のバックスラッシュ** | `\b` が**リテラルのバックスペース(0x08)**になり、端末で不可視。正規表現が無言で何にもマッチしなくなる。`co_consts` を見て初めて判明。**バックスラッシュを含むコードは Write/Edit で書く** |
| **sshd は最初の指定が勝つ** | `99-` で置いた設定が `50-cloud-init.conf` に負けていた。`00-` に改名して解決 |
| **Caddy のファイルログ** | `/var/log/caddy` はユニットの `ProtectSystem` で書けず起動失敗する。journald に出す |
| **Cloudflare の XFF は追記** | 偽XFFが先頭に入りIP偽装で制限回避できる。`header_up X-Forwarded-For {remote_host}` で上書きさせる |
| **CRLF ファイルの複数行置換** | このリポジトリは CRLF 混在。複数行パターンは改行を正規化してから当てる |
| **`build_staging/engine` は丸ごと同梱** | full 版が残っていると田辺版に梶原ロジックが混入する。`stage_engine.ps1` で必ず1本にする |

---

## 4. ★訂正した自分の誤り

| 誤り | 訂正 |
|---|---|
| 「旧IPは実行系から全消去した」 | **誤報告**。バッファリングで欠落した grep 結果を信じていた。実際は42箇所残っていた |
| 「直下の `dual_line_*.py` を隔離する」 | **実行すると壊れる**。`bacopy_api.py` 含む約25本が import している |
| 「田辺版もロジック非同梱でよい」 | **行き過ぎ**。資金管理は必要。logic/match だけ外す |
| 「Phase 0 が終わらないとビルドが無駄になる」 | **過剰**。`casinoId` は env なので再ビルド不要。BET経路も Pragmatic 側でミラー非依存 |
| 「`default` セキュリティグループは全受信許可」 | **誤読**。IP/CIDR 列が空=同一グループ参照型。インターネットからは一切許可していない |

---

## 5. 明日 (2026-09-13) やること

**Xserver のデスクトップクラウドを契約 → 田辺版受け子を設置 → マスターから実際に動かす。**

★最初に確かめるのは **Camoufox が Stake にログインできるか**。
ここが通らなければ BET まで到達しない。手順と失敗時の3案は
`MIRROR_BROWSER_PATH_2026-09-12.md` §3〜§4。

設置に要るものは `RESUME_3TRACK.md` §4-1 にまとめてある。

---

*作成: 2026-09-12*
