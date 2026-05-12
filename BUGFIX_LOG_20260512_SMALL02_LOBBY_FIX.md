# BUGFIX LOG 2026-05-12 — SMALL02 稼働確認 & ロビー遷移修正

## 概要

SMALL02モード ($0.20 刻み) の本番稼働確認と、AUTO BET の安定化およびロビー遷移バグの修正。

---

## 1. SMALL02モード 動作確認

### 確認内容
- `executor_id: warakaji` / `user02` 両方で `mode=small02` 稼働確認
- `bet_amount=$0.20`, `unit_idx=0`, `chip_base=1.0` — float截断バグ修正済み (前回 `int(0.2)=0` → `float(0.2)=0.20`)
- `cum_profit_usd` の利益計算も正常

### 確認コマンド (VPS DB)
```sql
SELECT executor_id, json_extract(seq_json,'$.mode'), json_extract(seq_json,'$.bet_amount')
FROM executors WHERE executor_id IN ('warakaji','user02');
-- small02 | 0.2
```

---

## 2. AUTO BET 途中停止バグ — 調査と対応

### 症状
AUTOボタンでBETするが、しばらくすると止まる。

### 原因
VPS の `bacopy_master_ui.py` が旧バージョンのままで未デプロイだった。

### 試みた修正 (後にリバート)

以下の3つの修正を `bacopy_master_ui.py` に加えたが、それぞれ副作用が発生したためリバートした。

| 修正 | 意図 | 副作用 |
|---|---|---|
| `betCompleted` 後に即時 `_autoBetSendNext()` | LOOK後の再開遅延解消 | `done→error` 二重送信が発生 |
| `lastBetWindowOpen = betCompleted ? false : anyBetWindowOpen` | 二重送信防止 | 3〜7分の長いギャップが発生 |
| `agg.totalBets` を SUM→MAX 集計 | 複数executor重複betCompleted防止 | 動作がさらに不安定に |

### 最終対応
**3修正をすべてリバート**して元のコードに戻した。

```diff
- a.totalBets = Math.max(a.totalBets, Number(g.total_bets) || 0);
+ a.totalBets += Number(g.total_bets) || 0;

- _autoBet.lastBetWindowOpen = betCompleted ? false : anyBetWindowOpen;
- if (lookTimedOut && !outstanding && anyBetWindowOpen) { _autoBetSendNext(); }
+ _autoBet.lastBetWindowOpen = anyBetWindowOpen;
```

リバート後、BETは28〜38秒ごとの正常間隔で連続動作を確認。

### 教訓
- `agg.totalBets` は複数executorのSUM — 1ラウンドで2回 `betCompleted` が発火する設計は把握済みで元コードで問題なく動作していた
- 元コードの「LOOK後1ラウンド遅れ」は軽微で許容範囲内

---

## 3. ロビー遷移バグ修正

### 症状
BET中にちょこちょこロビーに出て、またテーブルに戻る挙動が発生。

### 原因
`bacopy_executor_pragmatic_ws_live.py` に以下の監視ロジックが存在：

```python
BACOPY_STAKE_BALANCE_NUDGE_SEC = 300  # デフォルト5分
# Stake 残高 GraphQL subscription が5分以上無音 → recover_session() → ロビーへ移動
if _stake_silence > _stake_nudge_sec and not _bet_win_active:
    recover_session(f"stake_balance_silent {int(_stake_silence)}s")
```

Stake の残高WebSocket購読は定期的に5〜10分の無音期間があるため、5分おきにロビー遷移が発生していた。

### 修正
両PCの `.env` に以下を追加：

```env
BACOPY_STAKE_BALANCE_NUDGE_SEC=0
```

`=0` でこのトリガーが完全に無効化される。BET自体はゲームWSで動作するため影響なし。
残高表示はBETのたびに更新されるため実用上問題なし。

### 変更ファイル
- `bafather Desktop Cloud (162.43.83.54)`: `C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources\.env`
- `hashimoto PC (user02)`: 同パス

### 変更方法
SSH tunnel (VPS port 2277 → hashimoto, xserver_key → bafather) 経由で直接変更。

---

## 4. 最終確認結果 (2026-05-12 11:46 UTC)

| 項目 | warakaji | user02 |
|---|---|---|
| mode | small02 | small02 |
| bet_amount | $0.20 | $0.20 |
| recovering | false ✅ | false ✅ |
| recovering_reason | (空) ✅ | (空) ✅ |
| bettable | 1 ✅ | 1 ✅ |
| total_bets | 5 | 5 |
| W/L/T | 5/0/0 | 5/0/0 |
| cum_profit | $1.00 | $1.00 |
| should_reset | false ✅ | false ✅ |

BET間隔: 31〜34秒（正常）  
ロビー遷移: なし ✅  
done→error 二重送信: なし ✅  

---

## 5. デプロイ状況

| ファイル | 場所 | 状態 |
|---|---|---|
| `bacopy_master_ui.py` | VPS `/opt/bacopy/` | リバート済みデプロイ完了 |
| `bacopy_executor_pragmatic_ws_live.py` | EXE内 (前回ビルド済み) | SMALL02/float修正/blank-screen watchdog 含む |
| `.env` (warakaji) | bafather Desktop Cloud | `NUDGE_SEC=0` 追加済み |
| `.env` (user02) | hashimoto PC | `NUDGE_SEC=0` 追加済み |
| `copytrade_gui` renderer | EXE内 | small02/small3 オプション含む |

---

## 6. 残課題

- `bacopy_master_ui.py` のBug1（LOOK後1ラウンド遅れ）は未修正のまま（元コードの挙動）
  - P4連続でLOOKが発動した後、次のラウンドの窓が開いてから初めてBET再開
  - 実害は「1ラウンドスキップ」のみで許容範囲内と判断
