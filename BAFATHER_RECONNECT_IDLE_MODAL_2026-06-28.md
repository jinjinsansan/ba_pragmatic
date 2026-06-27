# bafather 再接続連発＝idleモーダル過剰反応・cooldown緩和で解消 — 2026-06-28

> bafather OS再起動後に「再接続が何回も」発生。別ログイン(セッション競合)はオーナーがログアウト済みで無関係。
> 真因＝Pragmaticのidle/サポートモーダルにエンジンが45秒ごとにロビー再入場で過剰反応していた。

## 症状/誤診
- 症状＝game WS再接続(reconnect=true)4回/90s ＋ SESSION-RECOVER 1回/90s。
- ★誤診注意：SESSION-RECOVERが回る＝一見「セッション競合(別ログイン)」だが、**オーナーはStakeの別ログインを既にログアウト済み**＝競合ではない。

## 真因（ログで特定）
SESSION-RECOVERの直前に必ず：
```
[LIVE] idle/support dialog auto-recovered (frame/tick) url=...pragmaticplaylive.net/...
[SESSION-RECOVER] re-entering lobby to restore session   ← 再接続の正体
```
- **Pragmaticがidle/サポートモーダル(右側パネル x868 y56 w320)を出す→エンジンが検出→ロビー再入場(SESSION-RECOVER)→WS切断→再接続**、をモーダルのたびに繰り返す。
- モーダルはインラインの`auto-recovered (frame/tick)`で既に処理されているのに、**追加でロビー再入場まで escalate**＝過剰反応。
- ＋`session elsewhere detect failed on 5 roots (eval error)`＝session-elsewhere検出evalが失敗→モーダルをきれいに消せず。
- モーダルが出る理由＝早朝の低活動でPragmaticのidleタイムアウト発火と推定。

## 対処（env緩和・低リスク）
- エンジンの `BACOPY_LOBBY_RECOVER_COOLDOWN_SEC`(既定45) を **300** に。ロビー再入場を最大5分に1回へ制限。
- 設定先＝bafather `C:\bacopy\.env` ＋ `C:\BACOPYRECEIVER_user01\resources\.env`。GUI STOP→START(フル起動でないのでクラッシュ回避)で反映。
- 結果＝再起動後90s観測で **reconnect=true 0 / SESSION-RECOVER 0 / game WS 0**（変更前は4+1/90s）＝**再接続が消えた**。卓処理(BETSOPEN 483)は正常＝機能維持。
- 仕組み＝idleモーダルはインライン処理で消えるので、毎回ロビー再入場(=再接続)しなくても賭けは続く。

## 残TODO（根治はコード改修）
- 根本＝`session elsewhere detect`のeval失敗修正＋idle/サポートモーダルを「ロビー再入場せずインラインで静かに消す」よう改修。engine `bacopy_executor_pragmatic_ws_live._SESSION_ELSEWHERE_SUPPRESSOR_JS` / `_dismiss_session_elsewhere_modal` 周り。
- 同症状の受け子があれば同じenv緩和(LOBBY_RECOVER_COOLDOWN_SEC=300)が効く（要.env配布）。

## 関連env（参考・engine `dual_line_live_executor.py`）
- `BACOPY_IDLE_RECOVER_CHECK_SEC`(既定0.5)＝idle検出間隔。
- `BACOPY_LOBBY_RECOVER_COOLDOWN_SEC`(既定45→300に変更)＝ロビー再入場クールダウン。
- バックアップ＝`.env.bak_cooldown_*`。

## 教訓
- SESSION-RECOVERが回る＝即「別ログイン競合」と決めつけない。**直前ログ(idle/support dialog)で真トリガーを確認**。別ログインを切っても続くなら自家中毒(過剰反応)を疑う。
- 関連＝[[project_baf_flash_slow_external_2026-06-27]]（前回はセッション競合が真因だった別ケース）。bafather=Administrator@162.43.83.54/鍵~/.ssh/laplace_vps。
