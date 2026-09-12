# 3方向復活 — 作業台帳 / 再開キット

> **★セッションが落ちたら、まずこの1本を読む。**
>
> `RESTART_KIT_2026-08-22.md` が「停止したプロジェクトの再開」の入口なのに対し、
> こちらは **2026-09 に始めた3方向復活の進行状況**を記録する**生きた台帳**。
> 作業するたびに §3 / §4 を更新すること(日付を付けずに1本で運用する。増やさない)。

**最終更新: 2026-09-12** / ブランチ `feat/engine-heartbeat` / 作業ツリー `V:\dev\Cusor\bacopy`

---

## 0. 読む順序

| # | 文書 | 何が書いてあるか |
|---|---|---|
| 1 | **本ファイル** | 現在地・完了/未完了・オーナー待ち・次の一手 |
| 2 | `REVIVAL_PLAN_3TRACK_2026-09-12.md` | **設計と全体計画**(なぜそうするか) |
| 3 | `DUAL_LINE_VARIANTS.md` | ★3系統の `dual_line_*.py` のどれが正か(SHA256付き) |
| 4 | **`MIRROR_BROWSER_PATH_2026-09-12.md`** | ★**実機テスト前に必読**。ミラー版が使うブラウザが Camoufox だと判明した件 |
| 5 | `PHASE0_DGA_BASELINE_2026-09-12.md` | dga実測の基準値・**60卓の qpid 対応表** |
| 5 | `STAKE_MIRRORS_2026-09-12.txt` | Stake ミラー121本の一覧 |
| 6 | `RESTART_KIT_2026-08-22.md` | 停止時の全体像・金庫の中身・復元手順 |
| 7 | `CREDENTIAL_REVOCATION_CHECKLIST_2026-08-22.md` | 旧鍵の失効(🔴6件が未着手) |

---

## 1. これは何をしているのか(3行)

2026-08-22 に全停止したバカラ支援システムを、**3つの異なる要望**に同時に応える形で復活させる。

| | チーム | 何を作るか | 頭脳の場所 |
|---|---|---|---|
| ① | 梶原(日本) | 元通り。テレグラム4ch + GUI + SEQ | クライアント(現行 `dual_line`) |
| ② | 韓国 | 梶原ロジックを**技術を盗ませずに**貸す。韓国語UI・テレグラム無し | **サーバー**(新規 `brain.py`) |
| ③ | 田辺(日本) | V2 マスター画面(卓移動 + **B/P/T**)→ 受け子が追随BET。額は受け子の設定 | マスターの人間(現行 `executor-pragmatic`) |

**②と③は同じ配管に乗る。** ②は新規製品ではなく③の派生(人間の指を `decide()` に差し替える)。

---

## 2. 決定事項 — 蒸し返さない

オーナー確定 2026-09-12。

| # | 論点 | 決定 |
|---|---|---|
| 1 | ②のロジック配置 | **サーバー側で計算**。受け子へは `{卓, 側, 金額}` のみ送る |
| 2 | ②の資金管理 | **SEQ もサーバー側で計算し金額だけ送る**(★階段の数列は口座履歴から復元されるので隠せない。守れるのはリセット規則・`loss_cut`・開始額の決め方) |
| 3 | 着手順 | **Phase 0 → ③田辺 → ①梶原 → ②韓国** |
| 4 | ①もサーバー側に寄せるか | **今回はやらない**(復元中に構造を変えない) |
| 5 | ②のインフラ | **別VPS・別APIキー・別executor名前空間** |
| 6 | 契約 | 技術対策と別に必要(再配布禁止・リバースエンジニアリング禁止・期間) |

**なぜ「暗号化」ではなくサーバー側なのか**: `app.asar` は `npx asar extract` で展開でき暗号化ではない。
PyInstaller `--onefile` は `pyinstxtractor` で数十分。相手にエンジニアがいる以上、
クライアントに置いて隠すのは成立しない。サーバー側なら**完全なキルスイッチ**も手に入る。

---

## 3. ✅ 完了した作業(検証コマンド付き)

すべて **2026-09-12** に実施。**まだコミットしていない**(§6)。

### 3-1. 実測で確定した事実(Phase 0 の一部が前倒しで片付いた)

| 対象 | 結果 |
|---|---|
| `stake.com` | **HTTP 451** + `Legal Reasons` = **日本から死亡** |
| ミラー121本 | **HTTP 403** + `Just a moment` = Cloudflare の JSチャレンジ = **生存**(実ブラウザなら通る) |
| `dga.pragmaticplaylive.net` | **生存**。DNS=AWS `3.165.39.x` で **Cloudflare 非経由** |
| `casinoId` `ppcds00000003709` | **今も有効**。卓一覧に `STAKE SPEED BACCARAT` があり Stake のものと確認 |
| `Baccarat 3` の qpid | `cbcf6qas8fscb222` = **6月に BET 突破した時と同一値**。qpid は安定 |

再確認コマンド:
```bash
curl -s -o /dev/null -w "%{http_code}\n" -A "Mozilla/5.0" https://stake.com/ja/casino/home   # → 451 なら日本から死亡
curl -s -o /dev/null -w "%{http_code}\n" -A "Mozilla/5.0" https://stake.bet/ja/casino/home   # → 403 なら生存(bot判定)
```

★**「非CDN URL」は誤解。** ミラーも全部 Cloudflare 上にいる。CDN を迂回しているのではなく
**まだ451のブロック対象リストに載っていないだけ**。連番の歯抜け(`stake1022`→`1034`)は
焼かれたドメインの墓場で、`stake3000.com → 301 → stake3015.com` と Stake 自身が転送している。
→ **ローテーション前提の設計が必須**(§4-1)。

### 3-2. オリジン抽象化 — `bacopy_executor_pragmatic_ws_live.py` (+43/-7)

