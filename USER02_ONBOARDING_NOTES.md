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

## 8. 関連ドキュメント

- `USER01_ONBOARDING_ISSUES.md` — 4/24 user01 配布で発見された5件 (本件は #1, #2, #3 の派生)
- `STABLE_RELEASE.md` — v1.0.0-copytrade-beta1 安定板の修正リスト
- `docs/BUILD_RELEASE_GUIDE.md` — リビルド・配布手順 (port 衝突回避設計、§3.3 の slot 表)
- `docs/BACOPYRECEIVER_ユーザー手順書.md` — エンドユーザー向け日本語 README
