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

function usage() {
  console.error('Usage: node scripts/provision-user-build.js <email> [--port <num>]');
  process.exit(1);
}

function parseArgs(argv) {
  const out = { email: null, port: 2222, salt: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--port') { out.port = parseInt(argv[++i], 10) || 2222; continue; }
    if (a === '--salt') { out.salt = argv[++i]; continue; }
    if (!out.email && !a.startsWith('-')) { out.email = a; continue; }
  }
  if (!out.email) usage();
  if (!/^[^@\s]+@[^@\s]+$/.test(out.email)) {
    console.error(`invalid email: ${out.email}`);
    process.exit(2);
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
  const { email, port, salt } = parseArgs(process.argv.slice(2));
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
  const merge = {
    BACOPY_SUPPORT_ENABLED: '1',
    BACOPY_SUPPORT_SSH_HOST: 'support@210.131.215.116',
    BACOPY_SUPPORT_SSH_KEY: 'support_key',
    BACOPY_SUPPORT_SSH_KEY_ENCRYPTED: '1',
    BACOPY_SUPPORT_USER_EMAIL: email,
    BACOPY_SUPPORT_REMOTE_PORT: String(port),
    BACOPY_SUPPORT_LOCAL_PORT: '22',
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
