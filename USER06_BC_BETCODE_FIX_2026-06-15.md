# user06「カスタマーサポート」BET拒否 — 真因=bc(賭け先コード)違い・解決記録 (2026-06-15)

> **次セッション/配布前に必読。** user06 で初日から続いた「自動WS BETを置くと『カスタマーサポートへ連絡』モーダルが出て賭けが成立しない」問題の**真因確定と解決**の記録。長時間 uId という誤った筋を追ったので、その経緯と教訓も残す。

---

## 0. 一行結論
**真因 = lpbet の `bc`(賭け先コード) が個体で違う。** 06 に配信された Pragmatic クライアントは **Banker=`10` / Player=`11`** を使うが、エンジンは 01-05 から採取した **Banker=`1` / Player=`0`** をハードコードしていた。06 では不正符号 → サーバが拒否(「カスタマーサポート」モーダル)。**`bc` を env 化(`BACOPY_BC_BANKER`/`BACOPY_BC_PLAYER`)し、06 を 10/11 に設定 → 実機で受理(GAME-BET-CONFIRM + stake_delta)確認。両側(Banker/Player)受理実証。**

---

## 1. 症状
- 06 で BET MODE=オート(WS自動BET)。NOW → 自動 lpbet 送信 → **「カスタマーサポートへ連絡してください」モーダル** → engine が自動 dismiss → `[PHANTOM-GUARD] not accepted → dropped`。残高動かず、SEQ 進まず。
- **手動 BET は通る**(Stake 履歴に残る)。01-05 は同じ自動WS BETで完璧に動く。
- **インストール初日から**発生。OS再インストール/クラウドPC再作成でも不変。

## 2. 真因(実測で確定)
- `bc` は **Stake が配信する Pragmatic ウェブクライアント**が、チップを置いた時に送る値。**クライアントのビルドが個体で違うと bc 符号も違う**(01-05=1/0、06=10/11)。「同じソースコードなのに 06 だけ失敗」の正体はこれ。
- **決定的比較**: 健全機 bafather(user01, 162.43.83.54) は `bc=1`(Banker) を同じ卓 `m88hicogrzeod202` に送って `[GAME-BET-CONFIRM]` で受理。06 は**同じ卓・同じ bc=1** で拒否。→ bc 符号はセッション(クライアント)依存。
- **06 の手動 BET 実測**: ユーザーが「バンカー」を置く → クライアントが `<lpbet ... bc="10">` 送出 → 受理。Player は `bc="11"`。
- **修正後の受理実証(2026-06-15 08:15)**:
  ```
  <lpbet gm="mtb_desktop" gId="14494681021" uId="ppc1735214860365" bc="10" ...>  ← bot が bc=10 送信
  [GAME-BET-CONFIRM] table=cbcf6qas8fscb222 bc=true amount=1                    ← サーバ受理
  [CONFIRM-PROBE] stake_delta_keys=['USDT'] ... game_confirm_age=7.0s           ← 残高変化=本物の受理
  ```
  カスタマーサポート無し・PHANTOM-GUARD無し。後に Player(bc=11) も受理確認。

## 3. ★uId は完全に無罪(長時間の遠回り。繰り返さないこと)
- 受理された bot BET の `uId=ppc1735214860365` は**スクレイプした他人の uId**(06本人でも学習値でもない)。それでも受理。**サーバは uId 所有を検証していない**。bafather も毎 BET 違う ppc uId を送って受理される。
- 経緯: 共有マルチエリア WS(cbcf6qas8fscb222) のフィードに全プレイヤーの uId が流れ、06 は自分の uId がフィードに出にくいので bot が「他人 uId をスクレイプ送信」していた。これは事実だが**拒否の原因ではなかった**(他人 uId でも受理されるから)。
- 試した(が無駄だった)対策: `BACOPY_USER_ID` env ピン / 手動 lpbet から自分 uId 自動学習 / UUID形式 env 無視。**いずれも無害だが本質でない**。さらに GUI(`main.js buildSpawnSpec`)が `BACOPY_USER_ID` を `config.user_id`(=Supabase アカウント UUID)で強制上書きする落とし穴もあり、env ピンが効かない原因にもなった(が、結局 uId 自体が無関係だった)。

