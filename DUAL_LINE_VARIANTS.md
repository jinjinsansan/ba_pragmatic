# dual_line_*.py の3系統 — どれが正か (2026-09-12 実測)

> `RESTART_KIT_2026-08-22.md` §3-1 の「3系統の罠」を、**ハッシュ付きで確定させたもの**。
> 再開時にここを読まずに触ると、シグナル生成が変わる。

---

## 0. 結論 — 3行

1. **中核の `dual_line_logic.py` は直下と `_vps_prod` で完全一致**(`8dde1db81f8d`)。
   6/10パターンの判定そのものは分岐していない。**分岐しているのは外殻(bot / money / match)**。
2. **用途ごとに「正」が違う。** VPSシグナルbot=`_vps_prod/laplace2/`、受け子engine=`_rev53144/`、
   直下=**そのどちらでもない回帰版**だが、**約25本のスクリプトが直下版を import している**
   (`bacopy_api.py` を含む)ので、**直下版を移動・削除してはいけない**。
3. ★**`_deploy_vps.ps1` は直下版を VPS 本番へ上書きする。実行禁止**(2026-09-12 にガードを追加済み)。

---

## 1. 実測 (サイズ / SHA256 先頭12桁)

| ファイル | リポジトリ直下 (回帰版) | `_vps_prod/laplace2/` (VPS本番) | `_rev53144/` (engine ビルド元) |
|---|---|---|---|
| `dual_line_pragmatic_bot.py` | 334,502 B<br>`0ac5cdde1cbb` | 160,368 B<br>`d2a45c5d9e2f` | 376,341 B<br>`5126de6a0512` |
| `dual_line_money.py` | 19,087 B<br>`c84a60d8c629` | 14,312 B<br>`60a2e57b326b` | 52,398 B<br>`bd18eb9b5428` |
| `dual_line_match.py` | 7,148 B<br>`acfb12ca1ba3` | 6,648 B<br>`d72aae79a976` | 7,148 B<br>`acfb12ca1ba3` |
| `dual_line_logic.py` | 13,124 B<br>`8dde1db81f8d` | 13,124 B<br>`8dde1db81f8d` | — |  ← **同一**
| `dual_line_live_executor.py` | 419,748 B<br>`590680296e41` | 228,422 B<br>`800bbf1c0945` | 463,459 B<br>`c93faa75b5b1` |
| `collector_pragmatic.py` | 25,197 B<br>`681cf9a2ab1d` | 32,860 B<br>`e69f386b6c8a` | — |
★`dual_line_logic.py` が直下と `_vps_prod` で同一なのは重要。
  **「盗まれたら終わる 344 行」はどの系統でも同じ物**なので、②韓国向けに
  サーバー側へ置く際にどれを使うか迷う必要がない。

★`dual_line_match.py` は 直下 == `_rev53144`(`acfb12ca1ba3`)だが `_vps_prod` だけ違う。
  `bacopy_api.py` が import するのは**直下版**。VPSへ載せる時に `_vps_prod` 版と取り違えないこと。

---

## 2. 用途別「正」の対応表

| 用途 | 正とすべき場所 | 備考 |
|---|---|---|
| VPS シグナル bot (テレグラム配信・①) | **`_vps_prod/laplace2/`** | 本番で動いていた実物。直下版で起動すると挙動が変わる |
| 受け子 engine のビルド元 | **`_rev53144/`** | `dual_line_money.py` が 52KB と最大 = SEQ 周りが最も進んでいる |
| `bacopy_api.py` (マスターAPI・②③) | **リポジトリ直下** | `from dual_line_match import ...`(57行目)。直下版に依存している |
| バックテスト各種 (`_bt_*.py` / `backtest_*.py`) | **リポジトリ直下** | 約25本。ここを動かすと全部壊れる |
| ②韓国向け `brain.py` の `decide()` | **どれでもよい**(logic は同一) | `_vps_prod/laplace2/dual_line_logic.py` を正にしておくのが無難 |

---

## 3. ★やってはいけないこと

| 禁止 | 理由 |
|---|---|
| **`_deploy_vps.ps1` を実行する** | 直下(回帰版)を `/opt/laplace2` へ SCP で上書きする。★2026-09-12 にガードを追加し、そのままでは走らないようにした |
| 直下の `dual_line_*.py` を移動・削除・「整理」する | `bacopy_api.py` + 約25本の import が壊れる |
| 「同名だから同じ物」と仮定する | 上表のとおりサイズが倍以上違う場合がある |

## 4. 差分を再確認する手順

```bash
python - <<'PY'
import hashlib,io,os
for f in ["dual_line_pragmatic_bot.py","dual_line_money.py","dual_line_match.py","dual_line_logic.py"]:
    for d in ["", "_vps_prod/laplace2/", "_rev53144/"]:
        p=d+f
        if os.path.exists(p):
            print(f"{len(io.open(p,'rb').read()):>9,} B  {hashlib.sha256(io.open(p,'rb').read()).hexdigest()[:12]}  {p}")
PY
```

---

*作成: 2026-09-12 / REVIVAL_PLAN_3TRACK_2026-09-12.md Phase 1 の一環*
