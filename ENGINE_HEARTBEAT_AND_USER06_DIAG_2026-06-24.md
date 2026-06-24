# エンジン心拍バッジ + user06「高額BET拒否」調査  2026-06-24

## 経緯
受け子4人（梶原05/ひろゆき06/きょうへい02/小川07）を6パターン追従で稼働中、
**ひろゆき(06)だけ**「$500超のBETになり、NOWが弾かれる/追従中に弾かれる/ロビーに戻される」と報告。
真因をログ実測で調査し、対策として「エンジン死亡の可視化(心拍バッジ)」を実装した。

---

## 第1部: user06「高額BET拒否」の真相（実データ調査）

### 受け子比較（SSH: `ssh -i support_keys/admin_key -o "ProxyCommand=ssh -i ~/.ssh/laplace_vps -W %h:%p laplace@210.131.215.116" -p <port> Administrator@localhost`。鍵admin_keyは権限を締めてから使う(icacls)。port: 02=2277/05=2241/06=2268/07=2222）

| | ひろゆき06(高額) | 梶原05(小額) | 小川07(小額) |
|---|---|---|---|
| mode | small6 | small3 | small02 |
| seq_level | 24 | 2 | 5 |
| next_bet | **$1,200** | $7 | $20 |
| session_pnl | **−$7,228** | +$85 | +$15 |
| エンジン | **死亡(07:28〜)** | 生存 | 生存 |
| RETURN-LOBBY回数 | 5 | 6 | - |
| 取りこぼし系 | 549 | 555 | - |

### 結論: 「高額BET拒否」は**実データで否定**
1. **$1,200のBETはサーバ受理・決済済み**。ログに `[WS-BET-SEND] <lpbet><bet amt="1200" bc="11"/>` → `[WIN-CONFIRM] (server-accepted) nwb=1200.0` → `result=WIN/LOSE` → 残高増減。**WS賭け(チップクリックではない)**で、金額は1メッセージ。カジノは弾いていない。残高も$16,205で十分。
2. **ロビー復帰・取りこぼしは小額勢と同頻度**（5 vs 6, 549 vs 555）＝高額固有でない・全GUI共通の通常挙動。
3. **追従も置けて負けて終了**（`[FOLLOW] place $1200 → END reason=lose`）＝拒否でなく敗北。
4. **ひろゆき固有の唯一の違い＝エンジンが07:28に死亡し復活せず**（ENG_PROC=0が4時間＋）。死亡後はNOWが来ても賭けない→「弾かれる/ロビーに戻る」ように見える。死亡直前の最後の動作が偶然 `[RETURN-LOBBY]` だったのでChromeはロビー画面で固まった。
5. **時系列が決定打**: エンジン死亡=07:28、ひろゆき本人からの連絡=11時台（=死亡から3.5h後）。彼が見ていたのは「3.5時間前に死んだエンジン＋ロビーで固まったChrome」。

### 死因（推測・ログに死因記録なし）
唐突停止(Pythonトレースバック無し)＝ハードクラッシュ(segfault)の特徴。greenlet 119回（未キャップの重いセンタリング中の同期Playwright）。**user06の.envにセンタリングキャップが無い**(LOOP-SLOW 17件全部6-7秒=諦めていない)＝[[project_centering_env_cap_2026-06-23]]の修正が**受け子に配布されていなかった**。ハードクラッシュなのでエンジン内蔵のCONTEXT-DEATH自己復活(D95684B0)は無力(プロセスごと消える)。GUIの自動再起動も走った形跡なし=旧ビルドは `BACOPY_DUAL_LINE_AUTO_RESTART` 既定OFFだった可能性(新srcは既定ON)。

### 教訓
- **体感 vs 実データ → 記録された事象は実データが答え**。「高額だから」の因果づけはデータで崩れた。
- 教訓: 「高額BETが拒否される」と聞いても、WSフレーム(`amt`/`bc`)とサーバ応答(`stake_delta`/`nwb`/`server-accepted`)を実測すれば真偽が出る。
- **WS賭けなのでチップ枚数は無関係**(私が一度チップクリック説で誤った)。

---

## 第2部: 対策 = エンジン心拍(ハートビート)バッジ ★今回の実装

### 問題
人間は**賭けChrome**(黄色枠/NOW)を見るが、それは**エンジンが死んでも最後の画面のまま残る**ので、死んでいることに気づけない(user06は4時間放置)。**監視している場所(Chrome)にエンジンの生死が見えない**。

### 実装（`_rev53144/dual_line_live_executor.py`）
- `_ENGINE_HEARTBEAT_JS`: 賭けChrome上部中央に常駐バッジを**add_init_script**で全フレーム注入(リロード後も)。**ページ内setIntervalで自走**し `window.__bacopyEngineHb`(最終ping時刻)からstalenessを計算:
  - 新鮮(<25s) = 小さな緑「● ENGINE LIVE」
  - **ping停止(=エンジン死亡)で赤点滅「⚠ ENGINE STOPPED 〇秒前から — 賭けていません/再起動してください」**。★肝=判定はページ内タイマーなので**エンジンが死んでも自走して死を表示し続ける**。
- `tick()` 冒頭で5秒間隔の軽量ping(`_ping_engine_heartbeat`)。**全てtry/except・決済/賭け/センタリング経路に非接触**。
- env無効化: `BACOPY_ENGINE_HEARTBEAT=0`(既定ON・再ビルド不要)。

### 検証(bafather)
`[EXEC-SETUP] engine-heartbeat badge pre-installed` ログ・ping err 0・賭けChromeに緑バッジ表示(起動直後はiframeロード待ちで数十秒遅れて出る)・STOP→約25秒で赤「ENGINE STOPPED」に変化=死亡可視化を実証。

### 残課題(別件)
- なぜ07:28の死亡をGUIが検知・自動再起動しなかったか(検知漏れ or 旧ビルドの既定OFF)=放置の根本。新ビルドは既定ON＋心拍で可視化だが、ハードクラッシュ+staレロックで再起動が詰まる経路は別途要対策(今朝bafatherで実例)。
- 副案(未実装): Telegram死亡通知(見ていない時用・GUI or 独立watchdogに置く)。

---

## デプロイ
- bafather: engine `F5575BC7`(STABLE+reverse+heartbeat・68733644)swap済・実証済。
- 受け子02-10: 新ビルド(reverse+heartbeat+centering cap)で再ビルド→配布。build_staging engine=F5575BC7・`build_staging/.env`にキャップ4行・GUI src=逆張りトグル/バナー。
- リバート: tag `pre-heartbeat-2026-06-24` / `pre-reverse-2026-06-24`・bafather各.bak・env kill-switch各種。
- 関連: [[project_reverse_bet_deployed_2026-06-24]] [[project_centering_env_cap_2026-06-23]] [[project_engine_death_reconnect_2026-06-21]] [[project_fleet_drawdown_2026-06-12]]
