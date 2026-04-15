/**
 * Production build script
 * - Strips developer mode from app.js and index.html
 * - Obfuscates JavaScript
 * - Builds Electron app
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'src', 'renderer');
const PROD = path.join(__dirname, '..', 'src_prod', 'renderer');

// Ensure prod dir exists
fs.mkdirSync(PROD, { recursive: true });
fs.mkdirSync(path.join(__dirname, '..', 'src_prod'), { recursive: true });

// Copy styles.css unchanged
fs.copyFileSync(path.join(SRC, 'styles.css'), path.join(PROD, 'styles.css'));

// ── Process app.js ──────────────────────────────────────────
let appJs = fs.readFileSync(path.join(SRC, 'app.js'), 'utf-8');

// Remove entire Developer Mode block
appJs = appJs.replace(
  /\/\/ --- Developer Mode ---[\s\S]*?(?=\/\/ ---|\n\/\/ ===|$(?=\s*$))/m,
  '// Developer Mode disabled in production build\nfunction isDevMode() { return false; }\nfunction updateDevPanel() {}\nfunction renderDevSets() {}\nfunction applyDevMode() {}\n\n'
);

// Remove dev mode conditionals: if (isDevMode()) { ... } (simple single blocks)
appJs = appJs.replace(/if \(isDevMode\(\)\) \{[^}]*\}\s*/g, '');
appJs = appJs.replace(/if \(isDevMode\(\) && [^)]+\) \{[^}]*\}\s*/g, '');

// Remove applyDevMode() call at bottom (now a no-op but keep clean)
appJs = appJs.replace(/^applyDevMode\(\);$/m, '');

fs.writeFileSync(path.join(PROD, 'app.js'), appJs);
console.log('[OK] app.js processed (developer mode removed)');

// ── Process index.html ─────────────────────────────────────
let html = fs.readFileSync(path.join(SRC, 'index.html'), 'utf-8');

// Remove Developer Mode Panel
html = html.replace(/<!-- Developer Mode Panel[\s\S]*?<!-- \/Developer Mode Panel -->/g, '');

// Remove devModeLink / devModeStatus span
html = html.replace(/<span id="devModeLink"[\s\S]*?<\/span>\s*<\/span>/g, '');

// Remove Developer Mode Password Dialog
html = html.replace(/<!-- Developer Mode Password Dialog -->[\s\S]*?<\/div>\s*<\/div>/m, '');

fs.writeFileSync(path.join(PROD, 'index.html'), html);
console.log('[OK] index.html processed');

console.log('\nProduction source ready in src_prod/');
console.log('Next: run electron-builder with src_prod/renderer');
