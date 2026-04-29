# ユーザー02 オンボーディング記録

日付: 2026-04-29
配布対象: shojihashimoto0922@gmail.com (橋本さん) / executor_id=`hashimoto` (label `hashimotoPC`)
最終結果: **ARMED 完了 (BACCARAT_9 着席, bettable=true)**

---

## 1. user02 Desktop Cloud 情報

| 項目 | 値 |
|------|---|
| OS | Windows Server 2025 Datacenter |
| 公開 IP | `162.43.81.110` |
| クラウドPC名 | `win-2026-04-29-02-15-04` |
| Windows ホスト名 | `win-2026-04-290` |
| ユーザー | `Administrator` (`win-2026-04-290\administrator`) |
| プラン | スタンダード (CPU 4コア / 8GB RAM / 300GB NVMe) |
| 契約 | 試用 |
| Bafather ログイン | `shojihashimoto0922@gmail.com` |
| Stake username | (Settings 未入力 — それでも今回は着席まで進行) |
| 割当 Port (VPS reverse tunnel) | `2277` |

### 公開 SSH (port 22)
**接続不可**: Xserver Desktop Cloud は標準で OpenSSH inbound 22 が閉じている。
管理者向け遠隔接続は**全て VPS reverse tunnel 経由** (詳細は §4)。

---

## 2. 当日のタイムライン

| 時刻 | 出来事 |
|------|------|
| (前) | user02 EXE を user02 Desktop Cloud にインストール → SIGN IN で `Failed to load Supabase public config` |
| 調査 | `.env` を RDP で確認 → `BACOPY_SUPPORT_*` のみで Supabase / API キー / EXECUTOR_ID 全部欠落 |
| 判明 | 既存 `dist_per_user/user02..05/*.exe` は **2026-04-23 ビルド** で、build_staging に Supabase キーが追加された **2026-04-24** より前。USER01 #1 と同じ症状。user03〜05 も同罪 |
| 対応決定 | user02 PC の GUI を完全削除 → bafather で user02〜05 全部リビルドして配り直す |
| 16:41 着手 | bafather 上で `build_per_user.ps1 -Start 2 -Count 4` を SSH バックグラウンド実行 |
| 17:04 完了 | 4本ビルド完了 (15.7 分, 各 580 MB) |
| 〜 | user02 PC に新 EXE を再配布 → 起動 → SIGN IN 成功 |
| 〜 | INSTALL ON THIS PC ボタン押下 → 体感では「何も起きない」 (実は setup-all.ps1 が UAC で起動失敗 — `C:\ProgramData\BACOPY\setup-all.log` 不在) |
| 〜 | しかし `Get-Service sshd` で OpenSSH Server は **既に Stopped で入っている** ことが判明 |
| 〜 | 管理者 PowerShell で手動セットアップ (sshd 起動 + admin_pubkey.txt → `administrators_authorized_keys`) |
| 〜 | こちらから VPS 経由 SSH 成功 (port 2277) |
| 19:27 | ユーザーが Stake.com にログイン後、STOP → START |
| 19:42 | エンジンが Baccarat 9 着席 → ARMED ✅ |

---

## 3. インストールされた EXE

```
配置元: bafather (162.43.83.54) C:\bacopy\dist_per_user\user02\BACOPYRECEIVER_user02_Setup_0.1.0.exe
ビルド日時: 2026-04-29 16:52:30
サイズ: 580.3 MB (608,456,865 bytes)
バージョン: 0.1.0
```

埋込まれている `.env` (キーは provision-user-build.js が build_staging から焼き込み):
```
BACOPY_SUPPORT_USER_EMAIL=user02@beta.bacopy.local
BACOPY_SUPPORT_REMOTE_PORT=2277
BACOPY_SUPPORT_LOCAL_PORT=22
BACOPY_API_URL=https://master.bafather.uk
BACOPY_API_KEY=fy6FNK8pM5xqGrTEG_HP38yqIgO3b6QrnkUPctCAgDA
BACOPY_EXECUTOR_ID=user02
BACOPY_EXECUTOR_LABEL=user02
NEXT_PUBLIC_SUPABASE_URL=https://xsxuyjctqvxxqdethmua.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIs... (省略)
```

