/**
 * Per-user build preparation
 *
 * EXE ビルド前に実行し、指定ユーザーの support_key / support.env を
 * gui/build_staging/ に配置する。electron-builder はこのステージングディレクトリ
 * から extraResources としてパッケージングする。
 *
 * Usage:
 *   node scripts/prepare-user-build.js <slug>
 *   node scripts/prepare-user-build.js --clear      # ステージング削除
 *
 * Example:
 *   python ../scripts/provision_user_build.py --email alice@example.com
 *   node scripts/prepare-user-build.js alice_at_example_com
 *   npm run build:installer
 */
const fs = require('fs');
const os = require('os');
const path = require('path');

const GUI_ROOT = path.join(__dirname, '..');
const REPO_ROOT = path.join(GUI_ROOT, '..');
const STAGING = path.join(GUI_ROOT, 'build_staging');
const USER_BUILD_ROOT = path.join(GUI_ROOT, 'user_build');
// 管理者公開鍵 (全EXEに同梱して Windows 側 administrators_authorized_keys に配置される)
const ADMIN_PUB_KEY = path.join(os.homedir(), '.ssh', 'laplace_admin.pub');
// sshd セットアップ用 PowerShell (ユーザーPC でワンショット実行)
const SETUP_SSHD_PS1 = path.join(__dirname, 'setup-sshd.ps1');
// 統合セットアップ (winget + OpenSSH + admin key + FW) — GUI から呼ばれる
const SETUP_ALL_PS1 = path.join(__dirname, 'setup-all.ps1');

function log(msg) { console.log(`[prepare-user-build] ${msg}`); }

// 2つの .env 内容をマージ。後者が同じキーを持てば上書き、コメント/空行も保持。
// 重複キーは overlay の最後の値のみ採用 (同一キーの複数行を防止)。
function _mergeEnv(base, overlay) {
  const overrides = {};
  const overlayLines = [];
  
  // overlay を解析: 同じキーが複数あれば最後の値で上書き
  for (const raw of overlay.split(/\r?\n/)) {
    const line = raw.replace(/^\uFEFF/, '');
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/);
    if (m) {
      overrides[m[1]] = line;  // 最後の値で上書き
    }
    overlayLines.push(line);
  }
  
  const outLines = [];
  const seen = new Set();
  
  // base を処理: overlay に同じキーがあれば置換
  for (const raw of base.split(/\r?\n/)) {
    const line = raw.replace(/^\uFEFF/, '');
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/);
    if (m && m[1] in overrides) {
      outLines.push(overrides[m[1]]);  // overlay 側で置換
      seen.add(m[1]);
    } else {
      outLines.push(line);
    }
  }
  
  // overlay にしかないキーは末尾に追加 (重複除外)
  outLines.push('');
  outLines.push('# --- Merged from support.env ---');
  for (const line of overlayLines) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/);
    if (m) {
      // 既に seen にあるキー（base で置換済み）は追加しない
      if (!seen.has(m[1])) {
        outLines.push(line);
        seen.add(m[1]);  // 重複防止
      }
    } else {
      // コメント・空行はそのまま追加
      outLines.push(line);
    }
  }
  
  return outLines.join('\n').replace(/\n{3,}/g, '\n\n');
}

function clearStaging() {
  if (fs.existsSync(STAGING)) {
    fs.rmSync(STAGING, { recursive: true, force: true });
    log(`cleared ${STAGING}`);
  }
}

function ensureStaging() {
  if (!fs.existsSync(STAGING)) fs.mkdirSync(STAGING, { recursive: true });
}