## 4. 副次的に解決した起動ループ(third-party)
- 06 は起動時に「Failed to start third party session / ゲームがありません」ループも持っていた(bc とは別問題)。
- 真因: GUI(`ensureCdpChrome` `main.js:567-595`)が :9222 Chrome を**いきなり深いゲームURL**(`.../pragmatic-play-live-lobby-baccarat`)で冷間起動 → 06 のホストは冷間ディープリンクで Pragmatic 第三者セッションを張れない。
- 対策(.env のみ・リビルド不要):
  - `BACOPY_LOBBY_URL=https://stake.com/ja/casino/home` … :9222 Chrome を home で起動(温めてから遷移)。
  - `BACOPY_MULTI_DEADLOCK_RELOAD_ENABLE=0` … engine がマルチエリアを「tab見つからない」と誤判定して**ページ強制リロードで蹴り出す**のを停止。
- これで起動が 01-05 同様に安定(ロビー→マルチエリア維持)。

## 5. 入った修正(コード)
- `_rev53144/dual_line_live_executor.py` `_build_lpbet_xml()`: `bc` を **`BACOPY_BC_BANKER`(既定`1`) / `BACOPY_BC_PLAYER`(既定`0`)** で env 上書き可に。既定維持で 01-05 無影響。
- (同 engine に同梱の副次変更=UUID形式 `BACOPY_USER_ID` 無視 / 手動lpbetからの own-uId 学習。**無害だが本質ではない**。将来整理可。)
- engine MD5 `fd694c3666f03b3bd298ff461878770e`(06 swap 済 / backup `bacopy_engine.exe.bak_uidfix`)。

## 6. 06 の最終 .env(効いている設定)
```
BACOPY_LOBBY_URL=https://stake.com/ja/casino/home
BACOPY_MULTI_DEADLOCK_RELOAD_ENABLE=0
BACOPY_BC_BANKER=10
BACOPY_BC_PLAYER=11
BACOPY_USER_ID=ppc1735316095437   ← 無害(無視される)。残置可
```

## 7. ★配布(07-10)への手順と教訓
- **bc 符号は個体(配信クライアント)依存**。新受け子で「カスタマーサポート」拒否が出たら:
  1. オートを止め、**手動でバンカーを1回、プレイヤーを1回**置く。
  2. engine ログ `engine_cli_capture.log` の `[WS-SENT-RAW] <lpbet ... bc="N">` で各々の `bc` を実測。
  3. `.env` に `BACOPY_BC_BANKER=<Bの値>` / `BACOPY_BC_PLAYER=<Pの値>` を設定 → STOP/START。
- 起動 third-party ループが出る個体は §4 の 2 つの .env も設定。
- **★最大の教訓**: 「同じソースコードなのに動かない」時は、**自分のコードでなく“相手(Stake が配信するクライアント)が実際に送る生フレーム”を手動 BET で採取し、ボット送信フレームと全フィールドでバイト比較**する。今回 uId に引っ張られて長時間迷走したが、決め手は**手動受理フレーム vs ボット拒否フレームの差分=唯一違うのは `bc` だけ**だった。**動いている個体(bafather)と直接比較**するのが最短だった。

## 8. SSH / 運用
- SSH(06): `ssh -i /tmp/admin_key_06 -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o "ProxyCommand=ssh -i ~/.ssh/laplace_vps -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -W %h:%p laplace@210.131.215.116" -p 2268 Administrator@localhost`(鍵=`support_keys/admin_key` を chmod600)。
- bafather 直: `ssh -i ~/.ssh/laplace_vps -o IdentitiesOnly=yes Administrator@162.43.83.54`。
- ログ追走: `engine_cli_capture.log` を PowerShell `-EncodedCommand`(UTF-16LE base64) で読む(SSH越しの `|` は cmd に食われる)。
- engine swap: 06 デスクトップ `SWAP_ENGINE_UIDFIX.bat`(GUI STOP 後に実行)。GUI を閉じると autossh トンネル(2268)が落ちるので、停止系操作は RDP。

## 9. 関連コミット
- `59d7fe3` bc env 化(本丸)。`2c5ac50`/`14a6a5f` uId 関連(副次・無害)。