**注**: master UI 上の表示は `executor_id=hashimoto / label=hashimotoPC`。`.env` の `BACOPY_EXECUTOR_ID=user02` は localStorage の値に上書きされている (Settings で手動入力された値が優先された結果と推定)。機能には影響なし。

---

## 4. 遠隔 SSH 接続手順 (admin から user02 PC へ)

### 必要なもの
- VPS への鍵: `~/.ssh/laplace_vps` (Windows: `$env:USERPROFILE\.ssh\laplace_vps`)
- user02 PC への鍵: `<repo>/support_keys/admin_key` (リポジトリ ↔ bafather 共通、`.gitignore`)

### コマンド (bash / WSL)
```bash
ssh -i /e/dev/Cusor/bacopy/support_keys/admin_key \
    -o ProxyCommand="ssh -i $HOME/.ssh/laplace_vps -W %h:%p laplace@210.131.215.116" \
    -p 2277 Administrator@localhost
```

### コマンド (PowerShell)
```powershell
ssh -i E:\dev\Cusor\bacopy\support_keys\admin_key `
    -o ProxyCommand="ssh -i $env:USERPROFILE\.ssh\laplace_vps -W %h:%p laplace@210.131.215.116" `
    -p 2277 Administrator@localhost
```

### Windows OpenSSH の落とし穴
- デフォルトシェルは **cmd.exe** → `;` 区切りや bash 風スクリプトは効かない
- PowerShell コマンドを送るときは必ず `powershell -Command "..."` でラップ
- ssh 経由で `$_` を含む PowerShell を流すと bash 側で `$_` が消費される → `Format-Table` のプロパティ名指定など `$_` を使わない書き方に変える

### よく使う ad-hoc 確認コマンド
```bash
# プロセス確認
ssh ... 'powershell -Command "Get-Process BACOPYRECEIVER, bacopy_engine, camoufox -EA SilentlyContinue | Format-Table ProcessName, Id, StartTime -AutoSize"'

# .env 確認
ssh ... 'powershell -Command "Get-Content $env:LOCALAPPDATA\Programs\bacopy-copytrade-gui\resources\.env"'

# sshd 状態
ssh ... 'powershell -Command "Get-Service sshd"'
```

---

## 5. INSTALL ON THIS PC が動かなかった場合の手動 sshd セットアップ

INSTALL ON THIS PC ボタンを押しても `C:\ProgramData\BACOPY\setup-all.log` が生成されないとき、**OpenSSH Server は既に入っている (Stopped) ケースがある**。
その場合 RDP の管理者 PowerShell で:

```powershell
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
$pubkey = Get-Content "$env:LOCALAPPDATA\Programs\bacopy-copytrade-gui\resources\admin_pubkey.txt" -Raw
New-Item -Path "$env:ProgramData\ssh" -ItemType Directory -Force | Out-Null
Set-Content -Path "$env:ProgramData\ssh\administrators_authorized_keys" -Value $pubkey.Trim() -Encoding ASCII
icacls "$env:ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
Restart-Service sshd
Get-Service sshd
```

これで `Status: Running` になり、即こちらから SSH 通る。

---

## 6. 解消した既知症状 (USER01_ONBOARDING_ISSUES.md との対照)

| # | USER01 で発生 | user02 では | 備考 |
|---|---|---|---|
| 1 | Supabase キー欠落 | **再ビルドで解消** | provision-user-build.js は build_staging\.env の Supabase キーを焼き込む。4/24 以降のビルドなら入る |
| 2 | BACOPY_API_KEY 欠落 | **再ビルドで解消** | 同上 |
| 3 | sshd 未インストール (setup-sshd.ps1 文字コード) | sshd は入っているが起動していなかった | INSTALL ON THIS PC が UAC で setup-all.ps1 を起動できなかった可能性。手動で対応 |
| 4 | バランス残高 0 | 未確認 | bafather 管理画面で「両方無料」が必要な可能性。次回確認 |
| 5 | executor_id=`gui-1` 衝突 / WAIT LOGIN | `executor_id=hashimoto` (Settings 入力値) で衝突なし | Stake username は空のままだが今回は着席まで完走 |

