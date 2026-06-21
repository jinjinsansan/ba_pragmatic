# 受け子エンジン死亡(greenlet→context死)の根治＝CDP自動再接続 — 2026-06-21

> 梶原さん(user05)・ひろゆきさん(user06)報告：「追従が完了せず抜ける」「テレグラム連勝なのにGUIが全く動かない・黄色枠すら出ない」。**GUI/OS再起動しても数分で再発**。
> 調査の結果 = 単純なスレッドバグではなく「負荷でページ接続が死ぬ→エンジンが諦めて停止」。**諦めず:9222へ完全再接続する**修正で根治。

---

## 1. 症状と切り分け
- user05: engineログが**77分更新なし＋`bacopy_engine`プロセス0**＝**エンジン死亡**。GUI/Chrome(:9222)は生存。NOW来ない＝黄色枠も出ない＝賭けない＝追従も完了不能。
- user06: engine生存も `pending bet mismatch` で詰まり0ベット(致命化はまだ)。
- **両名とも同じengine `AA78019B`(6/19版)／bafatherだけ `292A6D85`(safety版)で安定** → 一時「エンジン版差」を疑うも誤り(下記)。

## 2. 真因(誤診の訂正含む)
- クラッシュ瞬間：`greenlet.error: Cannot switch to a different thread` 多発 → `[BOT] bet_page closed unexpectedly` → `[BOT] reinit failed (context dead), stopping` → **エンジン自己停止**。フレッシュ起動から**約9分(elapsed=567s)で再現**＝再起動が効かない理由。
- **★スレッド誤用バグではない**：調査エージェントがエンジンのスレッド設計を精査 → バックグラウンドスレッド(decision/prepos/safety poll・stdin・dga)は**Playwrightを直接触らず**、BET経路は `_owner_thread_id` ガードでtickスレッドに正しく委譲。`greenlet`エラーは**長時間同期Playwright操作(48卓センタリング等)中にイベントループが乱れる基底ノイズ**で、bafatherでも68回出る。
- **致命化(context死)は負荷次第**：user05=small6+追従$900+48卓で重い→eval失敗連鎖→ページclose。bafather=small1で軽い→致命化せず。＝**「エンジン版」でなく「負荷+ページ不安定+復旧で諦める」**が真因。
- 旧復旧ロジック(`dual_line_pragmatic_bot.py` run内 except `_page_err`)：`ctx.new_page()`(既存context再利用)だけ試し、contextごと死ぬと `break`＝**:9222 Chromeは生きているのに再接続を試さず諦める**。

## 3. 修正(commit `c59d6ed`+`a0cc298`)
復旧の except `_reinit_err` 節で、`break`の前に **起動時と同じ `connect_over_cdp(:9222)` で完全再接続**を試行：再connect→context取得→stake/lobbyページ再選択→`page.on("websocket")`再bind→`executor.setup()`再実行→`continue`。chrome_attach限定(no-reload/camoufoxは無変更)。
- 効果：**context死してもエンジンが自己復帰**＝「死んで放置→ユーザー再起動でも数分で再死」を解消。ログ `[BOT] full CDP reconnect OK, resuming loop`。
- **起動バナー**(`a0cc298`)：`[BOT] ★CONTEXT-DEATH AUTO-RECOVERY: ENABLED (...2026-06-21 fix)` を起動直後に出力 → context死を待たずに「修正版engineが動いている」と一目で確認可。

## 4. デプロイ
- ✅ **bafather(user01) パイロット合格**(2026-06-21 15:30)：engine MD5 `D95684B0C2B206012832B9258B877B35`(68,726,238)swap済・**起動バナー実出力を確認**・安全モード配線も健在。backup `bacopy_engine.exe.bak_20260621_152822`(旧292A6D85)。
- ✅ **user02-10 NSISインストーラ 全9本リビルド+検証済**(runbook §2-4→§3-A `_build_user0210.ps1`)：同梱engine=D95684B0(reconnect+safety+Kelly+small2+winrate)・bc=user06のみ10/11(残留バグ修正済`27f32ce`・user10にbc無しで確認)・asar=現src(安全モードUI/勝率チャート)。`dist/BACOPYRECEIVER_userNN_Setup.exe`×9。
- 🔲 配布=オーナーが各ユーザーへSetup.exe送付→再インストール(取り違え厳禁)。**特に user05/06 は再インストールで死亡ループが止まる**。

## 5. ビルド手順(runbookどおり)
①変更`_rev53144/dual_line_pragmatic_bot.py`をbafather`C:\bacopy`へSCP→②bafatherで`_build_engine_handend.ps1`(PyInstaller)→`bacopy_engine.exe.new`→③`_baf_engine_swap.ps1`でbafatherにswap→④`bacopy_engine.exe.new`を`copytrade_gui/build_staging/engine/`へpull(§2-4)→⑤`_build_user0210.ps1`(provision+electron-builder×9)。SSH越し`powershell -Command`は`$`壊れる→`.ps1 -File`。

## 6. 関連
- 安全モード: `SAFETY_MODE_FEATURE_2026-06-20.md` / メモリ `[[project_safety_mode_2026-06-20]]`
- Chrome膨張×センタリング: `FOLLOW_FLASH_DELAY_CHROME_BLOAT_2026-06-19.md` / `[[project_follow_flash_delay_chrome_bloat_2026-06-19]]`
- 配布手順: `DISTRIBUTION_BUILD_RUNBOOK.md`
- commit: `c59d6ed`(reconnect) `a0cc298`(banner) `27f32ce`(provision bc fix)
