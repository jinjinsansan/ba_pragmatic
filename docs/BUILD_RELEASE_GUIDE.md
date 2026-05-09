# BACOPYRECEIVER ビルド & リリース ガイド（AI エージェント向け）

> **このドキュメントは Droid / Claude Code / その他 AI エージェント が新規チャットで本プロジェクトを引き継ぐための運用ガイドです。**  
> 最終更新: 2026-04-23
> 作成経緯: BACOPYRECEIVER v0.1.0 の初回配布準備セッション

---

## 🚨 最重要ルール（先に読め）

### 1. EXE リビルドは **Desktop Cloud** でのみ行う

- **WSL / Linux / macOS では絶対にビルドしない**（electron-builder が Windows `.exe` を生成できない）
- ユーザー本人の日常 PC でもビルドしない（環境汚染、署名環境の分散、Camoufox キャッシュ依存の再現性問題）
- 正規のリビルド場所: **`Xserver デスクトップクラウド (162.43.83.54)` の `C:\bacopy\` リポジトリ**

### 2. GUI / Master / executor の改修があったら必ずリビルド

以下のいずれかに変更があれば **v0.1.1 / v0.2.0 等にバージョンを上げて** Desktop Cloud で再ビルド:
- `bacopy_executor_pragmatic_ws_live.py`（BET ロジック）
- `bacopy_engine.py` / `bacopy_watch_*.py`（エンジン CLI）
- `copytrade_gui/src/**`（Electron GUI）
- `requirements.txt`（Python 依存）
- `copytrade_gui/package.json`（Node 依存 or build 設定）
- `web/**`（bafather.uk フロント・API。ただしこちらは Vercel 自動デプロイで独立）

### 3. β配布は **ユーザーごとに個別 EXE**（同じファイルを全員に配らない）

- 理由: VPS の Support tunnel port は **ユーザーごとに一意**（2222〜2299）で、同一ポートは衝突する
- ビルド済み slot: `user01..user05`（追加が必要なら `user06..user10` などを同じ方式で）
- 台帳管理: どの slot を誰に渡したか **必ず記録**

---

## 1. インフラ接続情報

### 1.1 Desktop Cloud（Windows Server 2022, ビルドマシン）

| 項目 | 値 |
|------|---|
| ホスト名 | `bafather` |
| IP | `162.43.83.54` |
| ポート | 22 |
| ユーザー | `Administrator`（ドメイン形式 `bafather\administrator`） |
| シェル | PowerShell |
| 接続鍵（ローカルから） | `~/.ssh/xserver_key` または `~/.ssh/laplace_vps` |
| 接続コマンド例 | `ssh -i ~/.ssh/xserver_key Administrator@162.43.83.54` |

#### 重要: authorized_keys の場所
- Administrators グループユーザーは **`C:\ProgramData\ssh\administrators_authorized_keys`** を参照
- 通常の `%USERPROFILE%\.ssh\authorized_keys` は **参照されない**
- ACL: `SYSTEM:(F)` + `BUILTIN\Administrators:(F)` のみ（他の権限は SSH が拒否）

#### シェル上の注意
- PowerShell なので `&&` は使えず `;` で区切る
- `\"` エスケープが多段で複雑になるときは、ローカルに `.ps1` 作成 → `scp` → `powershell -ExecutionPolicy Bypass -File <path>` で実行

### 1.2 VPS（Ubuntu 25.04, bacopy-api + Support Tunnel Hub）

| 項目 | 値 |
|------|---|
| IP | `210.131.215.116` |
| 主ユーザー | `laplace`（shell あり, sudo 可）|
| サポート用ユーザー | `support`（`/usr/sbin/nologin`, tunnel 専用） |
| 接続鍵 | `~/.ssh/laplace_vps` |
| 接続コマンド例 | `ssh -i ~/.ssh/laplace_vps laplace@210.131.215.116` |

#### VPS の Support Tunnel 受入れ設定
```
/etc/ssh/sshd_config:
  Match User support
      AllowTcpForwarding yes
      X11Forwarding no
      PermitTTY no
      AllowStreamLocalForwarding no
```

#### VPS の authorized_keys（support ユーザー）
```
/home/support/.ssh/authorized_keys:
  ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILJAVQI93wYJmGDyYoFJM6s3sqLlIPxL25BVMq5LSKWW bacopy-client-tunnel
```
**全 β ユーザー共通の `client_key.pub` が1本だけ** 登録されている。各ユーザー別に暗号化された `support_key` は中身（復号後）が全員同じ秘密鍵。port だけユーザーごとに分ける設計。

### 1.3 GitHub Repository

| 項目 | 値 |
|------|---|
| URL | `https://github.com/jinjinsansan/ba_pragmatic.git` |
| ブランチ | `main` |
| Desktop Cloud 上の clone 先 | `C:\bacopy\` |
| ローカル WSL 上の clone 先 | `/mnt/e/dev/Cusor/bacopy/` |

---

## 2. リビルド手順（GUI / executor の改修後）

### 2.1 前提確認

1. ローカル WSL で開発・修正する
2. すべてコミット & `git push origin main`
3. Desktop Cloud を最新に同期する

### 2.2 ステップ手順

#### Step 1: Desktop Cloud を最新化
```powershell
# RDP or SSH で Desktop Cloud にログイン
cd C:\bacopy
git fetch --all
git reset --hard origin/main
git log --oneline -3   # 最新コミットを確認
```

#### Step 2: 旧ビルド成果物を削除
```powershell
Remove-Item -Recurse -Force C:\bacopy\copytrade_gui\dist -ErrorAction SilentlyContinue
Remove-Item -Force C:\bacopy\copytrade_gui\build_staging\engine\bacopy_engine.exe -ErrorAction SilentlyContinue
# 注: camoufox_firefox/ は再利用可能（956MB, 毎回コピーすると時間かかる）
```

#### Step 3: Python 依存 & camoufox fetch
```powershell
cd C:\bacopy
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m camoufox fetch  # Firefox バイナリ最新化
deactivate
```

#### Step 4: engine.exe を PyInstaller でビルド
```powershell
cd C:\bacopy
.\venv\Scripts\Activate.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\build_bacopy_engine.ps1
deactivate
# 出力: C:\bacopy\copytrade_gui\build_staging\engine\bacopy_engine.exe (65-70MB)

# 動作確認
C:\bacopy\copytrade_gui\build_staging\engine\bacopy_engine.exe executor-pragmatic --help
```

#### Step 5: Camoufox Firefox を build_staging にコピー（初回 or 更新時のみ）
```powershell
cd C:\bacopy
powershell -ExecutionPolicy Bypass -File .\scripts\copy_camoufox_assets.ps1
# 出力: C:\bacopy\copytrade_gui\build_staging\camoufox_firefox\ (956MB)
```
2回目以降で Firefox バージョン変更なしなら Step 5 はスキップ可。

#### Step 6: admin 用シングル installer（goldbenchan@gmail.com）を作る場合

```powershell
cd C:\bacopy\copytrade_gui
node scripts/provision-user-build.js goldbenchan@gmail.com
npm run build:installer
# 出力: C:\bacopy\copytrade_gui\dist\BACOPYRECEIVER Setup <version>.exe
```

#### Step 7: β配布用 個別 EXE を一括生成する場合

詳細は §3 参照。

---

## 3. β配布: ユーザーごとの個別 EXE ビルド

### 3.1 なぜ個別ビルドが必要か

1つの VPS `support` ユーザーに対して **port 2222〜2299 の 78 slots** を割り当て、各 β ユーザーは自分専用の port で reverse SSH tunnel を張る。

```
田中さん  --2234-->  VPS(210.131.215.116)
山田さん  --2277-->  VPS(210.131.215.116)
佐藤さん  --2255-->  VPS(210.131.215.116)
...
```

同じ port を複数ユーザーが使うと VPS の sshd が片方を弾く（`ExitOnForwardFailure=yes`）ので、**メールアドレスの SHA-256 ハッシュで決定的に port を割り振り**、ユーザーごとに違う EXE を作る必要がある。

### 3.2 なぜ "ダミー slot 方式" を採用したか

- 実ユーザーの確定前に EXE を用意しておきたい
- メールアドレスは後から変更できない（暗号鍵の派生材料なのでリビルド必須）
- 解決策: `user01@beta.bacopy.local` ... `user05@beta.bacopy.local` という **意味のないダミー識別子** で先に 5 本ビルドして、ユーザー確定時に「user01 の EXE を田中さんに渡す」という運用

### 3.3 ビルド済み slot（2026-05-10 更新）

| Slot | Email (ダミー) | 割当 Port | 配置場所 |
|---|---|---|---|
| user01 | user01@beta.bacopy.local | 2234 | `C:\bacopy\dist_per_user\user01\BACOPYRECEIVER_user01_Setup_0.1.0.exe` |
| user02 | user02@beta.bacopy.local | 2277 | `C:\bacopy\dist_per_user\user02\BACOPYRECEIVER_user02_Setup_0.1.0.exe` ※2026-05-10 リビルド済み |
| user03 | user03@beta.bacopy.local | 2255 | `C:\bacopy\dist_per_user\user03\BACOPYRECEIVER_user03_Setup_0.1.0.exe` |
| user04 | user04@beta.bacopy.local | 2291 | `C:\bacopy\dist_per_user\user04\BACOPYRECEIVER_user04_Setup_0.1.0.exe` |
| user05 | user05@beta.bacopy.local | 2241 | `C:\bacopy\dist_per_user\user05\BACOPYRECEIVER_user05_Setup_0.1.0.exe` |

### 3.4 追加の slot をビルドする（例: user06〜user10）

Desktop Cloud 上で:

```powershell
# まず最新コードに同期済みであること (Step 1-5 完了)
powershell -ExecutionPolicy Bypass -File C:\bacopy\scripts\build_per_user.ps1 -Count 5 -Start 6
```

- `-Count 5` : 何本作るか
- `-Start 6` : 何番から始めるか（user06 から）

所要時間: **1 slot あたり約 2-3 分**（engine.exe と camoufox_firefox はキャッシュ効く）。  
5 本で ~14 分、10 本で ~30 分。

### 3.5 ビルド後の出力構造

```
C:\bacopy\dist_per_user\
├── build_report.csv              ← slot/email/port/file パス/サイズ/build時刻
├── user01\BACOPYRECEIVER_user01_Setup_<version>.exe
│        BACOPYRECEIVER_user01_Setup_<version>.exe.blockmap
├── user02\...
├── user03\...
├── user04\...
└── user05\...
```

### 3.6 VPS 側の認証設定

**追加作業不要**。全 β ユーザーは同じ `client_key` を共有し、VPS の `/home/support/.ssh/authorized_keys` には `client_key.pub` が1行入っているだけで全員認証できる。各ユーザーの installer に埋め込まれる `support_key` は、ユーザーのダミーメールで AES-256-CBC 暗号化されているだけで、復号後の中身は全員同一の client 秘密鍵。

---

## 4. 配布運用の管理台帳

### 4.1 必須: どの slot を誰に渡したか記録

推奨: Google スプレッドシート or Notion。`dist_per_user\build_report.csv` をベースに以下を追記:

| Slot | Port | 配布先（本名） | 配布先メール（bafather 登録） | 配布日 | 状態 | 備考 |
|---|---|---|---|---|---|---|
| user01 | 2234 | - (warakaji/bafather 専用) | - | - | 稼働中 | bafather (162.43.83.54) で稼働 |
| user02 | 2277 | 橋本 (hashimoto) | - | 2026-05-10 | 稼働中 | 新PC: win-2026-05-090 (16GB RAM) 2026-05-10 移行 |
| user03 | 2255 | - | - | - | 在庫 | |
| user04 | 2291 | - | - | - | 在庫 | |
| user05 | 2241 | - | - | - | 在庫 | |

### 4.2 遠隔サポート（admin から各 PC に SSH 接続）

```bash
# warakaji/bafather (slot=user01相当, port=2252 or 直接IP)
ssh -i ~/.ssh/laplace_vps Administrator@162.43.83.54

# hashimoto 新PC (slot=user02, port=2277) via VPS tunnel
ssh -i support_keys/admin_key \
  -o ProxyCommand="ssh -i ~/.ssh/laplace_vps -W %h:%p root@210.131.215.116" \
  -o StrictHostKeyChecking=no -p 2277 Administrator@localhost
```

**WSL からの接続コマンド（/tmp/admin_key を使う場合）:**
```bash
ssh -i /tmp/admin_key \
  -o ProxyCommand="ssh -i /root/.ssh/laplace_vps -W %h:%p root@210.131.215.116" \
  -o StrictHostKeyChecking=no -p 2277 Administrator@localhost
```

- admin_key は `support_keys/admin_key`（ローカル側に保管）、WSL では `/tmp/admin_key`
- ユーザー PC 側の sshd: installer 実行後に `Start-Service sshd` + `Set-Service sshd -StartupType Automatic` が必要
- `C:\ProgramData\ssh\administrators_authorized_keys` に admin_key.pub を追加し ACL を設定すること（§4.2.1 参照）

#### 4.2.1 新規 PC の SSH セットアップ手順（PC 側 PowerShell）
```powershell
# 1. sshd 起動・自動起動設定
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic

# 2. admin 公開鍵を登録
$key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEmxcAURb96MSBIn4iFnUdFWb9DibiX6D2oIVZgbckW1 bacopy-admin"
New-Item -ItemType Directory -Force -Path "C:\ProgramData\ssh"
Add-Content -Path "C:\ProgramData\ssh\administrators_authorized_keys" -Value $key
icacls "C:\ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "SYSTEM:(F)" "BUILTIN\Administrators:(F)"
Restart-Service sshd
```

### 4.3 ユーザーを入れ替える場合

- slot は使い回せる（例: 田中さんが離脱 → user01 EXE を別の人に渡し直す）
- ただし以下は留意:
  - 旧ユーザーの profile (`%APPDATA%\bacopy-copytrade-gui\profiles\`) に前の人の Stake セッションが残る → **ユーザーに削除を指示**するか、別 slot を渡す方が安全
  - 旧ユーザーの PC を使ったままだと、admin から接続して BET 結果を確認できてしまう → 意図的でないなら slot を回収

### 4.4 slot 在庫が尽きたら

- 同じ方式で `user06..` を追加ビルド
- `support_keys/port_registry.json` に衝突情報が永続化されるので、同じメール＋違う port の再割り当ても可能

---

## 5. 稼働 PC 一覧（2026-05-10 更新）

### 5.0 稼働中 PC サマリー

| PC 名 | 役割 | IP / 接続方法 | slot | VPS port | ホスト名 | RAM | 備考 |
|---|---|---|---|---|---|---|---|
| warakaji (bafather) | Master + Executor | SSH: `ssh -i ~/.ssh/laplace_vps Administrator@162.43.83.54` | - | 2252 | - | - | ビルドマシン兼稼働PC |
| hashimoto (新PC) | Executor | VPS tunnel port 2277 | user02 | 2277 | win-2026-05-090 | 16GB | 2026-05-10 移行 |

### 5.0.1 hashimoto PC 移行履歴

| 日付 | 変更内容 |
|---|---|
| 2026-04-29 | 旧 hashimoto PC (win-2026-04-290, 8GB RAM) 運用開始 |
| 2026-05-10 | 橋本さんが 16GB Xserver デスクトップクラウドを新規契約 |
| 2026-05-10 | user02 インストーラーをリビルド（ロビー stuck バグ修正 24e0a6b 含む）して新 PC にインストール |
| 2026-05-10 | Supabase セッション・Stake cookies・seq7_state.json を旧 PC から移行 |
| 2026-05-10 | sshd セットアップ・admin_key 登録完了。SSH 接続確認済み (win-2026-05-090) |
| 2026-05-10 | 旧 PC (win-2026-04-290) は BACOPYRECEIVER を停止・廃止 |

---

## 6. 本番 Desktop Cloud 自身の運用

### 6.1 現状（2026-04-23）

| 項目 | 値 |
|---|---|
| インストール先 | `C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\` |
| .env 場所 | `...\resources\.env`（本番キー + Support tunnel 設定 + `BACOPY_DB_PATH`） |
| DB | `C:\Users\Administrator\Desktop\data\bacopy.sqlite3` |
| Camoufox キャッシュ | `C:\Users\Administrator\AppData\Local\camoufox\`（約 956MB, 既存） |
| デスクトップショートカット | `C:\Users\Administrator\Desktop\BACOPYRECEIVER.lnk` |
| 割当 Port | 2252（`goldbenchan@gmail.com`） |
| 起動方法 | RDP ログイン → ショートカットをダブルクリック（手動）|

### 5.2 過去のスケジュールタスク（削除済み）

- `BacopyGUI` (Running だった) → Unregister 済
- `BacopyDevGui` (Ready) → Unregister 済
- `bacopy_login` / `bacopy_watch_evo` / `bacopy_watch_prag` → Disabled のまま放置

### 5.3 本番バージョンアップ時の手順

1. GitHub にコードを push
2. Desktop Cloud にログイン（RDP or SSH）
3. **現 BACOPYRECEIVER を終了**（必要ならβユーザーにも通知）
4. §2 のリビルド手順を実行
5. `scripts/build_per_user.ps1` で admin 用 installer を作る or NSIS で上書きインストール
   - NSIS `oneClick: true` なので自動で上書き → 再起動
6. **.env は installer 同梱のテンプレで上書きされるので、毎回 merge 作業が必要**
   - backup 推奨: `Copy-Item <install>\resources\.env <backup>` で保存
   - インストール後: 本番キーを再マージ（`BACOPY_API_KEY` など）
   - **または** `provision-user-build.js` で `goldbenchan@gmail.com` 向けの環境ファイルを先に整えてからビルド

### 5.4 バックアップ

`C:\BACOPY_migration_backup\` に以下を保管:
- `env.desktop` - 移行前 Desktop\.env のコピー（本番キー一式）
- `env.build_staging` - 移行前 build_staging\.env
- `bacopy.sqlite3` - 移行前 DB スナップショット
- 各種 PowerShell スクリプト（`cleanup_and_shortcut.ps1`, `relaunch.ps1`, `test_tunnel_manual.ps1` など）

---

## 7. ビルドが壊れたとき（トラブルシュート）

### 7.1 engine.exe が動かない（camoufox ImportError）

症状: `bacopy_engine.exe executor-pragmatic --help` で `camoufox is required to run this executor. ... ModuleNotFoundError`

原因: PyInstaller のときに camoufox が venv に入っていなかった or `--collect-all` を指定し忘れた

対処: `requirements.txt` に `camoufox>=0.4.0`, `browserforge>=1.2.0` 入っているか確認 → `pip install -r requirements.txt` 再実行 → `build_bacopy_engine.ps1` 再実行

### 7.2 配布先 PC で Camoufox Firefox が見つからない

症状: BET START 後 Camoufox 起動に失敗

原因: 配布先 PC の `%LOCALAPPDATA%\camoufox` が空で、かつ installer の `resources\camoufox_firefox\` からのリストアも失敗

対処:
1. installer 内容を確認 (`Get-ChildItem <install>\resources\camoufox_firefox\`)
2. Electron main.js の `ensureCamoufoxAssets()` が呼ばれているか（起動ログに `[camoufox] restoring bundled Firefox -> ...` が出るはず）
3. 手動実行: `xcopy <install>\resources\camoufox_firefox\* %LOCALAPPDATA%\camoufox\ /s /e /h /y`

### 7.3 Support tunnel が接続できない

症状: ログに `[support] ... Permission denied (publickey)` or `port NNNN not listening`

切り分け:
1. VPS の authorized_keys に `client_key.pub` が入っているか  
   ```bash
   ssh -i ~/.ssh/laplace_vps laplace@210.131.215.116 "sudo cat /home/support/.ssh/authorized_keys"
   ```
2. VPS 側 sshd 設定が壊れていないか  
   ```bash
   ssh -i ~/.ssh/laplace_vps laplace@210.131.215.116 "sudo sed -n '150,170p' /etc/ssh/sshd_config"
   ```
   → `Match User support: AllowTcpForwarding yes` が入っていること
3. port 衝突 → 別ユーザーが同じ port を使っていないか
   ```bash
   ssh -i ~/.ssh/laplace_vps laplace@210.131.215.116 "sudo ss -tlnp | grep :<PORT>"
   ```
4. ユーザーが別の EXE（他人の slot）を使ってしまっている → 台帳確認

### 7.4 NSIS ビルドで `python3` コマンドが見つからない

症状: `npm run build:installer` で `'python3' is not recognized`

対処: `copytrade_gui/package.json` の scripts が `python` を呼んでいるか確認（`python3` は Windows にない）。  
`.gitignore` に入っていないはずなのでコミット済みの最新版が Desktop Cloud に同期されていることを確認。

### 7.5 electron-builder の署名警告（無害）

症状: ビルド中に `deprecated field fields=["signingHashAlgorithms","publisherName"]` or `no signing info identified, signing is skipped`

- 前者: electron-builder 25.1.8 の deprecation 警告。`win.signtoolOptions` 配下に移動済み（コミット `ad8cb87`）
- 後者: コード署名証明書が未設定。配布物はエンドユーザー PC で「WindowsによってPCが保護されました」警告が出るが、「詳細情報 → 実行」で回避可能。商用配布するなら CSC_LINK 環境変数を設定してから再ビルド

---

## 8. ファイル & スクリプト早見表

### 7.1 ローカル（WSL）/ Desktop Cloud 共通

| パス | 役割 |
|---|---|
| `scripts/build_bacopy_engine.ps1` | PyInstaller で engine.exe ビルド |
| `scripts/copy_camoufox_assets.ps1` | `%LOCALAPPDATA%\camoufox` → `build_staging/camoufox_firefox/` に展開物コピー |
| `scripts/build_per_user.ps1` | ユーザー別 installer を一括ビルド（v0.1.0 時点で新設） |
| `copytrade_gui/scripts/provision-user-build.js` | 指定メールで `build_staging/.env` と `support_key` を生成 |
| `copytrade_gui/scripts/minify_js.py` | JS minify（build:installer 内で自動実行） |
| `copytrade_gui/package.json` | electron-builder 設定（extraResources, signtoolOptions 等） |
| `bacopy_executor_pragmatic_ws_live.py` | BET 実行本体（camoufox で Stake を操作） |
| `bacopy_engine.py` | CLI ラッパ（`executor-pragmatic` / `watch-pragmatic` / `watch-evolution` サブコマンド） |
| `requirements.txt` | Python 依存（camoufox, browserforge, tzdata, playwright, anthropic, pyinstaller）|
| `support_keys/client_key` | 全 β 共通の ED25519 秘密鍵（gitignore）|
| `support_keys/client_key.pub` | VPS に登録する公開鍵（gitignore）|
| `support_keys/admin_key` | admin が逆接続するときに使う鍵（gitignore）|
| `support_keys/port_registry.json` | メール → port 割当の永続化レジストリ（gitignore）|
| `docs/BACOPYRECEIVER_ユーザー手順書.md` | エンドユーザー向け日本語 README |
| `docs/BUILD_RELEASE_GUIDE.md` | 本ドキュメント |

### 7.2 Desktop Cloud 固有

| パス | 役割 |
|---|---|
| `C:\bacopy\` | ビルド用 git clone（source of truth, ユーザーの日常 PC ではなくここで build） |
| `C:\bacopy\venv\` | Python venv (3.14.x) |
| `C:\bacopy\copytrade_gui\node_modules\` | Electron + electron-builder |
| `C:\bacopy\copytrade_gui\build_staging\` | installer 用素材（.env, support_key, engine/, camoufox_firefox/） |
| `C:\bacopy\copytrade_gui\dist\` | 最後のビルド出力（user ごとに上書きされる） |
| `C:\bacopy\dist_per_user\` | ユーザー別 installer アーカイブ（`user01\`..`userNN\`）|
| `C:\bacopy\support_keys\` | WSL から scp で転送した鍵（provision-user-build.js が参照）|
| `C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\` | **admin 自身のインストール**（運用中の BET 実行環境）|
| `C:\BACOPY_migration_backup\` | 移行前の .env / DB / 作業スクリプトの永続バックアップ |

---

## 9. コミット履歴（本 v0.1.0 セッション関連）

| SHA | 内容 |
|---|---|
| `bfd5d10` | BET MODE selector / WS URL normalize / keep_alive randomize |
| `691627d` | camoufox+browserforge+fingerprint data を PyInstaller に同梱 |
| `ef21840` | signal panel のコード再生成ロジック修正 |
| `185b1b2` | Camoufox Firefox を installer に同梱 + first-run restore ロジック |
| `ad8cb87` | electron-builder deprecation 警告対応 (signtoolOptions) |
| `5bb2546` | per-user installer 自動化スクリプト |

---

## 10. よくある依頼パターンと対応

### 「GUI に新機能を追加したい」

1. ローカル (WSL) で `copytrade_gui/src/renderer/app.js` や `index.html` を修正
2. 動作確認（ローカルでは `electron .` で dev 実行可）
3. commit + push
4. Desktop Cloud で §2 のリビルド手順
5. slot 在庫があるならそのまま差し替え配布、なければ `build_per_user.ps1` で再生成

### 「executor のロジックを変えたい」

1. ローカルで `bacopy_executor_pragmatic_ws_live.py` を修正
2. 構文チェック: `python3 -c "import ast; ast.parse(open('bacopy_executor_pragmatic_ws_live.py').read())"`
3. commit + push
4. Desktop Cloud で §2 の Step 4 以降（engine.exe 再ビルドが必須）
5. 以下同上

### 「β ユーザーが追加されたので EXE をもう1本作って」

Desktop Cloud で:
```powershell
powershell -ExecutionPolicy Bypass -File C:\bacopy\scripts\build_per_user.ps1 -Count 1 -Start <次の番号>
```

### 「某ユーザーの PC に遠隔で入って調査したい」

1. 台帳で該当ユーザーの slot / port を確認
2. ローカルから:
   ```bash
   ssh -i ~/.ssh/laplace_vps -o ProxyJump=laplace@210.131.215.116 \
     -i support_keys/admin_key clientuser@localhost -p <PORT>
   ```
3. ProxyJump の構文: 実際には `-J laplace@210.131.215.116` を使う方が簡単
   ```bash
   ssh -i support_keys/admin_key -J laplace@210.131.215.116 clientuser@localhost -p <PORT>
   ```
4. 接続できたら、ユーザーの BACOPYRECEIVER や Camoufox 状態を調査

---

## 11. 連絡先 & 参照ドキュメント

- 本 repo: `/mnt/e/dev/Cusor/bacopy/`（WSL）/ `C:\bacopy\`（Desktop Cloud）
- 親 repo (infrastructure notes): `/mnt/e/dev/Cusor/ba/INFRA.md`
- エンドユーザー向け手順書: `docs/BACOPYRECEIVER_ユーザー手順書.md`
- admin コンタクト: goldbenchan@gmail.com

---

**このファイルを更新する責任**:  
BUILD 手順・インフラ構成・ポリシーに変更があれば、必ずこの MD を更新して commit すること。AI エージェントは新規セッションで本ファイルを先頭で読むことが期待されている。