---

## 7. 残課題

1. **GUI Settings の値が `.env` の `BACOPY_EXECUTOR_ID` を上書きする件**: `STABLE_RELEASE.md` では「.env が localStorage より優先」とあるが実際は `hashimoto` が使われている。user01 でも `kajigui` が同じ挙動だった。priority ロジックの実装と仕様が乖離している可能性 — 別途検証
2. **INSTALL ON THIS PC ボタン → setup-all.ps1 が起動しない問題**: UAC ダイアログが他の RDP セッションに出てしまう / 押し切れていない可能性。`main.js:1313` のスクリプト起動部分を確認 → 改善案を実装すべき
3. **`'_PragmaticState' object has no attribute 'operator_table_name'`**: engine 内部 sync で属性参照ミス。非致命だが要修正
4. **Stake username 設定なしで進行できた件**: USER01 のときは必須と認識していたが、空でも着席できた。条件を切り分けて文書を更新する
5. **user03〜user05 在庫 EXE**: 今回の再ビルドで新規版 (4/29) に差し替え済 → 配布時はそのまま使える
6. **Stake WS 無音化 / balance null 問題 (2026-04-29 ARMED 後に発覚 → 同日 GUI 再起動後に再発確認)**: ARMED 完了の約21分後から `last_stake_recv_at` が更新されなくなり、`balance / session_open_balance / daily_open_balance / session_pnl / daily_pnl` が全部 null のまま。Pragmatic game_ws は健全で BET と SWITCH_TABLE は正常動作 (W8/L5/T1=14発成立を確認)。`profit_stop_chips=50` / `loss_cut_chips=200` は chip ベース判定なので機能影響なし、表示問題のみ。
   - 2回目の再現観察: GUI 再起動 → ARMED → balance 一時的に取得成功 ($95,703.25 etc.) → ~13分後に再び `last_stake_recv` 停止 → balance 値が古いまま固定 → daily_pnl も止まる
   - 仮説の絞り込み: WS 接続自体は維持 (`pong` 受信継続) だが GraphQL subscription 再送の動作なし。Stake のフロント JS が wallet パネル開放等で subscription 始める設計の可能性。新規 Camoufox プロファイルで一度も Wallet UI 見ていないと永久に subscribe しない可能性
   - 既知問題: commit `39f53a7 diag(executor): Stake WS 診断ログを追加 (残高取得の真の WS URL 特定用)` で診断強化済みだが根本修正なし
   - 関連コード: `bacopy_executor_pragmatic_ws_live.py:1376-1404` (`availableBalances` / `vaultBalances` パーサ)、`:1328` (診断ログ `_stake_ws_diagnostic_log`)、`:1606-1615` (Stake WS 捕捉)
   - 友人 BET 中は STOP できないため、**セッション終了後に検査**:
     1. STOP → START で Stake WS 再接続するか
     2. Camoufox 内で stake.com を開いて実残高を目視確認
     3. `BACOPY_STAKE_WS_DEBUG=1` (デフォルト on) の診断ログから `[stake-ws][balance-hit N/10]` が出るか確認 — 出ていれば WS は届いているがパースが拾えていない、出ていなければ WS subscription 自体が始まっていない
     4. admin (`goldbenchan`) / kajigui の Camoufox プロファイルと user02 の差分調査 (Cookie / GraphQL ヘッダ / IP 国別ルーティング)

---