- `BACOPY_STAKE_ORIGIN` を導入(既定 `https://stake.com`)
- 判定を `_is_stake_host()` / `_is_stake_account_ws()` の**2関数に集約**
- ★**危険だった `stake.com/_api/websockets` の4箇所**(1955/1979/4676/4701)を全て置換。
  ここをドメイン固定のまま移すと**エラーを出さずに BET が飛ばなくなる**
- `LOBBY_URL` が env を読むようになった + ハードコード `goto` を置換

★**副産物で潜在バグを修正**: `046f140`(ロビー直リンク廃止)は `provision-user-build.js` だけを
直しており、**エンジン本体は `BACOPY_LOBBY_URL` を一切読んでいなかった**(123行目が定数直書き)。
復帰ナビゲーション4箇所が直リンクのままで「Failed to start third party session」を踏み続ける状態だった。

検証:
```bash
python -m py_compile bacopy_executor_pragmatic_ws_live.py
grep -n "stake\.com" bacopy_executor_pragmatic_ws_live.py   # → コメントと既定値のみ (124/129/154行) であること
```
ヘルパのユニットテスト(3オリジン × 8URL・実施済み・全て期待通り):
```bash
python - <<'PY'
import io
lines = io.open('bacopy_executor_pragmatic_ws_live.py',encoding='utf-8').read().split('\n')
a = next(i for i,l in enumerate(lines) if l.startswith('STAKE_ORIGIN ='))
b = next(i for i,l in enumerate(lines) if 'return "/_api/websockets" in (url or "").lower()' in l)
block = '\n'.join(lines[a:b+1])
for origin in ["https://stake.com","https://stake.bet","https://stake1017.com"]:
    ns={'os':type('o',(),{'getenv':staticmethod(lambda k,d="": {"BACOPY_STAKE_ORIGIN":origin}.get(k,d))})}
    exec(block, ns); f=ns['_is_stake_account_ws']
    for u,exp in [("wss://stake.bet/_api/websockets",True),("wss://stake.com/_api/websockets",True),
                  ("wss://stake1017.com/_api/websockets",True),("wss://stake3015.com/_api/websockets",True),
                  ("wss://dga.pragmaticplaylive.net/ws",False),("wss://client.pragmaticplaylive.net/game",False),
                  ("https://stake.games/ja/casino/home",False),("",False)]:
        assert f(u)==exp, (origin,u)
    print("OK", origin, ns['LOBBY_URL'])
PY
```

### 3-3. dga-only モード — `_vps_prod/laplace2/collector_pragmatic.py` (+99/-63)

★**遅延importだけでは足りなかった。** `_vps_prod` 版は dga直結フィードを**ブラウザと併走**させる
作りで Camoufox は依然起動していた。

- `camoufox` の import をモジュール冒頭から**削除**し、ブラウザ経路の直前へ移動
- **`BACOPY_DGA_ONLY=1` を新設** — ブラウザを一切起動しない。プロファイル準備もスキップ
- 監視ループを `_monitor_loop(page, ...)` に抽出し、両モードで共有(`page=None` が dga-only)
- 必要な pip が **`websockets` と `requests` だけ**になった(= 1vCPU/1GB VPS で足りる)
- 失うのはロビーDOM由来の qpid スキャンのみ(卓IDは dga フィードから取れている)

検証(★camoufox を遮断して実走行する。この環境には camoufox が入っているので遮断が必須):
```bash
# 偽 camoufox を作って import を塞ぐ
BD="$TEMP/fakecamo"; mkdir -p "$BD"
printf 'raise ImportError("camoufox not installed (simulating dga-only VPS)")\n' > "$BD/camoufox.py"

cd _vps_prod/laplace2
PYTHONPATH="$BD" BACOPY_DGA_ONLY=1 BACOPY_DGA_DIRECT=1 python collector_pragmatic.py --duration 70
rm -f analytics_pragmatic.sqlite3     # ★テストでDBが出来るので消す (gitignore済みだが)
```
2026-09-12 の実測結果: `connected` 1.1秒 / `msgs=625 shuffles=3 tables=60` / `exit=0`。

### 3-4. 旧IP `210.131.215.116` の除去(★当初の報告は不完全だった)

**経緯**: 最初に「実行系から全消去」と報告したが**誤りだった**。`grep -r` をパイプに繋ぐと
ブロックバッファリングが効き、`timeout` で殺された時に**結果が出力されないまま空に見える**。
この空を「クリーン」と誤読していた。ripgrep で取り直して**42箇所**が判明し、全て処理した。
→ 教訓は §7 に記載。**この巨大リポジトリで `grep -r ... | ...` を検証に使わないこと。**

処理した内訳:

