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
  const supabaseUrl = localEnv.NEXT_PUBLIC_SUPABASE_URL || process.env.NEXT_PUBLIC_SUPABASE_URL || '';
  const supabaseAnonKey = localEnv.NEXT_PUBLIC_SUPABASE_ANON_KEY || process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || '';
  const laplaceApiKey = process.env.LAPLACE_API_KEY || localEnv.LAPLACE_API_KEY || '';

  if (!apiKey) console.warn('[warn] BACOPY_API_KEY not found - exe will fail to connect to master');
  if (!supabaseUrl) console.warn('[warn] NEXT_PUBLIC_SUPABASE_URL not found - login will fail');
  if (!laplaceApiKey) console.warn('[warn] LAPLACE_API_KEY not found - session-state POST (cron/settle) will not work');

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
    ...(supabaseUrl ? { NEXT_PUBLIC_SUPABASE_URL: supabaseUrl } : {}),
    ...(supabaseAnonKey ? { NEXT_PUBLIC_SUPABASE_ANON_KEY: supabaseAnonKey } : {}),
    ...(laplaceApiKey ? { LAPLACE_API_KEY: laplaceApiKey } : {}),
    BACOPY_EXECUTOR_ID: executorId,
    BACOPY_EXECUTOR_LABEL: executorId,
  };
  let out = existing;
  for (const [k, v] of Object.entries(merge)) {
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
