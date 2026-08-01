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