| 種別 | 対象 | 対応 |
|---|---|---|
| **A. APIキー漏洩経路**<br>`childEnv.BACOPY_API_FALLBACK_IPS` | `copytrade_gui/` 配下の未追跡コピー5本<br>`_asar_build` `_asar_cond`×4 `_asar_live`×3 `_asar_work`×2 `_live_verify` `_verify_asar` `_v2`<br>**`dist_client/copytrade_gui/src/main.js`** ← ★配布物 | 行を**無害化**(ファイルは残す) |
| **B. ビルド雛形** | `copytrade_gui/build_staging/.env` | 踏み台ホストを空に + `BACOPY_SUPPORT_ENABLED=0` + ロビー直リンクも空に + `BACOPY_STAKE_ORIGIN` 追加 |
| **C. プロビジョニング** | `copytrade_gui/scripts/provision-user-build.js` | 踏み台を env 駆動化。未設定なら**トンネル機能ごと無効**。オリジンから `BACOPY_LOBBY_URL` を生成 |
| **D. 配布テンプレ** | `.env.dist:15` / `.env.template:27` | `LAPLACE_SSH_HOST=` を空に + `BACOPY_STAKE_ORIGIN` 追加 |
| **E. env既定値が IP** | `gui/src/main.js:179` / `dist_client/gui/src/main.js`<br>`scripts/laplace_admin.py` / `scripts/provision_user_build.py`(+`_vps_prod/` 版)<br>`scripts/check_vps_sshd.sh`(+`_vps_prod/` 版) / `scripts/deploy_master_to_vps.sh` | 既定値を**空**に。sh 系は `${VAR:?メッセージ}` で**未設定なら即停止** |
| **F. SSH先ハードコード**<br>(分析スクリプト) | `analyze_*.py` 11本 / `ai_pattern_analysis.py`(+`_vps_prod/`・`dist_client/` 版) / `verify_top3_regularity.py` / `scripts/test_all_tables.py` | `_LAPLACE_SSH_HOST()` ヘルパを挿入。**未設定なら SystemExit** |
| **G. ★最も危険** | `gui/setup.bat:39` / `dist_client/gui/setup.bat` | `-o StrictHostKeyChecking=no` 付きで解約済みIPへ接続していた(=**第三者のホスト鍵を known_hosts に焼き付ける**)。env 駆動化 + 未設定ならスキップ |

**残っているのは説明コメント / docstring のみ。** 唯一の実行行 `_deploy_vps.ps1:31 $REMOTE=...` は
§3-5 のガード(`exit 1`)より後にあり**到達しない**。

検証(★ripgrep か `git grep` を使う。`grep -r | pipe` は使わない):
```bash
git grep -n --no-index -I -E "210[.]131[.]215[.]116" -- "*.py" "*.js" "*.bat" "*.ps1" "*.sh"   | grep -v node_modules | grep -v _ARCHIVE
# → 出るのはコメント/docstring と _deploy_vps.ps1 の到達不能行のみ
```
構文チェック(全て通ることを確認済み):
```bash
python -m py_compile bacopy_executor_pragmatic_ws_live.py _vps_prod/laplace2/collector_pragmatic.py   scripts/laplace_admin.py scripts/provision_user_build.py scripts/test_all_tables.py   ai_pattern_analysis.py dist_client/ai_pattern_analysis.py analyze_*.py verify_top3_regularity.py
for f in copytrade_gui/src/main.js copytrade_gui/scripts/provision-user-build.js gui/src/main.js          dist_client/gui/src/main.js dist_client/copytrade_gui/src/main.js; do node -c "$f"; done
```

★**`copytrade_gui/dist/BACOPYRECEIVER_user01..11_Setup.exe` は配布禁止のまま**
(ビルド済みバイナリなので今回の修正は入っていない。再ビルドが要る)。

### 3-6. Phase 2 着手分 (2026-09-12)

**(a) `BACOPY_STANDALONE` — Supabase 依存を切る**(`copytrade_gui/src/main.js`)

`ensureSession()` と `billingStatus()` の先頭で早期リターン。`is_free=true / ok=true`
を返すので renderer は残高チェックを飛ばし、START が有効になる(表示は `FREE / UNLIMITED`)。
`provision-user-build.js` は standalone ビルドで **Supabase / LAPLACE の4キーを焼き込まない**。

★切っても運用に必要な可視性は失われない。残高・日次PnL・現在の卓・BET可否は
ハートビート(`upsert_executor`)でマスター画面に出る。失うのは課金・ロックダウン・Web帳簿だけ。

検証(実行して確認):
```bash
# _isStandalone の判定と、provision の merge を両モードで評価する
# → standalone=true のとき Supabase/LAPLACE の4キーが入らないこと
```

**(b) `GET /api/origin` — ミラーの451を検知して自動切替**(`stake_origin.py` 新規)

| 機能 | 内容 |
|---|---|
| `probe()` | 451/Legal Reasons → blocked、**403+"Just a moment" → ok**(実ブラウザなら通る) |
| `refresh_candidates()` | `playstake.io` から121本を取得 |
| `check_and_rotate()` | 焼けていたら候補へ切替 |
| `GET /api/origin` | 要認証。モジュールが無くても既定値を返すフェイルソフト |

VPS: `bacopy-origin-check.timer` が**30分毎**に refresh+check。終了コード10(切替発生)は
`SuccessExitStatus=0 10` で成功扱い。状態は `/opt/bacopy/data/stake_origin.json`。

★**日本のVPSだから成立する。** 海外VPSからは 451 にならないので、この判定は
日本国内から行う必要がある。ConoHa を日本リージョンにしたことがここで効いている。

検証: VPSから `stake.com`→451判定、候補121本取得、`stake.ac` へ自動切替、
公開URL経由で origin 取得、認証なしは401。

**残: 受け子側**(起動時と定期的に `/api/origin` を取得 → Chrome起動URLと
エンジンの `BACOPY_STAKE_ORIGIN` に反映、451検知で次候補へ)。

### 3-7. ③の配管を本番マスターで一周実証 (2026-09-12・実弾なし)

`bacopy_executor_dryrun.py` はカジノに一切触れずに decision の往復を検証できる。
本番 `https://master.bafather.uk` に対して実行した結果:

```
POST /api/decisions (卓415 / side=P / $0.2)  → {"accepted": true}
  ↓
受け子が 1.0秒後に取得して ack        ack_json に記録
  ↓
9秒後に結果を観測                     result_json outcome="banker"
  ↓
status = done
```

DB に残った記録から確認できたこと:

| 項目 | 内容 |
|---|---|
| `side` | **P**(送った通り。取り違えなし) |
| snapshot | 投入時点の罫線 `sequence="PPBTBPBPT"` をAPI側が自動付与 |
| `derived_roads` | `big_eye_boy` / `small_road` / `cockroach_road` まで自動計算 |
| ★**先読みが無い** | 投入時 `hands=9` / 結果観測時 `hands=10`。判断材料は決定時点のものだけで、結果は後から別途観測している |

