# Stable Release: v1.0.0-copytrade-beta1

**Tag**: `v1.0.0-copytrade-beta1`  
**Commit**: `f1e5dc1`  
**Date**: 2026-04-24  
**Build**: Desktop Cloud `bacopy_engine.exe` 17:55 / Electron exe 17:59

---

## ⚠️ 重要：何か問題が起きたらここに戻る

```bash
git reset --hard v1.0.0-copytrade-beta1
```

この安定板は **user01 オンボーディング完了・SWITCH_TABLE 同期確認済み** の状態です。
新機能追加・実験的修正を行う前に必ずこのタグを確認してください。

---

## この安定板で確認済みの動作

| 機能 | 状態 |
|------|------|
| admin GUI → ARMED | ✅ |
| user01 GUI → ARMED（初回ロビーフィルター自動解除） | ✅ |
| SWITCH_TABLE → admin・user01 同時移動 | ✅ |
| BET 信号配信（long-poll 同時通知） | ✅ |
| SSH サポートトンネル（GUI 独立・再起動後も継続） | ✅ |
| 複数 GUI 起動防止（シングルインスタンスロック） | ✅ |
| 無操作モーダル自動クリック | ✅ |

---

## 安定板ビルド構成

### Desktop Cloud
- リポジトリ: `C:\bacopy` (ba_pragmatic)
- エンジンバイナリ: `copytrade_gui/build_staging/engine/bacopy_engine.exe`
  - ビルド日時: 2026-04-24 17:55
  - サイズ: 68,262,673 bytes
- Electron installer: `dist_per_user/user01/BACOPYRECEIVER_user01_Setup_0.1.0.exe`
  - ビルド日時: 2026-04-24 17:59
  - サイズ: 608,456,901 bytes

### user01 マシン（直接パッチ済み）
- `resources/engine/bacopy_engine.exe` → 17:55 ビルド
- `resources/app.asar` → シングルインスタンスロック + executor_id 優先ロジック
- `resources/.env` → 全必須キー + `BACOPY_EXECUTOR_ID=user01`

---

## 主要修正サマリー（user01 オンボーディングで発見・修正）

### 1. Pragmatic ロビー カテゴリフィルター自動解除
**ファイル**: `bacopy_executor_pragmatic_ws_live.py`  
**問題**: 新規プロファイルで Pragmatic ロビーを開くと「日本語スピードバカラ」カテゴリが
デフォルト選択され、Speed Baccarat 6 等が見えずエンジンがフッターまでスクロールして停止。  
**修正**: `_LOBBY_TRY_CLICK_JS` に Pre-pass 0 を追加。アクティブなフィルターを検出して
× ボタン / All タブ / toggle-off の順で自動解除。

### 2. SWITCH_TABLE の受け子への伝達問題
**ファイル**: `bacopy_executor_pragmatic_ws_live.py`  
**問題**: admin のエンジンが SWITCH_TABLE decision を先に `done` にすると、
受け子エンジンが `pending` ポールで取得できず移動しない。  
**修正**: ARMED ループに 60 秒ごとの master テーブル同期を追加。
`done` 履歴の最新 SWITCH_TABLE と現テーブルを比較し、差異があれば自動移動。

### 3. シングルインスタンスロック
**ファイル**: `copytrade_gui/src/main.js`  
**問題**: アイコンダブルクリックで複数の GUI インスタンスが起動し、
エンジンも複数立ち上がって競合。  
**修正**: `app.requestSingleInstanceLock()` を追加。2 つ目以降は既存ウィンドウにフォーカス。

### 4. ゼロタッチオンボーディング
**ファイル**: `copytrade_gui/scripts/setup-all.ps1`, `provision-user-build.js`, `main.js`  
**修正**:
- `setup-all.ps1`: 全日本語テキストを英語化（文字コードエラー解消）
- `provision-user-build.js`: `BACOPY_API_KEY` / Supabase キー / `BACOPY_EXECUTOR_ID` を自動注入
- `main.js` `buildSpawnSpec`: `.env` の `BACOPY_EXECUTOR_ID` が localStorage より優先
- Phase 6: SSH サポートトンネルを Task Scheduler に登録（GUI 独立）

---

## user02 以降のオンボーディング手順

```bash
# 管理者側（Desktop Cloud）
node copytrade_gui/scripts/provision-user-build.js user02@example.com
# → build_staging/.env に全キー自動注入

powershell -File scripts/build_per_user.ps1 -Start 2 -Count 1
# → dist_per_user/user02/BACOPYRECEIVER_user02_Setup_0.1.0.exe
```

ユーザー側の手順:
1. exe をダウンロード → インストール（ダブルクリック）
2. GUI 起動 → 「INSTALL ON THIS PC」ボタン押す（OpenSSH + SSH トンネル 全自動）
3. camoufox で Stake Casino にログイン（一度だけ）
4. スタートボタン押す → 自動で ARMED

**管理者の PowerShell 手動作業: ゼロ**

---

## 接続情報

| 項目 | 値 |
|------|-----|
| Master URL | `https://master.bafather.uk/master` |
| VPS | `laplace@210.131.215.116` |
| Desktop Cloud | `Administrator@162.43.83.54` |
| user01 トンネルポート | `2234` |
| user01 executor_id | `kajigui`（localStorage 値） |
| user01 email | `hikata26621146@gmail.com` |

---

## 既知の制限事項

- **exe 配布**: 608MB のため Google Drive / OneDrive 等での共有が必要
- **Stake Casino 初回ログイン**: ユーザー本人が camoufox で行う必要あり
- **executor_id 表示**: user01 は localStorage の `kajigui` が使われる（機能に影響なし）