7. **【構造的バグ・最重要】bafather `cron/settle` が常に skip される疑い (2026-04-29 発覚)**:
   `web/src/app/api/cron/settle/route.ts` は `billing.session_state.{daily_open.balance, current_balance, last_balance_at}` を見て課金を計算するが、これらを更新する経路は **LAPLACE 旧コードの `agent_api.py:_post_session_state_to_server` のみ**。**bacopy 用の `bacopy_executor_pragmatic_ws_live.py` には session-state POST 機能が実装されていない**。
   - `bafather/api/session-state` (POST) は存在するが、bacopy の executor / GUI から呼んでいない
   - つまり **bacopy 移行時に課金経路が抜け落ちている疑いが極めて強い**
   - 影響: bacopy で動かしている全ユーザー (user01, user02, admin の hashimoto/kajigui/gui-1 含む) で `cron/settle` が `incomplete_state` で skip → **手数料が一切取れていない可能性**
   - 確認すべき (BET 終了後): bafather 管理画面 / Supabase の `deductions` テーブルに過去の課金履歴があるか? user01 (kajigui, hikata26621146@gmail.com) の `deductions` も0件なら構造バグ確定
   - 修正方針: `bacopy_executor_pragmatic_ws_live.py` に `_post_session_state_to_server` 相当を移植 (`agent_api.py:417-422` 参照)。bafather URL = `https://www.bafather.uk` (= LAPLACE_API_KEY 認証)。daily_open / session_open / current_balance / last_balance_at を engine の status_payload と同タイミングで POST する

8. **【今日の暫定対応・要復元】user02 を一時的に「両方無料」に変更 (2026-04-29 夜)**:
   `cron/settle` skip による suspend リスクと「日付越えで止まるかも」懸念から、bafather `/admin/users` で user02 (橋本さん) を **`is_free: true` + `balance: 99999`** に一時変更。**元の状態は `is_free: false` + `balance: $2000` + `profit_share_rate: 0.80` (ライセンスのみ無料)**。
   - **復元タスク (重要)**: 構造バグ #7 修正後 / 友人 BET 終了後に **`is_free: false` + `balance: $2000` に手動で戻す**。忘れると永久に課金されない
   - 復元前に確認: 修正済みの bacopy_executor が session-state POST を成功させているか (Supabase `billing.session_state` が更新されるか)

9. **GUI Settings 保存バグ (`||` falsy 落ち)** — `app.js:1013, 1015` の `parseFloat(...) || 50/200` が `0` を falsy 扱いして default 値に倒す。`bet_mode` も `normalizeBetMode` の許可リスト外で `flat_1usd` に倒れる疑い。`index.html:280, 288` の `<input min>` も `0` を許さない設定。
   - 修正案: `Number.isFinite(parseFloat(...)) ? v : default` に置換 + `min="0"`
   - engine 側 `:176-177, :284, :286, :4027-4028` の `max(1, ...)` も `0 = 無効化` 対応に変更
   - 一時回避策: ロスカット 2,000,000,000 などの巨大数で実質無効化 (動作確認済)

10. **`[ws-discover] game_ws_url DIFFERENT from stored` ノイズログ大量出力 (実害なし)** — 同一テーブルでも frame ごとに数百件のログが出る。動作影響なし (BET / SWITCH_TABLE / `[done]` イベント正常)。
    - 原因: `bacopy_executor_pragmatic_ws_live.py:1461-1477` `_maybe_update_from_game_ws_url` で **state.game_ws_url を初回のみ set し、その後の差分検出時に上書きしない**。同テーブルでも `?pageRefresh=true` 付き/なしの2バリエーションで毎 frame DIFFERENT 判定 → 永久にログ吐き続ける
    - 修正案: line 1477 の `print` 直後に `state.game_ws_url = url` を追加 (1行)、または同一秒内の重複ログを抑制
    - 副作用: ログファイル `executor_debug.log` が肥大化 (1セッションで数 MB)
    - 観察例: 2026-04-29T23:18:59 で同一秒に約 200 件出力

