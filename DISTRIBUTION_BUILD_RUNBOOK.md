# 配布パッケージ ビルド手順書（デュアルライン手動アシスト版）

> **新しいClaude/作業者へ：これが配布パッケージ(user01〜05)の唯一の正規ビルド手順です。**
> 修正のたびにこの手順で作り直す。我流でやらない。2026-06-03 に user01 を bafather 実機でフル検証して確立した手順。

---

## 0. 用語・接続情報

| 物 | 値 |
|---|---|
| ローカル開発機（ビルドする所） | `E:\dev\Cusor\bacopy`（win32, gitリポジトリ。**electron-builderはここで動かす**） |
| bafather（管理者のDesktop Cloud＝engineビルド機＆パイロット機） | `Administrator@162.43.83.54` 鍵 `C:\Users\USER\.ssh\laplace_vps` |
| VPS（master API・SSH踏み台） | `210.131.215.116`（user: laplace / support） |
| master決定API | `https://master.bafather.uk`（NOW決定・preposition配信） |
| 課金API | `/api/session-state`（bafather.uk） |

- **engine（`bacopy_engine.exe` 約68MB・onefile）は bafather 上で PyInstaller でビルド**する（ローカルにPyInstaller環境は無い前提）。
- **GUI（main.js/app.js/index.html/styles.css）は src/ に置き、electron-builder が asar にパックする**（ローカルでビルド）。

---

## 1. 前提（初回のみ確認。通常は揃っている）

- `web/.env.local`（**gitignore対象＝秘密OK**）に次が必要：
  - `BACOPY_API_KEY=fy6FNK8pM5xqGrTEG_HP38yqIgO3b6QrnkUPctCAgDA`（master決定の本キー。実測 `GET https://master.bafather.uk/api/decisions/pending` を Bearer で叩くと200、無キーで401）
  - `BACOPY_REMOTE_API_KEY=`（同じ値）
  - `LAPLACE_API_KEY=`（課金 session-state 認証）
  - ※`BACOPY_BAFATHER_EMAIL_USERxx` は**設定しない**（課金メールはGUIサインインから来る。在庫パッケージにメールは焼かない）
- `support_keys/`（**リポジトリ直下**, gitignore対象）に：`admin_key` `admin_key.pub` `client_key` `port_registry.json`
- `copytrade_gui/build_staging/engine/bacopy_engine.exe` が**現行のデュアルラインengine**であること（後述2で更新）
- `copytrade_gui/package.json` は：`"author"` あり / extraResources に **camoufox_firefox は無い**（chrome_attachでは不要・あるとビルド失敗）

---

## 2. 【engineを直した時だけ】engineを再ビルドして build_staging を更新

`dual_line_pragmatic_bot.py` / `dual_line_live_executor.py` / `dual_line_money.py` 等を直したら：

```bash
# 1) 変更したpyをローカルでsyntaxチェック
python -c "import ast; ast.parse(open(r'E:\dev\Cusor\bacopy\dual_line_pragmatic_bot.py',encoding='utf-8').read()); print('OK')"

# 2) bafatherへSCP（変更した全pyを送る。dual_line_money.py を送り忘れない＝過去のdalembert事故）
scp -i /c/Users/USER/.ssh/laplace_vps dual_line_pragmatic_bot.py dual_line_live_executor.py dual_line_money.py Administrator@162.43.83.54:C:/bacopy/

# 3) bafatherでengineビルド（PyInstaller, 3-6分） → C:\bacopy\bacopy_engine.exe.new
ssh -i /c/Users/USER/.ssh/laplace_vps Administrator@162.43.83.54 "powershell -NoProfile -ExecutionPolicy Bypass -File C:\bacopy\_build_engine_handend.ps1"

# 4) 新engineをローカル build_staging に取り込む（配布に同梱されるのはこれ）
scp -i /c/Users/USER/.ssh/laplace_vps "Administrator@162.43.83.54:C:/bacopy/bacopy_engine.exe.new" copytrade_gui/build_staging/engine/bacopy_engine.exe
# 確認: 約68MB（224MBは旧camoufox版＝間違い）
stat -c%s copytrade_gui/build_staging/engine/bacopy_engine.exe
```

