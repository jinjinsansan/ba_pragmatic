# バグ修正・改善ログ 2026-05-22 セッション

## 概要

本セッションでは以下の4件の修正と1件の整備を実施した。

---

## 1. カスタマーサポートモーダル自動クリック修正

### 問題
BET するたびに Pragmatic フレーム内に「カスタマーサポートに連絡してください」モーダルが表示され、手動クリックが必要だった。

### 原因
`_AUTO_RECOVER_IDLE_JS` は `page.evaluate()` でメインページ（Stake.com）コンテキストで実行されており、Pragmatic iframe 内のモーダルを `document.querySelectorAll('[role="dialog"]')` で検出できなかった。

### 修正
**`dual_line_live_executor.py`** の idle dialog チェックループ（6秒ごと）に、`find_lobby_frames(page)` で取得した各 Pragmatic iframe に対して同じ JS を `fr.evaluate()` で実行するコードを追加。

```python
# Pragmatic iframe 内のモーダルも同様にチェック
from bacopy_executor_pragmatic_ws_live import find_lobby_frames
for _fr in (find_lobby_frames(page) or []):
    rec2 = _fr.evaluate(_AUTO_RECOVER_IDLE_JS)
    if isinstance(rec2, dict) and int(rec2.get("clicked") or 0) > 0:
        logger.info(f"[LIVE] idle dialog auto-recovered (frame) clicks={rec2.get('clicked')}")
```

---

## 2. 誤テーブル BET 問題の修正（tryFromEl ロジック）

### 問題
マルチロビー共有 iframe で `_JS_BET_COORDS` が qpid を含む最初の要素を返すが、外側コンテナ（全テーブルの qpid をまとめて持つ要素）がヒットすると、その中の最初の `.ym_yP` ボタン＝別テーブルのボタンをクリックしてしまっていた。

### 修正
`tryFromEl` 関数を実装。qpid がヒットした要素から上方向に走査し、`.ym_yP` が **1個だけ**あるノードを特定するまで複数候補を試す。2個以上あれば「複数テーブルコンテナ」と判断してスキップ。

---

## 3. GUI シグナルパネル表示修正（CYCLE / RATIO / DRIFT / ROUND / STREAM）

### 問題
デュアルラインモードで `$1 SEQ` を選択していても、シグナルパネルの CYCLE・RATIO・DRIFT・ROUND・STREAM（○×）・LIVEFEED (W/L/T)・フラッシュが一切表示されなかった。

### 原因
`case 'resolution':` ハンドラ（`app.js`）が `_pushStreamMark()` と `updateDevPanel()` を呼んでいなかった。また `resolution` メッセージに SEQ の状態（seq_turn, seq_overshoot, current_turns）が含まれていなかった。

### 修正

**`dual_line_pragmatic_bot.py`**：`resolution` メッセージに `seq7_current_turns` を追加。
```python
_seq7_turns = list(self.money._seq7_tracker.current_turns) if getattr(self.money, "_seq7_tracker", None) else []
send_msg({
    ...
    "seq7_current_turns": _seq7_turns,
})
```

**`copytrade_gui/src/renderer/app.js`**：`case 'resolution':` に以下を追加。
```javascript
// STREAM (○×)
if (r.result !== 'TIE') _pushStreamMark(r.result === 'WIN' ? 'O' : 'X');
// CYCLE / RATIO / DRIFT / ROUND
const _ms = r.money_status || {};
const _turnsStr = Array.isArray(r.seq7_current_turns) ? r.seq7_current_turns.join('') : '';
updateDevPanel({ current_turn: _ms.seq_turn, overshoot: _ms.seq_overshoot, turns_display: _turnsStr });
```

---

## 4. VPS bot クラッシュ修正（dual_line_money 未 deploy + DryRunBetExecutor 属性不足）

### 問題
VPS の dual_line bot が `ModuleNotFoundError: No module named 'dual_line_money'` でクラッシュループに陥り、約50分間シグナルが生成されなかった。

### 原因1：`dual_line_money.py` が VPS に未デプロイ
`dual_line_pragmatic_bot.py` は `from dual_line_money import BetManager` をインポートしているが、`dual_line_money.py` が `/opt/laplace2/` に存在しなかった。過去のデプロイで `dual_line_pragmatic_bot.py` だけ送られ、依存ファイルが漏れていた。

### 原因2：`DryRunBetExecutor` に必要属性が未定義
`dual_line_pragmatic_bot.py` の `run()` で `self.bet_executor.is_bet_in_flight` / `has_pending_bet` / `is_ready` / `return_to_lobby()` を参照しているが、`DryRunBetExecutor` クラスにこれらが実装されていなかった。

### 修正
`dual_line_pragmatic_bot.py` の `DryRunBetExecutor` に不足プロパティを追加：
```python
@property
def is_bet_in_flight(self) -> bool: return False

@property
def has_pending_bet(self) -> bool: return False

@property
def is_ready(self) -> bool: return True

def return_to_lobby(self) -> None: return None
```

`dual_line_money.py` と修正済み `dual_line_pragmatic_bot.py` を VPS へ deploy。

### 根本原因
VPS 向けの deploy スクリプトが存在せず、ファイルの sync が属人化・手動だったため、依存関係が漏れていた。

---

## 5. デプロイスクリプト整備

### 追加ファイル
| スクリプト | 用途 |
|---|---|
| `_deploy_vps.ps1` | VPS の `dual_line_*.py` 一括 SCP + pycache クリア + bot 再起動 |
| `_deploy_bafather.ps1` | bafather への Python ファイル SCP + PyInstaller ビルド + EXE コピー（`$HOST` 予約変数バグ修正済み） |
| `_deploy_bafather_gui.ps1` | Electron ビルド → `app.asar` を bafather に SCP（GUI 停止後に実行） |

### 運用ルール
`dual_line_*.py` を変更した場合は **VPS と bafather の両方** にデプロイする。

- VPS: `.\_deploy_vps.ps1`
- bafather EXE: `.\_deploy_bafather.ps1`
- bafather GUI: `.\_deploy_bafather_gui.ps1`（`app.js` / `index.html` 変更時のみ）

---

## 変更ファイル一覧

| ファイル | 変更種別 |
|---|---|
| `dual_line_live_executor.py` | カスタマーサポートモーダル iframe 対応 |
| `dual_line_pragmatic_bot.py` | `seq7_current_turns` 追加、`DryRunBetExecutor` 属性追加 |
| `copytrade_gui/src/renderer/app.js` | `case 'resolution':` に STREAM / DEV パネル更新追加 |
| `_deploy_vps.ps1` | 新規作成 |
| `_deploy_bafather.ps1` | `$HOST` → `$BFHOST` 修正、ファイルリスト整備 |
| `_deploy_bafather_gui.ps1` | 新規作成 |
