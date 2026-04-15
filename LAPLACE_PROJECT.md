# LAPLACE Project — 開発者向けリファレンス

## 1. システム概要

バカラ自動BETシステム。〇✖ロジック（MaruBatsu）に基づき Stake.com のEvolutionバカラテーブルに自動でBETする。

```
[クラウドデスクトップ / ローカルPC]          [VPS: 210.131.215.116]
  LAPLACE.exe (Electron GUI)
    ↓ IPC
  agent_api.py (Python エージェント)
    ↓ SSH トンネル (127.0.0.1:8000)  ←→  laplace-api.service (FastAPI)
    ↓                                       ↑ MaruBatsuロジック
  Camoufox ブラウザ (可視)                laplace-collector.service
    ↓                                       (62テーブル データ収集)
  Stake.com (実際にBETクリック)
```

---

## 2. リポジトリ構成

| リポジトリ | 用途 | 公開設定 |
|---|---|---|
| `jinjinsansan/ba` | ソースコード全体 | **Private** |
| `jinjinsansan/laplace-releases` | GUIリリース配信専用 | **Public** |
| Vercel (web/) | 管理パネル・ランディングページ | Public |

---

## 3. ディレクトリ構成

```
ba/
├── agent_api.py          # メインエージェント (BETループ、watchdog、session check)
├── executor.py           # BET実行 ($500チップ、11秒制限、partial BET対応)
├── marubatsu_bet.py      # 〇✖ロジック BETセッション
├── laplace_client.py     # VPS API クライアント (selector_config連携)
├── laplace_api.py        # VPS上で動く FastAPI (selector_config受け取り)
├── table_selector.py     # テーブル選定ロジック (閾値パラメータ化済み)
├── game_ws.py            # WebSocket監視 (last_message_at でfreeze検知)
├── scraper.py            # Camoufox / Playwright ブラウザ操作
├── .env                  # 認証情報 (下記参照)
├── gui/
│   ├── src/
│   │   ├── main.js       # Electron メインプロセス
│   │   ├── preload.js    # IPC ブリッジ
│   │   └── renderer/
│   │       ├── index.html
│   │       ├── app.js
│   │       └── styles.css
│   ├── setup.bat         # ユーザー初回セットアップ用
│   ├── package.json      # version番号はここで管理
│   └── dist/win-unpacked/  # ビルド成果物
└── web/                  # Next.js 管理パネル (Vercel)
    ├── src/app/
    │   ├── page.tsx          # ランディングページ
    │   ├── dashboard/        # ユーザーダッシュボード
    │   ├── admin/            # 管理パネル
    │   └── api/              # API routes
    └── supabase/schema.sql
```

---

## 4. .env 設定

```env
STAKE_USERNAME=lselfloveself@gmail.com
STAKE_PASSWORD=040505Aoi

ADMIN_BOT_TOKEN=8778891506:AAFAY9OFLVzULpP0sUdXOQMh2ioFru1cvw8
ADMIN_CHAT_ID=197618639

LAPLACE_MODE=normal
LAPLACE_USER=dev-machine          # マシン識別子 (XserverなどはXserver-vpsなどに変更)

STAKE_API_TOKEN=...
ANTHROPIC_API_KEY=...

# VPS接続
LAPLACE_USE_REMOTE=1
LAPLACE_API_URL=http://127.0.0.1:8000
LAPLACE_API_KEY=c6gDoe0xIyBOTQ7bvzRaAHNYn4ZE1W9Mriumqkw8Shf5Jlsd
LAPLACE_SSH_HOST=laplace@210.131.215.116
LAPLACE_LOCAL_PORT=8000
LAPLACE_REMOTE_PORT=8000

# LAPLACE_FORCE_DRYRUN=1  ← コメントアウト済み (実BET有効)
```

---

## 5. GUIビルド手順

```powershell
# gui/ ディレクトリで実行
cd E:\dev\Cusor\ba\gui

# 初回または node_modules 壊れた場合
cmd /c "npm install"   # 5分程度かかる

# ビルド (win-unpacked を生成)
$env:CSC_IDENTITY_AUTO_DISCOVERY = "false"
node node_modules\electron-builder\out\cli\cli.js --dir --config.win.signAndEditExecutable=false
```

成果物: `gui/dist/win-unpacked/LAPLACE.exe`

---

## 6. VPS デプロイ手順

