/**
 * Key Moments backend - one Apps Script web app serving two jobs:
 *
 *   1. ?action=trigger_refresh  fires a workflow_dispatch on the
 *      key_moments.yml GitHub Action using a PAT held in Script Properties.
 *   2. ?key=<slug>              reads a favorites list; POST writes one.
 *
 * Deploy as a Web App with "Execute as: Me" and "Who has access: Anyone".
 * See DEPLOY.md. The per-user key is the only access control on favorites -
 * that is an accepted tradeoff, not an oversight.
 */

var SHEET_NAME = 'favorites';
// last_seen_iso (column 5) is the Catch Me Up cursor: the moment this key last
// had the page open. Two independent writers touch this row - writeFavorites_
// (stars) and markSeen_ (cursor) - and neither may clobber the other's column.
// See the read-then-preserve note on writeFavorites_.
var HEADERS = ['key', 'player_ids_json', 'updated_at_iso', 'note', 'last_seen_iso'];

// The cursor is stored as a NAIVE Central wall-clock string ("YYYY-MM-DDTHH:mm:ss"),
// deliberately not new Date().toISOString(). The client compares it
// lexicographically against play timestamps, and key_moments_build.py writes
// those as naive Central. A UTC-with-Z cursor would read 5-6 hours ahead of the
// local wall clock and silently swallow that many hours of genuinely new plays.
var CURSOR_TZ = 'America/Chicago';
var CURSOR_FORMAT = "yyyy-MM-dd'T'HH:mm:ss";

var GITHUB_OWNER = 'arpeterson93';
var GITHUB_REPO = 'numberball';
var GITHUB_WORKFLOW = 'key_moments.yml';
var GITHUB_REF = 'main';

var REFRESH_COOLDOWN_MS = 60 * 1000;

// ── entry points ──────────────────────────────────────────────────────────────

function doGet(e) {
  var params = (e && e.parameter) || {};
  try {
    if (params.action === 'trigger_refresh') {
      return json_(triggerRefresh_());
    }
    if (params.action === 'push_list') {
      return json_(pushList_(params.secret));
    }
    var key = normalizeKey_(params.key);
    if (!key) {
      return json_({ error: 'missing key' });
    }
    return json_(readFavorites_(key));
  } catch (err) {
    return json_({ error: String(err) });
  }
}

function doPost(e) {
  try {
    // Sent as text/plain so the browser skips the CORS preflight Apps Script
    // cannot answer; the body is still JSON.
    var body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    if (String(body.action || '').indexOf('push_') === 0) {
      return json_(pushAction_(body));
    }
    var key = normalizeKey_(body.key);
    if (!key) {
      return json_({ error: 'missing key' });
    }
    if (body.action === 'mark_seen') {
      return json_(markSeen_(key, body.last_seen || centralNow_()));
    }
    var ids = (body.player_ids || [])
      .map(function (v) { return parseInt(v, 10); })
      .filter(function (v) { return !isNaN(v); });
    return json_(writeFavorites_(key, ids, body.note || ''));
  } catch (err) {
    return json_({ error: String(err) });
  }
}

// ── refresh trigger ───────────────────────────────────────────────────────────

function triggerRefresh_() {
  var props = PropertiesService.getScriptProperties();
  var pat = props.getProperty('GITHUB_PAT');
  if (!pat) {
    return { error: 'GITHUB_PAT is not set in Script Properties' };
  }

  var now = Date.now();
  var last = parseInt(props.getProperty('LAST_REFRESH_MS') || '0', 10);
  if (last && now - last < REFRESH_COOLDOWN_MS) {
    var wait = Math.ceil((REFRESH_COOLDOWN_MS - (now - last)) / 1000);
    return { triggered: false, error: 'rate limited, try again in ' + wait + 's' };
  }

  var url = 'https://api.github.com/repos/' + GITHUB_OWNER + '/' + GITHUB_REPO +
            '/actions/workflows/' + GITHUB_WORKFLOW + '/dispatches';
  var res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + pat,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify({ ref: GITHUB_REF }),
    muteHttpExceptions: true,
  });

  var code = res.getResponseCode();
  if (code !== 204) {
    return { triggered: false, error: 'GitHub returned ' + code + ': ' + res.getContentText().slice(0, 300) };
  }
  props.setProperty('LAST_REFRESH_MS', String(now));
  return { triggered: true };
}

// ── favorites store ───────────────────────────────────────────────────────────

function sheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) {
    sh = ss.insertSheet(SHEET_NAME);
    sh.appendRow(HEADERS);
  }
  if (sh.getLastRow() === 0) {
    sh.appendRow(HEADERS);
  }
  return sh;
}

function findRow_(sh, key) {
  var last = sh.getLastRow();
  if (last < 2) return -1;
  var keys = sh.getRange(2, 1, last - 1, 1).getValues();
  for (var i = 0; i < keys.length; i++) {
    if (String(keys[i][0]) === key) return i + 2;
  }
  return -1;
}

