# 勝率チャート（パターン/追従）汚染除去・正確化 — 2026-06-26〜27

> GUI勝率レンジチャート＋テレグラムdry-runの v3(6パターン) が「除外済みパターンの負け」を
> 引きずって不正確に見えた問題を、パターンタブ・追従タブの両方で正確化した記録。
> 源データは全てVPS（`dual_line_pragmatic_bot.py` の counters）。実BETには影響しない統計専用系。

## 背景：2系統のチャート源
- **パターンタブ** v3=botの `self.wins`(`[STATUS] W/L=`) / v4=`self._v4`(`[v4 STATUS]`)。`winrate_trend.py`(VPS cron毎分)がbotログをparse→`/opt/bacopy/winrate_trend.json`→`/api/winrate-trend`→GUI。
- **追従タブ** = `dual_line_follow_sim.json`（同botが書く・**per-pattern内訳なし**）。`big`(pattern_keyの2番目)が telecho/dragon の手だけ集計。
- テレグラムdry-run も同じ counters（`DUAL_LINE_RESULT_CHAT_ID` へ送信）。

## ① パターンタブ v3 = 50.64% → 51.87%（除外分を減算・履歴維持）
- 真因：6→3精製([[project_pattern_refine_oos]])時に `LOGIC_VERSION` を変えず state未リセット＝除外3パターン(`niconico\|dragon\|P`/`telecho\|niconico\|P`/`niconico\|niconico\|B`)の負けが累計に焼付。さらに2026-06-23設計で「パターン変更でも通算維持(0リセット回避)」が意図的だった。
- 修正：state `dual_line_pragmatic_state.json` の `per_pattern`(pred/wins/losses/ties/pnl完備)から**精製3だけ再計算**→wins=2174 losses=2017=**51.87%**(バックテスト一致)・除外3をper_pattern削除。logic_version維持(loadでリセットさせず補正値を読ます)。bot kill→隙にstate補正→wrapper再生成で確定。
- v4(50.5%)は健全(汚染ほぼ無し)＝触らない。

## ② 追従タブ v3 = 49.82%(汚染) → 48.31%(正確・再構築)
- 追従simにper-pattern内訳が無く減算不可。当初リセットしたが**前向き集計で点が貯まらず数日ブランク**（4〜8件/時）。バックアップ復元すると古い`niconico\|dragon\|P`(big=dragon)込みの49.82%＝不正確。
- ★オーナー要望「エッジ有無でなく%の推移を正確に見たい」→**履歴を再構築**：botの追従ロジック(`_follow_sim_now_result`/`_follow_sim_on_hand`)を忠実再現し、過去シュー(6/21-26)を**精製3パターンだけ**でリプレイ→追従込み時系列を再構築→follow_sim+チャートに注入。
- 結果＝**追従込み 48.31%(n=1362・推移48.3〜51.7%)**。**汚染版49.82%より1.5%低い＝`niconico\|dragon\|P`の水増しを除去**(オーナー指摘が正しかった)。
- 注入後ライブは精製3パターンだけ追うので正確なまま継続。

## 追従ロジック(再構築で再現した仕様)
- 集計対象=big が telecho/dragon のパターンのみ(精製v3では `sansan\|telecho\|P`/`telecho\|telecho\|B`。`telecho\|nikoichi\|P`はnikoichiで対象外)。
- NOW勝ち→追従arm(telecho=方向反転P↔B/dragon=同方向)→次手から決済(fw/fl)。連勝で継続(telechoは毎回反転)・T=同方向再BET・負けでチェーン終了。追従込み%=(w+fw)/(w+fw+l+fl)。

## テレグラムdry-run
- 「追従込み: ◯W/◯L」行は6パターンresult通知に毎回付く(MIN_N等の閾値なし)。但しv3追従が決済する時だけ(≈4件/時)＝稀＋小サンプルで埋もれて見えにくい(故障でなく頻度)。定期ステータス投稿を足せば常時可視化できる(未実装)。

## 教訓
- 「チャートと実測が逆」=見てるデータが別物(ライブ累計カウンター vs 母集団)＋カウンターが古いパターン集合を引きずる。
- per-pattern内訳があれば減算で履歴維持／無ければ**過去リプレイで再構築**が正解(リセットの数日ブランクも・古いバックアップ復元の汚染も回避)。
- 追従は無エッジ(~50%)だが**「%の推移を正確に見たい」用途では1.5%のズレも重要**＝「無エッジだから誤差でいい」は的外れだった(オーナー指摘)。
- script=`_reconstruct_follow_v3.py`(再構築)。バックアップ=`dual_line_follow_sim.json.bak_*`/`winrate_trend.json.bak_*`。MIN_Nは500に復帰。
- 関連=[[project_winrate_chart_v3_decontaminate_2026-06-26]]・`WITHIN_PATTERN_EDGE_SCAN_2026-06-23.md`。
