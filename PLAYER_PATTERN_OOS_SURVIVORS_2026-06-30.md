# Player パターン OOS 検証 — 生き残り2本 + フラグ2件（2026-06-30）

> ## ⚠️⚠️ 2026-06-30 追記：本検証は本番と不一致＝結論は信頼不可・要再検証 ⚠️⚠️
> 後の調査で、本検証の replay（`dual_line_backtest_html` 流儀＝**T込みで珠盤路を構築**）が
> **本番bot（`observed_sequence` は L2524 で `c != "T"` ＝Tを除外）と一致しない**ことが判明。
> 本番は **Tを除外したP/B列で `decide()`** を呼ぶ。さらにローカル `dual_line_logic.predict_big_road` の
> 複数一致解決も本番（VPS master）と異なり、採用 nikoichi 系が n=0 になる別の不一致もある。
> **＝本ファイルの数値（sansan劣化・生き残り2本 等）は本番を反映しておらず、結論にしてはならない。**
> 正しい検証は **VPS の本番 logic/match を引いて再実行**（別途実施）。


> 質問「`dual_line_all_patterns_report_NEW.html` の中から Player（イーブンマネー＝手数料ゼロ）だけ選んでエッジがあるものを探せないか」に対し、
> in-sample の $/BET+ を鵜呑みにすると過学習を踏むため、**ローカル 126,560 シューを4つの cut で OOS 分割**して「どの cut でも OOS+ か」を検証した。

---

## ★結論：堅牢に生き残った Player エッジは2本だけ（どちらも既採用）

| パターン | 賭け | 採用 | 全4cut OOS $/BET | 評価 |
|---|---|---|---|---|
| **telecho\|nikoichi\|P** | P | **v3** | +.012 / +.020 / +.023 / +.045 | ★全cut+（堅牢・n535〜1360） |
| **niconico\|nikoichi\|P** | P | **v4** | +.032 / +.020 / +.052 / +.092 | ★全cut+（堅牢・n178〜504） |

- **新しい単独 Player エッジは事実上ゼロ**。OOS を生き残った Player はこの2本で、**既に採用済み**（v3 の telecho\|nikoichi\|P、v4 の niconico\|nikoichi\|P）。
- 「Player×エッジ」の探索は既に完了済みで、答えは現行採用の正しさの裏付けに帰着した。

---

## 手法
- **replay はレポート生成器 `dual_line_backtest_html.py` の `run_backtest` と同一**：各手で `predict_bead`(中国式)と `predict_big_road`(大路)が**一致した時だけ bet**、T=push、Banker勝ち×0.95。pattern_key=`china|big|side`。
- データ＝`_edge_analysis/shoes_export.csv`（126,560 シュー・seq＋created_at・2026-04-16〜06-18）。
- OOS cut＝**5/20・5/26・6/1・6/8**（各 cut 以降を OOS 集計）。n>=50 のみ判定。
- script＝`_bt_player_oos_multi.py`（単cut版＝`_bt_player_oos.py`）。Z基準 p=0.4932(Player)。

## 全結果（Player・in-sample $/BET+ だった7本）

| パターン | 5/20 | 5/26 | 6/1 | 6/8 | 判定 |
|---|---|---|---|---|---|
| telecho\|nikoichi\|P [v3] | +.012 | +.020 | +.023 | +.045 | ★全cut+ |
| niconico\|nikoichi\|P [v4] | +.032 | +.020 | +.052 | +.092 | ★全cut+ |
| telecho\|niconico\|P [除外済] | +.052 | +.051 | +.042 | +.117 | ★全cut+（謎・下記） |
| sansan\|telecho\|P [v3] | +.058 | +.042 | −.019 | −.113 | ✗ 直近で劣化（下記） |
| niconico\|telecho\|P | −.015 | −.017 | −.025 | −.098 | ✗ 全cut負（過学習） |
| telecho\|telecho\|P | −.013 | −.017 | −.034 | +.002 | ✗ ほぼ負（過学習） |
| niconico\|dragon\|P | −.032 | −.036 | −.030 | −.029 | ✗ 全cut負（過学習） |

→ **過学習フィルタは機能**：`niconico|telecho|P` / `niconico|dragon|P` / `telecho|telecho|P` は in-sample で +.003〜.007 だったが**全cut負**＝非採用が正解。

---

## ★人が見るべきフラグ2件（要・追加調査）

### ① `telecho|niconico|P` の謎（backtest+ vs ライブ除外）
- **全4cutで堅牢に OOS+（+.042〜.117・n242〜662）** なのに、オーナーは **2026-06-19 にライブ実データ ~49% として除外済み**（`PATTERN_EXCLUDE_TELECHO_NICONICO_P_2026-06-19.md`）。
- backtest（収集シュー）とライブ実績が真っ向から食い違う。考えられる原因：
  1. 私の `predict_bead`/`predict_big_road`（root `dual_line_logic.py`）が**現行 production の "新bline/pline定義"（`repat_bt`）と違う**可能性。
  2. 除外を決めたライブ判定が**小サンプルの不運**だった可能性。
- **自動で足すな**。要照合＝(a) production の predict 定義で再backtest (b) VPS `pattern_prereg_history.csv` 等でライブ前向き実績を直接確認。

### ② `sansan|telecho|P`（現v3）の直近劣化
- レポートの "最強"（in-sample 58.7%）だが、**OOS窓を直近に寄せるほど 53%→52%→49%→44% と単調悪化**。
- 小n（55〜156）だが**単調な右肩下がりはノイズらしくない**＝直近のエッジ減衰の兆候。別study（`PATTERN_REFINE_OOS_2026-06-23.md`・cut 5/26）では +.058 だったので本物ではあるが、**低頻度で振れが大きい**。**監視推奨**（パニックで外すほどではない）。

---

## 教訓（再確認）
- **in-sample の $/BET+ は鵜呑み禁止**＝7本中3本が全cut負（過学習）。
- **1cut の OOS も結論にできない**＝sansan は cut 次第で +.058 にも −.113 にもなる。**マルチcutで「どのcutでも+か」を見て初めて堅牢性が分かる**。
- 唯一の本物 Player エッジは既採用の2本に帰着。新規発見なし。
- 関連：`PATTERN_REFINE_OOS_2026-06-23.md` / `WITHIN_PATTERN_EDGE_SCAN_2026-06-23.md` / `PATTERN_EXCLUDE_TELECHO_NICONICO_P_2026-06-19.md`。