ローカルで `laplace_api.py` または `table_selector.py` を変更した場合は必ずVPSに反映する。

```powershell
# ファイルをVPSに転送
scp -i "$env:USERPROFILE\.ssh\laplace_vps" laplace_api.py laplace@210.131.215.116:/opt/laplace/laplace_api.py
scp -i "$env:USERPROFILE\.ssh\laplace_vps" table_selector.py laplace@210.131.215.116:/opt/laplace/table_selector.py

# サービス再起動
ssh -i "$env:USERPROFILE\.ssh\laplace_vps" laplace@210.131.215.116 "sudo systemctl restart laplace-api"
```

---

## 7. ユーザーへの配布手順

### 初回配布 (ZIP)

ZIPに含めるもの:
```
LAPLACE/
├── (gui/dist/win-unpacked/ の中身をすべて)
├── setup.bat           ← gui/setup.bat
└── laplace_vps         ← SSH秘密鍵 (~/.ssh/laplace_vps)
```

ユーザーの操作:
1. ZIP解凍
2. `setup.bat` をダブルクリック → SSH鍵が `%USERPROFILE%\.ssh\laplace_vps` に配置される
3. `LAPLACE.exe` を起動

### アップデート配信

1. コードを修正・ビルド
2. `gui/package.json` の `"version"` を上げる (例: `1.0.0` → `1.1.0`)
3. `jinjinsansan/laplace-releases` リポジトリに GitHub Release を作成
   - タグ: `v1.1.0`
   - 添付ファイル: 新しい `win-unpacked/` のZIP
4. ユーザーのGUIが次回起動時に自動検知 → 上部バナーに「新バージョン利用可能」表示
5. ユーザーが「ダウンロードページを開く」をクリック → ブラウザでリリースページが開く

---

## 8. VPS サービス

| サービス | 内容 | 確認コマンド |
|---|---|---|
| `laplace-api` | FastAPI (MaruBatsuロジック) | `systemctl status laplace-api` |
| `laplace-collector` | 62テーブルデータ収集 | `systemctl status laplace-collector` |

VPS情報:
- ホスト: `laplace@210.131.215.116`
- SSH鍵: `~/.ssh/laplace_vps`
- コード: `/opt/laplace/`

---

## 9. 主要な実装済み機能

### BET実行 (executor.py)
- `$500` チップ対応 (`_calc_chip_plan`)
- BETウィンドウ 11秒タイムリミット
- 部分BET検出・補正
- 残高取得 3回リトライ

### 障害対応 (agent_api.py)
- **ブラウザフリーズ検出**: WS無活動90秒 → ページリロード → テーブル再入室
- **Stakeセッション切れ**: 5分おき `_is_logged_in()` チェック → 自動再ログイン
- **SSH トンネル切断**: 自動再接続 (5秒後、Bot稼働中のみ)
- **STOP→START競合**: 旧プロセスのcloseイベント待ち後に再起動

### テーブル選定 (GUI ↔ VPS)
- GUI「TABLE FILTER」タブの設定値 → `selector_config` として VPS API に送信
- VPS 側で `table_selector.py` が閾値に使用
- 設定はアカウントメールをキーに Supabase (`billing.bot_config`) に保存・同期

### 管理パネル (web/ - Vercel)
- ユーザー管理・課金確認・ZIP配信
- 紹介URL・コミッション管理・出金申請
- Telegram通知 (出金申請時など)

---

## 10. クラウドデスクトップ推奨設定

日本KYC済みアカウントで運用する場合は**日本IPのクラウドデスクトップ**を使用すること。

| サービス | IP | 推奨度 |
|---|---|---|
| Xserver VPS (Windows) | 日本DC | ◎ KYCと一致 |
| ConoHa VPS (Windows) | 日本DC | ◎ KYCと一致 |
| Shadow PC | フランス | △ KYC不一致リスク |
| AWS WorkSpaces | 海外DC | △ KYC不一致リスク |

Xserver VPS での `.env` 変更点: `LAPLACE_USER=xserver-vps`

---

## 11. 注意事項

- `LAPLACE_FORCE_DRYRUN=1` が有効だとデモモードになる。実BET時は必ずコメントアウト確認
- VPS の laplace-collector メモリ使用量が 1.5GB/2GB 付近 → 要監視
- `gui/dist/win-unpacked/` は git 管理外 (ビルドの都度生成)
- `node_modules/` は git 管理外 (`npm install` で再生成)