★ログの `-> banker` は**BET側ではなくその手の実際の勝敗**(`_infer_outcome` が
`last_hand.winner` から判定)。読み違えやすいので注意。

★`/api/executors` が空なのは dryrun ツールが `upsert_executor`(ハートビート)を
呼ばないため。実エンジンでは登録される。

テストデータは削除済み(`decisions` 0件)。

### 3-8. 田辺チームの仕様確定 + 資金管理3方式 (2026-09-12)

**★当初の理解が誤っていたので訂正した。**「田辺チームは SEQ 不要 = ロジックも資金管理も
不要」と考えて「ロジック非同梱ビルド」を提案したが、**資金管理は必要**。

```
[マスター画面 — 田辺さんが操作]
   卓グリッドから卓を選ぶ       → SWITCH_TABLE
   B / P / T のボタンを押す     → BET (★側だけ。金額は送らない)
        ▼
[受け子GUI — 受け子ごとに設定]
   BET開始額     ← 受け子が自分で設定
   資金管理モード ← 受け子が自分で選ぶ
   SEQ(梶原式)   ← 不要
        ↓ その設定に従って金額を決めて BET
```

**受け子に必要なモジュール**

| モジュール | 田辺 | 理由 |
|---|---|---|
| `dual_line_logic.py`(6/10パターン判定) | **不要** | 判断するのは人間 |
| `dual_line_match.py` | **不要** | 同上 |
| **`dual_line_money.py`(資金管理)** | ★**必要** | 金額を決めるのはここ |

★`dual_line_money.py` は `dual_line_logic` に依存しない(`marubatsu_strategy` のみ)ので、
**資金管理だけ渡してパターン判定は渡さない**ことが技術的に可能。

**使う資金管理は3方式のみ**(オーナー指示): ダランベール / マーチンゲール / **グランドマーチンゲール**。
グランドマーチンゲールのみ未実装だったので追加(`u*(2^(n+1)-1)`)。

★実装時に**状態遷移の3箇所**にも反映が要る。漏らすと従来SEQ扱いになって壊れる:
勝ち時の `seq_level` リセット判定、負け時の `seq_level` 進行判定、負け時の `loss_count` 進行対象。

★グランドマーチンゲールは純マーチンゲールより増え方が急で、**同じ連敗数でも必要資金が約2倍**。
`martingale_max_bet` と `loss_cut` の設定は必須と考えること(unit=$1 で5連敗時 $63)。

**TIE ボタン**: マスター画面に常時表示にした。ただし実行できる受け子が居ない間は disabled。
★**TIE の BETコードは未確定**(B=10/P=11 は実測値だが TIE は既定 "2" の推測値)。
受け子側は `--allow-tie` を付けた時だけ実行する。確定には `sniff_pragmatic_bet_ws.py` で
`BACOPY_PRAGMATIC_BC_TIE` を実測すること。**誤ったコードは意図しない側への BET になり得る。**

### 3-9. ビルド方針 (改訂版)

| ビルド | 同梱するもの | 配布先 |
|---|---|---|
| **full** | logic + match + money | 梶原チーム |
| **mirror** | **money のみ**(logic/match 非同梱) | **田辺チーム** |
| **thin** | なし(サーバーが金額まで決める) | 韓国チーム |

★現在の `scripts/build_bacopy_engine.ps1` は `--hidden-import` で
`dual_line_logic` / `dual_line_money` 等4本を明示同梱 = full 相当。
mirror / thin 用の spec を分ける必要がある。

★検証は**文字列検索ではなく実行**で行う(`SEQ_CUSTOM_START` の教訓)。

### 3-10. ★ミラー版のブラウザ経路が想定と違った (2026-09-12)

`MIRROR_REVIVAL_PLAN_2026-08-28.md` は「engine = `bacopy_executor_pragmatic_ws_live.py` が
**実Chrome を :9222 CDP接続**」としていたが、**実測では誤り**。

```
grep -c                                 connect_over_cdp   chrome_attach
  bacopy_executor_pragmatic_ws_live.py         0                0
  dual_line_pragmatic_bot.py                   1               12
```

`executor-pragmatic` がミラー実行機なのは正しい(`api/decisions` を8箇所で参照、
`SWITCH_TABLE` の割り込み処理あり)。**だがブラウザは Camoufox を自前で起動する。**
`main()` が無条件に `from camoufox.sync_api import Camoufox` する(4356行)。

★`build_staging/.env` の `BACOPY_BROWSER=chrome_attach` は
`executor-pragmatic` では**読まれない**(文字列がファイル内に存在しない)。

**なぜ気付かなかったか**: 8/28 と 9/12 の「一周実証」はどちらも
`bacopy_executor_dryrun.py` で行った。これは**ブラウザに触れない**ツールなので、
BET の実行経路は未検証のままだった。
→ **「配管は通った」と「BETできる」は別**。

**明日のリスク**: 2026-08-03 に VPS の headless Camoufox が Cloudflare に弾かれた
前科がある。明日は headful なので通る見込みはあるが**確証は無い**。
失敗時の選択肢3案は `MIRROR_BROWSER_PATH_2026-09-12.md` §4。本命は
**`dual_line_pragmatic_bot.py` の chrome_attach 実装を移植する案**
(受け子の実Chromeは 8/03 に無傷だったと実証済みで、Cloudflare 耐性が高い)。

### 3-11. ミラー版 engine のビルド完了 (2026-09-12)

```
copytrade_gui/build_staging/engine_variants/
    bacopy_engine_full.exe     66 MB   ← 梶原チーム用 (既存)
    bacopy_engine_mirror.exe  214 MB   ← 田辺チーム用 (sha256 5a06cefc82b5ca6b)
```

