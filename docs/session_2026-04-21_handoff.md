# bacopy セッション引継ぎ — 2026-04-21

## 1. デスクトップクラウド SSH 接続情報

### 接続先
- **クラウドPC名**: bafather
- **IP アドレス**: `162.43.83.54`
- **OS**: Windows Server 2022 Datacenter
- **スペック**: CPU 4コア / メモリ 8GB / NVMe SSD 300GB
- **UUID**: `c60939bf-3045-43ad-9a4d-9902875f1c38`
- **収容ホスト**: host02-2
- **契約**: 通常 (プラン: スタンダード)

### SSH 接続
- **ユーザー**: `Administrator`
- **秘密鍵**: `~/.ssh/laplace_vps` (同じ鍵で VPS と Desktop Cloud 両方接続可)
- **ポート**: 22

### 接続コマンド
```bash
ssh -i ~/.ssh/laplace_vps -o StrictHostKeyChecking=no Administrator@162.43.83.54
```

### 注意 (PowerShell シェル経由)
- Desktop Cloud の default shell は PowerShell
- コマンド連結は `&&` 不可、`;` を使う
- Linux の `ls` は動くが、複雑なパスは `C:/Users/...` 形式で forward slash 推奨

### ファイル配置 (2026-04-21 終盤時点)
```
C:\Users\Administrator\Desktop\
├── .env                                 # Supabase + BACOPY_API + SUPPORT 設定
├── bacopy_executor_pragmatic_ws_live.py # 最新 executor (v0.5.0-bet-ux)
├── bacopy_db.py
├── marubatsu_strategy.py
└── copytrade_gui\
    ├── src\          # Electron main + renderer (最新コミット)
    ├── build_staging\ # .env, support_key, admin_pubkey.txt, engine\
    ├── node_modules\ # 転送済 (npm install 不要)
    ├── dist\         # 古い win-unpacked (4/18) あるが無視. npm start で最新動作
    └── scripts\
```

### 既インストール環境
- Python 3.14.4
- Node.js 25.9.0
- Git 2.53.0.2
- pip パッケージ: `playwright 1.58.0`, `camoufox 0.4.11`, `requests 2.33.1` 等
- OpenSSH Server (認証: `administrators_authorized_keys` に `laplace-vps-droid` 鍵登録済)

### GUI 起動手順 (RDP で実行)
```powershell
cd C:\Users\Administrator\Desktop\copytrade_gui
npm start
```
→ SIGN IN (`goldbenchan@gmail.com`) → START → Camoufox 起動 → Stake ログイン → Pragmatic lobby

### SSH セッションから GUI を起動しない
- SSH セッションと RDP セッションは別 Windows session
- SSH 経由で `Start-Process` した electron ウィンドウは RDP 画面に表示されない
- 必ず RDP の PowerShell で `npm start`

---

## 2. 2026-04-21 の成果

### タグ
`v0.5.0-bet-ux` (Current HEAD: `09ba668`)

### 主要改善
| # | コミット | 効果 |
|---|---|---|
| 1 | `5c321f8` feat: qpid-based lobby click | 誤爆ゼロ (Baccarat 1→10 等の取り違え不可能) + virtual scroll 対応 (wheel + PageDown + scrollTo + 進捗検知) |
| 2 | `b44ec6a` perf: optimistic BET ack | `ws_send OK` 瞬時に `ack.bet_confirm` を非同期 POST → Master UI "✓ BET確定" を 500ms-2s 早く表示 |
| 3 | `02ef260` perf+ux: BET visibility | `⏳ BET 窓待機中...` `📤 BET 送信中...` `✓ BET sent (Xms)` と各 stage 明示 |
| 4 | `abd6891` fix: BET hang on session reload | `wait_bets_open` の 20s timeout が page.reload で無限 hang する事故に対し hard_deadline (wall-clock) 導入 |
| 5 | `c1f0de9` feat: visual result delay | Master UI に "結果遅延 [N] s" 入力 (default 4s). 🏆/❌ flash を映像タイミングに同期. 内部処理は即時 |
| 6 | `d30663d` fix: dismiss session modal in waits | `wait_bets_open` / `wait_bet_confirm` / `wait_result` の各 predicate で modal を 200-250ms 周期 dismiss. 以前は 10-15s 放置 |
| 7 | `317c38e` fix: captured_at supersede ordering | `_peek_new_switch_decision` の時刻比較を `received_at` → `captured_at` に. Fetcher 再フェッチで `received_at` が新しくなり古い決定が "newer" 誤判定される bug 解消 |
| 8 | `b4d6daf` ui: replace LOOK with BANKER | LOOK ボタン撤去, BANKER 常時表示 |
| 9 | `3372b68` feat: BANKER default + red | BC code 0/1/2 を業界標準として常時デフォルト. sniff 不要. BANKER ボタン `#ff3344` 赤, PLAYER `#00bfff` 青 |
| 10 | `09ba668` ui: remove 4 checkboxes | `Allow SWITCH_TABLE / Allow BANKER / Allow TIE / Assume bc 0/1/2` を UI 撤廃. ハードコード (SWITCH_TABLE/BANKER/bc012=true, TIE=false) |

