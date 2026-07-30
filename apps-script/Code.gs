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
var HEADERS = ['key', 'player_ids_json', 'updated_at_iso', 'note'];

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
    return { key: key, player_ids: [], updated_at: null };
  }
  var vals = sh.getRange(row, 1, 1, HEADERS.length).getValues()[0];
  var ids = [];
  try {
    ids = JSON.parse(vals[1] || '[]');
  } catch (err) {
    ids = [];
  }
  return { key: key, player_ids: ids, updated_at: vals[2] || null };
}

function writeFavorites_(key, ids, note) {
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var sh = sheet_();
    var row = findRow_(sh, key);
    var record = [key, JSON.stringify(ids), new Date().toISOString(), note];
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

// ── util ──────────────────────────────────────────────────────────────────────

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
