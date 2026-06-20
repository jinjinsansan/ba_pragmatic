#!/usr/bin/env node
/**
 * provision-user-build.js
 *
 * 各ユーザー向けに暗号化された support_key を生成し build_staging に配置する.
 * また .env テンプレート (BACOPY_SUPPORT_* フル埋込み版) を生成する.
 *
 * Usage (developer 側):
 *   node scripts/provision-user-build.js <email> [--port 2222]
 *
 * 例:
 *   node scripts/provision-user-build.js friend1@example.com --port 2222
 *
 * 事前条件:
 *   ../support_keys/client_key (未暗号化, 全ユーザー共通) が存在すること.
 *   ../support_keys/admin_key.pub も配置済.
 *
 * 出力:
 *   build_staging/support_key     — AES-256-CBC で email 派生鍵で暗号化した秘密鍵
 *   build_staging/admin_pubkey.txt — 既に配置されていればそのまま
 *   build_staging/.env             — BACOPY_SUPPORT_* を埋込んだテンプレ
 *   build_staging/build_meta.json  — email + port のメタデータ
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ROOT = path.resolve(__dirname, '..', '..');                      // bacopy repo root
const SUPPORT_KEYS = path.join(ROOT, 'support_keys');
const STAGING = path.join(__dirname, '..', 'build_staging');
const PORT_REGISTRY = path.join(SUPPORT_KEYS, 'port_registry.json');   // email → port マッピング永続化

function loadRegistry() {
  try { return JSON.parse(fs.readFileSync(PORT_REGISTRY, 'utf-8')); }
  catch (_) { return { version: 1, emails: {} }; }
}
function saveRegistry(reg) {
  fs.mkdirSync(SUPPORT_KEYS, { recursive: true });
  fs.writeFileSync(PORT_REGISTRY, JSON.stringify(reg, null, 2), 'utf-8');
}

// email のハッシュから算出した候補 port が他 email に取られていれば次の空きへ.
function allocatePortSafely(email, preferredPort) {
  const reg = loadRegistry();
  const em = String(email || '').toLowerCase();
  // 既に登録されていれば同ポート返却 (決定的).
  if (reg.emails[em]) return { port: reg.emails[em], registry: reg, reused: true };
  const used = new Set(Object.values(reg.emails));
  let p = preferredPort;
  if (used.has(p)) {
    // 次の空きを線形探索.
    let probe = p;
    for (let i = 0; i < (PORT_MAX - PORT_MIN + 1); i++) {
      probe = PORT_MIN + ((probe - PORT_MIN + 1) % (PORT_MAX - PORT_MIN + 1));
      if (!used.has(probe)) { p = probe; break; }
    }
    if (used.has(p)) {
      console.error(`[ERROR] port range ${PORT_MIN}-${PORT_MAX} is full (${used.size} emails registered)`);
      process.exit(4);
    }
    console.log(`[port] collision on ${preferredPort} → reassigned to ${p}`);
  }
  reg.emails[em] = p;
  return { port: p, registry: reg, reused: false };
}

function usage() {
  console.error('Usage: node scripts/provision-user-build.js <email> [--port <num>]');
  process.exit(1);
}

// Port 自動割当範囲 (2222-2299 = 78 slots). 衝突時は手動 --port で上書き可.
const PORT_MIN = 2222;
const PORT_MAX = 2299;

// ── 受け子別 bet-code (bc) オーバーライド ──────────────────────────────
// Pragmatic の lpbet フレームが運ぶ "bc"(賭け先コード)は、Stake が配信する
// クライアントのビルドが決める値で **個体依存**(うちのコードではない)。大半の
// クライアントは Banker=1 / Player=0 (エンジン既定) だが、一部のアカウントは
// 別符号を使うクライアントを配信される(user06 = Banker 10 / Player 11)。
// 誤符号で送るとサーバが **沈黙拒否**(stake_delta 無し)→ PHANTOM-GUARD が
// ドロップ → ボットが「数時間BETせずロビーに戻る」だけに見える。
// この件は 2026-06-15 に手動 .env で修正→ 2026-06-20 に再発(再インストールで
// .env が消えた)。ここに焼き込めば再ビルド/再インストールでも維持される。
// 新規ユーザー追加手順: 手動で Banker と Player を 1 回ずつ置かせ、
// engine_cli_capture.log の [WS-SENT-RAW] の bc 値を実測してここに追記。
// キーは executorId (email の @ 前部分。例 user06@... → 'user06')。
const BC_OVERRIDES = {
  user06: { banker: '10', player: '11' },
};

function portFromEmail(email) {
  // SHA-256(email.lowercased) の先頭 4 byte を PORT_MIN..PORT_MAX にマップ.
  // 決定的 (同 email なら常に同ポート) なので再ビルド時も変わらない.
  const h = crypto.createHash('sha256').update(String(email || '').toLowerCase()).digest();
  const n = h.readUInt32BE(0);
  const range = PORT_MAX - PORT_MIN + 1;
  return PORT_MIN + (n % range);
}

function parseArgs(argv) {
  const out = { email: null, port: null, salt: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--port') { out.port = parseInt(argv[++i], 10) || null; continue; }
    if (a === '--salt') { out.salt = argv[++i]; continue; }
    if (!out.email && !a.startsWith('-')) { out.email = a; continue; }
  }
  if (!out.email) usage();
  if (!/^[^@\s]+@[^@\s]+$/.test(out.email)) {
    console.error(`invalid email: ${out.email}`);
    process.exit(2);
  }
  // --port 未指定なら email ハッシュから自動割当.
  if (!out.port) {
    out.port = portFromEmail(out.email);
    console.log(`[port] auto-allocated from email hash: ${out.port}`);
  } else if (out.port < PORT_MIN || out.port > PORT_MAX) {
    console.warn(`[warn] port ${out.port} outside recommended range ${PORT_MIN}-${PORT_MAX}`);
  }
  return out;
}

function encryptClientKey(plainBuffer, email, salt) {
  const SALT = Buffer.from(salt || 'bacopy-support-v1-2026', 'utf-8');
  const key = crypto.pbkdf2Sync(String(email || '').toLowerCase(), SALT, 100000, 32, 'sha256');
  const iv = crypto.randomBytes(16);
  const cipher = crypto.createCipheriv('aes-256-cbc', key, iv);
  const ciphertext = Buffer.concat([cipher.update(plainBuffer), cipher.final()]);
  // 形式: base64(IV || ciphertext)
  return Buffer.concat([iv, ciphertext]).toString('base64');
}

function main() {
  let { email, port, salt } = parseArgs(process.argv.slice(2));
  // 衝突回避 + レジストリ永続化.
  const alloc = allocatePortSafely(email, port);
  port = alloc.port;
  saveRegistry(alloc.registry);
  if (alloc.reused) console.log(`[port] reused existing registry entry: ${port}`);
  const privPath = path.join(SUPPORT_KEYS, 'client_key');
  if (!fs.existsSync(privPath)) {
    console.error(`client_key not found: ${privPath}`);
    console.error('先に ssh-keygen -t ed25519 -f support_keys/client_key を実行してください');
    process.exit(3);
  }
  fs.mkdirSync(STAGING, { recursive: true });

  // 1. 暗号化鍵を生成して build_staging/support_key に書き出し
  const plain = fs.readFileSync(privPath);
  const encB64 = encryptClientKey(plain, email, salt);
  fs.writeFileSync(path.join(STAGING, 'support_key'), encB64, 'utf-8');
  console.log(`✓ support_key written (encrypted with ${email})`);

  // 2. admin_pubkey.txt コピー (既になければ)
  const adminPubSrc = path.join(SUPPORT_KEYS, 'admin_key.pub');
  const adminPubDst = path.join(STAGING, 'admin_pubkey.txt');
  if (fs.existsSync(adminPubSrc) && !fs.existsSync(adminPubDst)) {
    fs.copyFileSync(adminPubSrc, adminPubDst);
    console.log(`✓ admin_pubkey.txt copied`);
  }

  // 3. .env テンプレ生成 (既存キーは温存して support 系のみ更新)
  const envPath = path.join(STAGING, '.env');
  let existing = '';
  try { existing = fs.readFileSync(envPath, 'utf-8'); } catch (_) { existing = ''; }

  // executor_id: email の @ 前部分 or port番号から生成
  const executorId = email.split('@')[0].replace(/[^a-zA-Z0-9_-]/g, '').substring(0, 16) || `port${port}`;

  // 必須キーを環境変数またはローカル .env から読み込む
  const localEnvPath = path.join(ROOT, 'web', '.env.local');
  const localEnv = {};
  try {
    for (const line of fs.readFileSync(localEnvPath, 'utf-8').split(/\r?\n/)) {
      const m = line.match(/^([A-Z_][A-Z0-9_]*)=(.*)/);
      if (m) localEnv[m[1]] = m[2];
    }
  } catch (_) {}

  const apiKey = process.env.BACOPY_API_KEY || localEnv.BACOPY_API_KEY || '';
  // 配布ユーザーはローカル中継が無いのでリモートmaster直結。BACOPY_REMOTE_API_KEY
  // (無ければ BACOPY_API_KEY) を remote 経路用にも書き出す。
  const remoteApiKey = process.env.BACOPY_REMOTE_API_KEY || localEnv.BACOPY_REMOTE_API_KEY || apiKey || '';
  const supabaseUrl = localEnv.NEXT_PUBLIC_SUPABASE_URL || process.env.NEXT_PUBLIC_SUPABASE_URL || '';
  const supabaseAnonKey = localEnv.NEXT_PUBLIC_SUPABASE_ANON_KEY || process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || '';
  const laplaceApiKey = process.env.LAPLACE_API_KEY || localEnv.LAPLACE_API_KEY || '';
  // bafather email: ユーザーの本物のメールアドレス (session-state POST 認証に使用)
  // build_per_user.ps1 では email が user02@beta.bacopy.local のため、
  // BACOPY_BAFATHER_EMAIL は web/.env.local の USER_EMAIL_MAP から引くか、
  // 環境変数 BACOPY_BAFATHER_EMAIL_{SLOT} で上書き可能。
  // 未設定の場合は main.js のログイン時に window.__authEmail から動的に設定される。
  const bafatherEmail = process.env[`BACOPY_BAFATHER_EMAIL_${executorId.toUpperCase()}`]
    || process.env.BACOPY_BAFATHER_EMAIL
    || localEnv[`BACOPY_BAFATHER_EMAIL_${executorId.toUpperCase()}`]
    || localEnv.BACOPY_BAFATHER_EMAIL
    || '';

  if (!apiKey) console.warn('[warn] BACOPY_API_KEY not found - exe will fail to connect to master');
  if (!supabaseUrl) console.warn('[warn] NEXT_PUBLIC_SUPABASE_URL not found - login will fail');
  if (!laplaceApiKey) console.warn('[warn] LAPLACE_API_KEY not found - session-state POST (cron/settle) will not work');
  if (!bafatherEmail) console.warn('[warn] BACOPY_BAFATHER_EMAIL not found - session-state POST will rely on GUI login');

  const merge = {
    BACOPY_SUPPORT_ENABLED: '1',
    BACOPY_SUPPORT_SSH_HOST: 'support@210.131.215.116',
    BACOPY_SUPPORT_SSH_KEY: 'support_key',
    BACOPY_SUPPORT_SSH_KEY_ENCRYPTED: '1',
    BACOPY_SUPPORT_USER_EMAIL: email,
    BACOPY_SUPPORT_REMOTE_PORT: String(port),
    BACOPY_SUPPORT_LOCAL_PORT: '22',
    BACOPY_API_URL: 'https://master.bafather.uk',
    ...(apiKey ? { BACOPY_API_KEY: apiKey } : {}),
    ...(remoteApiKey ? { BACOPY_REMOTE_API_KEY: remoteApiKey } : {}),
    ...(supabaseUrl ? { NEXT_PUBLIC_SUPABASE_URL: supabaseUrl } : {}),
    ...(supabaseAnonKey ? { NEXT_PUBLIC_SUPABASE_ANON_KEY: supabaseAnonKey } : {}),
    ...(laplaceApiKey ? { LAPLACE_API_KEY: laplaceApiKey } : {}),
    BACOPY_BAFATHER_EMAIL: bafatherEmail,
    BACOPY_EXECUTOR_ID: executorId,
    BACOPY_EXECUTOR_LABEL: executorId,
    // ── dual-line manual-assist distribution config ──
    // BACOPY_BROWSER=chrome_attach is REQUIRED so the GUI (a) launches the CDP
    // Chrome on port 9222 at app-ready (ensureCdpChrome) and (b) attaches the
    // engine to it. Manual-assist is forced (no auto-click) — this build is for
    // human-eyes manual betting; the engine only sizes bets + tracks SEQ + bills.
    BACOPY_BROWSER: 'chrome_attach',
    BACOPY_CHROME_CDP_URL: 'http://127.0.0.1:9222',
    BACOPY_LOBBY_URL: 'https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat',
    BACOPY_MANUAL_NO_AUTOCLICK: '1',
    BACOPY_ASSIST_FOCUS_HOLD_SEC: '80',
    BACOPY_NOW_LOCK_MAX_SEC: '80',
    BACOPY_ASSIST_NOW_TTL_MS: '85000',
    BACOPY_DGA_LOCAL_SIGNAL: 'off',
    // VPS-driven NOW: GUIのNOWをVPS decision(=テレグラム配信と1対1)だけで駆動。
    // ローカル独自signalは抑止。複数受け子のfan-out衝突を避けるため受け子からは
    // decisionのack/result(status書換)を送らない(VPSがライフサイクル所有)。
    BACOPY_VPS_DRIVEN_NOW: '1',
    // 受け子別 bet-code(bc) オーバーライド。該当ユーザーのみ値を入れ、非該当は
    // 空文字 → 下の削除ロジックで .env から除去(=エンジン既定 Banker=1/Player=0)。
    // ★build_staging/.env は連続ビルドで使い回されるため、空でも「明示削除」しないと
    //   直前ユーザー(例 user06=10/11)の bc 行が次ユーザーに残留する(2026-06-20 実害)。
    BACOPY_BC_BANKER: (BC_OVERRIDES[executorId] && BC_OVERRIDES[executorId].banker) || '',
    BACOPY_BC_PLAYER: (BC_OVERRIDES[executorId] && BC_OVERRIDES[executorId].player) || '',
  };
  let out = existing;
  for (const [k, v] of Object.entries(merge)) {
    // 値が空のキーは .env に残さず削除(前ユーザーの値の残留を防ぐ)。
    if ((k === 'BACOPY_BAFATHER_EMAIL' || k === 'BACOPY_BC_BANKER' || k === 'BACOPY_BC_PLAYER') && !v) {
      out = out.replace(new RegExp('^' + k + '=.*\\r?\\n?', 'm'), '');
      continue;
    }
    const re = new RegExp(`^${k}=.*$`, 'm');
    if (re.test(out)) out = out.replace(re, `${k}=${v}`);
    else {
      if (out && !out.endsWith('\n')) out += '\n';
      out += `${k}=${v}\n`;
    }
  }
  fs.writeFileSync(envPath, out, 'utf-8');
  console.log(`✓ .env merged (BACOPY_SUPPORT_*)`);

  // 4. build_meta.json 記録
  fs.writeFileSync(path.join(STAGING, 'build_meta.json'), JSON.stringify({
    email, port, provisioned_at: new Date().toISOString(),
  }, null, 2));
  console.log(`✓ build_meta.json`);

  console.log(`\nDone. Now run: npm run build:installer`);
  console.log(`Admin can reach this client via VPS by:`);
  console.log(`  ssh -i support_keys/admin_key -J laplace@210.131.215.116 clientuser@localhost -p ${port}`);
}

main();