★**検証は実行で行った**:

```
ミラー版  dual-line → ModuleNotFoundError: No module named 'dual_line_pragmatic_bot'
full 版   dual-line → import は通り Chrome(:9222) へ接続を試みて失敗
```

→ 梶原ロジックがミラー版に**物理的に存在しない**ことを確認。無効化ではなく不在。

★**`--help` では検証にならない。** argparse が遅延importの**前**に処理して exit 0 するので、
除外できていなくても成功に見える。**importを実際に踏ませる引数**で試すこと。

★**`build_staging/engine` は electron-builder が丸ごと同梱する。**
full 版を置いたまま田辺版を焼くと梶原ロジックが流出する。
→ `scripts/stage_engine.ps1 -Variant mirror|full` で**必ず1本だけ**置いてから梱包する。
  engine の名前は `main.js` が `bacopy_engine.exe` で固定している(4箇所)ので、
  どの版でもこの名前にリネームして置く必要がある。

**インストーラ生成の手順**
```powershell
powershell -File scripts/stage_engine.ps1 -Variant mirror
cd copytrade_gui
node scripts/provision-user-build.js <...>   # BACOPY_MONEY_MODES 等を .env へ
npm run build:installer
```

### 3-5. 3系統問題 — ★当初の方針が誤りだったので変更した

計画書には「直下版を `_archive_regression/` へ隔離」と書いたが、**実行すると壊れる**:
**`bacopy_api.py`(VPSに載せる本体・57行目 `from dual_line_match import`)を含む約25本が
直下版を import している**。移動・削除は禁止。

代わりに実施:
- **`DUAL_LINE_VARIANTS.md` を作成** — 3系統をサイズ+SHA256で確定し、用途別の「正」を表にした
- ★**`_deploy_vps.ps1` に実行停止ガードを追加** — **これが真の地雷だった**。
  直下(回帰版)を `/opt/laplace2` へ SCP で上書きする作りで、解約済みIPと存在しない `E:\` パスを
  指していた。今は即 `exit 1` する(UTF-8 BOM 付きで警告文が化けないことも確認済み)

★**朗報**: 中核の `dual_line_logic.py` は直下と `_vps_prod` で**ハッシュ完全一致**(`8dde1db81f8d`)。
分岐は外殻(bot/money/match)だけで、6/10パターンの判定はどの系統でも同じ物。
**②のサーバー側 `decide()` にどれを使うか迷う必要がない。**

---

## 4. ⬜ 未着手の作業

### 4-1. 次の一手 — 明日 (2026-09-13) の実機テスト

**Xserver のデスクトップクラウドを契約 → 田辺版受け子を設置 → マスターから実際に動かす。**

★**最初に確かめるのは Camoufox が Stake にログインできるか**(§3-10)。
ここが通らなければ BET まで到達しない。順序と失敗時の3案は
`MIRROR_BROWSER_PATH_2026-09-12.md` §3〜§4。

| # | 確認すること |
|---|---|
| 1 | Camoufox が起動し、ミラードメインに**ログインできるか** |
| 2 | Cloudflare の JSチャレンジを通過するか |
| 3 | ロビーから卓へ移動できるか (SWITCH_TABLE) |
| 4 | `casinoId` が `ppcds00000003709` と一致するか |
| 5 | **$0.20 の BET が受理されるか** |
| 6 | ack と result がマスターへ返るか |

**設置に要るもの**

| 項目 | 値 |
|---|---|
| マスター | `https://master.bafather.uk` (`/master` のPWは `tanabe`) |
| API キー | `ssh -i ~/.ssh/bacopy_vps_2026 root@160.251.211.181 'grep BACOPY_API_KEY /opt/bacopy/.env'` |
| engine | `copytrade_gui/build_staging/engine/bacopy_engine_mirror.exe` |
| `.env` | `BACOPY_STANDALONE=1` / `BACOPY_MONEY_MODES=dalembert,martingale,grand_martingale` / `BACOPY_API_URL` / `BACOPY_API_KEY` / `BACOPY_EXECUTOR_ID` |
| オリジン | ★`.env` に焼かなくてよい。マスターの `/api/origin` が配る(現在 `https://stake.ac`) |

★**Stake アカウントのログインが別途要る**。Camoufox のプロファイル
(`auth_state/`)に手動で一度ログインしておくと、チャレンジを毎回通す必要がなくなる
(`MIRROR_BROWSER_PATH_2026-09-12.md` §4 の案A)。

### 4-2. 以降(Phase 順)

| Phase | 内容 | 前提 |
|---|---|---|
| **0** | ミラー実機スパイク(§5-3) | オーナーのログイン作業 |
| **1 残** | VPS構築・Caddy・systemd・DNS・**鍵の新規発行** | VPS |
| **2 = ③** | 認証ゲート除去(`billingStatus()` バイパス。呼び出し元は `src/main.js:2163` の1箇所)→ 受け子ビルド → **1台で $0.20 テスト** | Phase 1 |
| **3 = ①** | `_vps_prod/laplace2/` からbot復元 → **テレグラム4ch 新規作成** → 受け子ビルド | Phase 1 |
| **4 = ②** | `brain.py` 新規 + **SEQサーバー化** + GUI機能削減 + 韓国語化 + **ロジック非同梱の別ビルド** | Phase 2 の配管 |
| **5** | 展開・監視 | — |

★**②で唯一の非自明な新規開発 = 決済フィードバックのループ**。
SEQ の段は結果確定後に進むので `決定を送る → BET → 勝敗が返る → 段を進める` の往復が要る。
③(ミラー)は金額がローカルなのでこのループが無かった。
**幻SEQ**(実BET無しに階段だけ進む・user07で発生)を再現しない設計にすること。

---

## 5. 🚧 オーナー待ち(これが無いと進めない)