11. **AI 学習セッション ON で出した decision が pending 詰まりを起こす (2026-04-30 0時越えに発覚)**:
    - 症状: master UI で **BET ボタンが光らない (disabled)** / 新しい BET 指示が executor に届かない / `total_bets` カウンタ増えない
    - 経緯: 2026-04-29 23:59:59 UTC に AI 学習セッション ON 状態で出された decision (`dec_77a399ed7416b05b`, `amount=0, in_learning_session=true, target_executor_id=""`) が `pending` のまま放置
    - その後 master UI で「学習 OFF」操作しても **既存の pending decision は自動的に done / cancelled に遷移しない** ため、新規 BET も詰まる連鎖
    - 関連コード:
      - `bacopy_master_ui.py:1305` `btnP.disabled = !allowAny` 等 — `allowAny = betReady = nOnTarget>0 && all(bet_window_open) && !stopped`。pending 詰まりとは別経路だが、両方の問題が複合した可能性
      - 学習 ON 時の payload: `friend_action.in_learning_session=true, amount=0` (`bacopy_master_ui.py:1383`)
      - cancel API: 手動キャンセルエンドポイントなし。`bacopy_db.py:cancel_pending_bets_for_executor` は SWITCH_TABLE 時の自動 cancel のみ
    - 応急対応 (今回成功した手順):
      1. `POST /api/decisions/<decision_id>/ack` に `{executor_id, action, received:false, placed:false, reason:"manual_cancel"}` を投げて pending → done 強制遷移
      2. master UI で AI 学習トグル OFF 確認 + テーブル選択 (BACCARAT_9) → BET ボタン点灯
      3. 仁さんが手動 BET 押下 → 正常実行 (`total_bets` 増加で確認)
    - 修正案 (engine / Master 側):
      - master UI 側で「学習 OFF」操作時に既存 pending を全部 cancel する処理を追加
      - もしくは **engine 側で `in_learning_session=true && amount=0` の decision を即 ack done** で消化させる
      - もしくは **GUI 学習トグル OFF 時に master ui から `cancel_pending` API を呼ぶ** (新エンドポイント追加)
    - 観察データ: pending decision の payload 詳細は USER02_ONBOARDING_NOTES.md §7-11 ではなく `git log` でこの commit メッセージ時点の master API スナップショットから取れる

12. **利確発火時の表示金額が実残高利益と大きく乖離 (2026-04-30 友人 BET 終了後に発覚)**:
    - 症状: profit_stop_chips=300 設定にもかかわらず GUI 上「$621 で利確」と表示。実残高では daily_pnl=+$352 に相当し、約 **$269 が理論値と実残高の差**として消える
    - 当日のサマリ: total_bets=339 (W151/L165/T23), profit_sessions=2 (= 2回利確発火), daily_pnl=+$352
    - 原因 (chip ベース vs 実残高ずれ):
      1. **set 完了で一気にジャンプ**: `marubatsu_strategy.py:139` `set_profit = diff * _seq[current_unit_idx]` でセット 7 ハンド完了時に大幅加算 (例: SEQ idx 5 で diff=+5 → +350 chip 一気増)。profit_stop=300 で「ピッタリ」発火せず、621 まで到達してから発火する設計
      2. **部分 BET (partial bet)**: SEQ 高 idx ($70, $90 等) で実残高に対しフル金額置けない場合 chip 計算は理論満額だが実残高は減らない (= 利益にもならない)
      3. **Banker 手数料 5%**: Banker BET 勝利時、chip 計算は `+amount` 全額だが実残高は `+amount × 0.95` (5% コミッション)。Banker 多発で誤差累積
      4. **BET 失敗の記録**: window 締切等で実 BET 失敗しても tracker.add_result が走ってしまう経路があると chip 累積だけ進む可能性 (要追加調査)
      5. **Tie の扱い**: tie は tracker でスキップ (`marubatsu_strategy.py:198`) だが、BET 自体は置かれて push (戻る) なので残高変動なし。chip 計算には含まれない (整合)
    - これは engine の **bug というより chip ベース理論計算が前提の設計**で、実残高との乖離は不可避。ただし運用上ユーザー混乱を招くので**早急に対応すべき**
    - 修正案 (優先順):
      - **A. chip ベース計算を balance 差分で都度校正**: 各 set 完了時に `set_profit` を理論値ではなく `current_balance - prev_balance` で再計算。Stake balance が null の時は理論値フォールバック
      - **B. profit_stop / loss_cut を balance ベース判定に切替**: chip 累積でなく `daily_pnl >= profit_stop` で発火。balance null 時は **既存** chip ベースに退避
      - **C. GUI 表示の「累積利益」を chip ベースと balance ベース両方表示**して混乱を回避 (UI のみの変更で済む応急策)
      - C → A → B の順で段階対応推奨
    - 関連コード:
      - `bacopy_executor_pragmatic_ws_live.py:284-286` 利確/損切判定 (`if cp >= self.profit_stop`)
      - `bacopy_executor_pragmatic_ws_live.py:291-300` `record_round` の `money_actual` 計算 — balance 差分でずれを記録する基盤は既にある
      - `marubatsu_strategy.py:125-153` `finalize_set` で `set_profit` を理論計算
    - 関連バグ #6 (Stake WS 無音→balance null) と複合: Stake WS が止まると balance 校正もできない → chip ベース理論値だけが暴走するリスク

