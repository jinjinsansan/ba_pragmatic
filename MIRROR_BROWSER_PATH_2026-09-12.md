# ★ミラー版の実行経路 — 実測で分かったこと (2026-09-12)

> 明日 (2026-09-13) にデスクトップクラウドへ田辺版受け子を設置して実機テストする前に、
> **必ず読むこと。** 従来の理解と実装が食い違っている点が1つある。

---

## 0. 結論 — 3行

1. `executor-pragmatic` が**ミラー実行機で正しい**。マスターの decision (SWITCH_TABLE / BET) を
   追う実装が入っている。
2. ★**ただしブラウザは Camoufox を使う。`chrome_attach`(実Chromeへ :9222 接続)ではない。**
   `MIRROR_REVIVAL_PLAN_2026-08-28.md` の「実Chrome を :9222 CDP接続」は**誤り**。
3. → **明日の実機テストで最初に確かめるべきは「Camoufox で Stake にログインできるか」**。
   ここが通らなければ BET まで到達しない。

---

## 1. 実測した事実

```
grep -c                                  connect_over_cdp   chrome_attach
  bacopy_executor_pragmatic_ws_live.py          0                0
  dual_line_pragmatic_bot.py                    1               12
  dual_line_live_executor.py                    0               15
```

`bacopy_executor_pragmatic_ws_live.py` には **CDP も chrome_attach も 1 箇所も無い**。
代わりに `main()` が無条件に Camoufox を import する:

```python
# bacopy_executor_pragmatic_ws_live.py:4356
try:
    from camoufox.sync_api import Camoufox
except ModuleNotFoundError as e:
    raise SystemExit("camoufox is required to run this executor. ...")
```

一方、マスターの decision を追う実装は**ある**(`api/decisions` を 8 箇所で参照、
`SWITCH_TABLE` の割り込み処理が 3964-3982 行)。つまり:

| | ミラー (田辺) | 梶原 |
|---|---|---|
| サブコマンド | `executor-pragmatic` | `dual-line` |
| 実体 | `bacopy_executor_pragmatic_ws_live.py` | `dual_line_pragmatic_bot.py` |
| 判断 | マスター画面の人間 | ローカルの `decide()` |
| **ブラウザ** | ★**Camoufox(自前で起動)** | **実Chrome へ :9222 アタッチ** |
| 資金管理 | `Seq7Session`(`--bet-mode`) | `dual_line_money`(`--money-mode`) |

★`build_staging/.env` の `BACOPY_BROWSER=chrome_attach` は
**`executor-pragmatic` では読まれない**(この文字列がファイル内に存在しない)。
梶原版の設定がそのまま残っているだけ。

---

## 2. なぜ今まで気付かなかったか

- 2026-08-28 の「ローカル一周実証」は `bacopy_executor_dryrun.py` で行った。
  これは**ブラウザに一切触れない**ツールなので、ブラウザ経路は検証されていなかった。
- 2026-09-12 の本番マスターでの一周実証も同じツール。decision の往復・結果観測は
  確かに通ったが、**BET の実行経路は未検証のまま**。

→ **「配管は通った」と「BETできる」は別**。ここを混同しない。

---

## 3. ★明日のテストで確かめる順序

ブラウザが立ち上がらなければ以降は全て無意味なので、**上から順に**。

| # | 確認すること | 失敗したら |
|---|---|---|
| 1 | Camoufox が起動し、ミラードメインに**ログインできるか** | §4 の選択肢へ |
| 2 | Cloudflare の JSチャレンジ("Just a moment")を通過するか | §4 の選択肢へ |
| 3 | ロビーから卓へ移動できるか (SWITCH_TABLE) | DOM 変更の調査 |
| 4 | `casinoId` が `ppcds00000003709` と一致するか | `.env` の `BACOPY_PRAGMATIC_CASINO_ID` で調整 |
| 5 | **$0.20 の BET が受理されるか** | `lpbet` の形式変更を疑う |
| 6 | ack と result がマスターへ返るか | ここまで来れば配管は実証済み |

★1〜2 が本命のリスク。**2026-08-03 に VPS の headless Camoufox が
Cloudflare に弾かれて全配信停止した**前科がある
(`INCIDENT_VPS_CLOUDFLARE_DGA_DIRECT_20260803.md`)。

ただし当時と条件が違う:

| | 2026-08-03 の VPS | 明日のデスクトップクラウド |
|---|---|---|
| 実行形態 | **headless** | headful (画面あり) |
| IP | データセンター (Xserver VPS) | デスクトップクラウド |
| 結果 | ★弾かれた | **未知** |

Camoufox は元々アンチ検知ブラウザなので、headful なら通る見込みはある。
**が、確証は無い。ここが明日の最大の不確実性。**

---

## 4. 1〜2 が失敗した場合の選択肢

| 案 | 内容 | 手間 |
|---|---|---|
| **A. Camoufox プロファイルを作って渡す** | 手動で一度ログインした `auth_state/` を持ち込み、セッションを再利用する。チャレンジを毎回通す必要がなくなる | 小 |
| **B. `executor-pragmatic` に chrome_attach を移植** | `dual_line_pragmatic_bot.py` の CDP 実装 (12〜15箇所) を移植する。梶原版で実績がある経路なので確実性は高い | 中 |
| **C. 梶原版エンジンをミラー用途で使う** | `dual-line` に「マスター追随モード」を足す。既に chrome_attach で動いている実績を活かす | 中〜大 |

★**B が本命**と考えている。受け子の実Chromeを使う方式は
2026-08-03 に「VPSのcamoufoxは弾かれたが受け子の実Chromeは無傷」と
実証されており、**Cloudflare 耐性がはっきり高い**。

ただし明日はまず現状のまま試す。A で足りるならそれが一番安い。

---

## 5. ビルドへの影響

ミラー版ビルド (`scripts/build_bacopy_engine_mirror.ps1`) は
**camoufox を除外できない**。`main()` が無条件 import するため、
除外すると起動直後に SystemExit する。

除外するのは梶原ロジックのみ:

```
--exclude-module dual_line_pragmatic_bot
--exclude-module dual_line_live_executor
--exclude-module dual_line_logic
--exclude-module dual_line_match
--exclude-module dual_line_money
```

→ `dual-line` サブコマンドは ImportError で失敗する (意図どおり)。
★検証は**実行**で行う。文字列検索では確かめない。

---

*作成: 2026-09-12 / 明日の実機テスト前に読む*
