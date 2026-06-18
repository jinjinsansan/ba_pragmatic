# 弱パターン除外: `telecho|niconico|P` (2026-06-19)

## 何を・なぜ
6/10パターンの中で最弱の **`telecho|niconico|P` を除外**（コメントアウト）。
- **根拠(2ソース一致)**: in-sample 12万シュー=**49.0%**(n2889) / 前向きprereg=**48.57%**(n1328)。Player(イーブンマネー)で50%割れ=**−EVの出血源**。
- **low-regret判断**: 統計的には50%割れを"証明"まではできない(プールz≈−1.5)が、**非対称性**から外す方向が合理的——「本当は公正な50%でも外して失うのは~ゼロEVのボリュームだけ／本当に49%なら放置で出血継続」。
- 詳細解析: `EDGE_TIMESTRUCTURE_ANALYSIS_2026-06-18.md`(エッジは時間構造で改善不可・唯一のレバー=パターン品質)。

## 適用範囲と状態
| 経路 | ファイル | 状態 |
|---|---|---|
| VPS監視bot v3 | `/opt/laplace2/dual_line_match.py` LIVE_SIGNAL_PATTERNS | ✅コメントアウト・反映済 |
| VPS監視bot v4(ハードコード) | `/opt/laplace2/dual_line_pragmatic_bot.py` V4_PATTERNS | ✅token除去・反映済 |
| master(予告/配信) | `/opt/bacopy/dual_line_match.py` v3+v4 | ✅コメントアウト+`systemctl restart bacopy-api` |
| GUIエンジン(受け子) | repo `dual_line_match.py` + `_rev53144/dual_line_match.py` v3+v4 | ✅staging(次ビルドで反映) |

→ **VPSが配信しなくなる=全受け子で即bet停止**。GUI内部whitelistは次のengine再ビルドに同梱(専用再配布不要)。bafather(稼働中)も配信停止で即対象外。

## ★(A) カウンター非リセットの担保（重要）
ユーザ要件=「除外しても累積勝率の計算は継続(リセットしない)→残りの強パターンで勝率が上がるのを観測」。
- **落とし穴**: bot `_load_v4_state` は **保存patterns ≠ 現V4_PATTERNS でv4状態をリセット**する(line 924)。
- **対策**: `dual_line_v4_state.json` の `"patterns"` から telecho|niconico|P を除去し**保存=現在に揃えた**(wins/losses/resolvedは保持)。
- **検証**: 再起動ログ `[v4] state restored: signals=8568 W/L/T=3922/3794/840` = **リセットされず保持**を確認。
- v3は logic_version変更時のみリセットなので無対応でOK。

## 検証結果(再起動後)
- 新bot稼働(PID変化)・`[STATUS]`更新中=生存。
- telecho|niconico|P の最終出現=**07:39(再起動08:02より前)・以降ゼロ**。
- 保存v4状態=**9パターン・telecho|niconico|P無し**。
- repo: root/_rev53144 とも構文OK・active occurrence=0(v3:6→5 / v4:10→9)。

## 期待される動き
テレグラム両チャンネル(パターン監視/追従監視)の**累積勝率(現v4 50.86%)が、telecho|niconico|Pのロスが新規に加わらなくなることで徐々に上昇**するはず=除外効果の確認になる。

## ロールバック
VPS各 `*.bak_excl_20260619_075950`(3 .py + 1 .json)を書き戻し→`systemctl restart bacopy-api`+bot再起動。repoは `git revert`。

## 残/次のステップ
- 次のengine再ビルド時に GUI からも自動除外(user02-10配布 / bafather swap のついで)。
- 監視: 数日後に累積勝率の上昇を確認。さらに `telecho|niconico|B`(v4・前向き47.8%)が弱いが in-sample 50.7%と矛盾→**前向きデータを溜めてから判断**(今回は外さない)。
- 関連: [[project_vps_bot_v4_hardcoded_2026-06-07]](v4変更3経路) / [[project_pattern_prereg_watch]](弱パターン監視)。
