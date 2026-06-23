# センタリング/tickブロック 診断（v4 LOOP-SLOWの正体）  2026-06-23

## 背景
v4(10パターン)でNOW連発時、光りが詰まる／取りこぼしが増える。原因＝`executor.tick`が長時間ブロック（LOOP-SLOW）。**推測でなく実機計測**で正体を特定した（commit `50049fd`で計測コード追加）。

## 計測結果（bafather・v4・8分）
```
LOOP-SLOW: count=60  max=53.4s  avg=8.7s   ← 8秒毎に1回・ほぼ常時ループが遅れている
TICK-SLOW 内訳(>2sのみ):
  _ensure_multi_area          max=38.1s  count=1   ← 単発最大（WS復旧のtab確認/ページreload・稀）
  _maintain_visible_bet_hold  max= 3.3s  count=3
  _inject_all                 max= 2.3s  count=3   (既に5秒スロットル済)
  _perform_switch             max= 3.1s  count=2
  _maintain_active_now_bet_hold = <2s   ← 無罪
```

## 結論＝「死の千刀」型（単一犯ではない）
- **稀な巨大ブロック**：`_ensure_multi_area`（38秒）。WSが180秒沈黙→ページreload、or マルチプレイtab未検出→`_MULTI_LOBBY_ENSURE_TAB_JS`評価＋デッドロック復旧reload（line 2927/2988/3031）。**復旧経路なので稀**。
- **常時の遅延**：LOOP-SLOW 60回/8分・各8.7秒。だが個別>2sは9件だけ→**残り~51回は「2秒未満のDOM操作が毎tick積み重なって8秒」**。maintainの**位置チェックが毎tick 48タイル走査**＋tab確認＋switch＋inject…が重いChrome(48卓・新鮮でも1.9GB)で各1-2秒、合計8秒。
- v4はNOW連発でこれが多発。**取りこぼしと光り遅延は同根**（tickブロック中はNOWの焦点合わせ・BETも間に合わない）。

## 直すなら（将来・優先順・ただし高リスク経路）
1. **毎tickの位置チェックをスロットル**（maintainは再センタリング(RECENTER_SEC=30)だけでなく「ズレ確認」も毎tick走査している→数秒に1回に）＝一番効く・比較的低リスク。
2. **全DOMクエリを目的タイル直接(`getElementById('TileHeight-'+qpid)`)に**＝`querySelectorAll('*')`/48タイル走査(`_MULTI_LOBBY_FOCUS_JS` maxScroll=48・maxMs=6000)を排除。
3. **過負荷時スキップ**＝tickが遅れている時は視覚維持(inject/center)を飛ばし決済を優先。
4. `_ensure_multi_area`の復旧reloadは稀＝優先度低。

## ★判断＝今は触らない
- ここは**今朝flash回帰を起こしたのと同じ敏感な決済経路**。**現コードは動いている**（v4で遅いだけ・賭けは着弾し決済もする）。
- **本番はv3**＝NOWがゆっくり＝この問題はほぼ出ない。重いのはv4高負荷時だけ。
- 「動いているものを主にv4のために決済経路全体のリスクで触る」＝割に合わない。やるなら**別worktree・1箇所ずつ・毎回bafather実測・即ロールバック可**な形で、長丁場でない時に集中して。
- 低リスク先行案＝**env調整**（再センタリング間隔↑/`BACOPY_MULTI_SCROLL_MAX`↓/`_MULTI_LOBBY_FOCUS_JS`のmaxMs↓）で効くか試す（コード無変更・即revert）。

## 計測の再開方法
計測コードは常駐（commit`50049fd`）。`grep '[TICK-SLOW]'` で内訳、`grep 'LOOP-SLOW'` で総ブロック。bafatherをv4で動かせば再収集できる。
