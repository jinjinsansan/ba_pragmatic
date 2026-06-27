# bafather 不安定（再接続ループ・GUIクラッシュ）＝asar不一致が真因・07のasarに統一で解消 — 2026-06-28

> bafatherだけが急に：GUI起動クラッシュ／再接続ループ／idleモーダル→ロビー／フリーズ。受け子(user02-10)は無事。
> 真因＝bafatherのasar(GUIビルド)が実験版で受け子と不一致。engineは同じ。07の安定asarに揃えて解消。

## 症状
- bafatherのみ：①GUIダブルクリックでクラッシュ(一瞬出て閉じる・2回目で起動) ②赤枠→再接続→BETしない→画面固まる→再接続→「他の場所」モーダル→ロビー、のループ。
- ★オーナーの決定的観察：「受け子user02-10はこんなこと一切ない。ここのところ急にbafatherだけ」。

## 切り分け（誤診→修正の過程）
1. セッション競合(別ログイン)→オーナーがStakeログアウト済=無関係。
2. idleモーダルへのエンジン過剰反応→`BACOPY_LOBBY_RECOVER_COOLDOWN_SEC`45→300で軽減したが消えず。
3. heartbeat疑い→`BACOPY_ENGINE_HEARTBEAT=0`にしても消えず=heartbeat主因でない。
4. ★engineビルドrevert計画→**07小川のengineを確認したら 68733831 でbafatherと"同じ"なのに07は無事**=engineは原因でない(計画破棄)。
5. ★**差はasar**：bafather 318416(実験版GUI) vs 07 313668(安定GUI)。engineは同一。

## 真因・対処
- **bafatherの古い/実験版asar(GUIビルド)が、エンジン起動・Chrome接続まわりを不安定にしていた**(再接続・モーダル・クラッシュはGUI起因だった。engineは07と同一)。
- 対処＝**07小川(port2222)のasar(313668)をダウンロード→bafatherのGUI/engine停止→現asarバックアップ→07のasarに差し替え→再起動**。
- 結果(差し替え後120s観測)＝**reconnect=true 0 / SESSION-RECOVER 0 / idle/support 0 / GUIクリーン起動(GUI=4) / BET-CONFIRMED 1**＝**再接続ループ・GUIクラッシュ完全解消**。bafatherはengine(68733831)+asar(313668)とも07と一致=同じく安定。

## 残る別問題：センタリング遅延(tickストール)
- tickストール10回/120s(worst 6.4s)は残存。=以前から既知の「センタリング/光り遅延(負荷・実用域)」([[project_baf_flash_slow_external_2026-06-27]])で、今回の再接続とは別物。env/Chrome/卓数で直らないと判明済。致命的でない(賭けは飛ぶ)。根治は構造改修C(preposition待たずWS直送)だが決済リスクで保留。

## 手順メモ(再現用)
- 07 asar取得: `scp -i support_keys/admin_key -o ProxyCommand="ssh -i ~/.ssh/laplace_vps -W %h:%p laplace@VPS" -P 2222 Administrator@localhost:'.../bacopy-copytrade-gui/resources/app.asar' /tmp/07_app.asar`(07の実体は標準パス`AppData\Local\Programs\bacopy-copytrade-gui`)。
- bafather差し替え: GUI/engine `Stop-Process`→asarバックアップ→`scp /tmp/07_app.asar Administrator@162.43.83.54:'C:/BACOPYRECEIVER_user01/resources/app.asar'`→GUI再起動。
- バックアップ＝`app.asar.bak_pre07sync_*`(旧318416)。
- bafather現env：`BACOPY_ENGINE_HEARTBEAT=0`/`BACOPY_LOBBY_RECOVER_COOLDOWN_SEC=300`/`MULTI_FOCUS_MS=1500`/`SCROLL_MAX=8`(これらは残してOK・動作正常)。

## 教訓
- 「once worked now broken・bafatherだけ」→ engineだけでなく**asar(GUIビルド)も実験版か疑う**。受け子(安定)と**engine+asar両方を一致**させるのが正解。
- ビルドrevert前に「受け子の実際のビルド(engine size/asar size)」を確認=思い込み(bafatherだけ実験engine)を実測で訂正できた(07も同じengineだった)。
- bafather=Administrator@162.43.83.54/鍵~/.ssh/laplace_vps。受け子=support_keys/admin_key+ProxyCommand(07=2222/05=2241/06=2268/02=2277)。