### 5-1. VPS 1台(日本) — ①③用

#### 要件(小さい)

| 項目 | 値 |
|---|---|
| スペック | **1 vCPU / 1GB / 20GB** で足りる(★camoufox/playwright 不要になったため) |
| OS | Ubuntu 24.04 LTS |
| リージョン | 日本 |
| 固定グローバル IPv4 | **必須**(`master.bafather.uk` を向ける) |
| 開放ポート | 443 のみ(8010 は localhost に閉じる) |
| 常時起動 | **必須**(受け子がロングポーリングで待つ) |

#### ★2026-09-12 決定: Xserver は不採用

理由: 新規申込可能な「おまとめ自動セットアップ」は
**`VPS:1件 + DBサーバー:1件` が最小構成として強制**され、DBサーバー(MariaDB)を外せない。

**このシステムは MariaDB を1バイトも使わない**(実測):
```
bacopy_db.py:5,17  import sqlite3 / BACOPY_DB_PATH="data/bacopy.sqlite3"
リポジトリ全体の pymysql / mariadb / mysql.connector 依存 → ゼロ
VPS に載る pip → websockets, requests のみ
```
使わないDBを常時動かして払い続ける形になり、費用の無駄に加えて
守る対象(ネットワークサービス)が1つ増える。提示額は ¥2,340/月 で要件の4〜8倍だった。

→ **ConoHa VPS / さくらのVPS など、VPS単体で契約できる事業者にする。**

#### (参考) 2026-09-12 時点の Xserver の状況

- サーバー在庫不足 + 調達コスト高騰により、**2GB〜64GB の全プランが「新規受付を一時停止」**
  (2GBは7/14停止、その後全プランへ拡大)。9/1 に料金改定も実施
- 代わりに **XServer VPSクラウド**(2026-08-31 提供開始)のみ新規申込可。開始 **約 ¥2,068/月**
- → **技術的には VPS クラウドでも全く問題ない**(要件が小さすぎるため)。
  ただし **Xserver を選ぶ理由も無い**。旧VPSは解約済みで移行するデータもロックインも無い

#### 事業者の選択肢(日本・1GB 目安・2026-09 時点)

| 事業者 | 月額目安 | 備考 |
|---|---|---|
| WebARENA Indigo | ~¥297 | 最安。時間課金だが月額上限あり |
| ConoHa VPS | ~¥550〜 | API が良い。管理しやすい |
| KAGOYA CLOUD VPS | ~¥550 | |
| さくらのVPS | ~¥807〜 | 最も枯れている |
| XServer VPSクラウド | ~¥2,068〜 | 要件に対して過剰。日次バックアップ込み |

★**この選択は低リスクで後戻り可能**。新VPSはほぼステートレス
(`bacopy.sqlite3` は空から始まる)ので、後から乗り換えても失うものが無い。
価格差(年 2万円程度)より**常時起動の安定性**を優先してよい。

★**どの事業者でも IP をハードコードしない。** 今日の作業の大半は、Xserver が旧IPを
別契約者へ再割当したことによる後始末だった。これは全事業者に共通の挙動。
オリジンもマスターAPIのIPも env / DNS 経由にすること(対応済み)。

★**デスクトップクラウドは①③には不要。** 旧 bafather 機は「オーナー自身が受け子として
動かす1台」だった。②韓国は**先方が自前で借りる**ので、こちらの手配も不要。
②用の別VPS(§2 決定5)は Phase 4 で必要になる。日本リージョンでも
ソウル↔東京は ~35ms なので実用上は問題ない見込み(要実測)。

#### ✅ 2026-09-12 契約済み: ConoHa VPS 2GB

| 項目 | 値 |
|---|---|
| 事業者 / プラン | **ConoHa VPS 2GB** |
| グローバル IPv4 | **160.251.211.181** (ゲートウェイ 160.251.210.1 / マスク 255.255.254.0) |
| DNS | 150.95.10.8 / 150.95.10.9 |
| 帯域 | In/Out 各 100 Mbps |
| OS | Ubuntu 24.04 LTS |

★**認証情報はここに書かない。** root パスワードは金庫へ。鍵認証に移行したら無効化する。

初期状態(2026-09-12 実測): **ping も 22/443 も全て遮断** = セキュリティグループ未設定。
設定するのは以下だけ:

| 方向 | ポート | 許可元 | 用途 |
|---|---|---|---|
| in | TCP **22** | **自宅IP のみ**(2026-09-12 時点 `221.92.188.24`) | 管理。★動的IPなので変わり得る。締め出されたら ConoHa のコンソール(VNC)から直せる |
| in | TCP **443** | 全て | 受け子・マスター画面。Caddy が受ける |
| in | それ以外 | **開けない** | ★**8010 は絶対に開けない**(Caddy が localhost へ流す) |

★運用に**インバウンド SSH は不要**(必要なのは 443 だけ)なので、22 は絞ってよい。

#### ✅ 2026-09-12 構築完了 — 残るは DNS のみ

```
接続: ssh -i ~/.ssh/bacopy_vps_2026 root@160.251.211.181
      ★パスワード認証は無効化済み。鍵のみ。締め出されたら ConoHa のコンソール(VNC)から
        /etc/ssh/sshd_config.d/00-bacopy-hardening.conf を消して systemctl restart ssh
```

| 項目 | 状態 |
|---|---|
| OS | Ubuntu 24.04.3 LTS / kernel **6.8.0-139**(更新+再起動済み) |
| スペック | 3 core / 1966MB / 99GB(6%使用) / Python 3.12.3 |
| sshd | **パスワード認証 no / 鍵のみ**。`00-bacopy-hardening.conf`(★`50-cloud-init.conf` より先に読ませるため `00-` 命名。sshd は**最初の指定が勝つ**) |
| ufw | active。22(OpenSSH) / 80 / 443 のみ |
| ConoHa SG | `default` + **`bacopy-master`**(22は自宅IPのみ、80/443は全て) |

