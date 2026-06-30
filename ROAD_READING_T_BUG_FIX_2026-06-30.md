# 罫線のT処理バグ修正 ＋ v3=6本 再選定（2026-06-30）

> ★本システム最大級のバグ発見と修正。**実運用開始(6/6)からずっと、本番botが「間違った罫線」で賭けていた。**

---

## 1. 何が起きていたか（バグの本質）
- パターン判定の珠盤路を組む `_bead_row_cells` は **「T もそのまま含める」設計**（docstring明記。T含み行は判定不能扱い）。レポート生成器 `dual_line_backtest_html` / 選定backtest `repat_bt`（VPS `/tmp`）も **T込みで読む**。オーナーの指定した罫線の読み方も **T込み**。
- ところが**本番bot（VPS監視bot `/opt/laplace2` ＋ 受け子エンジン `_rev53144`）は `decide()` を呼ぶ前に T を除外**していた（`if c and c != "T":` で T を捨てる）。
  - VPS bot: `seq_chars.append(c)` を `if c and c != "T"` でガード（L722/1150/1275/2117）。
  - 共有関数 `live_signal_for_history`/`live_preposition_for_history` も内部で `if c in ("P","B")` と T を捨てる。
- ＝**設計・選定・意図はすべて「T込み」なのに、本番だけ「T除外」で珠盤路を読んでいた**＝罫線のマス配置が変わり、**全パターンの検出が系統的にズレる**。

## 2. 影響（重大）
- **6/6 の実運用開始から、本番botは意図したパターンと別物に賭けていた。**
- 採用 **nikoichi系4本（telecho\|nikoichi\|P, niconico\|nikoichi\|B/P, telecho\|nikoichi\|B）が T除外では一度も発火しない（n=0）**＝「死にパターン」だった。126kシュー検証で確認。
- **なぜ気づかなかったか（数学が隠した）**：正しいパターン~51.5% vs 間違ったパターン~50%＝差はわずか~1.5%。一方、日次/週次のノイズは±3〜5%（σ≈4.6%）。**~1.5%の差がノイズに完全に埋もれ、勝率を見てもバグが見えなかった。** テレグラムのドライラン（T込み理論）と実運用（T除外）が「なかなか合わない」のもこれが原因。
- 不幸中の幸い：間違って賭けたパターンも勝率~50%前後（強い負けでない）＋小額BETで、**破滅せず「エッジを取り損ねていた」だけ**で済んだ。

## 3. 切り分け（コードで確認した事実）
| 方法 | T処理 | 役割 |
|---|---|---|
| `_bead_row_cells` 設計(docstring) | T込み | 「正しい」読み方 |
| `repat_bt`(選定backtest VPS) | T込み | パターン選定はこれ |
| VPS監視bot `/opt/laplace2` | **T除外(バグ)** | bafather等の実BET生成元 |
| `live_signal_for_history`/`preposition` | **T除外(バグ)** | 共有シグナル関数 |
| VPS master `/opt/bacopy` `bacopy_api.py` | — | 計算せず中継のみ(修正不要) |

## 4. 修正内容（T込みに統一）
- **bot側**：`if c and c != "T":` → `if c:`（T を残す）。VPS bot4箇所 + `_rev53144` bot3箇所。next_n は `len(seq)+1` のまま自動で全位置になり repat_bt と一致。
- **共有関数**：`live_signal_for_history`/`live_preposition_for_history` の `if c in ("P","B")` → `if c in ("P","B","T")`（repo + `_rev53144` + VPS）。予告の suffix(P/B) は将来手予想なので不変。
- **予測関数内部のT処理（形状はT含み行を判定不能・line用pb_only）は設計通りなので不変**。

## 5. v3 = T込み(正定義)OOS生存6本 + sansan
オーナー決定。`dual_line_match.py` LIVE_SIGNAL_PATTERNS：
1. telecho\|telecho\|B / 2. telecho\|nikoichi\|P / 3. sansan\|telecho\|P（稀だが残す）/ 4. niconico\|nikoichi\|P / 5. niconico\|dragon\|B / 6. niconico\|nikoichi\|B
- 検証（修正後T込み・126k全データ）：6本すべて発火 + 全部プラス$/BET（sansan+.099/nikoichi系も+.02〜.03）。
- **V4 はホワイトリスト不変・T込み修正だけ**適用（オーナー指定）。

## 6. デプロイ状況
- ✅ **VPS監視bot `/opt/laplace2`**：T込み + v3=6本 反映・再起動済（PID更新）。検証=nikoichi系が発火（`[v4-publish] telecho\|nikoichi\|B`）・v3決定公開・v3フィルタ機能。バックアップ=`*.bak_20260630_160751`。
- ✅ **master 中継のみ**＝修正不要。
- **＝VPS駆動の全受け子は今この瞬間からT込み・正しい罫線で賭ける**（master中継・受け子エンジン再ビルド不要）。
- ⚠️ **bafather停止中**＝起動すれば正しく賭ける。
- **Phase 3（受け子エンジン再ビルド）優先度低**：VPS駆動では実BET側はmaster決定なので旧エンジンでも正しい。再ビルドはローカルDGA経路/GUI表示/caught_now整合用。
- repo変更（`dual_line_match.py`±`_rev53144`, `_rev53144/dual_line_pragmatic_bot.py`）コミット予定。

## 7. ★未決：カウンタの汚染（様子見中）
- bot再起動で**カウンタ復元**＝6/6からの間違いBET累計に、今からの正しいBETが**加算され混ざる**。
- テレグラムのドライラン累計・GUI勝率チャートは**混在**＝修正後の真の成績が汚染された過去に埋もれる。
- **クリーンに測るにはカウンタ・リセット（v3/v4状態・ドライランpnl/signals・勝率チャート源）が必要**。オーナー判断で**現在は様子見**。リセットすれば修正後をクリーンに検証でき、ドライランとチャートが一致する。

## 8. 関連
- 検証script：`_bt_oos_v4def.py`（T除外＝本番忠実検証で nikoichi n=0 を発見）/ `_bt_player_oos_multi.py` / `_bt_banker_oos_multi.py`。
- T込み(正定義)の生存パターン：`PLAYER_PATTERN_OOS_SURVIVORS_2026-06-30.md`(T込みが正と判明し有効)/`BANKER_PATTERN_OOS_2026-06-30.md`。
- 経緯：レポート`dual_line_all_patterns_report_NEW.html`→Player/Banker検証→「本番不一致」発見→T処理が真因→オーナー「レポート(T込み)が正・本番がバグ」。
