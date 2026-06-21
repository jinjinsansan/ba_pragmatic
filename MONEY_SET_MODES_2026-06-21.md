# 新資金管理 2種（7ハンドセット版 1-2-3 / ダランベール）— 2026-06-21

> 動機：**1-2-3打法**は連敗(同結果連続)中に1→2→3と上げるので資金が早く減る。**SEQ**はこの数日の通り急ラダーで飛ぶ。
> そこで「1-2-3 / ダランベールの**上げ方・戻り方を、1手ごとでなく Nハンド(=ターン制 5/7)=1セット単位で適用**」する2モードを新設。セット単位なので escalation が緩やか＝飛びにくく、緩く回収する。

---

## 1. bet123set（1-2-3×セット）★破滅しない
- **1セット = seq_set_size(5 or 7) ハンド**（タイ除く＝7決着）。セット中は賭け額**固定** = `unit × step`（step1=1単位 / step2=2単位 / step3=3単位）。
- セット確定後の純結果で 1-2-3打法に**忠実**に更新：
  - **負け越し** → step+1（1→2→3）。**step3で更に負け越し→step1**（1-2-3を一巡したらリセット＝深追いしない）。
  - **勝ち越し** → step1へリセット。
- 賭け額の**最大 = 3単位 → 絶対に破滅しない**。
- ユーザー指定 = 「1-2-3打法の上げ方/戻り方を7ハンドで考える」「step3で更に負けたらstep1（案A・1-2-3に忠実）」。

## 2. dalembertset（ダランベール×セット）※loss_cut必須
- 1セット = seq_set_size ハンド。セット中は賭け額**固定** = `unit × level`（level=1,2,3,...）。
- セット確定後：**負け越し→level+1 / 勝ち越し→level-1（下限1）**。
- **上限なし**（線形に青天井）。1-2-3より回収力は高いが、連敗で増え続けるので **loss_cut（損切り）併用を強く推奨**（GUIに赤字明記）。

## 3. バックテスト（`_bt_123set.py`・4000セッション×700ハンド・$1・v3エッジ~50.9%/tie9.5%）
| モード | 中央益 | p05 | 最悪 | 中央DD | 最大BET | マイナス日% | 性質 |
|---|---|---|---|---|---|---|---|
| flat | 1.7 | -39 | -88 | 27 | $1 | 47% | 基準 |
| bet123(手ごと) | 4.1 | -77 | -153 | 52 | $3 | 47% | 連敗でじわ減り |
| **bet123set** | **4.2** | -68 | -162 | 46 | **$3** | 46% | **有界・破滅せず**・手ごと123よりテール僅かにマシ |
| **dalembertset** | **48.6** | -282 | -1,084 | 113 | **$35** | **33%** | 回収力↑・**上限なし**(loss_cut必須) |
| SEQ small1 | 175.6 | -717 | **-8,465** | 238 | $333 | 22% | 指数・破滅域 |

→ スペクトル＝**有界(123×セット) ＜ 線形(ダランベール×セット) ＜ 指数(SEQ)**。状態機械も検証（bet123set: 3W4L→step2 / 4W3L→step1 / 連敗1→2→3→1。dalembertset: level 2,3,4,3,2）。

## 4. 実装
- **エンジン** `_rev53144/dual_line_money.py`（ビルド元・rootコピーは旧版で未使用）：
  - `BET_MODES` に `bet123set` / `dalembertset` 追加。
  - 状態 `b123set_step`/`b123set_marks`・`dalembertset_level`/`dalembertset_marks`（save/load 永続）。
  - `_compute_next_bet`（loss_cut clamp付）・`apply_result`（セット境界でstep/level更新・両モードを従来SEQ/martingale経路から除外）・`status_dict`（GUI表示用フィールド）。
  - **エンジン本体(`dual_line_pragmatic_bot.py`)は無変更**：CLIは `MONEY_MODES` から選択・`seq_set_size`(=ターン制)をBetManagerに渡すため、money module追加だけで両モードが流れる。
- **GUI**：
  - MONEY MODE に「1-2-3×セット」「ダランベール×セット」追加（ターン制5/7＋ユニット額を共有・隠し`inputDualMoneyMode`に集約）。
  - パネル `renderBet123SetPanel`(123×SET: STEP/3)・`renderDalembertSetPanel`(DALEMBERT×SET: LEVEL)＝現セットの〇✕進行＋ラダー(現在地強調)。
  - **安全モードをコメントアウト**（HTML form-group＋JSリスナー・復活可・エンジン配線は無害で残置）＝使用しなくなったため。

## 5. デプロイ
- **bafather**：engine `26BDA93B`(68,730,505・reconnect+2モード) swap済（旧922C73DBをbackup）。asar `295826`(勝率レンジ＋2モード＋安全モード非表示) swap済（旧288550をbackup）。GUI起動・engine稼働・エラー無し確認済。
- **build_staging**：engine 68730505 取り込み済。
- **受け子02-10**：NSIS全9本リビルド（`_build_user0210.ps1`・engine 26BDA93B・bc=user06のみ10/11・asar=現src）。配布=オーナー判断。
- money moduleは engine.exe 同梱のため、有効化に**エンジン再ビルド必須**（GUIだけでは不可）だった。

## 6. 関連
- 旧123/SEQ/ターン制の正確な仕様＝`_rev53144/dual_line_money.py` + `marubatsu_strategy.py`（7ハンド=1セット・overshoot回収）。
- SEQ破滅の実害＝`FLEET_SEQ_DRAWDOWN_2026-06-21.md` / `[[project_fleet_drawdown_2026-06-12]]`。
- Kelly（もう一つの非破滅型）＝`[[project_kelly_money_mode_2026-06-20]]`。
- 勝率レンジチャート（同asarに同梱）＝`WINRATE_RANGE_CHART_2026-06-21.md` / `[[project_winrate_range_chart_2026-06-21]]`。
- ビルド手順＝`DISTRIBUTION_BUILD_RUNBOOK.md` §2(engine) §3-A(NSIS)。
