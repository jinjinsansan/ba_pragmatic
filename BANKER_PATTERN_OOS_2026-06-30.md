# Banker パターン OOS 検証 — 堅牢3本 + フラグ2件（2026-06-30）

> ## ⚠️⚠️ 2026-06-30 追記：本検証は本番と不一致＝結論は信頼不可・要再検証 ⚠️⚠️
> Player 版と同じく、本検証の replay は **T込みで珠盤路を構築**するが、**本番bot は T を除外**（L2524）。
> さらにローカル `dual_line_logic` の大路解決が本番（VPS master）と異なり、採用 nikoichi 系が n=0 になる。
> **＝本ファイルの「堅牢3本」「telecho\|nikoichi\|B 劣化」等の数値は本番を反映しておらず、結論にしてはならない。**
> 正しい検証は **VPS の本番 logic/match を引いて再実行**（別途実施）。


> Player 検証（`PLAYER_PATTERN_OOS_SURVIVORS_2026-06-30.md`）に続き、**Banker 系を根本から**同じ手法で検証。
> 全 Banker パターンを動的発見し、in-sample(<5/20) で $/BET>0 のものを**4つの cut で OOS 分割**して「どの cut でも OOS+ か」を確認。
> Banker は勝ち×0.95（5%手数料）＝**分岐 51.28%**。手数料は $/BET に反映済み（$/BET>0 = 手数料を超えてプラス）。

---

## ★結論：堅牢に生き残った Banker エッジは3本（すべて既採用）

| パターン | 採用 | 全4cut OOS $/BET | 評価 |
|---|---|---|---|
| **telecho\|telecho\|B** | **v3+v4** | +.009 / +.013 / +.043 / +.100 | ★全cut+（堅牢・直近で強化中・n834〜2262） |
| **niconico\|dragon\|B** | **v4** | +.033 / +.042 / +.034 / +.039 | ★全cut+（堅牢・一貫・n402〜1065） |
| **niconico\|nikoichi\|B** | **v4** | +.057 / +.046 / +.018 / +.079 | ★全cut+（堅牢・n158〜424） |

- **新しい単独 Banker エッジはゼロ**。in-sample+ で非採用だったのは過学習の1本（下記）だけ。堅牢3本は**既に採用済み**。
- Player と同じ結論：エッジ探索は既に完了済みで、現行採用の正しさの裏付けに帰着。

---

## 手法
- replay はレポート生成器 `dual_line_backtest_html.py` の `run_backtest` と同一（`predict_bead`×`predict_big_road` 一致時のみ bet・T=push・Banker勝ち×0.95）。
- データ＝`_edge_analysis/shoes_export.csv`（126,560 シュー・2026-04-16〜06-18）。
- in-sample = <2026-05-20／OOS cut = 5/20・5/26・6/1・6/8（各 cut 以降）。n>=50 のみ判定。Z基準 p=0.5068(Banker)。
- script＝`_bt_banker_oos_multi.py`。

## 全結果（in-sample $/BET+ だった Banker 5本）

| パターン | 採用 | in-sample | 5/20 | 5/26 | 6/1 | 6/8 | 判定 |
|---|---|---|---|---|---|---|---|
| telecho\|telecho\|B | v3+v4 | +.013/52%/n2283 | +.009 | +.013 | +.043 | +.100 | ★全cut+ |
| niconico\|dragon\|B | v4 | +.007/52%/n1115 | +.033 | +.042 | +.034 | +.039 | ★全cut+ |
| niconico\|nikoichi\|B | v4 | +.004/52%/n425 | +.057 | +.046 | +.018 | +.079 | ★全cut+ |
| telecho\|nikoichi\|B | v4 | +.009/52%/n1367 | +.020 | +.033 | −.002 | −.002 | ⚠️ 直近水面下 |
| niconico\|niconico\|B | 除外 | +.099/57%/n222 | −.112 | −.098 | −.050 | +.051 | ✗ 過学習 |

→ **過学習トラップ＝`niconico|niconico|B`**：in-sample **+.099/57%（Z+1.95＝最強に見えた）**のに OOS で全部マイナス。「in-sample で一番強そうなものが一番危ない」の完璧な見本。除外済みで正解（`PATTERN_REFINE_OOS_2026-06-23.md` の除外と一致）。

---

## ★人が見るべきフラグ2件

### ① `telecho|nikoichi|B`（採用v4・Banker）の直近劣化
- 序盤cut +（.020/.033）だが **直近2cut（6/1・6/8）で −.002＝水面下（実質トントン）**。n十分（522〜1441）。
- ★**side差の実例**：**Player版 `telecho|nikoichi|P` は全cut+で堅牢生存**（Player MD参照）なのに、**Banker版は手数料(0.95)の重しで分岐51.28%に届かず沈みかけ**。同じ china|big でも賭ける side で生死が分かれる。要監視/レビュー。

### ② `bline|telecho|B`（採用v4）＝私の検証では評価不能（定義不一致）
- in-sample **−.009/51%/n3955**＝候補にすら入らず。
- だがこれは 2026-06-07 に**「新bline定義（5個以上・逆1個まで・T無視）」で選定**されたパターン。**私のローカル `dual_line_logic.predict_bead` は旧/別の bline 定義**の可能性大＝**bline系は私の backtest で正しく計算できていない**。
- Player 側 `telecho|niconico|P` の謎と**同じ定義不一致**が根。**この穴のため bline/pline 系の新エッジを見落とす/誤評価する可能性**が残る。

---

## 残課題：定義不一致の解決（Player+Banker 共通）
両検証で **bline/pline 系の定義不一致**が2回障害（`telecho|niconico|P` の謎・`bline|telecho|B` の評価不能）。
**現行 production の predict 定義（VPS `repat_bt` / 新bline・pline定義）を引いて再検証**すれば両方解ける。これが次の最も価値ある一手。

## 教訓（Player と共通・再確認）
- in-sample の $/BET+ は鵜呑み禁止（Banker でも `niconico|niconico|B` が +.099→OOS負）。
- 1cut では結論不可・マルチcutで初めて堅牢性が見える。
- side で生死が変わる（`telecho|nikoichi` は P=生存 / B=沈みかけ）＝手数料分岐の差。
- 関連：`PLAYER_PATTERN_OOS_SURVIVORS_2026-06-30.md` / `PATTERN_REFINE_OOS_2026-06-23.md` / `WITHIN_PATTERN_EDGE_SCAN_2026-06-23.md`。
