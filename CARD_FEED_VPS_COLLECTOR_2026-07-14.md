# カード収集VPS移管 + テレグラム下限バンド指標検証 — 2026-07-14

セッション記録。前半=テレグラムのドライラン「累計%が下限で反発/上限で反落」の検証、後半=カード収集をbafather→VPS(dgaロビーWS)へ移管し常駐化。

---

## Part 1. テレグラム下限バンド指標の検証（結論: 無エッジ・遅行指標）

### 発端
- ユーザー観察: テレグラムのドライラン「%が一定の下限に来たら上昇、上限に来たら下降」を2ヶ月継続確認。
- 打ち手・梶原さんが「下限に張り付くと上限しやすい」を1つのインジケーターとして使っている。

### 検証方法
- VPS分析DB `analytics_pragmatic_vps.sqlite3`（46,876シュー・Apr15以降）に **v3の6パターン**（`live_signal_for_history`実ロジック）を適用し、チャンネルと同じ**シグナル勝敗列4,408手**を再生。
- スクリプト: scratchpad `band_test.py` / `dump_stream.py` / `kajiwara_rule.py`。

### 結果①: バンド位置→前向き勝率（band_test.py）
| | floor/ceil | 下限付近の後続200手勝率 | 上限付近 | 差 |
|---|---|---|---|---|
| 実データ(時系列) | 50.17/53.08% | 54.9% | 51.9% | +2.9pt |
| シャッフル#1(順序破壊) | 50.27/53.93% | 61.0% | 50.3% | +10.8pt |
| シャッフル#2 | 49.73/52.48% | 54.2% | 52.3% | +1.9pt |
| シャッフル#3 | 49.34/52.60% | 52.9% | 52.3% | +0.6pt |

→ **時間構造を完全に破壊したシャッフルでも同じ(むしろ大きい)効果**が出る＝「反発」はrunning min/maxに対する位置で測ることが生む**数学的アーティファクト**。本物の時間構造エッジなら順序破壊で消えるはず。

### 結果②: 梶原ルールの直接検定（kajiwara_rule.py）
- **[1] 往復は実在**: 下限張り付き→300シグナル以内に上限到達 = 全期間13/13=100%。**梶原さんの観察は正確**。ただし中央からでも92.6%到達＝下限の特権でない。**後半(成熟バンド)では下限→上限到達は6.2%に崩壊**（往復は若い狭バンドの性質）。
- **[2] 賭けた手の実現勝率（決定打）**:
  | 局面 | 実現勝率 |
  |---|---|
  | 下限に張り付き中 | **26.4%**(全期間n=53) / **47.1%**(後半n=848) |
  | 上限に張り付き中 | 55.1% / 87.5% |
  | 基準 | 52.1% |
  → 累計が下限なのは**今負けている最中だから**。「下限で入る」=冷えた真っ最中に賭ける＝基準を下回って負ける。戻りは冷えが終わった**後の別の手**が押し上げるだけで事前に賭けられない。**遅行・同時指標であって先行指標でない**。シャッフルでも同じ勾配。

### 結論
- 累計%が下限→上限へ戻る**動き自体は本物**（梶原さんの目は正確）。
- しかし賭けに使うと**逆効果**（下限エントリー=最も冷えた手にタイミングを合わせに行く）。エントリー根拠にはしない方が本人の利益。
- v3全体の素の+2%エッジは本物（floorが全期間50%割れず）。だが**一定**で、バンド位置からの上乗せは取れない。
- 関連メモリ: `wave-switch-no-edge-2026-07-11` / `streak-no-prediction`。

---

## Part 2. カード収集をVPSへ移管・常駐化

### 状況訂正（重要）
- bafather `C:\bacopy\_cdp_card_collector.py`(:9222 CDP方式)は**常駐できておらず、07-12に約16秒/151行だけ収集して停止**したまま放置。想定の76MB/16万ハンドは未達。
- bafatherは現在「休憩」中（GUI/エンジン/賭けChrome全停止）。**未払いロックダウン(7/14)とは無関係**とユーザー明言。

### ★大発見: dga gameResult に配牌フルが含まれる
`wss://dga.pragmaticplaylive.net/ws` の `gameResult` 各ハンドに:
```json
{"time":"..","player":4,"banker":3,"winner":"PLAYER_WIN","gameId":"..",
 "playerCards":["9S","5S","JC"],"bankerCards":["6H","4C","3S"]}
```
→ **ナチュラル(8/9)・ペア・点数・3枚目逆転9 は全てここから導出可能**。multibaccaratクライアント不要。`[[card-feed-discovery-2026-07-12]]`(bafather :9222方式)を上位互換で置換。

