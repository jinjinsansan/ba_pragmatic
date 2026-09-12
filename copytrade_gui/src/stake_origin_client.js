// stake_origin_client.js — マスターから「今使うべき Stake オリジン」を取得する。
//
// 背景 (2026-09-12):
//   stake.com は日本向け HTTP 451 になった。ミラー (https://playstake.io に121本) は
//   生きているが、通知はドメイン単位なので 1本ずつ焼かれる。
//   オリジンを exe に焼くと、焼かれる度に全インストーラを作り直す羽目になるため、
//   マスター (GET /api/origin) が配る値に従う。
//
// 方針:
//   - 起動時と一定間隔で取得し、メモリと .env フォールバックの二段で解決する
//   - マスターに繋がらなくても **止めない**。最後に取れた値 → .env → stake.com の順で縮退する
//   - 取得した値は Chrome の起動URLと、エンジンへ渡す BACOPY_STAKE_ORIGIN に使う

'use strict';

const DEFAULT_ORIGIN = 'https://stake.com';
const DEFAULT_API = 'https://master.bafather.uk';
const TTL_MS = 15 * 60 * 1000; // 15分

let _cache = null;      // { origin, candidates, at }
let _inflight = null;

function _norm(s) {
  return String(s || '').trim().replace(/\/+$/, '');
}

function _envOrigin(envFile) {
  const e = envFile || {};
  return _norm(e.BACOPY_STAKE_ORIGIN || process.env.BACOPY_STAKE_ORIGIN || '');
}

/**
 * マスターから取得する。失敗しても throw せず null を返す。
 */
function _fetch(envFile, timeoutMs = 10000) {
  return new Promise((resolve) => {
    try {
      const e = envFile || {};
      const base = _norm(e.BACOPY_API_URL || process.env.BACOPY_API_URL || DEFAULT_API);
      const key = String(e.BACOPY_API_KEY || process.env.BACOPY_API_KEY || '').trim();
      if (!key) return resolve(null);
      const u = new URL(base + '/api/origin');
      const mod = u.protocol === 'http:' ? require('http') : require('https');
      const req = mod.get(
        u,
        { headers: { Authorization: `Bearer ${key}` }, timeout: timeoutMs },
        (res) => {
          let body = '';
          res.on('data', (c) => { body += c; });
          res.on('end', () => {
            try {
              const d = JSON.parse(body);
              const origin = _norm(d && d.origin);
              if (!origin) return resolve(null);
              const cands = Array.isArray(d && d.candidates) ? d.candidates.map(_norm).filter(Boolean) : [];
              resolve({ origin, candidates: cands, at: Date.now() });
            } catch (_) { resolve(null); }
          });
        }
      );
      req.on('timeout', () => { try { req.destroy(); } catch (_) {} resolve(null); });
      req.on('error', () => resolve(null));
    } catch (_) { resolve(null); }
  });
}

/**
 * 現在のオリジンを解決する。
 * 優先順: 取得できた値 > キャッシュ > .env > stake.com
 * ★マスターに繋がらないことを理由に受け子を止めない。
 */
async function resolveOrigin(envFile, { force = false } = {}) {
  const fresh = _cache && !force && (Date.now() - _cache.at) < TTL_MS;
  if (fresh) return _cache.origin;

  if (!_inflight) _inflight = _fetch(envFile).finally(() => { _inflight = null; });
  const got = await _inflight;

  if (got && got.origin) {
    const changed = _cache && _cache.origin !== got.origin;
    _cache = got;
    if (changed) {
      console.log(`[stake-origin] origin changed: ${_cache.origin}`);
    }
    return got.origin;
  }
  if (_cache && _cache.origin) return _cache.origin;
  return _envOrigin(envFile) || DEFAULT_ORIGIN;
}

/** 直近に解決した値を同期的に返す (取得を待てない箇所用)。 */
function cachedOrigin(envFile) {
  if (_cache && _cache.origin) return _cache.origin;
  return _envOrigin(envFile) || DEFAULT_ORIGIN;
}

/** フェイルオーバー候補 (先頭が現在のオリジン)。 */
function candidates(envFile) {
  if (_cache && Array.isArray(_cache.candidates) && _cache.candidates.length) return _cache.candidates.slice();
  return [cachedOrigin(envFile)];
}

/** ロビーURL。★直リンクは Chrome 冷間起動で必発の不具合があるため casino/home から入る。 */
function lobbyUrl(envFile) {
  const e = envFile || {};
  const explicit = String(e.BACOPY_LOBBY_URL || process.env.BACOPY_LOBBY_URL || '').trim();
  if (explicit) return explicit;
  return cachedOrigin(envFile) + '/ja/casino/home';
}

function _resetForTest() { _cache = null; _inflight = null; }

module.exports = {
  DEFAULT_ORIGIN,
  resolveOrigin,
  cachedOrigin,
  candidates,
  lobbyUrl,
  _resetForTest,
};
