# LAPLACE GUI デザイン依頼

## 依頼内容

バカラ自動BETツール「LAPLACE」のデスクトップアプリ (Electron) のGUIデザインを刷新してください。
**styles.css のみ**を書き換えてください。HTMLの構造 (id, class名) は変更禁止です。

---

## デザインコンセプト

**近未来的・SF風のダークテーマ。**

参考イメージ:
- 映画「TRON: Legacy」のUI
- サイバーパンク風のダッシュボード
- 宇宙船のコックピットモニター
- ブルーとシアンのネオンが光る暗い空間

**キーワード:** ネオン、グロー、ガラスモーフィズム、ホログラム感、微細なグリッドパターン背景

---

## 最重要ルール

1. **枠線と枠線の間に十分な余白 (gap/margin/padding) を取ること。** セクション同士がくっつかない。呼吸できるレイアウト。最低14px以上の gap。
2. **ウィンドウサイズは 480px × 720px。** この中に主要要素が収まる。スクロール可。
3. **HTMLの構造変更は禁止。** class名やid名は一切変えない。
4. **テキストは全て英語。**

---

## 色の指定

| 用途 | 推奨色 | CSS変数名 |
|---|---|---|
| 背景 | #05080f〜#0a0e1a (非常に暗いネイビー) | --bg |
| カード背景 | rgba(15,20,35,0.85) 半透明 | --bg-card |
| ガラス背景 | rgba(20,28,50,0.6) 半透明 | --bg-glass |
| メインアクセント | シアン (#00e5ff, #6cf3ff) | --accent |
| 勝ち (WIN) | #00ff88 ネオングリーン | --win |
| 負け (LOSE) | #ff3366 ネオンレッド | --lose |
| タイ (TIE) | #ffcc00 イエロー | --tie |
| テキスト (メイン) | #e0e8f0 | --text |
| テキスト (薄い) | #7888a0 | --text-muted |
| テキスト (最薄) | #4a5568 | --text-dim |
| ボーダー | rgba(0,229,255,0.12) 微かなシアン | --border |
| グロー | 0 0 20px rgba(0,229,255,0.15) | --glow |

---

## GUI構成要素の詳細説明 (上から順)

### 1. タイトルバー (.title-bar) — 高さ36px固定
- ウィンドウ最上部。ドラッグでウィンドウ移動。
- 左: ロゴ記号 (∫) + 「LAPLACE」テキスト。ネオンテキストシャドウ。
- 右: ─ (最小化) □ (最大化) × (閉じる) ボタン。

### 2. アクションバー (.action-bar) — 1行テキスト
- 現在の動作状況テキスト。例: 「Entering Korean Speed Baccarat A...」
- 左に脈動するシアンのドット (.action-dot)。
- monospace フォント。

### 3. ステータスカード4枚 (.stats-row → .stat-card × 4)
- 横4列の grid。
- **BALANCE**: 残高。例: $4,350.00
- **SESSION**: セッション損益。プラス=緑、マイナス=赤。アクセント枠 (.stat-card.accent)。
- **BETS**: 勝敗数 (12W / 8L)。右上に OS タグ (.os-tag)。
- **WIN RATE**: 勝率 (60.0%)。
- 各カードは glass 背景 + backdrop-filter: blur。

### 4. Developer Panel (.dev-panel) — 開発者専用、通常は hidden
- **黄色 (#ffcc00) アクセント**のセクション。右上に「DEV」バッジ。
- 8つのミニカード (4列×2行の grid):
  - **Current Unit Idx**: 現在の SEQ インデックス (例: 13)
  - **Current Unit ($)**: 現在の BET 額 (例: $31)
  - **Current Set**: 現在のセット番号 (例: Set #25)
  - **Turn in Set**: 7ターン中の何ターン目 (例: 4/7)
  - **Set Profit (chip)**: 現セットのチップ損益
  - **Cumul. Profit (chip)**: 累計チップ損益
  - **Overshoot**: OS 値 (〇✖ロジックの過去セット残差)
  - **Total Bets**: 総BET回数
- その下に **Set History**: 7ハンドごとの 〇✖ 履歴。1行=1セット。
  - 形式: `#25  ○○×○×○×  3/4 -31ch OS:27`
  - ○ は緑 (.set-mark-o)、× は赤 (.set-mark-x)
  - 斜線で消された行 (.set-line.slashed): 半透明 + 横線アニメーション
  - max-height: 120px でスクロール可

### 5. Daily P&L (.daily-section)
- ヘッダー「DAILY TOTAL」右に今日の合計 (.today-pnl)。
- 横スクロールする日別カード。日付 + 金額。プラス=緑枠、マイナス=赤枠。

### 6. LIVE FEED (.feed-row)
- 直近10件のBET結果を丸いドットで表示。
- W = 緑丸、L = 赤丸、T = 黄丸。新しい結果は scale アニメーション。
- 右端にカーソル (.feed-cursor) が点滅。

### 7. RECENT HANDS (.recent-grid)
- 直近20件の O/X/T を monospace テキストで表示。
- O=緑、X=赤、T=黄。

### 8. コントロールボタン (.controls) — flex 横並び
- **START** (.btn-start): シアングラデーション。
- **STOP** (.btn-stop): 赤グラデーション。
- **SKIP TABLE** (.btn-skip): オレンジグラデーション。
- **⚙** (.btn-gear): 設定ボタン。小さめ (44px幅固定)。
- 全ボタン: hover で translateY(-1px) + グロー強化。disabled で opacity: 0.3。

### 9. コンソール (.log-panel)
- 「CONSOLE ▲」で開閉。暗い背景。monospace。
- .log-win (緑), .log-lose (赤), .log-info (シアン)。

### 10. Settings モーダル (#settingsModal)
- BOT タブ: Base Bet, Profit Target, Loss Cut, Telegram, Email, Dry Run, BET Mode
- TABLE FILTER タブ: ステッパー (.fx-stepper)、セグメントボタン (.fx-segment)、トグル (.fx-toggle)
- ステッパー: [-] [値] [+] のエンボス風3分割ボタン
- セグメントボタン: OFF/3/4/5/6/7 の選択式
- トグル: iOS風スイッチ。ON時にシアン発光。

### 11. Win/Lose フラッシュ (.flash-overlay)
- 勝ち (.win): 全画面が緑に光って1秒で消える
- 負け (.lose): 全画面が赤に光って1秒で消える
- 利確 (.profit): 全画面が金色に2秒光る
- 損切 (.losscut): 全画面が赤に2秒強く光る

### 12. Reset トースト (.reset-toast)
- 利確/損切時に画面中央にポップアップ
- .profit: 金色のボーダー + グロー。タイトル「PROFIT TARGET」+ 金額
- .losscut: 赤のボーダー + グロー。タイトル「LOSS CUT」+ 金額
- scale(0.7→1) のバウンスアニメーション

---

## デザイン指示

### 余白 (最重要)
- セクション間 gap: 14-18px
- カード内 padding: 14px以上
- ボタン間 gap: 10-12px
- フォームグループ間: 14-16px

### 近未来的要素
- 背景に微細なグリッドライン (40px間隔、rgba(0,229,255,0.04))
- カードのボーダーがシアンに微かに光る
- hover 時にグロー (box-shadow) が強まる
- ボタンにグラデーション + 外側グロー
- 「LAPLACE」テキストに text-shadow: 0 0 12px rgba(0,229,255,0.5)

### ガラスモーフィズム
- カード/セクション背景: 半透明 + backdrop-filter: blur(10-12px)
- inset box-shadow で内側から微かな光

### アニメーション
- .action-dot: pulse (脈動) 2秒サイクル
- .feed-cursor: 点滅 1秒サイクル
- .feed-dot: scale(0→1.2→1) の出現アニメーション
- .flash-overlay: opacity 0→1→0 のフラッシュ
- ボタン hover: translateY(-1px)
- .set-line.slashed::after: 横線が左から右に引かれるアニメーション

---

## 禁止事項

- HTMLの構造変更 (class名やid名の変更・追加・削除)
- JavaScript の変更
- 要素の追加・削除
- 日本語テキストの使用
- inline style の追加

---

## 納品物

**styles.css ファイル1つのみ。**

480px × 720px の Electron ウィンドウで正常に表示され、上記の全要素のスタイリングを含むこと。

---

## 現在の index.html (変更禁止)

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LAPLACE</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <div class="flash-overlay" id="flashOverlay"></div>
  <div id="updateBanner" style="display:none;background:#1a2a1a;border-bottom:1px solid #2a4a2a;padding:6px 16px;font-size:11px;color:#6fbf6f;align-items:center;gap:10px;-webkit-app-region:no-drag;">
    <span id="updateText"></span>
    <button id="btnInstallUpdate" style="display:none;padding:2px 10px;background:#2a5a2a;color:#9fdf9f;border:1px solid #3a7a3a;border-radius:3px;cursor:pointer;font-size:11px;">OPEN DOWNLOAD PAGE</button>
  </div>
  <div class="title-bar">
    <div class="title-bar-left">
      <span class="title-bar-logo">∫</span>
      <span class="title-bar-text">LAPLACE</span>
    </div>
    <div class="title-bar-controls">
      <button class="title-btn" id="btnMinimize">─</button>
      <button class="title-btn" id="btnMaximize">□</button>
      <button class="title-btn title-btn-close" id="btnClose">×</button>
    </div>
  </div>
  <!-- ... 以下 index.html と同一構造 ... -->
</body>
</html>
```

※ 完全な index.html は別途テキストファイルで提供します。

---

## 現在の styles.css (参考・書き換え対象)

```css
/* 現在の styles.css を別途テキストファイルで提供 */
```

※ このCSSをベースに改善するか、全面書き直しでも可。
  ただし全てのclass名に対するスタイル定義を含めること。
