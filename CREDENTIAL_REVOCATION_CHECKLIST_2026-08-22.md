# 認証情報 失効チェックリスト — サーバー廃棄前 (2026-08-22)

対象: Xserver VPS `210.131.215.116` / デスクトップクラウド bafather `162.43.83.54`

> **なぜフォーマットだけでは足りないか**
> 管理画面の OS再インストール(フォーマット)はディスク上のデータを消すが、
> **そこに書かれていたトークンやパスワードは無効にならない**。
> ディスクが手を離れた後も、鍵そのものは有効なまま生き続ける。
> 本当に効くのは「消すこと」ではなく「失効させること」。
>
> 2026-08-01 に Chrome を廃止した際、パスキーと拡張機能のシークレット2件を
> 棚卸ししないまま失った前例がある。同じ轍を踏まないための表。

---

## 1. 鍵の所在マップ

同じ `#hash` = **同一の鍵が複数箇所で使われている**。片方だけ失効させると、もう片方が壊れる。
(ハッシュは sha256 の先頭8文字。値そのものはこの文書に書かない)

| # | 鍵 | 所在 | VPS外にも存在? |
|---|---|---|---|
| `4740529f` | **ANTHROPIC_API_KEY** | VPS `laplace/.env` | **VPSのみ** |
| `40e69635` | **STAKE_API_TOKEN** | VPS `laplace/.env` | **VPSのみ** |
| `ab21b923` | **STAKE_PASSWORD** (9文字・平文) | VPS `laplace/.env` | **VPSのみ** |
| `92102e0a` | **PAYMENTS_SECRET** | VPS `laplace2/.env.crypto` | ⚠️ **Vercel(bafather.uk)にも設定あり** |
| `db743fed` | Telegram bot **A** | VPS `laplace2/.env` (3変数名で使い回し) / ローカル `bacopy/.env` | あり |
| `af32dc8e` | Telegram bot **B** | VPS `laplace/.env` (`TELEGRAM_BOT_TOKEN` / `PUBLIC_BOT_TOKEN`) | **VPSのみ** |
| `73c78f23` | Telegram bot **C** (admin) | VPS `laplace/.env` / ローカル3箇所 / **Vercel** | あり |
| `5e97e5b4` | LAPLACE_API_KEY | VPS `laplace/.env` + **crontab に平文** / ローカル3箇所 / **Vercel** | あり |
| `ab308b22` | BACOPY_API_KEY | VPS ×2 / ローカル2箇所 / **bafather機** | あり |
| `b5f8ac7a` | LAPLACE_ADMIN_KEY | VPS `laplace/.env` | **VPSのみ** |
| `964b126c` | SUPABASE_SERVICE_ROLE_KEY | ローカルのみ | ✅ **VPSに無い** |
| `6c9f5e98` | SUPABASE_ANON_KEY | ローカル / bafather機 | ✅ VPSに無い |

### 分かったこと

- **Telegram bot は5本でなく3つ**。`laplace2` 側は1つのbotを3つの変数名で使い回している
- **`SUPABASE_SERVICE_ROLE_KEY` はVPSに置かれていない** — 最も強い鍵が守られている。これは良い設計だった
- `LAPLACE_API_KEY` は **crontab(laplace)に平文で書かれている**。`.env` を消しても crontab に残る

---

## 2. 失効チェックリスト(優先順)

### 🔴 最優先 — 実害が出るもの

- [ ] **`ANTHROPIC_API_KEY` を revoke**
  - 場所: Anthropic コンソール
  - 理由: 生きたAPIキー。第三者が使えば**こちらに課金される**
  - 壊れるもの: なし(VPSのみで使用・その用途も停止済み)

- [ ] **Stake のパスワード変更**
  - 理由: **実弾の賭け口座**のパスワードが9文字平文で置かれている
  - 併せて: 2段階認証が未設定なら設定する
  - 壊れるもの: 受け子GUIの自動ログイン(既に全停止済みなので影響なし)

- [ ] **`STAKE_API_TOKEN` を失効**
  - 場所: Stake のアカウント設定(APIトークン管理)
  - 壊れるもの: 同上

- [ ] **`stake_cookies.json` の無効化**
  - VPS `/opt/laplace2/monitor/auth_state_pragmatic_collector/stake_cookies.json`(2026-04-20)
  - Stake で「全デバイスからログアウト」を実行すればセッションが無効化される
  - ※パスワード変更で無効になる実装が多いが、明示的にやる方が確実

### 🟠 次点 — 乗っ取り・不正利用の余地があるもの

- [ ] **Telegram bot A `db743fed` のトークンを再発行**
  - @BotFather → `/revoke` → **botは残したままトークンだけ無効化**できる
  - 再開の可能性があるので **bot削除ではなく revoke** を推奨
  - 壊れるもの: なし(配信は全停止済み)

- [ ] **Telegram bot B `af32dc8e` のトークンを再発行** — VPSのみ・同上