### VPS デプロイ済
- `/opt/bacopy/bacopy_master_ui.py` を v0.5.0 版に更新
- `bacopy-api` systemd サービス再起動済
- ブラウザ Ctrl+F5 で Master UI 最新版反映される状態

### Desktop Cloud へのデプロイ済
- `copytrade_gui/` 全部 (node_modules 込み)
- executor 3 ファイル (`bacopy_executor_pragmatic_ws_live.py`, `bacopy_db.py`, `marubatsu_strategy.py`)
- `Desktop/.env` (Supabase + BACOPY_API + SUPPORT 設定)

### 実測 diag
- qpid 選択 click 成功率: **5/5** (Baccarat 1/2, Speed Baccarat 6, Speed Baccarat 1, + 初期起動)
- 全て `via=selector` で一発命中 (scroll / カテゴリ brute-force 到達なし)

---

## 3. 次回作業 (2026-04-22 〜)

### メインテーマ: **AI 学習システムの調整**

#### 背景
- bacopy プロジェクトのピボット: 自動戦略の "edge なし" 判明 (2026-04-15〜17) → 友人代打ちシステム + ML 学習パイプライン
- Master UI で admin が手動 BET 判断 → decision_log.jsonl に記録
- 目標 5000 BET サンプルで ML モデル学習

#### 優先すべき調整項目 (想定)
1. **decision_log 契約の検証**: すべての decision event に必要フィールドが揃っているか (memory: `project_decision_log_contract.md`)
2. **ack event / result event の JSONL 追記確認**: `append_ack_event` / `append_result_event` が正常動作しているか (ログ欠損ないか)
3. **学習用データ前処理**: snapshot.statistics / last_hand / shoe_summary から特徴量抽出
4. **特徴量エンジニアリング**:
   - 大路 / ビーズロード → tensor 化
   - 連続勝敗パターン (streak)
   - 桁別 tereko (ba/ の研究成果流用)
5. **モデル候補**:
   - LightGBM / XGBoost (構造化データ向け)
   - TabNet (深層学習 + 解釈性)
6. **検証**: walk-forward validation (時系列 CV)

#### 既存材料 (ba/ から流用可能)
- `ba/scripts/daily_learning.py` — LAPLACE 側の学習パイプライン (雛形)
- `ba/backtest_*.py` — バックテスト基盤
- `ba/ai_pattern_analysis.py` — パターン分析
- `ba/_analyze_*.py` — diag スクリプト群

#### 今の BET サンプル数 (確認必要)
- `data/decisions.jsonl` を wc -l で計数
- 目標 5000 に対し何 % か

#### 次回チャット冒頭でやること
1. SSH で Desktop Cloud 接続確認 (動作チェック)
2. VPS の `/opt/bacopy/data/decisions.jsonl` の件数確認
3. 最新 BET/SWITCH データ型の確認
4. AI 学習の優先度決定 (user と相談)

---

## 4. 引継ぎ用クイックコマンド

### VPS (master API)
```bash
ssh -i ~/.ssh/laplace_vps laplace@210.131.215.116
sudo systemctl status bacopy-api
sudo tail -n 100 /var/log/syslog | grep bacopy-api
ls -la /opt/bacopy/
```

### Desktop Cloud
```bash
ssh -i ~/.ssh/laplace_vps Administrator@162.43.83.54
# PowerShell なので ; 区切り
cd C:/Users/Administrator/Desktop/copytrade_gui; npm start
```

### ローカル (開発機)
```bash
cd E:/dev/Cusor/bacopy
git log --oneline -20
git tag | tail -5
# 最新タグ: v0.5.0-bet-ux
```

### 直近 decision 確認
```bash
curl -s -H "Authorization: Bearer fy6FNK8pM5xqGrTEG_HP38yqIgO3b6QrnkUPctCAgDA" \
  "https://master.bafather.uk/api/decisions?status=done&limit=20" | \
  python -c "import sys, json; d=json.load(sys.stdin); \
    [print(f\"{x.get('captured_at','')[:19]} | {x.get('friend_action',{}).get('action','')} | {x.get('table_name','')}\") \
     for x in sorted(d.get('decisions',[]), key=lambda x: x.get('captured_at','') or '', reverse=True)[:15]]"
```
