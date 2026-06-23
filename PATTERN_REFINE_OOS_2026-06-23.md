# パターン再検証(136k OOS)→劣化3本除外 + 自動Chromeリフレッシュ撤回  2026-06-23

## 背景
5月のレポート(`dual_line_all_patterns_report.html`・77,528シュー)を、**シューターが1.7倍(136,742)に増えた今**もう一度実施。
目的=①6/10パターンは今もエッジがあるか ②別パターンが浮上するか ③総入れ替えが必要か。
要件=①同パターン ②未来型(1手ずつ・過学習禁止) ③Bライン=v4新定義(5+/逆1/T無視)・Pラインも同定義(対称)。

## 方法
- `_vps_dlbt_html.py`(=VPS `dual_line_backtest_html.py`)の`predict_bead`を**新bline/pline定義**に差替(telecho/niconico/sansan→無ければ T除外5+セル・逆≤1で pline(B≤1)/bline(P≤1))。
- `run_backtest`に`where_extra`追加し **full(136,742) と OOS(created_at>=2026-05-26 = 61,303シュー=レポート以降の未知データ)** を集計。
- driver=`_bt_repat_driver.py`。Z基準 p=0.5068(B)/0.4932(P)。**採用パターンは再選択せず**、現v3/v4をOOSで検証(過学習排除)。

## 結果(OOS=未知61kでの $/BET)
**現採用14本中8本がOOSプラス維持＝核は本物。3本が負け転落。**

維持(OOS+): sansan|telecho|P +.058 / niconico|nikoichi|P +.045 / telecho|nikoichi|B +.036 /
telecho|nikoichi|P +.016 / telecho|telecho|B +.013 / bline|telecho|B +.010 /
niconico|dragon|B +.019 / niconico|nikoichi|B +.005

**劣化(OOS−)=除外:**
- **niconico|niconico|B (v3)  OOS −0.077**
- **niconico|dragon|P (v3)   OOS −0.034**
- **telecho|niconico|B (v4)  OOS −0.035**

浮上候補(in-sample選択=要再確認): bline|niconico|B(OOS+.030)・telecho|niconico|P(+.017・6/19除外分)。**焦って足さない**。

## 結論
**総入れ替え不要。核は健在。「負け転落3本を外す」だけのチューニング**(過学習でなく純粋なmoney-loser除去)。
→ v3=**3本**(telecho|telecho|B・telecho|nikoichi|P・sansan|telecho|P) / v4=**8本**。
最近のv3軟調(49.65%/4日)の一因が劣化2本の可能性→外せばv3が締まる。

## 反映(全経路)
1. engine/GUI: `_rev53144/dual_line_match.py`(+root)→engine再ビルド`2A6C8A09`(68731639)→bafather swap。
2. master予告: VPS `/opt/bacopy/dual_line_match.py` + `systemctl restart bacopy-api`。
3. **テレグラム ドライラン**: VPS `/opt/laplace2/dual_line_match.py`(v3=V2 import) + `/opt/laplace2/dual_line_pragmatic_bot.py`(v4ハードコード)→bot再起動。**勝率カウンタは state復元で保持**(`state 復元: signals=8580`)＝累計継続・劣化分が抜けて徐々に上がる。
4. 受け子02-10リビルド(新engine+クリーンsrc)・commit `8213c9e` push済。

## ★同時対応=自動Chromeリフレッシュ撤回(flash遅延回帰)
今日の作業中、bafatherで**①決済→即光らない ②黄色枠が残り続け消えるまで結果が光らない**が再発。
- 真因=**昨日入れた自動Chromeリフレッシュ(10b6988)**。再起動毎に賭けChrome全kill→**48卓再センタリング(1-2分)**中は決済pumpが回らず光り遅延。テスト中6分間隔で常時発生していた。
- flash修正自体は無傷(エンジン既定ON: skip-refocus=1・WS先行送信=1・追従再計算=0)。**回帰でなく新機能の副作用**。
- 対処= main.jsの`killCdpChrome`呼び出し撤去・**6hエンジンのみ再起動に戻す**(関数は将来の穏当版用に残置)。bafather=.env`BACOPY_PERIODIC_RESTART_HOURS=0`+再起動で**光り即速復活を実証**。
- Chrome膨張のNOW取りこぼし対策は当面**手動STOP→START**。後日churnの出ない方式で作り直す。

## 教訓
- 「増えたデータで再検証」=**採用パターンを再選択せず、未知OOSで現パターンを検証**(過学習禁止の核心)。負け転落を外すのは改善・浮上候補を足すのは要再確認。
- **新機構(自動リフレッシュ)は必ずbafather実証**(churnの副作用はテストでしか見えない)。once-worked症状の再発は構造論より「直近の自分の変更」を疑う。