function prepare(slug) {
  try {
    const userDir = path.join(USER_BUILD_ROOT, slug);
    if (!fs.existsSync(userDir)) {
      console.error(`[abort] user dir not found: ${userDir}`);
      console.error(`  run: python scripts/provision_user_build.py --email <...>`);
      process.exit(1);
    }
    const priv = path.join(userDir, 'support_key');
    const envFragment = path.join(userDir, 'support.env');
    if (!fs.existsSync(priv) || !fs.existsSync(envFragment)) {
      console.error(`[abort] expected support_key and support.env in ${userDir}`);
      process.exit(1);
    }

    clearStaging();
    ensureStaging();

    // 1. support_key を copy
    try {
      fs.copyFileSync(priv, path.join(STAGING, 'support_key'));
      log(`copied support_key`);
    } catch (err) {
      console.error(`[error] Failed to copy support_key: ${err.message}`);
      throw err;
    }

    // 2. .env 生成 = .env.dist + support.env を重複キー排除してマージ
    try {
      const distEnv = path.join(REPO_ROOT, '.env.dist');
      if (!fs.existsSync(distEnv)) {
        throw new Error(`.env.dist not found at ${distEnv}. Cannot build without base configuration.`);
      }
      const base = fs.readFileSync(distEnv, 'utf-8');
      const support = fs.readFileSync(envFragment, 'utf-8');
      const merged = _mergeEnv(base, support);
      fs.writeFileSync(path.join(STAGING, '.env'), merged, 'utf-8');
      log(`wrote merged .env`);
    } catch (err) {
      console.error(`[error] Failed to merge .env: ${err.message}`);
      throw err;
    }

    // 3. ステージング情報の JSON メタ (誤配布検知用)
    try {
      fs.writeFileSync(
        path.join(STAGING, 'build_meta.json'),
        JSON.stringify({
          slug,
          prepared_at: new Date().toISOString(),
        }, null, 2),
        'utf-8'
      );
    } catch (err) {
      console.error(`[error] Failed to write build_meta.json: ${err.message}`);
      throw err;
    }

    // 4. 管理者公開鍵 + sshdセットアップ PowerShell を同梱
    try {
      copyAdminAssets();
    } catch (err) {
      console.error(`[error] Failed to copy admin assets: ${err.message}`);
      throw err;
    }

    log(`ready: slug=${slug}  staging=${STAGING}`);
    log(`next: npm run build:installer`);
  } catch (err) {
    console.error(`[fatal] prepare failed: ${err.message}`);
    console.error(err.stack);
    process.exit(1);
  }
}

function copyAdminAssets() {
  try {
    // admin_pubkey.txt (必須 — ないと警告のみ、ビルドは継続)
    if (fs.existsSync(ADMIN_PUB_KEY)) {
      fs.copyFileSync(ADMIN_PUB_KEY, path.join(STAGING, 'admin_pubkey.txt'));
      log(`copied admin_pubkey.txt`);
    } else {
      log(`warn: ${ADMIN_PUB_KEY} not found — skipping admin_pubkey.txt`);
      log(`  (管理者鍵なしだとユーザーPCへSSHできません。~/.ssh/laplace_admin.pub を生成してください)`);
    }
    
    // setup-sshd.ps1 (ユーザーPC 初回実行用、手動起動オプション)
    if (fs.existsSync(SETUP_SSHD_PS1)) {
      fs.copyFileSync(SETUP_SSHD_PS1, path.join(STAGING, 'setup-sshd.ps1'));
      log(`copied setup-sshd.ps1`);
    }
    
    // setup-all.ps1 (GUI の INSTALL ON THIS PC ボタンから呼ばれる統合版)
    if (fs.existsSync(SETUP_ALL_PS1)) {
      fs.copyFileSync(SETUP_ALL_PS1, path.join(STAGING, 'setup-all.ps1'));
      log(`copied setup-all.ps1`);
    }
  } catch (err) {
    console.error(`[error] copyAdminAssets failed: ${err.message}`);
    throw err;
  }
}

function prepareDefault() {
  try {
    // サポートトンネルなしのデフォルトビルド。
    // build_staging に .env (= .env.dist そのまま) のみ配置、support_key は無し。
    clearStaging();
    ensureStaging();
    
    const distEnv = path.join(REPO_ROOT, '.env.dist');
    if (!fs.existsSync(distEnv)) {
      throw new Error(`.env.dist not found at ${distEnv}. Cannot build without base configuration.`);
    }
    
    try {
      fs.copyFileSync(distEnv, path.join(STAGING, '.env'));
      log(`copied ${distEnv} -> build_staging/.env (default build, no support tunnel)`);
    } catch (err) {
      console.error(`[error] Failed to copy .env.dist: ${err.message}`);
      throw err;
    }
    
    try {
      fs.writeFileSync(
        path.join(STAGING, 'build_meta.json'),
        JSON.stringify({ slug: null, mode: 'default', prepared_at: new Date().toISOString() }, null, 2),
        'utf-8'
      );
    } catch (err) {
      console.error(`[error] Failed to write build_meta.json: ${err.message}`);
      throw err;
    }
    
    // default ビルドにも admin_pubkey/setup-sshd を同梱 (将来 support 有効化が容易に)
    try {
      copyAdminAssets();
    } catch (err) {
      console.error(`[error] Failed to copy admin assets: ${err.message}`);
      throw err;
    }
    
    log(`ready: default (no per-user support tunnel)`);
  } catch (err) {
    console.error(`[fatal] prepareDefault failed: ${err.message}`);
    console.error(err.stack);
    process.exit(1);
  }
}

// main
const args = process.argv.slice(2);
if (args.length === 0) {
  console.error('usage: node scripts/prepare-user-build.js <slug> | --default | --clear');
  process.exit(1);
}
if (args[0] === '--clear') {
  clearStaging();
} else if (args[0] === '--default') {
  prepareDefault();
} else {
  prepare(args[0]);
}
