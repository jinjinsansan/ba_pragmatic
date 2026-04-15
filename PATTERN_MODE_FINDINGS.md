# Pattern Mode 開発日誌 (Test → Production)

## 目的

`pattern_test` モードで Pattern モード戦略を実 BET ($1 固定) で検証しながら、
発見した改善点を記録し、検証完了後に本番 `pattern` モードへマージする。

## ワークフロー

```
1. pattern_test モードで動かす
   ↓
2. 動作・違和感を観察
   ↓
3. test モード内で修正・検証
   ↓
4. このファイルに記録 (修正前 → 修正後 → コミットハッシュ)
   ↓
5. 検証十分 → 本番 pattern モードに移植
   ↓
6. 本番モード再検証
```

---

## 既に test モードに実装済の改善点

| # | 改善 | コミット | 本番マージ | 説明 |
|---|---|---|---|---|
| 1 | bead road 直読み | `9fd6af1` | ⭐ 強く推奨 | balance/WS の timing 問題を回避。0.5秒間隔ポーリングで結果検知 ~1秒以内 |
| 2 | Tie 正確判定 | `9fd6af1` | ⭐ 強く推奨 | bead road の 'T' 文字で確実に判定。balance diff の Tie 誤判定を解消 |
| 3 | 結果フラッシュ即時 | `9fd6af1` | ⭐ 推奨 | 結果が画面に出た瞬間 GUI フラッシュ (従来は 5秒遅延) |

---

## 検証ログ

### セッション #1 (2026-04-11)
- **モード**: pattern_test
- **目的**: bead road 方式の動作確認
- **観察**:
  - 即時フラッシュが想定通り動作
  - Tie 誤判定が解消
- **発見**: なし (正常動作)

### セッション #2 (未実施)
記録待ち...

---

## 発見した問題と修正

このセクションは観察ベースで追記する。テンプレート:

### [発見 #N] タイトル
- **発見日**: YYYY-MM-DD
- **モード**: pattern_test
- **症状**: 何が起きたか
- **再現条件**: どんな状況で
- **原因**: 調査結果
- **修正**: 何を変えたか
- **コミット**: ハッシュ
- **本番マージ**: ⭐推奨 / △検討 / ❌不要
- **マージ済**: 未 / コミット ハッシュ

---

## 本番モード (pattern) マージ計画

### Phase A: 低リスク修正 (検証完了後すぐ)
- [ ] bead road 直読み方式 → `executor.wait_for_result` のフォールバックとして追加 (置換ではなく)
- [ ] Tie 正確判定 → 上記の派生

### Phase B: 中リスク修正 (Phase A 安定後)
- [ ] 結果検知の高速化 → 通常モードでも bead road 優先に
- [ ] (今後の発見)

### Phase C: 高リスク修正 (慎重に)
- [ ] (今後の発見)

---

## 注意点

### test モード ⇄ 本番モードの切替
- test モード中: VPS state は読み込み済だが触らない
- test 終了 → STOP → モードを `pattern` に変更 → START → CONTINUE FROM LAST SESSION
- これで本番 state は前回の状態から再開

### test モード固有のリスク
- **実 BET**: $1 のみだが、リアルマネー
- **〇✖ 進行なし**: 連敗しても $1 のまま
- **VPS 記録なし**: 本番セッション保護
- **Supabase 記録なし**: 本番セッション保護

### 既知の制約
- bead road の更新タイミングは Evolution 側依存
- DOM 読み取りなのでネットワーク遅延の影響を受ける
- iframe 死亡時は読めない (既存の recovery で対応)

---

## 関連ファイル

- `agent_api.py` — `pattern_test` モード本体 (BET ブロック内)
- `pattern_classifier.py` — 大路罫線パターン分類器
- `strategy_router.py` — Strategy A/D ルーティング
- `executor.py` — `read_bead_road()` / `place_bet()` / `wait_for_result()`
- `PATTERN_STRATEGY_FINDINGS.md` — backtest 結果と戦略定義

---

**最終更新**: 2026-04-11