**配置(依存関係を ast で閉包計算して転送・SHA256照合済み)**

| 場所 | 内容 |
|---|---|
| `/opt/bacopy/` | `bacopy_api.py` `bacopy_db.py` `decision_logger.py` `snapshot_store.py` `dual_line_match.py` `dual_line_logic.py`(`8dde1db81f`) `bacopy_master_ui.py` `bacopy_master_ledger_ui.py` |
| `/opt/collector/` | `collector_pragmatic.py`(★`_vps_prod` 版 `e69f386b6c`) `analytics_pragmatic_db.py` `snapshot_store.py` |
| venv | `/opt/bacopy/.venv` に **websockets 17.1 / requests 2.34.2 のみ**(camoufox・playwright 無し) |
| systemd | `bacopy-api.service`(127.0.0.1:8010) / `bacopy-collector.service` — 両方 enable+active |
| Caddy | v2.11.4。`master.bafather.uk → 127.0.0.1:8010`。ログは **journald**(★`/var/log/caddy` はユニットの `ProtectSystem` で書けず起動失敗する) |

**★秘密情報の置き場(この文書には値を書かない)**

`/opt/bacopy/.env`(600)に `BACOPY_API_KEY` と `BACOPY_MASTER_PASSWORD` を
サーバー上で `secrets.token_urlsafe` 生成済み。受け子ビルド時と `/master` ログイン時に使う。
取り出しは `ssh ... 'grep BACOPY_API_KEY /opt/bacopy/.env'`。

**動作確認済み(2026-09-12)**

```
/api/health            → {"ok": true}
/master                → HTTP 302 (未ログインなのでログイン画面へ。正常)
snapshots.json         → 60卓収集 / うち56卓が罫線データ保持
                         各卓に sequence / last_results / good_roads_map / statistics
メモリ                  → api 29MB + collector 38MB = 67MB (1966MB中)  ★1GBで十分だった
外部到達                → 22/80/443 とも OK。80 は Caddy が 308 で HTTPS へ転送
```

**残: DNS のみ。** `master.bafather.uk` の A を `160.251.211.181` へ向ければ、
Caddy が Let's Encrypt から証明書を自動取得する(現在も再試行中)。

### 5-2. ✅ DNS — 完了 (2026-09-12)

**`bafather.uk` の DNS は Cloudflare で管理**していた。

★**発見**: `master.bafather.uk` の A レコードが**解約済みの旧Xserver IP
`210.131.215.116` を指したまま**残っていた。受け子がこれを引くと、Xserver が
再割当した第三者のサーバーへ接続しに行く状態だった。→ `160.251.211.181` へ変更済み。

| レコード | 値 | プロキシ |
|---|---|---|
| `master.bafather.uk` A | **160.251.211.181** | ★**DNS のみ(灰色)** |
| `bafather.uk` A | 216.150.1.1 (Vercel) | DNS のみ |
| `www` CNAME | vercel-dns | DNS のみ |

★★**`master.bafather.uk` は絶対にプロキシ(オレンジ雲)にしない。** 理由:
1. **7/23 に `stake.com` を日本向け 451 にしたのは Cloudflare のプロキシ層そのもの。**
   自らその経路に乗ると、マスターAPIが止まった時点で①②③が同時に止まる
2. プロキシ経由だと Caddy の TLS-ALPN-01 検証が通らない

**TLS 取得済み**: HTTP-01 チャレンジで成功(★80番を開けておいたのが効いた)。
証明書は `/var/log/...` ではなく `/var/lib/caddy/.local/share/caddy/certificates/` 配下。

```
https://master.bafather.uk/api/health → {"ok": true}
https://master.bafather.uk/master     → 302 → /master/login
```

**→ Phase 1 完了。** 次は Phase 2(③田辺・受け子ビルドと $0.20 テスト)。

### 5-3. Phase 0 実機スパイク(ログインが要るのでオーナーのみ・半日)

1. ミラー1本(例 `stake.bet`)に**実Chromeで手動ログイン** → JSチャレンジ通過してロビーが出るか
2. DevTools → Network → WS で **`casinoId` を実測** → `ppcds00000003709` と一致するか ★最大の分岐
3. WS のホスト名を記録 → `<mirror>/_api/websockets` の形か
4. 卓に入り `lpbet` のフレームとホストを記録
5. **$0.20 を手で BET** → 受理されるか
6. **別ドメインでも 1〜2 を繰り返す** → ★ミラー間で `casinoId` が同じならフェイルオーバー設計が成立
7. 突き合わせ先 = `PHASE0_DGA_BASELINE_2026-09-12.md` の60卓 qpid 表

### 5-4. その他の未確定

- ①②③ それぞれの受け子の**人数** → インストーラの本数が決まる
- ②の開始額・SEQ形状(サーバー側設定なので後から変えられる)
- `CREDENTIAL_REVOCATION_CHECKLIST_2026-08-22.md` の**🔴6件が全て未着手**
  (ANTHROPIC_API_KEY revoke / Stakeパスワード変更 / STAKE_API_TOKEN失効 / Telegram bot トークン3本再発行)

---

## 6. git の状態

### (a) ✅ コミット済み — 2026-09-12

ブランチ `feat/engine-heartbeat`。**追跡ファイルの未コミットはゼロ。**

| commit | 内容 |
|---|---|
| `23f4f56` | docs: 3方向復活の計画書・作業台帳 + _deploy_vps.ps1 に実行停止ガード |
| `d438a2e` | feat(collector): dga直結のみモード (BACOPY_DGA_ONLY) — ブラウザ不要にする |
| `f006242` | feat(stake-origin): Stake のミラードメインに対応する |
| `f510190` | fix(security): 解約済みVPS IPのハードコード除去 + 雛形の実キー掃除 |