> **GUIだけ直した場合はこのセクション不要**（engineは現行のまま）。3へ進む。

---

## 3. 配布パッケージ（user01〜05）をビルド

`copytrade_gui` ディレクトリで、各ユーザーを「プロビジョニング → electron-builder」で回す。
**出力形式は2つ。配布は A（単一インストーラ）推奨。**

### 3-A. NSIS 単一インストーラ（推奨・1ファイル約160MB）= `BACOPYRECEIVER_userNN_Setup.exe`

PowerShell（ローカル）で一括：

```powershell
Set-Location E:\dev\Cusor\bacopy\copytrade_gui
$users = @('01','02','03','04','05')
foreach($n in $users){
  Write-Output ("==== user" + $n + " ====")
  node scripts/provision-user-build.js ("user" + $n + "@beta.bacopy.local") 2>&1 | Where-Object { $_ -match 'allocated|reused|reach' }
  if($LASTEXITCODE -ne 0){ Write-Output ("PROVISION_FAILED user" + $n); break }
  $before = (Get-Date).AddSeconds(-2)
  npx electron-builder --win nsis --config.win.signAndEditExecutable=false 2>&1 | Select-Object -Last 3
  $inst = Get-ChildItem dist -Filter '*Setup*.exe' -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -ge $before } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if($inst){
    $dst = Join-Path 'dist' ("BACOPYRECEIVER_user" + $n + "_Setup.exe")
    Move-Item -LiteralPath $inst.FullName -Destination $dst -Force
    Write-Output ("INSTALLER_DONE user" + $n + " size=" + [math]::Round((Get-Item $dst).Length/1MB,1) + "MB")
  } else { Write-Output ("NO_INSTALLER user" + $n); break }
}
Write-Output "BATCH_NSIS_COMPLETE"
```

### 3-B. ZIP版（フォルダ一式・各約205MB）= 必要な時だけ

`--win nsis` を `--dir` に変え、出力 `dist\win-unpacked` を `BACOPYRECEIVER_userNN` にリネーム → `Compress-Archive` で `BACOPYRECEIVER_userNN.zip`。
（電子builderは毎回 `dist\win-unpacked` に出すので、次のユーザーの前にリネームで退避する）

### 出力先・ポート

- 出力: `E:\dev\Cusor\bacopy\copytrade_gui\dist\BACOPYRECEIVER_userNN_Setup.exe`
- ポート（`support_keys/port_registry.json` で永続・メールハッシュ由来で固定）:
  user01=**2234** / user02=**2277** / user03=**2255** / user04=**2291** / user05=**2241**

---

## 4. 検証（ビルド後）

```bash
# インストーラ5本の存在・サイズ
ls -la copytrade_gui/dist/BACOPYRECEIVER_user0*_Setup.exe
# （ZIP/--dir版を作った場合）同梱engine・asar・envの中身を抽出確認
#   engine が 68676136 近辺か / asarに ensureCdpChrome,playNowSound 等があるか /
#   .env に BACOPY_API_KEY=fy6 / BACOPY_BROWSER=chrome_attach / 固有ポート があるか
```

中身の確定設定（provisionが各 .env に書く）：
`BACOPY_API_URL=https://master.bafather.uk` / `BACOPY_API_KEY=fy6...`(+REMOTE同値) / `LAPLACE_API_KEY` / `BACOPY_BROWSER=chrome_attach` / `BACOPY_MANUAL_NO_AUTOCLICK=1` / `BACOPY_SUPPORT_REMOTE_PORT=<固有>` / `BACOPY_EXECUTOR_ID=userNN`。BACOPY_BAFATHER_EMAILは無し（=課金はログイン）。

---

## 5. 配布とユーザー操作

