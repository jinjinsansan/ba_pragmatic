# セッションハンドオフ 2026-07-13(対象: 07-11夜〜07-13)

前セッション=`SESSION_HANDOFF_2026-07-11.md`(06幻BET復旧・DGA=off統一)。
branch: `feat/engine-heartbeat`。本セッションのcommit: `7ba9782`→`eaeb148`→`3b14549`→`c36761c`→`1d7cffc`。**未コミットなし**。

---

## ★★ 最優先: 未払いロックダウン(7/14実行待ち・準備完了)

**背景**: 全員無料アカウント運用中だが未払い発生。支払い済みは山口さん(`milliongold999@gmail.com`・GUIおそらく09)のみ。山口さん以外のGUIを使用不可にし、テレグラムドライランも山口さんのミラーchのみ残す。未払い解消で元に戻す。

**仕組み(コード確認済み・新規開発なし)**: GUI `main.js billingStatus()` が**60秒毎**にSupabase billingをチェック。`bot_paid=false` は **is_freeより先に判定=無料アカウントにも効く**。稼働中でも `stopBotFlow(forced)` で自動安全停止+「License not active. Please complete your purchase.」表示+START無効化。ネットワーク断では止まらない(誤停止なし)。

**実行手順(ユーザーが「ロックダウン実行して」と言ったら)**:
1. `cd E:\dev\Cusor\bacopy && python _billing_lockdown.py`
   - KEEP 3アカウント以外の9件を `bot_paid=false` に一括PATCH
   - 実行前に全行を `_billing_lockdown_backup_<stamp>.json` へ自動保存
2. VPSで `bash /opt/laplace2/_tg_lockdown.sh lock`
   - DUAL_LINE_CHAT_ID / TELEGRAM_CHAT_ID → ミラーch `-1004326175826` に昇格・RESULT/EXTRA空化・.envはbak付き・bot自動再起動(20秒)
   - ★`_send_telegram` はメインchat_id空だと**ミラーも送らない実装**なので「ミラーをメインに昇格」が正解(空化はNG)
3. 60秒後、SSHで各稼働PCのプロセス停止を検証(06=2268が主対象。07/10は停止中でもSTART不可になる)

**KEEP(絶対に止めない)**: `milliongold999@gmail.com`(山口=支払済) / `lselfloveself@gmail.com`(**bafather自機のGUIログイン**=止めると自機停止・ユーザー確認済) / `goldbenchan@gmail.com`(管理・元々paid=false)

**復旧**: `python _billing_lockdown.py --restore <backup json>` + `bash /opt/laplace2/_tg_lockdown.sh unlock`

**★既知の穴**: lselfloveselfは旧03つたいし用アカウント。03のPCが同アカウントなら03は動けてしまう(07-13時点で03トンネル落ち=GUI非稼働=当面無害)。恒久対処=bafatherを別アカウントに再ログイン後、lselfloveselfも止める。

**テレグラム構成**: パターンch=`-1003858043245` / 結果ch=`-1003707017974` / ミラー(山口さんの監視ch)=`-1004326175826`

---

## ★ カードデータ大発見+収集稼働中(07-12〜)

**5/10の「WSではカード取れない→Vision API必要」(`AI_VISION_CARD_CAPTURE.md`)は不完全な結論だった**。当時見たのはgs18のgame WS。**multibaccarat iframe(client.pragmaticplaylive.net)受信のWSに全卓分が流れている**:
- `{"card":{sc,place,value,cardCount,table,game}}` = 配られた1枚ごと(45秒で770枚)
- `{"gameresult":{result,score,natural,player_pair,banker_pair,super6,...}}` = ハンド確定ごと(**約16万ハンド/日**)

**稼働中コレクタ**: bafather `C:\bacopy\_cdp_card_collector.py`(Start-Process常駐・読み取り専用CDP:9222・Chrome再起動に自動再接続・800MBガード・実測76MB/日)→ `C:\bacopy\card_feed.jsonl`。**賭けChromeを閉じると収集停止**(戻れば自動再開)。

**目的=ユーザー仮説の検証**: 「波が来ている時はナチュラル9/8が多い・3枚目逆転9も多い→それで波の方向を予想している」
- テストA(記述): 勝ち区間vs他区間のナチュラル率(注意=勝ち手にナチュラルが多いのは定義上当然)
- **テストB(本命・予測)**: 直近Nハンドのナチュラル率が高い時、次ハンドの勝率が変わるか。VPS decision(dl_v4/dl_vps)とtable+gameIdでjoin可
- 3枚目逆転=cardフレーム(cardCount順)から2枚時点合計vs最終scoreで再構成
- ユーザーが「カードの分析して」と言ったら1〜2日分で第1回分析

## ★ 「波」仮説の完全検証(07-11夜〜07-12・全て無エッジ確定)

テレグラムドライラン39日(6/3〜7/11・26,094決済→ハンドdedup後19,294)で検証:
- ★**罠(再発注意)**: v3(dl_vps)とv4(dl_v4)は同じ卓・同ハンドに二重シグナル(3秒内同結果6,851ペア)。**合算系列は偽の波(勝後勝率63%)を作る**が系統内では完全ゼロ(z=±0.3)。時系列検証は必ず系統別+ハンドdedup
- 日内前半→後半 corr+0.25(t=1.6有意でない)/隣接2hブロック corr-0.04/前日→翌日 50.1%
- スイッチ戦略(直近K多数決/日内リーダー)最良50.6%・シャッフルp=0.11〜0.13=偶然・手数料分岐50.8%未満
- 連敗分布=幾何分布と完全一致・最長12連敗=理論期待12.0ぴったり・過分散なし(分散比1.02)=**波は構造として存在しない**
- 最長12連敗=6/7(日)22:58-23:25。ワースト10の時間帯は完全にバラバラ
- 詳細メモリ: `wave-switch-no-edge-2026-07-11`