**まだ push していない**(4コミット先行)。push は `origin/feat/engine-heartbeat` へ。

### (b) ★git に入っていない変更 — ここだけ復元できない

**意図的に除外されているファイル**なので追加しなかった。無害化はディスク上で完了しているが、
**作業ツリーを失うと消える**。再適用は容易(いずれも1行のコメントアウト/env化)。

| 対象 | 除外理由 | 何を変えたか |
|---|---|---|
| `analyze_*.py` 11本 / `verify_top3_regularity.py` / `scripts/test_all_tables.py` | `.gitignore:22/49/50` で**意図的に除外**(ad-hoc 分析スクリプトの規約) | `_LAPLACE_SSH_HOST()` ヘルパ挿入 + SSH先の置換 |
| `dist_client/` 4本 | `.gitignore` 対象(配布ステージング) | 旧IP無害化。★ここから配ると旧IPが出ていくので、**再生成時は必ず再確認** |
| `copytrade_gui/build_staging/.env` | `.gitignore:1`(実キーを含むため) | 4キーのみ変更。実キー類には触れていない:<br>`BACOPY_SUPPORT_ENABLED=1→0` / `BACOPY_SUPPORT_SSH_HOST` を空 / `BACOPY_LOBBY_URL` を空 / `BACOPY_STAKE_ORIGIN=` 追加 |
| `_asar_*` `_v2` `_live_verify` `_verify_asar` `copytrade_gui/{main.js,_verify,_asar_tmp,_asar_peek,.codex_app_asar_stage}` 計13本 | 未追跡のビルドスクラッチ | `BACOPY_API_FALLBACK_IPS` 行の無害化 |

### (c) ★要ローテーション(コミットでは消えない)

`.env.dist:14` に実キー `LAPLACE_API_KEY` が直書きされていた。**2026-04-15 の初回コミット以来**
入っていたもので、今回ファイルからは掃除したが**git 履歴には残る**。
`CREDENTIAL_REVOCATION_CHECKLIST_2026-08-22.md` の作業と併せてローテーションすること。

---

## 7. この環境固有のこと(ハマりどころ)

| 事象 | 対処 |
|---|---|
| ★**`grep -r ... | pipe` が「空 = クリーン」と誤読させる** | パイプ時はブロックバッファリングが効くので、`timeout` で殺されると**結果が出ないまま空に見える**。2026-09-12 に実際に誤報告した。**検証には `git grep` か ripgrep(`Grep` ツール)を使う** |
| Bash の `grep -r` がリポジトリ全体だと**120秒でタイムアウト**する | V: が HDD で巨大。`Grep` ツールか、パスを絞る |
| `python -c "print('✅')"` が `UnicodeEncodeError: cp932` | `export PYTHONIOENCODING=utf-8`。ASCII で出力するのが無難 |
| `grep -P '[\x{3040}-...]'` が日本語を検出できない | ロケール依存。Python で数える |
| PowerShell 5.1 が `.ps1` の日本語を化けさせる | **UTF-8 BOM を付ける**(`_deploy_vps.ps1` は付与済み) |
| `open()` が cp932 で落ちる | 必ず `io.open(..., encoding='utf-8')` |
| ★**heredoc にバックスラッシュを書くと1段落ちる** | `` が**リテラルのバックスペース(0x08)**としてファイルに入る。端末では不可視なので `sed`/`grep` で見ても気づけず、正規表現が無言で何にもマッチしなくなる。`co_consts` を見て初めて判明した。**バックスラッシュを含むコードは Write/Edit で書く** |
| この環境には **camoufox が入っている** | dga-only の検証は偽モジュールで遮断しないと意味がない(§3-3) |

---

## 8. 罠(3チームとも最初から入れる)

| 罠 | 対処 |
|---|---|
| **ミラードメインが 451 で焼かれる** | オリジンを exe に焼かない。サーバー配信 + 自動フェイルオーバー |
| ドメイン切替直後は**未ログイン** | cookie はドメイン単位 |
| `_api/websockets` の判定を直し忘れる | **エラーを出さず BET が飛ばなくなる**。判定は `_is_stake_account_ws()` 一本に集約済み |
| `_deploy_vps.ps1` を実行する | 直下の回帰版を本番へ上書きする。**ガード済み** |
| 直下の `dual_line_*.py` を「整理」する | `bacopy_api.py` + 約25本が壊れる |
| Chrome 冷間起動の「Failed to start third party session」 | ロビー直リンク禁止。`casino/home` 経由 |
| 個人Chrome と `:9222` の競合 | 賭け用は専用プロファイル |
| 受け子ごとに BET コードが違う (Banker=10/Player=11) | `.env` の `bc` |
| `loss_cut=0` | ゼロで配らない(user04 全損の直接原因) |
| `.env` を変えても効かない | **source した親プロセスを再起動**。`/proc/<pid>/environ` で確認 |
| 停止し残し | systemd / cron / **手動nohupの孤児** の3経路 |
| exe の中身の検証 | **文字列検索でなく実行**して確かめる |
| ssh越しの `pkill -f` | **自セッションを殺す**。PID を目視して kill |
| `.gitignore` の `_*.py` | `find | wc -l` と `git ls-files | wc -l` を突合 |

---

## 9. 次の一手

1. **オーナー**: VPS 1台(§5-1)を発注 / DNS の所在を確認(§5-2) / Phase 0 スパイク(§5-3)
2. **こちら**: `GET /api/origin` の実装(§4-1)。VPS 無しで進む
3. VPS が来たら Phase 1 残(構築・鍵の新規発行)→ Phase 2(③田辺)

★作業したら**このファイルの §3 / §4 を更新する**。日付違いのコピーを作らない。