- [ ] **Telegram bot C `73c78f23`(admin)のトークンを再発行**
  - ⚠️ **Vercel(bafather.uk)とローカル3箇所でも使用**
  - revoke するなら **Vercel の環境変数 `ADMIN_TELEGRAM_BOT_TOKEN` も同時更新**が必要
  - 更新しない場合、bafather.uk の管理通知が無言で失敗する

- [ ] **`PAYMENTS_SECRET` をローテーション**
  - ⚠️ **VPS と Vercel の共有シークレット**。`/api/cron/wallet-watch` の認証に使う
  - VPS側だけ変えても無意味。**Vercel の環境変数も同時に**
  - サイトを止めるなら、Vercel 側ごと消してしまう方が早い

### 🟡 低優先 — サーバーと運命を共にするもの

- [ ] `LAPLACE_ADMIN_KEY `b5f8ac7a`` — VPSのみ。廃棄で自然に無効
- [ ] `LAPLACE_API_KEY` `5e97e5b4` — ⚠️ Vercel とローカルにもある。変えるなら3箇所同時
- [ ] `BACOPY_API_KEY` `ab308b22` — VPS/ローカル/bafather機。APIサーバーが消えるので実質無効

### ⚪ 対象外

- `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_ANON_KEY` — **VPSに無い**ので今回の廃棄では露出しない。
  ただし Supabase 自体を止める判断をするなら別途検討
- SSH 公開鍵(`/root/.ssh/authorized_keys` に1件) — サーバー消滅で無効。
  秘密鍵 `~/.ssh/laplace_vps` は**デスクトップクラウドと共用**なので、両方廃棄後に削除してよい

---

## 3. bafather デスクトップクラウド側の棚卸し

- [ ] `C:\Users\Administrator\Desktop\.env` — `BACOPY_API_KEY` / `SUPABASE_ANON_KEY` を含む
- [ ] **Chrome / Firefox プロファイルの Stake ログイン状態**
  - `%APPDATA%\bacopy-copytrade-gui\cdp_chrome_profile\`
  - `%APPDATA%\bacopy-copytrade-gui\profiles\executor_pragmatic\`(cookies.sqlite / key4.db / cert9.db)
  - → Stake で「全デバイスからログアウト」すれば一括で無効化できる
- [ ] `bafather_supabase_session.json` — Supabase のログインセッション
  - → Supabase 側でセッション失効、またはパスワード変更

---

## 4. 実行順序

```
1. B: バックアップHDDへのミラー        ← ★まだ未了。ここが最優先
2. 本チェックリストの失効作業
3. VPS / デスクトップクラウドをフォーマット(OS再インストール)
4. 解約
```

**★2 を 1 より先にやらないこと。** 失効させると金庫の `secrets/` に入っている値も
使えなくなるが、それ自体は問題ない(§5参照)。問題は 1 が終わる前に 3 をやること。

**★3 を 2 より先にやらないこと。** フォーマット後は VPS 上の `.env` を読めなくなるため、
「どの鍵がどこにあったか」を確認できなくなる。本文書がその代わりになるが、
値そのものは金庫 `secrets/_vps_secrets.tar.gz` にしか無くなる。

---

## 5. 失効後の注意 — 金庫の `secrets/` は「古い鍵」になる

`V:\_ARCHIVE_BACOPY_SHUTDOWN_20260822\secrets\` には**失効前の値**が入っている。
失効を実行すると、これらは**復元しても動かない鍵**になる。

これは想定どおりで問題ない。むしろ、
**金庫が流出しても被害が出ない状態になる**という点で望ましい。

ただし **再開時は「新しい鍵を発行し直す」工程が必要**になる。
`RESTART_KIT_2026-08-22.md` の復元手順で `.env` を戻しても、
下記は必ず作り直しになると理解しておくこと:

| 再開時に再発行が要るもの |
|---|
| Anthropic API キー |
| Stake のパスワード / API トークン / セッション |
| Telegram bot トークン ×3(botとチャンネル自体は残る) |
| `PAYMENTS_SECRET`(Vercel と揃える) |

**残るもの**(再発行不要): Supabase の URL / キー、Telegram の**チャンネルID**、
ドメイン、GitHub、そして金庫のデータ本体。

---

## 6. 進捗

| 日付 | 作業 | 状態 |
|---|---|---|
| 2026-08-22 | 全ユーザーロックダウン(13/13) | ✅ 完了 |
| 2026-08-22 | 全Telegram配信停止(宛先0件) | ✅ 完了 |
| 2026-08-22 | 保全アーカイブ 442MB + 検証 | ✅ 完了 |
| — | B: へのミラー | ⏸ 未了 |
| — | 本チェックリストの失効作業 | ⏸ 未了 |
| — | フォーマット | ⏸ 未了 |
| — | 解約 | ⏸ 未了 |

---

*作成: 2026-08-22 / 関連: `RESTART_KIT_2026-08-22.md` `SESSION_HANDOFF_2026-08-22_SHUTDOWN_COLD_ARCHIVE.md`*