## ★ AUTO逆張りモード実装+実弾検証+実運用は非推奨で終了

- GUIのみ(asar)・逆張りセレクトに `AUTO` 追加: **前1時間のTIME RATE**(選択系統・master /api/hourly-stats)が50%以上→順張り/50%未満→逆張り。60秒毎に冪等送信=**エンジン再起動しても60秒で自動再適用**。commit `eaeb148`
- 実弾検証: 01:00:38に`[REVERSE] ENABLED`(境界から38秒)=完璧動作
- **実運用結果(07-12・80BET)**: AUTO 43.8% vs 固定順張り50.0% vs 固定逆張り50.0%=切替コストだけ払った(バックテスト予告通り)。ユーザーも納得済み・**OFF推奨で終了**
- ★診断の教訓: 「bafatherだけ負けを拾う」の正体=**06は逆張りON表示でも実は順張りだった**(decision ID突合で94件中93件が逆側・06ログにREVERSE行ゼロ)。GUIトグル表示はエンジン実状態の証明にならない。メモリ: `user06-reverse-not-applied-2026-07-11`

## ★ ダランベール×SET/123×SETのSEQ風GUI表示(bafatherデプロイ済)

- **engine**(`_rev53144/dual_line_money.py`): 完了セット〇×履歴 `dalembertset_sets`/`b123set_sets` を新設(500件cap)→state永続化→status_dict同梱。commit `3b14549`
- **GUI**: 両モードのパネルをSEQ同等に=全セット色分けストリーム(renderDevSets流用)+セル{LEVEL|STEP, NEXT$, 負け越し, HAND}
- **負け越しの確定仕様(ユーザー指定・二転した末)**: SEQのovershootと同一=**セット確定ごとに(×−〇)ハンド数を足し引き・勝ちセットで減・下限0**。level-1(セット単位)は却下。式は小川07の実SEQ66セットで全一致検証済。commit `1d7cffc`
- **bafatherデプロイ(07-12)**: engine `BB3FCEE8`(68,745,423・bak_20260712_114726)+asar 329,996(bak_20260712_131110)。**`build_staging/engine/bacopy_engine.exe`更新済=次回配布に自動同梱**(セット履歴+拾NOWモード非依存+reset_caught全部入り)
- 履歴は差替後の完了セットから蓄積(過去分なし)。エンジンビルド手順: `_rev53144`の4pyをC:\bacopy\へscp→`_build_engine_handend.ps1`→swap=`_baf_engine_swap.ps1`(★ライブC:\BACOPYRECEIVER_user01対応版。`_swap_engine_yellowfix.ps1`と`_swap_asar_generic.ps1`は死んだAppDataパスを見るので使用禁止)

## 資金管理の議論(実装なし・方針確認)

- 「波を掴む資金管理」は不可能(検証済)。残る実利= **v3・NOW単独(追従OFF)50.90%>分岐50.66%** + **7ハンドダランベール**(同勝率で破滅率1.4% vs SEQ18.7%)+ **loss_cut=ユニット×300**
- ダランベール7ハンドの「利益が遅い」への正答=**ユニットを上げる**(最大BET$34 vs SEQ$167の安全余地を使う)。速く見える方式は速く飛ぶ方式
- 実測: 小川07 loss_cut=0だった(危険・停止中)。bafatherは300設定済

---

## 現在の状態(07-13時点)

| 対象 | 状態 |
|---|---|
| bafather | 新engine+新GUI稼働・dalembertset(level7前後)・loss_cut$300・カード収集常駐 |
| 06ひろゆき | 稼働中(順張り・逆張りは効いていない状態のまま)→**明日ロック対象** |
| 07小川/10さら | 梶原さん判断で停止中→ロック対象(START不可化) |
| 03つたいし | GUI非稼働(トンネル落ち) |
| 受け子02-09配布 | 未(build_staging engineは最新化済・インストーラ再ビルドすれば全部入り) |

## 残タスク
1. ⬜ **7/14 ロックダウン実行**(上記手順・ユーザーの合図待ち)
2. ⬜ カード分析 第1回(1〜2日分貯まったら・「カードの分析して」で)
3. ⬜ 06のSEQリセット/逆張り意図確認(実は順張りで走っている)
4. ⬜ 受け子配布(ロックダウン解除後に判断)
5. ⬜ admin/users逆張り列(次回engineビルド時・**エンジン実状態を出すこと**=GUIトグルは嘘をつく)

## アクセス系(前回から変更なし+追加)
- SSH: VPSジャンプ `ssh -i support_keys/admin_key -o "ProxyCommand=ssh -i ~/.ssh/laplace_vps -W %h:%p laplace@210.131.215.116" -p <port> Administrator@localhost`(06=2268/07=2222/10=2290/03=2255)。bafather=直 `Administrator@162.43.83.54`(laplace_vps鍵)
- Supabase: 鍵=`web/.env.local`(SERVICE_ROLE)。billing一覧=`_billing_list.py`(bafather C:\bacopy\)
- bafather GUIログインsession: `C:\Users\Administrator\AppData\Roaming\bacopy-copytrade-gui\bafather_supabase_session.json`
- ★SSH越し複雑PS($_や$var入り)はbashに食われる→ASCIIの.ps1/.pyをscp→`-File`/python実行