### 技術的切り分け（診断: scratchpad diag_lobby.py / diag_dga.py）
- ヘッドレスcamoufox+hakudasama cookieで**ロビーに認証到達OK**。
- `page.on("websocket")` は dga WS を確実に捕捉（3本: stake/intercom/dga）。
- **`route_web_socket("**")` はこのVPS camoufoxビルドで0本しか傍受できない**（当初のOption2設計は不可）→ dga方式(page.on)に切替。
- ロビーには `/desktop/lobby2/` フレームのみ、`/desktop/multibaccarat` は無し（カード用クライアントはロビーだけでは繋がらない。だがdgaに配牌が来るので不要）。

### 稼働構成（VPS 210.131.215.116 / laplace ユーザー）
| 項目 | 内容 |
|---|---|
| systemd | `bacopy-card-collector.service`（enabled + active・Restart=always・User=laplace） |
| 実行 | `/opt/bacopy/.venv/bin/python /opt/laplace2/card_collector.py --headless` |
| ソース | repo `_vps_card_collector.py`（.gitignore対象→`git add -f`） |
| 方式 | dgaロビーWSを **読み取り専用**傍受（BET送信ゼロ・`page.on("websocket")`+`framereceived`） |
| アカウント | **hakudasama(入金0)** = `/opt/laplace2/monitor/auth_state_pragmatic_collector/stake_cookies.json`（既存2セッションと共用）・専用プロファイル `/opt/laplace2/auth_state/camoufox_profile_cardfeed`。bafather(lselfloveself)と別アカ=kick無し |
| 出力 | `/opt/laplace2/card_feed.jsonl`・1行/ハンド・(tableId,gameId)で重複排除・**約16万ハンド/日** |
| ローテ | `/etc/logrotate.d/bacopy-card-feed`（daily・rename+`systemctl restart`・21世代）＋800MBガード |
| ログ | `/opt/laplace2/card_collector.log` |

**出力フォーマット**:
```json
{"k":"r","ts":..,"tb":tableId,"tn":tableName,"g":gameId,"win":"P|B|T",
 "ps":playerScore,"bs":bankerScore,"pc":[配牌],"bc":[配牌],"t":"時刻"}
```

**★データ注意**: dgaには標準バカラ以外の変種も混在（例 "Seotda Baccarat"・カード表記`016`等）。**標準バカラのみ抽出**して分析（カード表記が `^[0-9JQKA][SHDC]$`・実測96%が標準）。tn="" のハンドも tb(tableId) は有り。

### 安全性・可逆性
- **既存ファイルは一切改変していない**（新規 `_vps_card_collector.py` + 新systemdユニット + 新logrotate設定のみ）。
- 復元点: **git tag `pre-card-collector-vps-20260714`**（commit `00ed4fb`）。
- コミット: `d6d3d33`(初版route_web_socket) → `d0f24f5`(dga方式へ書き直し)。branch `feat/engine-heartbeat`。

### 撤去手順（revert）
```bash
sudo systemctl disable --now bacopy-card-collector
sudo rm /etc/systemd/system/bacopy-card-collector.service \
        /etc/logrotate.d/bacopy-card-feed \
        /opt/laplace2/card_collector.py
sudo systemctl daemon-reload
# repo側: git checkout pre-card-collector-vps-20260714 -- .  (または _vps_card_collector.py 削除)
```

---

## 次のステップ（1日蓄積後・「カードの分析して」で実行）
1. **ナチュラル(8/9)のクラスタリング**: 発生が独立か、直前ハンドと相関するか（ユーザー本命「波=ナチュラル多発」仮説）。
2. **残存カウント→次手予測**: 簡易ハイロー等が次手P/B/Tieを基準超で予測するか。
3. **逆/順選択器のforward OOS**: ①②をエントリー選択に変換し、52%超が残るか。
- 標準バカラのみ抽出必須。VPS decision(dl_v4/dl_vps)と `tb`+`gameId` でjoin可能。
- 関連: `[[project_wave_switch_no_edge_2026-07-11]]`（勝敗系列の波は否定済・カード構成は未検証の新情報）。
