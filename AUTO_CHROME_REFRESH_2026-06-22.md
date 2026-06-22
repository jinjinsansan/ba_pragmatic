# NOW取りこぼし対策：賭けChromeの定期自動リフレッシュ — 2026-06-22

> 梶原さん「NOWの取りこぼしを改善できないか」→ 実測で**取りこぼしの主因は“同時重なりドロップ(稀)”でなく
> “卓フォーカス/センタリングの失敗(915回/1.75h)”＝Chrome膨張**と判明。膨張Chromeを定期的に新鮮化して対処。

---

## 1. 診断（実測・bafather）
- 「同時重なりで落とす(`drop overlapping`)」＝**1.75hで16件＝稀**。6:06ケースは珍しい。
- 桁違いに多いのは **`PREPOS focus miss / retry`＝915件**＝エンジンが卓フォーカスに失敗して再試行＝**Chrome膨張(2,033MB/2.9h)でセンタリングが遅い**（既知 `FOLLOW_FLASH_DELAY_CHROME_BLOAT`）。
- ＝取りこぼしの本体は**Chrome膨張**。

## 2. 既存の穴
- 既存の定期再起動(`schedulePeriodicRestart`)は **6時間・エンジンのみ**再起動。**賭けChrome(:9222)は再アタッチ方式で生き残る**ため、**膨張がクリアされない**。

## 3. 実装（GUI/main.jsのみ・エンジン無変更）
- 定期再起動を **6h→2.5h**(env `BACOPY_PERIODIC_RESTART_HOURS` 既定2.5)に短縮し、firing時に **`killCdpChrome(port)` で賭けChromeを全プロセスkill**→`_doStartBot`が`ensureCdpChrome`で**新鮮なChromeを起動し直す**→エンジン再接続→賭け再開。`chrome_attach`モードのみ・個人Chromeは無傷。
- ＝**2.5hごとに賭けChromeを自動新鮮化**（手動STOP→STARTの自動化）。膨張前にDOM/状態をクリーンにしてセンタリング高速維持。

## 4. ★テストで捕まえたバグ（bafather先行検証の価値）
- 初版`killCdpChrome`は **PowerShellの入れ子ダブルクォート**バグ：`-Filter "Name='chrome.exe'"`の`"`が`powershell -Command "..."`の外側`"`を壊し、クエリ空振り→**`pids=`空＝1個もkillできず**（リフレッシュ無効）。**このまま配布したら全受け子で無効だった**。
- 修正：**シングルクォートのみ**で記述＋**専用profile名(`cdp_chrome_profile`)でも一致**（メイン4＋子プロセス含む全10個を確実にkill）。実測クエリ＝10プロセス一致(profile=10/port=4)。
- 再検証：`remaining port-procs=0`＝全kill成功・Chrome全プロセスuptime<2.5min＝新鮮化・WS再接続`BETSOPEN/PREPOS`feed流れて賭け再開、を確認。

## 5. 正直な効果
- RAMは**新鮮1,739MB vs 膨張2,033MB＝差~300MBと小さい**。効果は「RAM削減」より「**DOM/状態のクリーン化でセンタリング高速維持**」。取りこぼし削減は時間をかけて確認。
- 自動化自体が目的（梶原さん/オーナーが手動再起動から解放）は達成。
- コスト：2.5hごとに~1-2分の賭け空白(Chrome再読込+48卓再センタリング)。v3は遅いので影響軽微。

## 6. デプロイ
- **bafather**：asar `299549`(修正版killCdpChrome+2.5h) swap済・6分テストで実証→2.5hに戻し済(要GUI最終再起動)。engine `26BDA93B` 無変更。
- **受け子02-10**：NSIS全9本リビルド(`_build_user0210.ps1` #4・現src=修正版+2.5h既定)。配布＝オーナー手動転送。
- commit予定・push。

## 7. 教訓
- **取りこぼし=重なりドロップと早合点せず実測**→主因はChrome膨張/フォーカス失敗だった。
- **PowerShellをexecSync経由で渡す時は入れ子ダブルクォート厳禁**(シングルクォートのみ)。
- **新機構は必ずbafatherで実証してから配布**（このバグはテストでしか捕まらなかった）。

関連=`FOLLOW_FLASH_DELAY_CHROME_BLOAT_2026-06-19.md`・`[[project_follow_flash_delay_chrome_bloat_2026-06-19]]`・`[[project_cdp_watchdog_2026-06-13]]`・`DISTRIBUTION_BUILD_RUNBOOK.md`