function readFavorites_(key) {
  var sh = sheet_();
  var row = findRow_(sh, key);
  if (row === -1) {
    return { key: key, player_ids: [], updated_at: null, last_seen_iso: null };
  }
  var vals = sh.getRange(row, 1, 1, HEADERS.length).getValues()[0];
  var ids = [];
  try {
    ids = JSON.parse(vals[1] || '[]');
  } catch (err) {
    ids = [];
  }
  return {
    key: key,
    player_ids: ids,
    updated_at: vals[2] || null,
    last_seen_iso: isoOf_(vals[4]),
  };
}

/**
 * Save a favorites list.
 *
 * This rewrites the whole row, so it MUST carry last_seen_iso through
 * untouched: setValues writes exactly the array it is given, and a 4-element
 * record against a 5-column range would blank the Catch Me Up cursor on every
 * star toggle. Read the existing value first, then put it back.
 */
function writeFavorites_(key, ids, note) {
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var sh = sheet_();
    var row = findRow_(sh, key);
    var lastSeen = '';
    if (row !== -1) {
      lastSeen = isoOf_(sh.getRange(row, 5, 1, 1).getValue()) || '';
    }
    var record = [key, JSON.stringify(ids), new Date().toISOString(), note, lastSeen];
    if (row === -1) {
      sh.appendRow(record);
    } else {
      sh.getRange(row, 1, 1, HEADERS.length).setValues([record]);
    }
    return { key: key, player_ids: ids, saved: true };
  } finally {
    lock.releaseLock();
  }
}

/**
 * Advance the Catch Me Up cursor.
 *
 * Touches only column 5, which is what keeps this direction safe: the
 * favorites list and note are never in the write range, so they cannot be
 * clobbered here no matter what else is in the row.
 */
function markSeen_(key, iso) {
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var sh = sheet_();
    var row = findRow_(sh, key);
    if (row === -1) {
      // No favorites row yet - create one with an empty list rather than
      // making the client save a favorite before it can track "seen".
      sh.appendRow([key, '[]', '', '', iso]);
      return { key: key, last_seen_iso: iso };
    }
    sh.getRange(row, 5, 1, 1).setValue(iso);
    return { key: key, last_seen_iso: iso };
  } finally {
    lock.releaseLock();
  }
}


// ── push subscriptions ────────────────────────────────────────────────────────
//
// One row per browser subscription in the 'push' tab, keyed by the endpoint URL (unique per browser and device). `key`
// is the Catch Me Up name slug, so the sender can look up that name's favorites; one name can have several rows (phone
// and laptop). There are no accounts: knowing your own endpoint is what authorizes changing your own row, and the
// endpoint is never returned by a public read - the sender's push_list needs PUSH_SECRET from Script Properties.

var PUSH_SHEET = 'push';
var PUSH_HEADERS = ['key', 'endpoint', 'subscription_json', 'prefs_json', 'created_at_iso', 'last_ok_iso'];

function pushSheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(PUSH_SHEET);
  if (!sh) {
    sh = ss.insertSheet(PUSH_SHEET);
  }
  if (sh.getLastRow() === 0) {
    sh.appendRow(PUSH_HEADERS);
  }
  return sh;
}

// Delivery receipts: each phone's service worker reports a push the moment it shows it (docs/sw.js), so a missing alert
// can be told apart: sent by the sender log but no receipt = never reached the phone; receipt but nothing on screen = iOS
// hid it (Focus, notification summary). `id` is the first 6 hex chars of sha1(endpoint), the same id push_send.py logs.
var RECEIPT_SHEET = 'push_receipts';
var RECEIPT_KEEP = 600;

function receiptSheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(RECEIPT_SHEET);
  if (!sh) {
    sh = ss.insertSheet(RECEIPT_SHEET);
    sh.appendRow(['received_at_iso', 'device_id', 'title', 'body', 'shown']);
  }
  return sh;
}

function pushFindRow_(sh, endpoint) {
  var last = sh.getLastRow();
  if (last < 2 || !endpoint) return -1;
  var eps = sh.getRange(2, 2, last - 1, 1).getValues();
  for (var i = 0; i < eps.length; i++) {
    if (String(eps[i][0]) === endpoint) return i + 2;
  }
  return -1;
}

function pushSecretOk_(secret) {
  var want = PropertiesService.getScriptProperties().getProperty('PUSH_SECRET');
  return !!want && String(secret || '') === want;
}

function cleanPrefs_(prefs) {
  // Tiny whitelist so a client cannot stuff arbitrary data into the sheet:
  //   teams: { ABC: { m: 0|1|2 (off / key moments / every play), e: 0|1 (game ends) } }, favs: 0|1 (key moments from my favorites)
  var out = { teams: {}, favs: prefs && prefs.favs ? 1 : 0 };
  var t = (prefs && prefs.teams) || {};
  Object.keys(t).slice(0, 64).forEach(function (abbr) {
    var a = String(abbr).toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 5);
    if (!a) return;
    var m = Math.max(0, Math.min(2, parseInt(t[abbr] && t[abbr].m, 10) || 0));
    var g = t[abbr] && t[abbr].e ? 1 : 0;
    if (m || g) out.teams[a] = { m: m, e: g };
  });
  return out;
}