- **送るのは `BACOPYRECEIVER_userNN_Setup.exe` 1本だけ**（フォルダ内の `BACOPYRECEIVER.exe` 単体はNG）。
- **ユーザー取り違え厳禁**：各 Setup.exe に固有のSSHポート/鍵が焼かれている。user02の人にはuser02用を。
- ユーザー手順：Setup.exe実行（未署名→SmartScreen「詳細情報→実行」）→ 自動インストール+起動 → **設定→SYSTEM→「INSTALL ON THIS PC」→ UACで必ず「はい」** → 9222 Chrome自動起動でStakeログイン → GUIで自分のbafather.uk登録メールでログイン → START。
- **管理者(あなた)の各受け子への接続**：
  ```
  ssh -i support_keys/admin_key -J laplace@210.131.215.116 Administrator@localhost -p <ポート>
  ```

---

## 6. ハマりどころ（必読・このセッションで踏んだ実例）

1. **build_staging/engine が古い**：5/28の224MB(camoufox版)が残っていた。engineを直したら必ず §2 で68MB版に更新。送り忘れ厳禁。
2. **dual_line_money.py の送り忘れ**：bafatherに古いmoneyが残り `dalembert` が argparse choices に無く起動失敗した事故あり。engineビルド前に関連pyを全部SCP。
3. **camoufox_firefox extraResource**：dirが無いのにpackage.jsonが参照→electron-builder失敗。chrome_attachでは不要なので削除済み（戻さない）。
4. **NSISに author 必須**：package.json に `"author"` が無いとNSISビルドが失敗し得る（追加済み）。
5. **PowerShellの `Remove-Item` グロブ（`*` や `[`）はハーネスにブロックされる**：`Get-ChildItem ...*Setup*.exe | Remove-Item` 等が弾かれた。→ 事前削除せず `LastWriteTime` で新出力を特定し `Move-Item -Force` で退避する（§3-Aの方式）。
6. **SSH越し `powershell -Command` は `$`/`'`/`[` が崩れる**：必ず `.ps1` をSCPして `-File` 実行。SCP/SSHは `dangerouslyDisableSandbox: true` 必須。
7. **INSTALL ON THIS PC が「無反応」=UAC未承認**（コードバグではない）。setup-all.ps1自体は正常（手動実行で admin_key 登録・トンネルタスク登録を確認済）。配布ユーザーにUAC「はい」を周知。
8. **生CDP Runtime.evaluate は IIFE `(()=>{...})()` 必須**（アロー関数のみは非invokeで空返り）。**Playwright frame.evaluate はアロー `()=>{...}`**（invokeされる）。残高/ベット履歴のDOM読取JSで重要。
9. **リアルタイム残高は口座WSでは取れない**（chrome_attach/OOPIFで`ws-silent`）。**Pragmaticゲームフレーム(`client.pragmaticplaylive.net/desktop/multibaccarat`)のDOMの `残高$ NNN.NN` を読む**のが正解（canvas=0でDOM可）。executor.read_stake_balance()。

---

## 7. bafather（管理者機）を最新コードで動かす場合（配布とは別系統）

bafather自身を最新で動かす/パイロットする時：
- engine差替：`_swap_engine_yellowfix.ps1`（GUI+engine停止→backup→swap）→ **ユーザーがRDPでGUI再起動**（SSH不可）。
- asar差替（GUIのみ変更時）：デプロイ済みasar取得→`@electron/asar`で展開→該当ファイル差替→再パック→`C:\bacopy\app.asar.new`にSCP→`_swap_asar_generic.ps1`。
- bafatherを user01 のインストーラ/--dir版でクリーン運用中の場合、旧dev installは `bacopy-copytrade-gui.old_<stamp>` にリネーム退避・`cdp_chrome`タスク無効化済（ロールバックは逆操作）。

---

## 8. 関連

- 仕様・経緯メモリ：`project_dual_line_distribution_ready_2026-06-02`（課金/残高/master鍵/ポート/検証結果の詳細）
- ブランチ：`feat/dual-line`。配布関連コミット：d270c95(課金集計) cb30dd6(送信) 14299fc(Chrome起動) 657bd05(provision env) f5fac25(残高) 6a3029e(camoufox削除+remote key) 23c8691(author)。
- 診断ツール（read-only生CDP, bafather C:\bacopy\ + ローカル）：`_cdp_billing_verify.py` `_cdp_bets_table.py` `_cdp_stake_balance_dom.py` 等。