13. **総合所感: 全体的に挙動が不安定 (2026-04-30 ユーザーフィードバック)**:
    - 仁さん原文: 「色々と挙動が不安定です」「bafather サイトのチャージ資金部分も正常にさせたい」
    - 本日 (2026-04-29 〜 2026-04-30) の累積問題:
      - Stake WS 無音化 → balance 表示停止 (#6)
      - bafather `cron/settle` 構造バグ → 課金永久 skip (#7)
      - user02 を一時 `is_free: true` に避難 (#8)
      - Settings `||` falsy バグ (#9)
      - ws-discover ノイズログ (#10)
      - 学習セッション ON で pending 詰まり (#11)
      - 利確 chip ベース vs 実残高乖離 ($621 vs $352) (#12)
      - 弟子AI「まなぶくん」cron が BET=0 と誤認 (#14)
    - **次セッション以降の優先順** (ユーザー要望「bafather チャージ資金正常化」+「AI 学習データ収集」を最優先):
      1. **#7 bafather settle 構造バグ** — `agent_api.py` から bacopy executor に session-state POST 移植 (最優先、課金パイプライン復旧)
      2. **#14 まなぶくん cron BET=0 誤認** — XServer cron と bacopy-api の連携を修復 (5000BET 学習データ収集の根幹)
      3. **#12 利確 chip vs balance 乖離** — Settings 表示と判定の整合 (運用混乱直結)
      4. **#11 学習セッション pending 詰まり** — master UI 側の自動 cancel 実装
      5. **#9 GUI Settings || バグ** — loss_cut/profit_target 0 保存対応
      6. **#6 Stake WS 自動再接続** — balance 表示の安定化
      7. **#8 user02 復元** — 上記#7 修正後に `is_free: false / balance: $2000 / 80%` に戻す
      8. **#10 ws-discover ノイズログ** — 1行修正
    - **修正の進め方**: ローカルで修正 → commit → bafather で再ビルド (`build_per_user.ps1`) → user02 PC 配布 → 動作確認 → user02 復元
    - 友人の運用継続中はビルド配布タイミングが限られるため、休止帯を活用すること

14. **AI 学習エージェント「まなぶくん」(= 弟子 apprentice agent) の 23時 cron が BET=0 と誤認 (2026-04-29 発覚)**:
    - 症状: 弟子AI (Telegram bot, APPRENTICE_AI_SETUP.md Phase 5 の 23:00 `apprentice_daily_review.py` cron) が以下のメッセージを投稿:
      > 📅 2026-04-29 本日の振り返り
      > 今日もBETデータが0件でした。
      > お休みだったんですね、おつかれさまです 🐾
    - 実際の本日 (2026-04-29) BET 実績: user02 (hashimoto) が **339発 BET 実行** (W151/L165/T23, profit_sessions=2, daily_pnl=+$352)
    - = **AI 学習データ収集パイプラインが完全に分断されている** 重大事象。5000BET 学習目標 (project_next_ai_learning.md) から見ても致命的
    - 原因候補:
      1. **#7 と同根の構造バグ**: XServer の cron が `bacopy-api` (https://master.bafather.uk 経由 SSH トンネル port 18010 → 127.0.0.1:8010) から「今日の hand list」を取得しているが、**bacopy-api 側の決定保存先と cron の取得先が食い違っている可能性**
      2. **API エンドポイントの不一致**: APPRENTICE_AI_SETUP.md §6 「23:00 夜のレビュー: bacopy-api から今日の hand list 取得」とあるが、具体的なエンドポイント名が docs に明記されていない。`/api/decisions?status=done` 等を呼ぶ実装になっているはずだが、実体は要確認
      3. **SSH トンネル切断**: XServer 上の `bacopy-tunnel.service` (autossh) が cron 実行時刻に切れていた可能性 (健全性チェック必要)
      4. **タイムゾーンずれ**: cron は JST 23:00 想定だが、bacopy-api 側の「今日」が UTC ベースで判定されている場合、JST 23:00 = UTC 14:00 で「明日のデータ」を見に行ってしまう可能性
      5. **decisions.jsonl への append が学習セッション以外の BET を捕捉していない**: 友人の通常 BET (in_learning_session=false) が `data/decisions.jsonl` に書かれていない可能性 → ログ確認必要
    - 確認手順 (次セッション):
      ```bash
      # 1. XServer cron 実行ログ確認
      ssh hermes@100.116.79.99 "tail -50 ~/apprentice_daily_review.log"
      # 2. bacopy-api 側の decisions.jsonl カウント
      ssh -i ~/.ssh/laplace_vps laplace@210.131.215.116 \
        "wc -l /opt/bacopy/data/decisions.jsonl; tail -3 /opt/bacopy/data/decisions.jsonl"
      # 3. SSH トンネル状態
      ssh hermes@100.116.79.99 "systemctl --user status bacopy-tunnel.service; ss -ltn | grep 18010"
      # 4. ~/bin/apprentice_daily_review.py のソースを読んで取得 API を特定
      ssh hermes@100.116.79.99 "cat ~/bin/apprentice_daily_review.py | head -80"
      ```
    - 関連:
      - `APPRENTICE_AI_SETUP.md` §4 〜 §6 (Phase 5/6 = 23:00/07:00 cron)
      - `project_next_ai_learning.md` (5000BET 目標 — 0件記録だと一生達成しない)
      - 修正は engine 側の session-state POST 構造バグ (#7) と並行で進めるのが効率的 (両方 bacopy-api と外部システム連携の問題)
    - **AI 学習システム全体としての位置づけ**: 弟子AI は「友人の判断ロジックを reasoning ペアとして蓄積し、5000 で ML モデルの精度を上げる」設計 (APPRENTICE_AI_SETUP.md §1.3)。BET=0 と誤認すると **学習データセットに「お休みだった」という嘘が記録され続ける** → 5000 達成時の bacopy DB JOIN で空 hand_id が大量発生 → モデル精度に致命的影響
    - 優先度は **#7 と同等の最重要**

## 8. 関連ドキュメント

- `USER01_ONBOARDING_ISSUES.md` — 4/24 user01 配布で発見された5件 (本件は #1, #2, #3 の派生)
- `STABLE_RELEASE.md` — v1.0.0-copytrade-beta1 安定板の修正リスト
- `docs/BUILD_RELEASE_GUIDE.md` — リビルド・配布手順 (port 衝突回避設計、§3.3 の slot 表)
- `docs/BACOPYRECEIVER_ユーザー手順書.md` — エンドユーザー向け日本語 README