function pushAction_(body) {
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    if (body.action === 'push_received') {
      var id = String(body.id || '');
      if (!/^[0-9a-f]{6}$/.test(id)) return { error: 'bad id' };
      var rs = receiptSheet_();
      rs.appendRow([new Date().toISOString(), id, String(body.title || '').slice(0, 120), String(body.body || '').slice(0, 160), body.shown ? 'yes' : 'no']);
      var over = rs.getLastRow() - 1 - RECEIPT_KEEP;
      if (over > 100) rs.deleteRows(2, over);   // keep the newest RECEIPT_KEEP rows, trimmed in batches
      return { ok: true };
    }
    var sh = pushSheet_();
    var sub = body.subscription || {};
    var endpoint = String(body.endpoint || sub.endpoint || '');
    var now = new Date().toISOString();

    if (body.action === 'push_prune') {
      if (!pushSecretOk_(body.secret)) return { error: 'forbidden' };
      var dead = (body.endpoints || []).map(String);
      var removed = 0;
      // bottom-up so row numbers stay valid while deleting
      for (var r = sh.getLastRow(); r >= 2; r--) {
        if (dead.indexOf(String(sh.getRange(r, 2).getValue())) !== -1) {
          sh.deleteRow(r);
          removed++;
        }
      }
      return { removed: removed };
    }
    if (body.action === 'push_ok') {
      if (!pushSecretOk_(body.secret)) return { error: 'forbidden' };
      var okEps = (body.endpoints || []).map(String);
      var lastRow = sh.getLastRow();
      if (lastRow >= 2) {
        var eps = sh.getRange(2, 2, lastRow - 1, 1).getValues();
        for (var i = 0; i < eps.length; i++) {
          if (okEps.indexOf(String(eps[i][0])) !== -1) sh.getRange(i + 2, 6).setValue(now);
        }
      }
      return { ok: true };
    }

    if (endpoint.indexOf('https://') !== 0) return { error: 'missing endpoint' };
    var row = pushFindRow_(sh, endpoint);

    if (body.action === 'push_subscribe') {
      var key = normalizeKey_(body.key);
      var rec = [key, endpoint, JSON.stringify(sub), JSON.stringify(cleanPrefs_(body.prefs)), now, ''];
      if (row === -1) {
        sh.appendRow(rec);
      } else {
        // same browser re-subscribing (or switching name): keep created_at, refresh the rest
        var created = sh.getRange(row, 5).getValue() || now;
        rec[4] = created;
        sh.getRange(row, 1, 1, PUSH_HEADERS.length).setValues([rec]);
      }
      return { subscribed: true };
    }
    if (body.action === 'push_prefs') {
      if (row === -1) return { error: 'not subscribed' };
      sh.getRange(row, 4).setValue(JSON.stringify(cleanPrefs_(body.prefs)));
      return { saved: true };
    }
    if (body.action === 'push_get') {
      if (row === -1) return { subscribed: false };
      var p = {};
      try { p = JSON.parse(sh.getRange(row, 4).getValue() || '{}'); } catch (err) { p = {}; }
      return { subscribed: true, prefs: p, key: String(sh.getRange(row, 1).getValue()) };
    }
    if (body.action === 'push_unsubscribe') {
      if (row !== -1) sh.deleteRow(row);
      return { unsubscribed: true };
    }
    return { error: 'unknown push action' };
  } finally {
    lock.releaseLock();
  }
}

// For the sender only (GitHub Actions): every subscription with its prefs and its name's favorites.
function pushList_(secret) {
  if (!pushSecretOk_(secret)) return { error: 'forbidden' };
  var sh = pushSheet_();
  var last = sh.getLastRow();
  var out = [];
  var favCache = {};
  if (last >= 2) {
    var vals = sh.getRange(2, 1, last - 1, PUSH_HEADERS.length).getValues();
    vals.forEach(function (v) {
      var sub = null, prefs = {};
      try { sub = JSON.parse(v[2]); } catch (err) { return; }
      try { prefs = JSON.parse(v[3] || '{}'); } catch (err) { prefs = {}; }
      var key = String(v[0]);
      if (!(key in favCache)) favCache[key] = key ? readFavorites_(key).player_ids : [];
      out.push({ key: key, endpoint: String(v[1]), subscription: sub, prefs: prefs, player_ids: favCache[key] });
    });
  }
  return { subs: out };
}

// ── util ──────────────────────────────────────────────────────────────────────

function centralNow_() {
  return Utilities.formatDate(new Date(), CURSOR_TZ, CURSOR_FORMAT);
}

/**
 * Read a cursor cell back as a naive Central string.
 *
 * Sheets sometimes coerces a written timestamp into a real Date value, and
 * getValue would then hand back a Date whose JSON form is UTC-with-Z - the
 * exact format that breaks the client's lexicographic comparison. Normalize
 * on the way out so the wire format is always the naive one.
 */
function isoOf_(v) {
  if (v === null || v === undefined || v === '') return null;
  if (v instanceof Date) return Utilities.formatDate(v, CURSOR_TZ, CURSOR_FORMAT);
  return String(v);
}

function normalizeKey_(raw) {
  return String(raw || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60);
}

function json_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
