/* Klikk Journals — Excel task pane.
 *
 * Reads the Klikk Financials general ledger (a mirror of Xero) into a worksheet
 * and refreshes it in place. Read-only by design: nothing here writes back to
 * Postgres or to Xero.
 */
(function () {
  'use strict';

  window.__klikkAppLoaded = true;

  var DEFAULT_BASE = 'https://console.8-bit.space/backend';
  var PAGE_SIZE = 1000;
  var WRITE_CHUNK = 2000;
  /* Sections.
   *
   * The pane used to show every panel stacked, which is fine at three panels
   * and unreadable at six. The ribbon tab now addresses each one by URL
   * fragment (taskpane.html#cube), so a ribbon button and the in-pane nav are
   * two doors into the same state rather than two separate mechanisms.
   *
   * Excel will not always re-navigate a pane that is already open, so the
   * in-pane nav is the reliable path and the fragment is the convenience. */
  var SECTIONS = {
    query:    'queryPanel',
    detail:   'detailPanel',
    cube:     'cubePanel',
    sheet:    'refreshPanel',
    comments: 'commentPanel',
    settings: 'settingsPanel'
  };
  var DEFAULT_SECTION = 'query';
  var currentSection = DEFAULT_SECTION;
  var connected = false;

  function sectionFromHash() {
    var h = (window.location.hash || '').replace(/^#/, '');
    return SECTIONS[h] ? h : null;
  }

  function applySection() {
    // Nothing but Connection is meaningful before a token is accepted, so an
    // unconnected pane always lands there regardless of which button was hit.
    var want = connected ? currentSection : 'settings';
    Object.keys(SECTIONS).forEach(function (k) {
      var node = el[SECTIONS[k]];
      if (node) node.hidden = (k !== want);
    });
    var nav = document.getElementById('sectionNav');
    if (nav) {
      Array.prototype.forEach.call(nav.querySelectorAll('button'), function (b) {
        b.className = 'navbtn' + (b.dataset.section === want ? ' navbtn--on' : '');
        b.disabled = !connected && b.dataset.section !== 'settings';
      });
    }
  }

  function showSection(name) {
    if (!SECTIONS[name]) return;
    currentSection = name;
    applySection();
  }

  var SETTING_PREFIX = 'klikkJournalQuery::';
  // Cube/pivot cell comments — GET lists them, POST upserts one, and
  // <id>/status/ marks one actioned. Server route: journals/pivot/comments/.
  var COMMENT_API = '/xero/data/journals/pivot/comments/';

  // Column order for the sheet. `fmt` drives the Excel number format.
  var COLUMNS = [
    { key: 'date',                    label: 'Date',       fmt: 'date',  width: 11 },
    { key: 'journal_number',          label: 'Jrnl #',     fmt: 'int',   width: 8  },
    { key: 'journal_type',            label: 'Type',       fmt: null,    width: 13 },
    { key: 'fin_year',                label: 'Fin year',   fmt: null,    width: 10 },
    { key: 'tenant_name',             label: 'Entity',     fmt: null,    width: 16 },
    { key: 'report',                  label: 'Report',     fmt: null,    width: 16 },
    { key: 'account_class',           label: 'Acct class', fmt: null,    width: 12 },
    { key: 'account_code',            label: 'Acct code',  fmt: null,    width: 12 },
    { key: 'account_name',            label: 'Account',    fmt: null,    width: 28 },
    { key: 'account_type',            label: 'Acct type',  fmt: null,    width: 12 },
    { key: 'supplier_name',           label: 'Supplier / contact', fmt: null, width: 30 },
    { key: 'supplier_via',            label: 'Name from',  fmt: null,    width: 10 },
    { key: 'description',             label: 'Description', fmt: null,   width: 34 },
    { key: 'reference',               label: 'Reference',  fmt: null,    width: 18 },
    { key: 'debit',                   label: 'Debit',      fmt: 'money', width: 13 },
    { key: 'credit',                  label: 'Credit',     fmt: 'money', width: 13 },
    { key: 'amount',                  label: 'Amount',     fmt: 'money', width: 13 },
    { key: 'tax_amount',              label: 'Tax',        fmt: 'money', width: 11 },
    { key: 'tracking1_category',      label: 'Tracking 1 category', fmt: null, width: 18 },
    { key: 'tracking1',               label: 'Tracking 1', fmt: null,    width: 20 },
    { key: 'tracking2_category',      label: 'Tracking 2 category', fmt: null, width: 18 },
    { key: 'tracking2',               label: 'Tracking 2', fmt: null,    width: 16 },
    { key: 'transaction_source_type', label: 'Source',     fmt: null,    width: 14 },
    { key: 'transaction_source_id',   label: 'Source ID',  fmt: null,    width: 20 },
    { key: 'id',                      label: 'Row ID',     fmt: 'int',   width: 10 }
  ];

  var MONEY_FMT = '#,##0.00;[Red]-#,##0.00';
  var DATE_FMT = 'yyyy-mm-dd';

  var settings = { baseUrl: DEFAULT_BASE, token: '' };
  var cancelFlag = { cancelled: false };
  var busy = false;
  var el = {};

  /* ── boot ──────────────────────────────────────────────── */

  Office.onReady(function (info) {
    window.__klikkOnReadyFired = true;
    window.__klikkHost = String(info && info.host);

    // Gate on the capability, not on the reported host. Some Office builds hand
    // back a host value that doesn't match Office.HostType.Excel (and on others
    // Office.HostType itself is undefined, which threw a TypeError that Office's
    // own promise chain swallowed — a silent permanent "Loading…"). Excel.run is
    // what this add-in actually needs, so test for that.
    if (typeof Excel === 'undefined' || typeof Excel.run !== 'function') {
      window.__klikkAppReportedError = true;
      document.getElementById('boot').textContent =
        'This add-in needs the Excel JavaScript API, which is not available here'
        + (info && info.host ? ' (host reported: ' + info.host + ').' : '.');
      return;
    }
    [
      'app', 'boot', 'connLabel', 'btnSettings', 'settingsPanel', 'baseUrl', 'token',
      'btnConnect', 'btnForget', 'settingsMsg', 'queryPanel', 'tenant', 'journalType',
      'typeHint', 'dateFrom', 'dateTo', 'account', 'accountList', 'contact', 'reference',
      'contactList', 'description', 'amount', 'q', 'maxRows', 'countLine', 'btnLoad', 'btnCount',
      'detailPanel', 'btnPivot', 'cubePanel', 'measure', 'btnResetComments',
      'queryPanel', 'refreshPanel', 'commentPanel', 'settingsPanel',
      'suppress', 'btnCube', 'cubeMsg', 'btnDrill', 'btnReload', 'wellAvail', 'wellRows',
      'wellCols', 'wellFilt', 'autoBuild', 'outline',
      'picker', 'pickerTitle', 'pickerClose', 'pickerSearch', 'pickerAll',
      'pickerNone', 'pickerCount', 'pickerList',
      'commentPanel', 'commentAuthor', 'btnSyncComments', 'commentMsg',
      'btnFullPivot', 'selNone', 'selHas', 'selPath', 'selVal', 'selComment',
      'btnSaveComment', 'btnDeleteComment', 'selBox', 'markCells', 'btnPushComments',
      'refreshPanel', 'sheetInfo', 'btnRefresh', 'btnRestore', 'progressPanel',
      'progressMsg', 'progressFill', 'btnCancel', 'errorMsg'
    ].forEach(function (id) { el[id] = document.getElementById(id); });
    window.__klikkStep = 'ids';

    try {
      window.__klikkStep = 'loadSettings';
      loadSettings();
      window.__klikkStep = 'wireEvents';
      wireEvents();
      window.__klikkStep = 'wired';
    } catch (e) {
      // Claim the boot panel so the watchdog does not overwrite this with a
      // vaguer message — that clobbering is what hid the real error before.
      window.__klikkAppReportedError = true;
      document.getElementById('boot').innerHTML =
        '<p style="margin:0 0 8px;font-weight:600">Klikk Journals failed to start.</p>' +
        '<pre style="white-space:pre-wrap;font-size:11px;margin:0">' +
        esc('at step: ' + window.__klikkStep + '\n' + (e && e.message ? e.message : String(e))) +
        '</pre>';
      return;
    }

    window.__klikkBootDone = true;
    el.boot.hidden = true;
    el.app.hidden = false;

    if (settings.token) {
      connect(true);
    } else {
      el.settingsPanel.hidden = false;
    }
    inspectActiveSheet();
  });

  function wireEvents() {
    el.btnSettings.addEventListener('click', function () {
      el.settingsPanel.hidden = !el.settingsPanel.hidden;
    });
    el.btnConnect.addEventListener('click', function () { connect(false); });
    el.btnForget.addEventListener('click', forget);
    el.btnLoad.addEventListener('click', function () { run(loadToNewSheet); });
    el.btnCount.addEventListener('click', function () { run(showCount); });
    el.btnCube.addEventListener('click', function () { run(buildCube); });
    el.btnPivot.addEventListener('click', function () { run(addNativePivot); });
    el.btnReload.addEventListener('click', function () { run(reloadThisSheet); });
    el.btnSyncComments.addEventListener('click', function () { run(syncComments); });
    el.btnResetComments.addEventListener('click', function () { run(resetSheetComments); });
    var nav = document.getElementById('sectionNav');
    if (nav) {
      nav.addEventListener('click', function (ev) {
        var b = ev.target.closest('button[data-section]');
        if (b) showSection(b.dataset.section);
      });
    }
    currentSection = sectionFromHash() || DEFAULT_SECTION;
    window.addEventListener('hashchange', function () {
      var h = sectionFromHash();
      if (h) showSection(h);
    });
    el.btnFullPivot.addEventListener('click', function () { run(pivotFromFullDetail); });
    el.btnPushComments.addEventListener('click', function () { run(pushCommentsToSheet); });
    el.btnSaveComment.addEventListener('click', function () { run(saveSelectedComment); });
    el.btnDeleteComment.addEventListener('click', function () { run(deleteSelectedComment); });
    el.btnDrill.addEventListener('click', function () { run(drillSelection); });
    watchSelection();
    el.btnRefresh.addEventListener('click', function () { run(refreshActiveSheet); });
    el.btnRestore.addEventListener('click', restoreFiltersFromSheet);
    el.btnCancel.addEventListener('click', function () { cancelFlag.cancelled = true; });
    el.journalType.addEventListener('change', updateTypeHint);

    // Track sheet switches so the Refresh panel always describes what is in front.
    Excel.run(function (ctx) {
      ctx.workbook.worksheets.onActivated.add(function () {
        return inspectActiveSheet();
      });
      return ctx.sync();
    }).catch(function () { /* older hosts: user can still hit Refresh */ });
  }

  /* ── settings ──────────────────────────────────────────── */

  /* Credential storage.
   *
   * NOT Office.context.roamingSettings — that is part of the Outlook/Mailbox
   * API and is undefined in Excel; reading it threw and left the pane stuck.
   * NOT Office.context.document.settings either: that persists inside the
   * workbook, so the token would travel to anyone the file is shared with.
   * localStorage is scoped to this add-in's origin on this machine, which is
   * the property we actually want. */
  var LS_BASE = 'klikk.baseUrl';
  var LS_TOKEN = 'klikk.token';

  function lsGet(k) {
    try { return window.localStorage.getItem(k) || ''; } catch (e) { return ''; }
  }

  function lsSet(k, v) {
    try { window.localStorage.setItem(k, v); return true; } catch (e) { return false; }
  }

  function loadSettings() {
    settings.baseUrl = lsGet(LS_BASE) || DEFAULT_BASE;
    settings.token = lsGet(LS_TOKEN);
    el.baseUrl.value = settings.baseUrl;
    el.token.value = settings.token;
  }

  function persistSettings() {
    lsSet(LS_BASE, settings.baseUrl);
    lsSet(LS_TOKEN, settings.token);
    return Promise.resolve();
  }

  function forget() {
    settings.token = '';
    el.token.value = '';
    persistSettings().then(function () {
      setConnected(false);
      el.settingsMsg.textContent = 'Token cleared.';
      el.settingsMsg.className = 'msg';
    });
  }

  async function connect(silent) {
    settings.baseUrl = (el.baseUrl.value || DEFAULT_BASE).replace(/\/+$/, '');
    settings.token = (el.token.value || '').trim();
    if (!settings.token) {
      el.settingsMsg.textContent = 'Paste the add-in API token first.';
      el.settingsMsg.className = 'msg msg--err';
      return;
    }
    el.settingsMsg.textContent = 'Checking…';
    el.settingsMsg.className = 'msg';
    try {
      await persistSettings();
      var opts = await apiGet('/xero/data/journals/filters/', {});
      populateFilters(opts);
      var cat = await apiGet('/xero/data/journals/pivot/dimensions/', {});
      populateCube(cat);
      setConnected(true);
      el.settingsMsg.textContent = 'Connected.';
      el.settingsMsg.className = 'msg msg--ok';
      if (silent) el.settingsPanel.hidden = true;
      else setTimeout(function () { el.settingsPanel.hidden = true; }, 700);
    } catch (e) {
      setConnected(false);
      el.settingsPanel.hidden = false;
      el.settingsMsg.textContent = e.message;
      el.settingsMsg.className = 'msg msg--err';
    }
  }

  function setConnected(ok) {
    el.connLabel.textContent = ok ? 'Connected to Klikk Financials' : 'Not connected';
    el.connLabel.className = ok ? 'hd__sub hd__sub--ok' : 'hd__sub';
    connected = ok;
    applySection();
  }

  function populateFilters(opts) {
    fill(el.tenant, opts.tenants || [], 'All entities', function (t) {
      return { value: t.tenant_id, label: t.tenant_name || t.tenant_id };
    });
    fill(el.journalType, (opts.journal_types || []).map(function (t) { return t; }), 'All types',
      function (t) { return { value: t, label: t }; });

    el.accountList.innerHTML = '';
    (opts.accounts || []).forEach(function (a) {
      var o = document.createElement('option');
      o.value = a.code;
      o.label = a.code + ' — ' + a.name;
      el.accountList.appendChild(o);
    });

    el.contactList.innerHTML = '';
    (opts.contacts || []).forEach(function (name) {
      var o = document.createElement('option');
      o.value = name;
      el.contactList.appendChild(o);
    });

    // Default to the ledger's real span rather than an arbitrary window.
    if (opts.date_from && !el.dateFrom.value) el.dateFrom.min = opts.date_from;
    if (opts.date_to && !el.dateTo.value) el.dateTo.max = opts.date_to;
    updateTypeHint();
  }

  function fill(select, items, placeholder, map) {
    var keep = select.value;
    select.innerHTML = '';
    var ph = document.createElement('option');
    ph.value = '';
    ph.textContent = placeholder;
    select.appendChild(ph);
    items.forEach(function (item) {
      var m = map(item);
      var o = document.createElement('option');
      o.value = m.value;
      o.textContent = m.label;
      select.appendChild(o);
    });
    select.value = keep;
  }

  function updateTypeHint() {
    el.typeHint.hidden = !!el.journalType.value;
  }

  /* ── API ───────────────────────────────────────────────── */

  async function apiGet(path, params) {
    var url = settings.baseUrl + path;
    var qs = Object.keys(params)
      .filter(function (k) { return params[k] !== '' && params[k] != null; })
      .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]); })
      .join('&');
    if (qs) url += '?' + qs;

    var res;
    try {
      res = await fetch(url, {
        headers: { Authorization: 'Token ' + settings.token, Accept: 'application/json' },
        cache: 'no-store'
      });
    } catch (e) {
      throw new Error('Cannot reach ' + settings.baseUrl + '. Check the URL and your connection.');
    }
    if (res.status === 401 || res.status === 403) {
      throw new Error('Token rejected (' + res.status + '). Check the API token.');
    }
    if (!res.ok) {
      throw new Error('Server returned ' + res.status + ' ' + res.statusText + '.');
    }
    return res.json();
  }

  async function apiPost(path, body) {
    var res;
    try {
      res = await fetch(settings.baseUrl + path, {
        method: 'POST',
        headers: {
          Authorization: 'Token ' + settings.token,
          'Content-Type': 'application/json',
          Accept: 'application/json'
        },
        body: JSON.stringify(body)
      });
    } catch (e) {
      throw new Error('Cannot reach ' + settings.baseUrl + '.');
    }
    if (res.status === 401 || res.status === 403) throw new Error('Token rejected (' + res.status + ').');
    if (!res.ok) throw new Error('Server returned ' + res.status + ' ' + res.statusText + '.');
    return res.json();
  }

  function readQuery() {
    return {
      tenant: el.tenant.value,
      journal_type: el.journalType.value,
      date_from: el.dateFrom.value,
      date_to: el.dateTo.value,
      account: el.account.value.trim(),
      contact: el.contact.value.trim(),
      reference: el.reference.value.trim(),
      description: el.description.value.trim(),
      amount: el.amount.value.trim(),
      q: el.q.value.trim(),
      maxRows: parseInt(el.maxRows.value, 10) || 0
    };
  }

  function applyQuery(qy) {
    el.tenant.value = qy.tenant || '';
    el.journalType.value = qy.journal_type || '';
    el.dateFrom.value = qy.date_from || '';
    el.dateTo.value = qy.date_to || '';
    el.account.value = qy.account || '';
    el.contact.value = qy.contact || '';
    el.reference.value = qy.reference || '';
    el.description.value = qy.description || '';
    el.amount.value = qy.amount || '';
    el.q.value = qy.q || '';
    if (qy.maxRows != null) el.maxRows.value = String(qy.maxRows);
    updateTypeHint();
  }

  function toParams(qy) {
    return {
      tenant: qy.tenant, journal_type: qy.journal_type,
      date_from: qy.date_from, date_to: qy.date_to,
      account: qy.account, contact: qy.contact,
      reference: qy.reference, description: qy.description,
      amount: qy.amount, q: qy.q,
      // Part of the comment anchor on purpose: the same row and column under a
      // different dimension filter is a different figure.
      dimf: qy.dimf || ''
    };
  }

  /* {dimf: '{"fin_year":["FY2026"]}'} — omitted entirely when nothing is
     narrowed, so an unfiltered cube's URL and comment anchors stay as they
     were before filters existed. */
  function dimfParam(spec) {
    var f = spec && spec.filters ? spec.filters : {};
    var live = {};
    Object.keys(f).forEach(function (k) {
      if (f[k] && f[k].length) live[k] = f[k].slice().sort();
    });
    return Object.keys(live).length ? { dimf: JSON.stringify(live) } : {};
  }

  function describe(qy) {
    var bits = [];
    if (qy.tenant) {
      var opt = el.tenant.querySelector('option[value="' + qy.tenant + '"]');
      bits.push(opt ? opt.textContent : 'one entity');
    }
    if (qy.journal_type) bits.push(qy.journal_type);
    if (qy.date_from || qy.date_to) bits.push((qy.date_from || '…') + ' to ' + (qy.date_to || '…'));
    if (qy.account) bits.push('account ' + qy.account);
    if (qy.contact) bits.push('contact ' + qy.contact);
    if (qy.reference) bits.push('ref ' + qy.reference);
    if (qy.description) bits.push('desc ' + qy.description);
    if (qy.amount) bits.push('amount ' + qy.amount);
    if (qy.q) bits.push('"' + qy.q + '"');
    return bits.length ? bits.join(' · ') : 'the whole ledger, unfiltered';
  }

  /* ── fetching ──────────────────────────────────────────── */

  async function fetchRows(qy) {
    var params = toParams(qy);
    var rows = [];
    var offset = 0;
    var total = null;
    var cap = qy.maxRows > 0 ? qy.maxRows : Infinity;

    while (true) {
      if (cancelFlag.cancelled) break;
      var page = await apiGet('/xero/data/journals/search/',
        Object.assign({}, params, { limit: PAGE_SIZE, offset: offset }));
      if (total === null) total = page.count;
      rows = rows.concat(page.results);
      offset += page.results.length;

      progress(rows.length, Math.min(total, cap), 'Fetched ' + fmtNum(rows.length) + ' rows…');

      if (page.results.length < PAGE_SIZE) break;
      if (rows.length >= cap) break;
    }
    if (rows.length > cap) rows.length = cap;
    return { rows: rows, total: total || 0 };
  }

  /* ── Excel rendering ───────────────────────────────────── */

  function toSerial(iso) {
    if (!iso) return '';
    var p = iso.split('-');
    if (p.length !== 3) return iso;
    return Date.UTC(+p[0], +p[1] - 1, +p[2]) / 86400000 + 25569;
  }

  function toNum(v) {
    if (v === '' || v == null) return '';
    var n = Number(v);
    return isNaN(n) ? v : n;
  }

  function toMatrix(rows) {
    return rows.map(function (r) {
      return COLUMNS.map(function (c) {
        var v = r[c.key];
        if (c.fmt === 'date') return toSerial(v);
        if (c.fmt === 'money' || c.fmt === 'int') return toNum(v);
        return v == null ? '' : String(v);
      });
    });
  }

  /* Writes `rows` onto a worksheet. `targetId` null => create a new sheet. */
  async function renderRows(targetId, rows, qy) {
    var header = COLUMNS.map(function (c) { return c.label; });
    var matrix = toMatrix(rows);
    var sheetId = targetId;
    var sheetName = '';

    // 1. Prepare the sheet (create or clear) and lay down the header.
    var keepTable = null;
    var prevRows = 0;

    await Excel.run(async function (ctx) {
      var sheet;
      if (targetId) {
        sheet = ctx.workbook.worksheets.getItem(targetId);
        var tables = sheet.tables;
        tables.load('items/name');
        var used = sheet.getUsedRange(true);
        used.load('rowCount');
        await ctx.sync();
        prevRows = used.rowCount || 0;
        // Reuse the existing table rather than dropping it, so a PivotTable
        // built on this sheet keeps its source and refreshes with the data.
        keepTable = tables.items.length ? tables.items[0].name : null;
        if (!keepTable) sheet.getRange().clear(Excel.ClearApplyTo.all);
      } else {
        sheet = ctx.workbook.worksheets.add(await uniqueSheetName(ctx));
      }
      sheet.load('id,name');
      sheet.getRangeByIndexes(0, 0, 1, header.length).values = [header];
      sheet.activate();
      await ctx.sync();
      sheetId = sheet.id;
      sheetName = sheet.name;
    });

    // 2. Write the body in chunks so a large pull does not blow one sync.
    for (var start = 0; start < matrix.length; start += WRITE_CHUNK) {
      if (cancelFlag.cancelled) break;
      var chunk = matrix.slice(start, start + WRITE_CHUNK);
      var at = start;
      await Excel.run(async function (ctx) {
        var sheet = ctx.workbook.worksheets.getItem(sheetId);
        sheet.getRangeByIndexes(at + 1, 0, chunk.length, header.length).values = chunk;
        await ctx.sync();
      });
      progress(start + chunk.length, matrix.length,
        'Writing ' + fmtNum(start + chunk.length) + ' of ' + fmtNum(matrix.length) + ' rows…');
    }

    // 3. Format: table, number formats, widths, frozen header.
    await Excel.run(async function (ctx) {
      var sheet = ctx.workbook.worksheets.getItem(sheetId);
      var rowCount = matrix.length + 1;
      var body = sheet.getRangeByIndexes(0, 0, rowCount, header.length);

      if (matrix.length > 0 && keepTable) {
        sheet.tables.getItem(keepTable).resize(body);
      } else if (matrix.length > 0) {
        var table = sheet.tables.add(body, true);
        // Named from the sheet, not the clock: stable across refreshes so a
        // PivotTable pointed at it stays pointed at it.
        table.name = 'K_' + sheetName.replace(/[^A-Za-z0-9]/g, '_');
        table.style = 'TableStyleLight8';
      } else {
        sheet.getRangeByIndexes(0, 0, 1, header.length).format.font.bold = true;
      }

      COLUMNS.forEach(function (c, i) {
        var col = sheet.getRangeByIndexes(1, i, Math.max(matrix.length, 1), 1);
        if (c.fmt === 'money') col.numberFormat = [[MONEY_FMT]];
        else if (c.fmt === 'date') col.numberFormat = [[DATE_FMT]];
        else if (c.fmt === 'int') col.numberFormat = [['0']];
        sheet.getRangeByIndexes(0, i, 1, 1).format.columnWidth = c.width * 7.5;
      });

      // A shorter result must not leave last refresh's rows stranded below.
      if (prevRows > rowCount) {
        sheet.getRangeByIndexes(rowCount, 0, prevRows - rowCount, header.length)
          .clear(Excel.ClearApplyTo.all);
      }

      sheet.freezePanes.freezeRows(1);
      await ctx.sync();
    });

    await bindQuery(sheetId, qy, rows.length, 'detail', null);
    // Remembered so a PivotTable built later through Excel's own Insert dialog
    // can inherit the filters that produced these rows.
    lastDetail = { sheetId: sheetId, query: qy, rows: rows.length };
    return { sheetId: sheetId, sheetName: sheetName };
  }

  async function uniqueSheetName(ctx, prefix) {
    prefix = prefix || 'Journals';
    var sheets = ctx.workbook.worksheets;
    sheets.load('items/name');
    await ctx.sync();
    var taken = sheets.items.map(function (s) { return s.name; });
    var n = 1;
    var name = prefix;
    while (taken.indexOf(name) !== -1) { n += 1; name = prefix + ' ' + n; }
    return name;
  }

  /* ── per-sheet query binding (lives in the workbook) ───── */

  var lastDetail = null;

  function bindQuery(sheetId, qy, rowCount, kind, spec) {
    var s = Office.context.document.settings;
    s.set(SETTING_PREFIX + sheetId, JSON.stringify({
      query: qy,
      kind: kind || 'detail',
      spec: spec || null,
      rows: rowCount,
      loadedAt: new Date().toISOString()
    }));
    return new Promise(function (resolve) { s.saveAsync(function () { resolve(); }); });
  }

  function readBinding(sheetId) {
    var raw = Office.context.document.settings.get(SETTING_PREFIX + sheetId);
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (e) { return null; }
  }

  var activeSheet = { id: null, name: '', binding: null };

  /* (declared above) The last detail sheet loaded this session. A PivotTable the user builds
   * through Excel's own Insert > PivotTable lands on a sheet Excel created, so
   * it carries no binding of ours -- we adopt it below and inherit the filter
   * context from here, because a comment's anchor is meaningless without it. */
  var lastInspected = null;

  async function inspectActiveSheet() {
    var adopt = null;
    try {
      await Excel.run(async function (ctx) {
        var sheet = ctx.workbook.worksheets.getActiveWorksheet();
        sheet.load('id,name');
        await ctx.sync();
        activeSheet.id = sheet.id;
        activeSheet.name = sheet.name;
        activeSheet.binding = readBinding(sheet.id);

        // No binding? It may still be a PivotTable the user built themselves.
        if (!activeSheet.binding) {
          var pts = sheet.pivotTables;
          pts.load('items/name');
          await ctx.sync();
          if (pts.items.length) adopt = pts.items[0].name;
        }
      });
    } catch (e) {
      activeSheet = { id: null, name: '', binding: null };
    }

    if (adopt && lastDetail) {
      await bindQuery(activeSheet.id, lastDetail.query, lastDetail.rows, 'pivot',
        { pivotName: adopt, sourceSheet: lastDetail.sheetId, adopted: true });
      activeSheet.binding = readBinding(activeSheet.id);
    } else if (adopt) {
      // A pivot with no detail sheet loaded this session: we cannot honestly
      // say which filters produced it, and a comment anchored to a guessed
      // context would attach to the wrong number later.
      activeSheet.binding = null;
      activeSheet.orphanPivot = adopt;
    } else {
      activeSheet.orphanPivot = null;
    }

    // Landing on a different cube sheet re-points the wells at it. Guarded on
    // an actual sheet change so it never overwrites edits mid-typing.
    if (activeSheet.id && activeSheet.id !== lastInspected) {
      lastInspected = activeSheet.id;
      if (DIMS.length) syncCubeToSheet();
    }
    paintRefreshPanel();
  }

  function paintRefreshPanel() {
    var b = activeSheet.binding;
    if (!b) {
      el.sheetInfo.innerHTML = activeSheet.orphanPivot
        ? '<strong>' + esc(activeSheet.name) + '</strong> holds a PivotTable, but no Klikk '
          + 'detail sheet has been loaded this session, so the pane cannot tell which '
          + 'filters produced it. Load the query again, then reopen this sheet — comments '
          + 'need that context to stay pinned to the right figure.'
        : activeSheet.name
          ? '<strong>' + esc(activeSheet.name) + '</strong> has no Klikk query bound to it. '
            + 'Load a query to a new sheet first.'
          : 'No Klikk query is bound to the active sheet.';
      el.btnRefresh.disabled = true;
      el.btnRestore.disabled = true;
      el.btnPivot.disabled = true;
      el.btnReload.disabled = true;
      el.btnSyncComments.disabled = true;
      el.btnResetComments.disabled = true;
      return;
    }
    var when = new Date(b.loadedAt);
    var kindNote = '';
    if (b.kind === 'cube' && b.spec) {
      kindNote = 'Cube — ' + esc(b.spec.measure) + ' by '
        + esc((b.spec.rows || []).join(' / '))
        + ((b.spec.cols || []).length ? ' across ' + esc(b.spec.cols.join(' / ')) : '')
        + '<br>';
    }
    el.sheetInfo.innerHTML =
      '<strong>' + esc(activeSheet.name) + '</strong> — ' + fmtNum(b.rows) + ' rows, loaded '
      + esc(when.toLocaleString()) + '.<br>' + kindNote + esc(describe(b.query));
    el.btnRefresh.disabled = false;
    el.btnRestore.disabled = false;
    el.btnPivot.disabled = b.kind !== 'detail';
    el.btnReload.disabled = false;
    el.btnSyncComments.disabled = (b.kind !== 'cube' && b.kind !== 'pivot');
    el.btnResetComments.disabled = el.btnSyncComments.disabled;
  }

  function restoreFiltersFromSheet() {
    if (!activeSheet.binding) return;
    applyQuery(activeSheet.binding.query);
    if (activeSheet.binding.kind === 'cube' && activeSheet.binding.spec) {
      applyCubeSpec(activeSheet.binding.spec);
    }
    el.countLine.textContent = 'Filters loaded from ' + activeSheet.name + '.';
  }

  /* ── actions ───────────────────────────────────────────── */

  async function showCount() {
    var qy = readQuery();
    var page = await apiGet('/xero/data/journals/search/',
      Object.assign({}, toParams(qy), { limit: 1, offset: 0 }));
    var cap = qy.maxRows > 0 ? qy.maxRows : Infinity;
    var willLoad = Math.min(page.count, cap);
    el.countLine.innerHTML = '<strong>' + fmtNum(page.count) + '</strong> rows match'
      + (willLoad < page.count ? ' — the row limit will load the first ' + fmtNum(willLoad) + '.' : '.');
  }

  async function loadToNewSheet() {
    var qy = readQuery();
    progress(0, 1, 'Querying…');
    var got = await fetchRows(qy);
    if (cancelFlag.cancelled) { el.countLine.textContent = 'Cancelled.'; return; }
    var out = await renderRows(null, got.rows, qy);
    await inspectActiveSheet();
    el.countLine.innerHTML = '<strong>' + fmtNum(got.rows.length) + '</strong> rows written to '
      + esc(out.sheetName) + (got.total > got.rows.length
        ? ' — ' + fmtNum(got.total) + ' matched, capped by the row limit.' : '.');
  }

  async function refreshActiveSheet() {
    var b = activeSheet.binding;
    if (!b) return;
    var sheetId = activeSheet.id;
    progress(0, 1, 'Refreshing ' + activeSheet.name + '…');

    if (b.kind === 'cube') {
      var cube = await fetchCube(b.query, b.spec);
      if (cancelFlag.cancelled) { el.countLine.textContent = 'Cancelled — sheet untouched.'; return; }
      await renderCube(sheetId, cube, b.query, b.spec);
      await inspectActiveSheet();
      el.countLine.innerHTML = 'Cube refreshed — <strong>' + fmtNum(cube.leaf_count) + '</strong> leaf rows.';
      return;
    }

    var got = await fetchRows(b.query);
    if (cancelFlag.cancelled) { el.countLine.textContent = 'Cancelled — sheet untouched.'; return; }
    await renderRows(sheetId, got.rows, b.query);
    await inspectActiveSheet();
    el.countLine.innerHTML = 'Refreshed — <strong>' + fmtNum(got.rows.length) + '</strong> rows.';
  }


  /* ── cube view ─────────────────────────────────────────── */

  /* Field wells. Excel's own field list is native UI we cannot host inside the
     grid, so this is the closest equivalent: drag chips between Fields, Rows and
     Columns, drop to reorder, and the sheet rebuilds from the resulting spec. */
  var DIMS = [];
  var wells = { avail: [], rows: [], cols: [], filt: [] };
  var MAX = { rows: 4, cols: 3, filt: 6 };
  // dimension key -> array of selected labels. Empty array = the field is
  // on Filters but not yet narrowed, which passes everything through.
  var filterVals = {};
  var memberCache = {};

  function populateCube(cat) {
    DIMS = cat.dimensions || [];
    fill(el.measure, cat.measures || [], 'Amount', function (m) {
      return { value: m.key, label: m.label };
    });
    var ph = el.measure.querySelector('option[value=""]');
    if (ph) ph.remove();
    if (!el.measure.value) el.measure.value = 'amount';

    /* Restoring a previous layout must never be able to cost you the field
       wells. populateCube runs inside the connect path, so anything thrown
       here used to surface as "not connected" with an empty Cube panel --
       no Fields, no Rows, no Columns, no Filters. The restore is a
       convenience; the wells are the feature. */
    if (!wells.rows.length && !wells.cols.length) {
      try {
        // The sheet in front wins; then what you last had; then a sane default.
        if (!syncCubeToSheet()) {
          var saved = recallCubeSpec();
          if (saved && (saved.rows || []).length) applyCubeSpec(saved);
        }
      } catch (e) { /* fall through to the default below */ }
      if (!wells.rows.length && !wells.cols.length) {
        wells.rows = ['account_class', 'account'];
        wells.cols = ['fin_year'];
      }
    }
    reflowWells();
    wireWells();
    wirePicker();
  }

  function dimLabel(key) {
    for (var i = 0; i < DIMS.length; i++) if (DIMS[i].key === key) return DIMS[i].label;
    return key;
  }

  function reflowWells() {
    var used = wells.rows.concat(wells.cols).concat(wells.filt);
    wells.avail = DIMS.map(function (d) { return d.key; })
      .filter(function (k) { return used.indexOf(k) === -1; });
    ['avail', 'rows', 'cols', 'filt'].forEach(renderWell);
  }

  function renderWell(zone) {
    var host = { avail: el.wellAvail, rows: el.wellRows,
                 cols: el.wellCols, filt: el.wellFilt }[zone];
    host.innerHTML = '';

    /* An empty well is a thin blank box, which reads as "there is nothing
       here" rather than "drop something here" -- the Filters zone in
       particular looked like a missing feature rather than an empty one. Say
       what the zone is for while it is empty. */
    if (!wells[zone].length) {
      var ph = document.createElement('span');
      ph.className = 'well__ph';
      ph.textContent = {
        avail: 'No fields left — all of them are in use below.',
        rows: 'Drag fields here for rows, or tap R on a field.',
        cols: 'Drag fields here for columns, or tap C on a field.',
        filt: 'Drag a field here (or tap F) to filter — then pick one or more values.'
      }[zone];
      host.appendChild(ph);
      return;
    }
    wells[zone].forEach(function (key, idx) {
      var chip = document.createElement('span');
      chip.className = 'chip';
      chip.dataset.key = key;
      chip.dataset.zone = zone;
      chip.dataset.idx = idx;

      var txt = document.createElement('span');
      txt.className = 'chip__t';
      if (zone === 'filt') {
        var sel = filterVals[key] || [];
        txt.textContent = dimLabel(key) + (sel.length
          ? ' · ' + (sel.length === 1 ? sel[0] : sel.length + ' selected')
          : ' · all');
      } else {
        txt.textContent = dimLabel(key);
      }
      chip.appendChild(txt);

      var acts = document.createElement('span');
      acts.className = 'chip__acts';
      function btn(act, glyph, title) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'chip__b';
        b.dataset.act = act;
        b.textContent = glyph;
        b.title = title;
        return b;
      }
      if (zone === 'avail') {
        acts.appendChild(btn('toRows', 'R', 'Move to Rows'));
        acts.appendChild(btn('toCols', 'C', 'Move to Columns'));
        acts.appendChild(btn('toFilt', 'F', 'Move to Filters'));
      } else if (zone === 'filt') {
        acts.appendChild(btn('pick', '\u25be', 'Choose values'));
        acts.appendChild(btn('remove', '\u00d7', 'Remove'));
      } else {
        acts.appendChild(btn('left', '\u2039', 'Move earlier'));
        acts.appendChild(btn('right', '\u203a', 'Move later'));
        acts.appendChild(btn('remove', '\u00d7', 'Remove'));
      }
      chip.appendChild(acts);
      host.appendChild(chip);
    });
  }

  function reorder(zone, from, to) {
    if (to < 0 || to >= wells[zone].length) return;
    var arr = wells[zone];
    var k = arr.splice(from, 1)[0];
    arr.splice(to, 0, k);
    reflowWells();
    rememberCubeSpec();
    if (el.autoBuild.checked && wells.rows.length) run(buildCube);
  }

  /* Dragging.

     Not HTML5 drag-and-drop: dragstart/drop do not fire reliably in Excel's
     macOS webview, which is what left the wells looking interactive but inert.
     Pointer events do work, so the drag is built by hand -- which also means
     it has to draw its own feedback, since there is no native drag image.

     A chip only becomes a drag after DRAG_SLOP pixels, so a tap that jitters
     is still a tap and still hits the R / C / F buttons.

     Every action also has a button. Dragging is the fast path, not the only
     path. */
  var DRAG_SLOP = 5;
  var ptr = null;
  var ghost = null;
  var wellsWired = false;

  function wellHosts() {
    return [el.wellAvail, el.wellRows, el.wellCols, el.wellFilt];
  }

  function makeGhost(chip, x, y) {
    ghost = chip.cloneNode(true);
    ghost.className = 'chip chip--ghost';
    ghost.style.width = chip.offsetWidth + 'px';
    document.body.appendChild(ghost);
    moveGhost(x, y);
  }

  function moveGhost(x, y) {
    if (!ghost) return;
    ghost.style.left = (x - ghost.offsetWidth / 2) + 'px';
    ghost.style.top = (y - ghost.offsetHeight / 2) + 'px';
  }

  function clearDragUI() {
    if (ghost && ghost.parentNode) ghost.parentNode.removeChild(ghost);
    ghost = null;
    wellHosts().forEach(function (h) { h.classList.remove('well--over'); });
    Array.prototype.forEach.call(document.querySelectorAll('.chip.is-drag,.chip--before'),
      function (c) { c.classList.remove('is-drag'); c.classList.remove('chip--before'); });
  }

  /* Where a drop at (x,y) would land: the well under the cursor, and the index
     to insert at -- before the chip whose left half the cursor is over. */
  function dropTarget(x, y) {
    var over = document.elementFromPoint(x, y);
    var well = over && over.closest ? over.closest('.well') : null;
    if (!well) return null;
    var chip = over.closest('.chip');
    var at = -1;
    if (chip && chip !== ghost && chip.dataset.zone === well.dataset.zone) {
      var box = chip.getBoundingClientRect();
      at = parseInt(chip.dataset.idx, 10) + (x > box.left + box.width / 2 ? 1 : 0);
    }
    return { well: well, zone: well.dataset.zone, at: at, chip: chip };
  }

  function paintDropTarget(t) {
    wellHosts().forEach(function (h) { h.classList.remove('well--over'); });
    Array.prototype.forEach.call(document.querySelectorAll('.chip--before'),
      function (c) { c.classList.remove('chip--before'); });
    if (!t) return;
    t.well.classList.add('well--over');
    if (t.chip && t.chip !== ghost) t.chip.classList.add('chip--before');
  }

  function wireWells() {
    if (wellsWired) return;
    wellsWired = true;

    wellHosts().forEach(function (host) {
      host.addEventListener('click', function (e) {
        var b = e.target.closest && e.target.closest('.chip__b');
        if (!b) return;
        var chip = b.closest('.chip');
        var key = chip.dataset.key, zone = chip.dataset.zone;
        var idx = parseInt(chip.dataset.idx, 10);
        var act = b.dataset.act;
        if (act === 'pick') openPicker(key);
        else if (act === 'toFilt') moveField(key, zone, 'filt', -1);
        else if (act === 'toRows') moveField(key, zone, 'rows', -1);
        else if (act === 'toCols') moveField(key, zone, 'cols', -1);
        else if (act === 'remove') moveField(key, zone, 'avail', -1);
        else if (act === 'left') reorder(zone, idx, idx - 1);
        else if (act === 'right') reorder(zone, idx, idx + 1);
      });

      host.addEventListener('pointerdown', function (e) {
        if (e.target.closest && e.target.closest('.chip__b')) return;
        var chip = e.target.closest && e.target.closest('.chip');
        if (!chip) return;
        ptr = {
          key: chip.dataset.key,
          from: chip.dataset.zone,
          chip: chip,
          x0: e.clientX, y0: e.clientY,
          dragging: false
        };
        // Keep receiving moves even when the cursor leaves the pane.
        if (host.setPointerCapture) {
          try { host.setPointerCapture(e.pointerId); ptr.host = host; ptr.id = e.pointerId; }
          catch (err) { /* capture unsupported: document handlers still fire */ }
        }
      });
    });

    document.addEventListener('pointermove', function (e) {
      if (!ptr) return;
      if (!ptr.dragging) {
        if (Math.abs(e.clientX - ptr.x0) < DRAG_SLOP
          && Math.abs(e.clientY - ptr.y0) < DRAG_SLOP) return;
        ptr.dragging = true;
        ptr.chip.classList.add('is-drag');
        makeGhost(ptr.chip, e.clientX, e.clientY);
      }
      moveGhost(e.clientX, e.clientY);
      // The ghost sits under the cursor, so hide it while asking what is below.
      ghost.style.display = 'none';
      var t = dropTarget(e.clientX, e.clientY);
      ghost.style.display = '';
      paintDropTarget(t);
      if (e.cancelable) e.preventDefault();
    });

    document.addEventListener('pointerup', function (e) {
      if (!ptr) return;
      var p = ptr;
      ptr = null;
      if (p.host && p.host.releasePointerCapture) {
        try { p.host.releasePointerCapture(p.id); } catch (err) { /* already gone */ }
      }
      if (!p.dragging) { clearDragUI(); return; }   // a tap, not a drag
      if (ghost) ghost.style.display = 'none';
      var t = dropTarget(e.clientX, e.clientY);
      clearDragUI();
      if (t) moveField(p.key, p.from, t.zone, t.at);
    });

    document.addEventListener('pointercancel', function () {
      ptr = null;
      clearDragUI();
    });
  }

  function moveField(key, from, to, at) {
    if (from === to && at < 0) return;
    // Leaving Filters drops the selection with it, rather than keeping a
    // hidden constraint alive on a field that is no longer shown as filtered.
    if (from === 'filt' && to !== 'filt') delete filterVals[key];
    if (to === 'filt' && !filterVals[key]) filterVals[key] = [];
    wells[from] = wells[from].filter(function (k) { return k !== key; });
    if (to !== 'avail') {
      if (wells[to].length >= MAX[to]) {
        el.cubeMsg.textContent = 'At most ' + MAX[to] + ' fields on ' + to + '.';
        el.cubeMsg.className = 'msg msg--err';
        reflowWells();
        return;
      }
      wells[to] = wells[to].filter(function (k) { return k !== key; });
      if (at >= 0 && at <= wells[to].length) wells[to].splice(at, 0, key);
      else wells[to].push(key);
    }
    reflowWells();
    rememberCubeSpec();
    el.cubeMsg.textContent = '';
    el.cubeMsg.className = 'msg';
    if (el.autoBuild.checked && wells.rows.length) run(buildCube);
  }

  /* Value picker for one filtered dimension.

     Members come from the server under the CURRENT journal filters, so the
     list is what is really in the data rather than a catalogue of everything
     that ever existed. Cached per dimension+query for the session. */
  var pickerKey = null;

  async function openPicker(key) {
    pickerKey = key;
    el.pickerTitle.textContent = dimLabel(key);
    el.pickerSearch.value = '';
    el.picker.hidden = false;
    el.pickerList.innerHTML = '<p class="hint">Loading values…</p>';

    var qy = readQuery();
    var ck = key + '::' + JSON.stringify(toParams(qy));
    try {
      if (!memberCache[ck]) {
        memberCache[ck] = await apiGet('/xero/data/journals/pivot/members/',
          Object.assign({}, toParams(qy), { dim: key }));
      }
      renderPicker();
    } catch (e) {
      el.pickerList.innerHTML = '<p class="msg msg--err">' + esc(e.message) + '</p>';
    }
  }

  function pickerData() {
    var qy = readQuery();
    return memberCache[pickerKey + '::' + JSON.stringify(toParams(qy))];
  }

  function renderPicker() {
    var data = pickerData();
    if (!data) return;
    var term = (el.pickerSearch.value || '').toLowerCase();
    var chosen = filterVals[pickerKey] || [];
    var frag = document.createDocumentFragment();
    var shown = 0;

    data.members.forEach(function (m) {
      if (term && m.value.toLowerCase().indexOf(term) === -1) return;
      shown++;
      var row = document.createElement('label');
      row.className = 'pick';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = chosen.indexOf(m.value) !== -1;
      cb.dataset.value = m.value;
      var t = document.createElement('span');
      t.className = 'pick__t';
      t.textContent = m.value;
      var n = document.createElement('span');
      n.className = 'pick__n';
      n.textContent = fmtNum(m.lines);
      row.appendChild(cb); row.appendChild(t); row.appendChild(n);
      frag.appendChild(row);
    });

    el.pickerList.innerHTML = '';
    if (!shown) {
      el.pickerList.innerHTML = '<p class="hint">Nothing matches.</p>';
    } else {
      el.pickerList.appendChild(frag);
    }
    el.pickerCount.textContent = chosen.length
      ? fmtNum(chosen.length) + ' of ' + fmtNum(data.count) + ' selected'
      : 'all ' + fmtNum(data.count) + (data.truncated ? '+ (list capped)' : '');
  }

  function setPicked(key, values) {
    filterVals[key] = values;
    reflowWells();
    renderPicker();
    rememberCubeSpec();
    if (el.autoBuild.checked && wells.rows.length) run(buildCube);
  }

  var pickerWired = false;

  function wirePicker() {
    if (pickerWired) return;
    pickerWired = true;
    el.pickerClose.addEventListener('click', function () {
      el.picker.hidden = true;
      pickerKey = null;
    });
    el.pickerSearch.addEventListener('input', renderPicker);

    el.pickerList.addEventListener('change', function (e) {
      var cb = e.target;
      if (!cb || cb.type !== 'checkbox' || !pickerKey) return;
      var cur = (filterVals[pickerKey] || []).slice();
      var v = cb.dataset.value;
      var i = cur.indexOf(v);
      if (cb.checked && i === -1) cur.push(v);
      if (!cb.checked && i !== -1) cur.splice(i, 1);
      setPicked(pickerKey, cur);
    });

    el.pickerAll.addEventListener('click', function () {
      var data = pickerData();
      if (!data || !pickerKey) return;
      var term = (el.pickerSearch.value || '').toLowerCase();
      // "All" means all VISIBLE — with a search term active, that is the
      // useful meaning and the only one that matches what is on screen.
      setPicked(pickerKey, data.members
        .filter(function (m) { return !term || m.value.toLowerCase().indexOf(term) !== -1; })
        .map(function (m) { return m.value; }));
    });

    el.pickerNone.addEventListener('click', function () {
      if (pickerKey) setPicked(pickerKey, []);
    });
  }

  var CUBE_SPEC_KEY = 'klikkCubeSpec';

  /* The wells survive a pane reload.

     Excel reloads the task pane freely -- switching sheets, reopening the
     pane, restarting -- and the wells were rebuilt from defaults every time,
     so a layout took several drags to get back. Kept in localStorage rather
     than document.settings: it is a personal working preference, not part of
     the workbook, and should not travel to whoever the file is shared with. */
  function rememberCubeSpec() {
    try { localStorage.setItem(CUBE_SPEC_KEY, JSON.stringify(readCubeSpec())); }
    catch (e) { /* private mode or quota: the wells just will not persist */ }
  }

  function recallCubeSpec() {
    try {
      var raw = localStorage.getItem(CUBE_SPEC_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  /* Point the wells at whatever the sheet in front actually is.

     A cube sheet carries its own spec in its binding, so returning to it
     restores the exact rows, columns, filters and measure that built it --
     rather than leaving the pane describing a different sheet, which made
     Build overwrite the sheet with the wrong layout. */
  function syncCubeToSheet() {
    var b = activeSheet.binding;
    if (b && b.kind === 'cube' && b.spec) {
      applyCubeSpec(b.spec);
      applyQuery(b.query);
      return true;
    }
    return false;
  }

  function readCubeSpec() {
    return {
      rows: wells.rows.slice(),
      cols: wells.cols.slice(),
      measure: el.measure.value || 'amount',
      filt: wells.filt.slice(),
      filters: JSON.parse(JSON.stringify(filterVals)),
      suppress: el.suppress.checked,
      outline: el.outline.checked
    };
  }

  function applyCubeSpec(spec) {
    wells.rows = (spec.rows || []).slice();
    wells.cols = (spec.cols || []).slice();
    wells.filt = (spec.filt || []).slice();
    filterVals = spec.filters ? JSON.parse(JSON.stringify(spec.filters)) : {};
    el.measure.value = spec.measure || 'amount';
    el.suppress.checked = !!spec.suppress;
    if (typeof spec.outline === 'boolean') el.outline.checked = spec.outline;
    reflowWells();
  }

  function validateCube(spec) {
    if (!spec.rows.length) return 'Pick at least one row dimension.';
    var all = spec.rows.concat(spec.cols);
    var seen = {};
    for (var i = 0; i < all.length; i++) {
      if (seen[all[i]]) return 'Each dimension can be used once — ' + all[i] + ' is repeated.';
      seen[all[i]] = true;
    }
    return null;
  }

  async function fetchCube(qy, spec) {
    // dimfParam(spec) is the authority; toParams(qy) only carries dimf so it
    // reaches the comment anchor. Spec wins if they ever disagree.
    var params = Object.assign({}, toParams(qy), {
      rows: spec.rows.join(','),
      cols: spec.cols.join(','),
      measure: spec.measure,
      suppress: spec.suppress ? '1' : '0'
    }, dimfParam(spec));
    return apiGet('/xero/data/journals/pivot/', params);
  }

  /* Lays the cross-tab out the way a drilled cube view reads: context above,
     one worksheet column per row dimension, consolidations bold. */
  async function renderCube(targetId, cube, qy, spec) {
    var nRowDims = cube.row_dims.length;
    var nCols = cube.cols.length;
    var width = nRowDims + nCols + 1;          // + grand total column
    var isCount = cube.measure === 'count';

    var head = [];
    head.push([cube.measure_label + ' — Klikk journals'].concat(blanks(width - 1)));
    head.push([describe(qy)].concat(blanks(width - 1)));
    head.push(blanks(width));

    var labelRow = cube.row_dims.map(function (d) { return d.label; })
      .concat(cube.cols, ['Total']);
    head.push(labelRow);

    var body = cube.rows.map(function (r) {
      var cells = [];
      // Only the row's own level carries a label; the consolidation above
      // already names its ancestors, so repeating them just adds noise.
      for (var i = 0; i < nRowDims; i++) {
        cells.push(i === r.depth && r.keys[i] != null ? r.keys[i] : '');
      }
      var total = 0;
      r.cells.forEach(function (v) { cells.push(v); total += v; });
      cells.push(total);
      return cells;
    });

    var totalRow = ['Grand total'].concat(blanks(nRowDims - 1));
    cube.col_totals.forEach(function (v) { totalRow.push(v); });
    totalRow.push(cube.grand_total);

    var matrix = head.concat(body, [blanks(width)], [totalRow]);
    var headerRowIdx = 3;
    var firstDataRow = CUBE_FIRST_DATA_ROW;

    var sheetId = targetId;
    var sheetName = '';

    await Excel.run(async function (ctx) {
      var sheet;
      if (targetId) {
        sheet = ctx.workbook.worksheets.getItem(targetId);
        var tables = sheet.tables;
        tables.load('items/name');
        await ctx.sync();
        tables.items.forEach(function (t) { t.delete(); });
        sheet.getRange().clear(Excel.ClearApplyTo.all);
      } else {
        sheet = ctx.workbook.worksheets.add(await uniqueSheetName(ctx, 'Cube'));
      }
      sheet.load('id,name');
      sheet.activate();
      await ctx.sync();
      sheetId = sheet.id;
      sheetName = sheet.name;
    });

    for (var start = 0; start < matrix.length; start += WRITE_CHUNK) {
      var chunk = matrix.slice(start, start + WRITE_CHUNK);
      var at = start;
      await Excel.run(async function (ctx) {
        var sheet = ctx.workbook.worksheets.getItem(sheetId);
        sheet.getRangeByIndexes(at, 0, chunk.length, width).values = chunk;
        await ctx.sync();
      });
      progress(start + chunk.length, matrix.length,
        'Writing ' + fmtNum(start + chunk.length) + ' of ' + fmtNum(matrix.length) + ' rows…');
    }

    await Excel.run(async function (ctx) {
      var sheet = ctx.workbook.worksheets.getItem(sheetId);

      sheet.getRangeByIndexes(0, 0, 1, 1).format.font.bold = true;
      sheet.getRangeByIndexes(0, 0, 1, 1).format.font.size = 13;
      sheet.getRangeByIndexes(1, 0, 1, 1).format.font.color = '#6b7280';

      var hdr = sheet.getRangeByIndexes(headerRowIdx, 0, 1, width);
      hdr.format.font.bold = true;
      hdr.format.borders.getItem('EdgeBottom').style = 'Continuous';
      hdr.format.horizontalAlignment = 'Right';
      sheet.getRangeByIndexes(headerRowIdx, 0, 1, nRowDims).format.horizontalAlignment = 'Left';

      if (body.length) {
        var nums = sheet.getRangeByIndexes(firstDataRow, nRowDims, body.length, nCols + 1);
        nums.numberFormat = [[isCount ? '#,##0' : MONEY_FMT]];
      }

      // Consolidation rows carry the weight; leaves are indented under them.
      cube.rows.forEach(function (r, i) {
        var rowRange = sheet.getRangeByIndexes(firstDataRow + i, 0, 1, width);
        if (r.is_total) {
          rowRange.format.font.bold = true;
        }
        if (r.depth > 0) {
          sheet.getRangeByIndexes(firstDataRow + i, r.depth, 1, 1).format.indentLevel =
            Math.min(r.depth, 5);
        }
      });

      var gt = sheet.getRangeByIndexes(firstDataRow + body.length + 1, 0, 1, width);
      gt.format.font.bold = true;
      gt.numberFormat = [[isCount ? '#,##0' : MONEY_FMT]];
      gt.getCell(0, 0).numberFormat = [['General']];
      gt.format.borders.getItem('EdgeTop').style = 'Continuous';

      sheet.getRangeByIndexes(headerRowIdx, 0, 1, nRowDims).format.columnWidth = 190;
      for (var c = nRowDims; c < width; c++) {
        sheet.getRangeByIndexes(headerRowIdx, c, 1, 1).format.columnWidth = 96;
      }

      // Freeze the context + header block and the row-dimension columns, so the
      // numbers stay readable when you scroll into 2029.
      try {
        sheet.freezePanes.freezeAt(sheet.getRangeByIndexes(0, 0, firstDataRow, nRowDims));
      } catch (e) {
        sheet.freezePanes.freezeRows(firstDataRow);
      }
      await ctx.sync();
    });

    // Real drill-down, using Excel's own outline: every run of rows beneath a
    // consolidation becomes a group, so the grid gets native +/- controls in the
    // margin. Nothing to reimplement and it survives saving the workbook.
    if (spec && spec.outline && cube.rows.length && nRowDims > 1) {
      await Excel.run(async function (ctx) {
        var sheet = ctx.workbook.worksheets.getItem(sheetId);
        var groups = [];
        cube.rows.forEach(function (r, i) {
          if (!r.is_total) return;
          var start = i + 1, end = i;
          for (var j = i + 1; j < cube.rows.length; j++) {
            if (cube.rows[j].depth <= r.depth) break;
            end = j;
          }
          if (end >= start) {
            groups.push({ from: firstDataRow + start, to: firstDataRow + end });
          }
        });
        // Sync in batches; a few thousand queued group() calls in one round
        // trip is a lot to push through the add-in bridge at once.
        for (var gi = 0; gi < groups.length; gi++) {
          try {
            sheet.getRangeByIndexes(groups[gi].from, 0, groups[gi].to - groups[gi].from + 1, 1)
              .group(Excel.GroupOption.byRows);
          } catch (e) { /* host without outlining: rows just stay expanded */ }
          if ((gi + 1) % 100 === 0) await ctx.sync();
        }
        await ctx.sync();
      }).catch(function () { /* outlining unsupported; the sheet is still correct */ });
    }

    lastCube[sheetId] = { cube: cube, firstDataRow: firstDataRow, nRowDims: nRowDims };
    await bindQuery(sheetId, qy, cube.rows.length, 'cube', spec);

    /* Pull comments automatically. A cube is rebuilt from scratch on every
     * load and refresh, which wipes the notes drawn on it, so leaving this to
     * a button meant the sheet silently came back bare and the notes looked
     * lost. They were never lost -- Postgres holds them -- but the grid lied
     * about it until someone clicked.
     *
     * Deliberately non-fatal: the ledger data is the point of this operation,
     * and a comment API that is unreachable or missing must not fail a load
     * that otherwise succeeded. */
    try {
      var n = await placeCubeComments(sheetId, lastCube[sheetId], spec, qy);
      if (n) el.countLine.innerHTML += ' · <strong>' + fmtNum(n) + '</strong> comment'
        + (n === 1 ? '' : 's') + ' restored';
    } catch (e) { /* sheet is correct; comments just are not mirrored */ }

    return { sheetId: sheetId, sheetName: sheetName };
  }

  function blanks(n) {
    var a = [];
    for (var i = 0; i < n; i++) a.push('');
    return a;
  }

  async function buildCube() {
    var spec = readCubeSpec();
    var bad = validateCube(spec);
    if (bad) { el.cubeMsg.textContent = bad; el.cubeMsg.className = 'msg msg--err'; return; }
    el.cubeMsg.textContent = '';
    el.cubeMsg.className = 'msg';

    var qy = readQuery();
    /* The dimension filters ride on the query, not just the spec, because the
       comment anchor is built from the query. Without this, the same row under
       "FY2026 only" and under no filter would share an anchor -- two different
       figures, one comment. */
    var df = dimfParam(spec);
    qy.dimf = df.dimf || '';

    progress(0, 1, 'Aggregating in Postgres…');
    var cube = await fetchCube(qy, spec);
    if (cancelFlag.cancelled) { el.cubeMsg.textContent = 'Cancelled.'; return; }

    var out = await renderCube(null, cube, qy, spec);
    await inspectActiveSheet();

    if (cube.balancing_hint) {
      el.cubeMsg.textContent = cube.balancing_hint;
      el.cubeMsg.className = 'msg msg--err';
      return;
    }
    var note = fmtNum(cube.leaf_count) + ' leaf rows × ' + fmtNum(cube.cols.length)
      + ' columns written to ' + out.sheetName + '.';
    if (cube.zero_rows && cube.spec !== null) {
      note += ' ' + fmtNum(cube.zero_rows) + ' zero rows suppressed.';
    }
    if (cube.truncated_rows) note += ' Row cap hit — narrow the filters.';
    if (cube.truncated_cols) note += ' Column cap hit — use a coarser column dimension.';
    el.cubeMsg.textContent = note;
    el.cubeMsg.className = cube.truncated_rows || cube.truncated_cols ? 'msg msg--err' : 'msg msg--ok';
  }

  /* ── native Excel PivotTable over a detail sheet ───────── */

  /* Create a native PivotTable over a detail sheet.
   *
   * Deliberately step-by-step with a ctx.sync() between each stage. The first
   * version created the pivot and added all four hierarchies in ONE batch,
   * which references hierarchies of a PivotTable that does not exist on the
   * host side yet -- Excel for Mac crashes on that rather than erroring. Each
   * field is also added independently so a missing column degrades to a
   * partial pivot instead of taking the whole operation down. */
  var PIVOT_ROW_CEILING = 100000;
  /* Why this file never calls PivotLayout.getPivotItems.
   *
   * It traps Excel for Mac -- a native EXC_BAD_INSTRUCTION, not a catchable
   * JS error -- when handed a cell it does not consider a leaf data cell.
   * Bounds-checking against getDataBodyRange() does NOT save you: re-laying
   * out a pivot creates SUBTOTAL rows that sit geometrically inside the body
   * but resolve to no single item, so a walk over the body reaches one and
   * Excel dies. That was diagnosed and "fixed" three times before the real
   * shape of it was clear.
   *
   * readPivotGrid replaces it: read the labels the pivot has already drawn
   * and reconstruct the paths, exactly as cellToIntersection does for a cube.
   * Never ask Excel what a cell means. Do not reintroduce the API. */

  async function addNativePivot() {
    if (!activeSheet.binding || activeSheet.binding.kind !== 'detail') {
      throw new Error('Open a detail sheet first.');
    }

    // Select the source table so Insert > PivotTable picks it up with no typing.
    var addr = null, rows = 0;
    await Excel.run(async function (ctx) {
      var src = ctx.workbook.worksheets.getItem(activeSheet.id);
      var tables = src.tables;
      tables.load('items/name');
      await ctx.sync();

      var range = tables.items.length ? tables.items[0].getRange() : src.getUsedRange();
      range.load('address,rowCount');
      src.activate();
      range.select();
      await ctx.sync();
      addr = range.address;
      rows = range.rowCount;
    });

    el.countLine.innerHTML = '<strong>' + fmtNum(rows) + ' rows selected (' + addr
      + ').</strong><br>Now use Excel\'s own <strong>Insert &rsaquo; PivotTable</strong>. '
      + 'Build it however you like — then come back here and <strong>Sync comments</strong> '
      + 'to pin notes to its cells.';
  }

  /* ── the selected cell ─────────────────────────────────── */

  var lastCube = {};
  // Title, blank, column headers, blank -> data starts on row index 4.
  var CUBE_FIRST_DATA_ROW = 4;
  var selection = null;
  var commentCache = null;

  /* One button to the optimal path: Excel's own PivotTable is a better pivot
     than anything an add-in can draw, and its only real weakness is that it
     aggregates just the rows on its sheet. So pull every matching line first,
     then hand it to Excel. */
  async function pivotFromFullDetail() {
    var qy = readQuery();
    qy.maxRows = 0;

    var probe = await apiGet('/xero/data/journals/search/',
      Object.assign({}, toParams(qy), { limit: 1, offset: 0 }));
    if (probe.count > PIVOT_ROW_CEILING) {
      throw new Error(fmtNum(probe.count) + ' rows match — too many to pivot natively '
        + 'on this machine. Narrow the filters, or use the cube view, which aggregates '
        + 'in Postgres and has no row limit.');
    }

    progress(0, 1, 'Pulling all ' + fmtNum(probe.count) + ' rows…');
    var got = await fetchRows(qy);
    if (cancelFlag.cancelled) return;
    await renderRows(null, got.rows, qy);
    await inspectActiveSheet();
    await addNativePivot();
  }

  /* Selection handling.
   *
   * Pivot cells resolve on selection again, now that resolution reads the
   * rendered grid instead of calling getPivotItems. A stale grid read is the
   * only cost of a re-layout, and re-reading is cheap -- two range reads for
   * the whole pivot. Debounce and single-flight stay: onSelectionChanged
   * fires on every click and arrow key. Cube sheets read plain range
   * values and has no such surface.
   *
   * Pivot cells are now resolved only when the user asks, via the button, and
   * only after the selection is proven to be inside the data body. */
  var selBusy = false;
  var selTimer = null;

  function watchSelection() {
    Excel.run(function (ctx) {
      ctx.workbook.onSelectionChanged.add(function () {
        // Debounced and single-flight: the event fires per keystroke while
        // arrowing around, and overlapping Excel.run batches against the same
        // proxies was itself a source of instability.
        if (selTimer) clearTimeout(selTimer);
        selTimer = setTimeout(function () { readSelection(); }, 150);
      });
      return ctx.sync();
    }).catch(function () { /* host without the event: the buttons still work */ });
  }

  async function readSelection(explicit) {
    if (selBusy) return;
    selBusy = true;
    try {
      var b = activeSheet.binding;
      if (!b) return showSelection(null);
      if (b.kind === 'cube') return showSelection(await resolveCubeSelection(b));
      if (b.kind === 'pivot') {
        if (!explicit) return showSelection({ pivotManual: true });
        return showSelection(await resolvePivotSelection(b));
      }
      return showSelection(null);
    } catch (e) {
      return showSelection(null);
    } finally {
      selBusy = false;
    }
  }

  /* Read a PivotTable's meaning off the RENDERED GRID.
   *
   * Replaces PivotLayout.getPivotItems, which takes Excel for Mac down with a
   * native trap on any cell it does not consider a leaf data cell -- subtotal
   * rows being the case that finally proved fencing it was hopeless. Nothing
   * here asks Excel what a cell means. It reads the label cells the pivot has
   * already drawn and reconstructs the paths the same way cellToIntersection
   * does for a cube.
   *
   * Two range reads and one sync for the entire pivot, regardless of size --
   * cheaper than the old per-cell walk by orders of magnitude, which is why
   * the 5,000-cell ceiling is gone with it.
   *
   * Labels carry forward: in compact and outline form Excel writes a label
   * once and leaves the cells beneath it blank, so a blank means "same as
   * above", not "no value". Subtotal rows inherit their parent's path, which
   * is the honest reading -- a subtotal IS its parent at that level. */
  async function readPivotGrid(sheetId, pivotName) {
    var g = null;
    await Excel.run(async function (ctx) {
      var sheet = ctx.workbook.worksheets.getItem(sheetId);
      var pivot = sheet.pivotTables.getItem(pivotName);
      var whole = pivot.layout.getRange();
      var body = pivot.layout.getDataBodyRange();
      var dh = pivot.dataHierarchies;
      var rh = pivot.rowHierarchies;
      var ch = pivot.columnHierarchies;
      whole.load('values,rowIndex,columnIndex,rowCount,columnCount');
      body.load('rowIndex,columnIndex,rowCount,columnCount');
      dh.load('items/name'); rh.load('items/name'); ch.load('items/name');
      await ctx.sync();

      var labelCols = body.columnIndex - whole.columnIndex;   // row-label columns
      var headerRows = body.rowIndex - whole.rowIndex;        // column-header rows
      var v = whole.values || [];

      function txt(r, c) {
        var row = v[r]; if (!row) return '';
        var x = row[c];
        return (x === null || x === undefined) ? '' : String(x).trim();
      }

      // Row paths: carry the last non-blank label down each label column.
      var rowPaths = [], carry = [];
      for (var i = 0; i < body.rowCount; i++) {
        var path = [];
        for (var j = 0; j < labelCols; j++) {
          var t = txt(headerRows + i, j);
          if (t) carry[j] = t;
          if (carry[j]) path.push(carry[j]);
        }
        rowPaths.push(path);
      }

      // Column paths: same carry, along each header row.
      var colPaths = [];
      for (var k = 0; k < body.columnCount; k++) {
        var cpath = [];
        for (var h = 0; h < headerRows; h++) {
          var ct = txt(h, labelCols + k);
          if (!ct) {
            for (var back = k - 1; back >= 0 && !ct; back--) ct = txt(h, labelCols + back);
          }
          if (ct) cpath.push(ct);
        }
        colPaths.push(cpath);
      }

      // Real field names, so a pivot comment anchors to WHICH dimension holds
      // a value rather than to which slot it sat in. pivot_row_1/pivot_col
      // meant reordering the rows orphaned every comment on the sheet.
      // Same call shape as dataHierarchies above -- a plain collection load,
      // not the per-cell resolution that traps.
      var rNames = (rh.items || []).map(function (h) { return h.name; });
      var cNames = (ch.items || []).map(function (h) { return h.name; });

      g = {
        rowFields: rNames, colFields: cNames,
        r0: body.rowIndex, c0: body.columnIndex,
        rows: body.rowCount, cols: body.columnCount,
        rowPaths: rowPaths, colPaths: colPaths,
        measure: (dh.items && dh.items.length && dh.items[0].name) || 'Amount',
        valueAt: function (rr, cc) {
          var x = txt(headerRows + rr, labelCols + cc);
          var n = parseFloat(String(x).replace(/[^0-9.\-]/g, ''));
          return isFinite(n) ? n : null;
        }
      };
    });
    return g;
  }

  /* A PivotTable field name -> the cube dimension key it corresponds to.

     Excel names a field the way the detail sheet's column header reads
     ("Account class"); the cube names it by key ("account_class"). Mapping
     them means a comment written on a PivotTable and one written on a cube
     land on the SAME anchor when they refer to the same figure. Unmatched
     names pass through unchanged rather than being forced into a key that
     might belong to a different dimension. */
  function dimKeyForField(name) {
    var n = String(name || '').trim().toLowerCase();
    if (!n) return name;
    for (var i = 0; i < DIMS.length; i++) {
      if (String(DIMS[i].label).toLowerCase() === n) return DIMS[i].key;
      if (String(DIMS[i].key).toLowerCase() === n) return DIMS[i].key;
    }
    return name;
  }

  /* One pivot body cell -> the anchor it represents. Sheet coordinates in,
     null out if the cell is outside the body. */
  function pivotAnchorAt(g, b, r, c) {
    if (!g) return null;
    var rr = r - g.r0, cc = c - g.c0;
    if (rr < 0 || rr >= g.rows || cc < 0 || cc >= g.cols) return null;
    var rp = g.rowPaths[rr] || [];
    if (!rp.length) return null;
    var cp = g.colPaths[cc] || [];
    return {
      measure: g.measure,
      row_dims: rp.map(function (_, i) {
        return g.rowFields[i] ? dimKeyForField(g.rowFields[i]) : ('pivot_row_' + (i + 1));
      }),
      row_path: rp,
      col_dims: cp.map(function (_, i) {
        return g.colFields[i] ? dimKeyForField(g.colFields[i]) : 'pivot_col';
      }),
      col_path: cp.join(' | ') || 'Total',
      value: g.valueAt(rr, cc),
      r: r, c: c,
      query: b.query
    };
  }

  async function resolvePivotSelection(b) {
    var g = await readPivotGrid(activeSheet.id, b.spec.pivotName);
    if (!g) return null;
    var out = null;
    await Excel.run(async function (ctx) {
      var cell = ctx.workbook.getSelectedRange();
      var cellSheet = cell.worksheet;
      cell.load('cellCount,rowIndex,columnIndex,address');
      cellSheet.load('id');
      await ctx.sync();
      if (cell.cellCount !== 1) return;
      if (cellSheet.id !== activeSheet.id) return;
      var pa = pivotAnchorAt(g, b, cell.rowIndex, cell.columnIndex);
      out = pa || { outsideBody: true, address: cell.address };
    });
    return out;
  }

  /* The cube behind a sheet, refetching if this pane has never seen it.

     lastCube is memory-only, so it is empty after a pane reload, after Excel
     restarts, and whenever the workbook was built in another session. It used
     to just return null there, which is why leaving a cube sheet and coming
     back made commenting quietly stop working -- the sheet was fine, the pane
     had simply forgotten what its cells meant. The binding holds the query and
     the spec, and the layout is deterministic (firstDataRow is a constant,
     nRowDims comes from the cube), so the exact same grid can be recovered
     without touching the sheet. */
  async function ensureCube(sheetId, b) {
    if (lastCube[sheetId]) return lastCube[sheetId];
    if (!b || b.kind !== 'cube' || !b.spec) return null;
    var cube = await fetchCube(b.query, b.spec);
    lastCube[sheetId] = {
      cube: cube,
      firstDataRow: CUBE_FIRST_DATA_ROW,
      nRowDims: cube.row_dims.length
    };
    return lastCube[sheetId];
  }

  async function resolveCubeSelection(b) {
    var cached = await ensureCube(activeSheet.id, b);
    if (!cached) return null;
    var out = null;
    await Excel.run(async function (ctx) {
      var cell = ctx.workbook.getSelectedRange();
      cell.load('rowIndex,columnIndex,cellCount,values');
      await ctx.sync();
      if (cell.cellCount !== 1) return;
      var x = cellToIntersection(cached.cube,
        cell.rowIndex - cached.firstDataRow, cell.columnIndex - cached.nRowDims);
      if (!x) return;
      out = {
        measure: b.spec.measure,
        row_dims: x.row_dims, row_path: x.row_path,
        col_dims: x.col_dims, col_path: x.col_path,
        value: x.cell_value, r: cell.rowIndex, c: cell.columnIndex, query: b.query
      };
    });
    return out;
  }

  async function showSelection(sel) {
    selection = sel;

    // Two informational states that carry no anchor: a pivot cell awaiting an
    // explicit read, and a cell proven to be outside the pivot's data body.
    if (sel && (sel.pivotManual || sel.outsideBody)) {
      selection = null;
      el.selHas.hidden = true;
      el.selNone.hidden = false;
      el.selBox.className = 'sel';
      el.selNone.innerHTML = sel.outsideBody
        ? 'That cell (' + esc(sel.address || '') + ') is outside the PivotTable\'s '
          + 'values area. Comments pin to a figure, so pick a number inside the pivot.'
        : 'Select a value cell in the PivotTable, then press '
          + '<button class="btn btn--tiny" id="btnReadCell">Read cell</button>';
      var rb = document.getElementById('btnReadCell');
      if (rb) rb.addEventListener('click', function () { readSelection(true); });
      return;
    }

    if (!sel) {
      el.selHas.hidden = true;
      el.selNone.hidden = false;
      el.selBox.className = 'sel';
      return;
    }
    el.selNone.hidden = true;
    el.selHas.hidden = false;
    el.selBox.className = 'sel';
    el.selPath.textContent = sel.row_path.join(' / ') + '  ×  ' + sel.col_path
      + '   [' + sel.measure + ']';
    el.selVal.textContent = typeof sel.value === 'number'
      ? sel.value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
      : (sel.value == null ? '' : String(sel.value));

    var existing = await findComment(sel);
    el.selComment.value = existing ? existing.comment : '';
    if (existing) el.selBox.className = 'sel sel--saved';
  }

  function anchorKey(x) {
    return x.measure + '\u001e' + x.row_path.join('\u001f') + '\u001e' + x.col_path;
  }

  async function findComment(sel) {
    if (!commentCache) {
      var r = await apiGet(COMMENT_API, { status: 'all', limit: 2000 });
      commentCache = {};
      (r.results || []).forEach(function (c) { commentCache[anchorKey(c)] = c; });
    }
    return commentCache[anchorKey(sel)] || null;
  }

  async function saveSelectedComment() {
    if (!selection) throw new Error('Select a value cell first.');
    var text = (el.selComment.value || '').trim();
    if (!text) throw new Error('Nothing to save — use Clear to remove a comment.');
    await postComment(selection, text);
    if (el.markCells.checked && selection.r != null) {
      await writeCellComment(activeSheet.id, selection.r, selection.c, text);
    }
    el.selBox.className = 'sel sel--saved';
    var cs = selCoords(selection);
    el.commentMsg.textContent = 'Saved against '
      + Object.keys(cs).map(function (k) { return dimLabel(k) + ' ' + cs[k]; }).join(' · ')
      + '. It stays on this figure wherever you move the fields.';
    el.commentMsg.className = 'msg msg--ok';
  }

  async function deleteSelectedComment() {
    if (!selection) throw new Error('Select a value cell first.');
    await postComment(selection, '');
    if (selection.r != null) await writeCellComment(activeSheet.id, selection.r, selection.c, null);
    el.selComment.value = '';
    el.selBox.className = 'sel';
    el.commentMsg.textContent = 'Comment cleared.';
    el.commentMsg.className = 'msg';
  }

  /* The journal lines that add up to the selected figure.

     The anchor's coordinates ARE the query -- {account_class: EXPENSE,
     account: 406 — ..., fin_year: FY2023} filters the ledger to exactly the
     lines the cell aggregated. Resolved live rather than from a stored list of
     ids: the lines behind a figure change when Xero is re-synced, and a frozen
     list would go stale while still looking authoritative.

     The returned rows carry the same field set as a journal search, so they go
     through the ordinary sheet writer. */
  function selCoords(sel) {
    var c = {};
    (sel.row_dims || []).forEach(function (d, i) { c[d] = (sel.row_path || [])[i]; });
    var cp = (sel.col_path && sel.col_path !== 'Total') ? String(sel.col_path).split(' | ') : [];
    (sel.col_dims || []).forEach(function (d, i) { if (cp[i] !== undefined) c[d] = cp[i]; });
    return c;
  }

  async function drillSelection() {
    var sel = selection;
    if (!sel || !sel.row_path) throw new Error('Select a value cell first.');
    var coords = selCoords(sel);
    progress(0, 1, 'Finding the transactions behind this figure…');
    var data = await apiGet('/xero/data/journals/pivot/drill/',
      Object.assign({}, toParams(sel.query), { coords: JSON.stringify(coords), limit: 5000 }));

    if (!data.count) {
      el.commentMsg.textContent = 'No journal lines match that figure — the ledger may have '
        + 'changed since it was written.';
      el.commentMsg.className = 'msg msg--err';
      return;
    }

    var out = await renderRows(null, data.rows, sel.query);
    await inspectActiveSheet();

    // Report the reconciliation rather than assuming it. A drill that does not
    // add up to the cell is a real signal -- the ledger moved under the figure.
    var shown = typeof sel.value === 'number' ? sel.value : null;
    var diff = shown === null ? null : Math.abs(shown - data.line_total);
    var msg = fmtNum(data.count) + ' line' + (data.count === 1 ? '' : 's')
      + ' totalling ' + data.line_total.toLocaleString(undefined, { minimumFractionDigits: 2 })
      + ' written to ' + (out ? out.sheetName : 'a new sheet') + '.';
    if (data.truncated) msg += ' Line cap hit — narrow the filters.';
    if (diff !== null && diff > 0.005) {
      msg += ' NOTE: the cell shows ' + shown.toLocaleString(undefined, { minimumFractionDigits: 2 })
        + ' — a difference of ' + diff.toFixed(2) + '. The underlying data has changed.';
      el.commentMsg.className = 'msg msg--err';
    } else {
      el.commentMsg.className = 'msg msg--ok';
    }
    el.commentMsg.textContent = msg;
  }

  async function postComment(sel, text) {
    await apiPost(COMMENT_API, {
      measure: sel.measure,
      row_dims: sel.row_dims, row_path: sel.row_path,
      col_dims: sel.col_dims, col_path: sel.col_path,
      filters: toParams(sel.query),
      cell_value: typeof sel.value === 'number' ? sel.value : null,
      comment: text,
      author: (el.commentAuthor.value || '').trim()
    });
    commentCache = null;                      // force a refresh on next lookup
  }

  /* Write (or clear) a native Excel comment on one cell. Excel has no
     "comment at address" lookup, so match on the resolved location. */
  function writeCellComment(sheetId, r, c, text) {
    return writeCellComments(sheetId, [{ r: r, c: c, t: text }]);
  }

  /* One pass for many cells. The previous version ran a full Excel.run per
     comment, each re-loading every comment on the sheet and its location — the
     bridge traffic grew with the square of the comment count, which is the kind
     of load that destabilises the host. */
  /* Drop every comment on the sheet, then re-place from Postgres.
   *
   * Needed because the grid is only a MIRROR. Comments live in
   * app.cube_comments anchored by meaning -- measure, row path, column path,
   * filter context -- never by cell address. So re-laying out a PivotTable
   * (dragging fields, collapsing, moving it) does not invalidate a single
   * comment; it invalidates where they were DRAWN. Sync alone cannot fix that:
   * it walks the current data body, so a note whose old cell now falls outside
   * the body is never visited and stays behind, pointing at a number it no
   * longer describes. That stale note is worse than no note.
   *
   * Clearing is safe precisely because the grid is not the record. Nothing is
   * lost here that a re-sync does not restore. */
  async function resetSheetComments() {
    if (!activeSheet.binding) throw new Error('Open a cube or PivotTable sheet first.');

    var removed = 0;
    await Excel.run(async function (ctx) {
      var sheet = ctx.workbook.worksheets.getItem(activeSheet.id);
      var comments = sheet.comments;
      comments.load('items');
      await ctx.sync();
      removed = comments.items.length;
      // Backwards: deleting shifts the collection under us.
      for (var i = comments.items.length - 1; i >= 0; i--) comments.items[i].delete();
      await ctx.sync();
    });

    progress(0, 1, 'Cleared ' + fmtNum(removed) + ' — re-reading from Postgres…');
    await pushCommentsToSheet();
  }

  async function writeCellComments(sheetId, writes) {
    if (!writes.length) return;
    try {
      await Excel.run(async function (ctx) {
        var sheet = ctx.workbook.worksheets.getItem(sheetId);
        var comments = sheet.comments;
        comments.load('items/content');
        await ctx.sync();

        var locs = comments.items.map(function (cm) {
          var rng = cm.getLocation();
          rng.load('rowIndex,columnIndex');
          return rng;
        });
        await ctx.sync();

        var at = {};
        for (var i = 0; i < locs.length; i++) {
          at[locs[i].rowIndex + ':' + locs[i].columnIndex] = i;
        }
        writes.forEach(function (w) {
          var hit = at[w.r + ':' + w.c];
          if (w.t) {
            if (hit != null) comments.items[hit].content = w.t;
            else comments.add(sheet.getRangeByIndexes(w.r, w.c, 1, 1), w.t);
          } else if (hit != null) {
            comments.items[hit].delete();
          }
        });
        await ctx.sync();
      });
    } catch (e) {
      // Comment API missing (needs ExcelApi 1.10) — comments are still safely in
      // Postgres, they just are not mirrored onto the grid.
    }
  }

  /* Paint every stored comment for this sheet back onto its cell. */
  /* Resolve one cube grid cell to the intersection it represents.
   *
   * (i, ci) are indexes INTO THE CUBE, not the sheet: i indexes cube.rows,
   * ci indexes cube.cols, and ci === cols.length is the grand-total column.
   * Callers convert from sheet coordinates by subtracting firstDataRow and
   * nRowDims, which is why out-of-range input is normal here and returns null
   * rather than throwing -- clicking a header or a margin lands outside.
   *
   * A row carries the keys of its whole ancestry; the path is that ancestry
   * down to the row's own depth, which is exactly what the consolidation
   * rows above it named. */
  function cellToIntersection(cube, i, ci) {
    if (!cube || !cube.rows || i < 0 || i >= cube.rows.length) return null;
    var nCols = cube.cols.length;
    if (ci < 0 || ci > nCols) return null;

    var r = cube.rows[i];
    var depth = typeof r.depth === 'number' ? r.depth : (r.keys.length - 1);

    var row_path = [], row_dims = [];
    for (var d = 0; d <= depth && d < r.keys.length; d++) {
      if (r.keys[d] == null || r.keys[d] === '') continue;
      row_path.push(String(r.keys[d]));
      var dim = cube.row_dims[d];
      row_dims.push(dim ? (dim.key || dim.label) : 'row_' + (d + 1));
    }
    if (!row_path.length) return null;        // a spacer or an unlabelled row

    var isTotal = ci === nCols;
    var value = isTotal
      ? (r.cells || []).reduce(function (a, v) { return a + (v || 0); }, 0)
      : (r.cells || [])[ci];

    return {
      row_dims: row_dims,
      row_path: row_path,
      col_dims: (cube.col_dims || []).map(function (d, n) {
        return d && (d.key || d.label) ? (d.key || d.label) : 'col_' + (n + 1);
      }),
      col_path: isTotal ? 'Total' : String(cube.cols[ci]),
      cell_value: typeof value === 'number' ? value : null
    };
  }

  /* Read the comments typed on this sheet and send them to Postgres.
   *
   * The other direction of the mirror. Excel gives a comment a location, not a
   * meaning, so each commented cell is resolved to its intersection first --
   * a note is only worth storing if we can say which figure it is about. Cells
   * whose meaning cannot be resolved are counted and reported rather than
   * uploaded against a guess. */
  async function syncComments() {
    var b = activeSheet.binding;
    if (!b) throw new Error('Open a cube or PivotTable sheet first.');
    if (!Office.context.requirements.isSetSupported('ExcelApi', '1.10')) {
      throw new Error('This Excel build has no comment API (needs ExcelApi 1.10).');
    }

    // 1. Every comment on the sheet, with where it sits.
    var notes = [];
    await Excel.run(async function (ctx) {
      var sheet = ctx.workbook.worksheets.getItem(activeSheet.id);
      var comments = sheet.comments;
      comments.load('items/content');
      await ctx.sync();
      var locs = comments.items.map(function (cm) {
        var rng = cm.getLocation();
        rng.load('rowIndex,columnIndex');
        return rng;
      });
      await ctx.sync();
      comments.items.forEach(function (cm, n) {
        var txt = (cm.content || '').trim();
        if (txt) notes.push({ r: locs[n].rowIndex, c: locs[n].columnIndex, t: txt });
      });
    });

    if (!notes.length) {
      el.countLine.textContent = 'No comments on this sheet to send.';
      return;
    }

    // 2. Resolve each commented cell, and only those cells.
    var sent = 0, skipped = 0;

    if (b.kind === 'cube') {
      var cached = await ensureCube(activeSheet.id, b);
      if (!cached) throw new Error('Rebuild this cube sheet first, then try again.');
      for (var n = 0; n < notes.length; n++) {
        var note = notes[n];
        var x = cellToIntersection(cached.cube,
          note.r - cached.firstDataRow, note.c - cached.nRowDims);
        if (!x) { skipped++; continue; }
        progress(n + 1, notes.length, 'Sending ' + (n + 1) + ' of ' + notes.length + '…');
        await postComment({
          measure: b.spec.measure,
          row_dims: x.row_dims, row_path: x.row_path,
          col_dims: x.col_dims, col_path: x.col_path,
          value: x.cell_value, query: b.query
        }, note.t);
        sent++;
      }
    } else {
      var g = await readPivotGrid(activeSheet.id, b.spec.pivotName);
      if (!g) throw new Error('Could not read that PivotTable.');
      for (var k = 0; k < notes.length; k++) {
        var pa = pivotAnchorAt(g, b, notes[k].r, notes[k].c);
        // A note on a header or off the body has no figure behind it; say so
        // rather than guess an anchor.
        if (!pa) { skipped++; continue; }
        progress(k + 1, notes.length, 'Sending ' + (k + 1) + ' of ' + notes.length + '…');
        await postComment(pa, notes[k].t);
        sent++;
      }
    }

    commentCache = null;
    el.countLine.innerHTML = 'Sent <strong>' + fmtNum(sent) + '</strong> comment'
      + (sent === 1 ? '' : 's') + ' to Postgres'
      + (skipped ? ', skipped ' + fmtNum(skipped)
          + ' whose cell could not be tied to a figure.' : '.');
  }

  /* Place stored comments onto a cube sheet.
   *
   * Explicit arguments rather than activeSheet, because this also runs during
   * a cube build -- at which point the sheet being written is not necessarily
   * the one in front.
   *
   * Cheap by construction: cellToIntersection is pure in-memory arithmetic
   * over the cube we already hold, so the whole grid resolves without a single
   * round trip, and only one write pass touches Excel. That is why a cube can
   * pull its comments automatically and a PivotTable cannot -- the pivot has
   * to ask Excel what each cell means, one cell at a time. */
  async function placeCubeComments(sheetId, cached, spec, query) {
    if (!cached || !Office.context.requirements.isSetSupported('ExcelApi', '1.10')) return 0;

    var all = await apiGet(COMMENT_API, { status: 'all', limit: 2000 });
    var want = {};
    (all.results || []).forEach(function (cm) { want[anchorKey(cm)] = cm.comment; });

    var writes = [];
    var nCols = cached.cube.cols.length;
    cached.cube.rows.forEach(function (r, i) {
      for (var ci = 0; ci <= nCols; ci++) {
        var x = cellToIntersection(cached.cube, i, ci);
        if (!x) continue;
        var txt = want[spec.measure + '\u001e' + x.row_path.join('\u001f') + '\u001e' + x.col_path];
        if (txt) writes.push({ r: cached.firstDataRow + i, c: cached.nRowDims + ci, t: txt });
      }
    });
    await writeCellComments(sheetId, writes);
    return writes.length;
  }

  async function pushCommentsToSheet() {
    var b = activeSheet.binding;
    if (!b) throw new Error('Open a cube or PivotTable sheet first.');
    if (!Office.context.requirements.isSetSupported('ExcelApi', '1.10')) {
      throw new Error('This Excel build has no comment API (needs ExcelApi 1.10).');
    }

    var all = await apiGet(COMMENT_API, { status: 'all', limit: 2000 });
    var want = {};
    (all.results || []).forEach(function (cm) { want[anchorKey(cm)] = cm.comment; });

    var placed = 0, unplaced = 0;

    if (b.kind === 'cube') {
      var cached = await ensureCube(activeSheet.id, b);
      if (!cached) throw new Error('Rebuild this cube sheet first, then try again.');
      placed = await placeCubeComments(activeSheet.id, cached, b.spec, b.query);
    } else {
      // Whole pivot read in one pass off the rendered grid; no per-cell
      // interrogation of Excel, so no ceiling and no trap.
      var g = await readPivotGrid(activeSheet.id, b.spec.pivotName);
      if (!g) throw new Error('Could not read that PivotTable.');
      var found = [];
      for (var rr = 0; rr < g.rows; rr++) {
        for (var cc = 0; cc < g.cols; cc++) {
          var pa = pivotAnchorAt(g, b, g.r0 + rr, g.c0 + cc);
          if (!pa) continue;
          var pt = want[anchorKey(pa)];
          if (pt) found.push({ r: pa.r, c: pa.c, t: pt });
        }
      }
      await writeCellComments(activeSheet.id, found);
      placed = found.length;
    }

    el.commentMsg.textContent = placed
      ? placed + ' comment' + (placed === 1 ? '' : 's') + ' written onto the sheet.'
      : 'No stored comments match what is currently on this sheet.';
    el.commentMsg.className = placed ? 'msg msg--ok' : 'msg';
  }

  /* ── plumbing ──────────────────────────────────────────── */

  async function run(fn) {
    if (busy) return;
    busy = true;
    cancelFlag.cancelled = false;
    el.errorMsg.hidden = true;
    el.progressPanel.hidden = false;
    setButtons(false);
    try {
      await fn();
    } catch (e) {
      el.errorMsg.textContent = e && e.message ? e.message : String(e);
      el.errorMsg.hidden = false;
    } finally {
      busy = false;
      el.progressPanel.hidden = true;
      el.progressFill.style.width = '0';
      setButtons(true);
    }
  }

  function setButtons(on) {
    el.btnLoad.disabled = !on;
    el.btnCount.disabled = !on;
    el.btnCube.disabled = !on;
    el.btnRefresh.disabled = !on || !activeSheet.binding;
    el.btnRestore.disabled = !on || !activeSheet.binding;
    el.btnPivot.disabled = !on || !activeSheet.binding || activeSheet.binding.kind !== 'detail';
    el.btnReload.disabled = !on || !activeSheet.binding;
    el.btnSyncComments.disabled = !on || !activeSheet.binding || (activeSheet.binding.kind !== 'cube' && activeSheet.binding.kind !== 'pivot');
    el.btnFullPivot.disabled = !on;
    el.btnPushComments.disabled = !on || !activeSheet.binding
      || (activeSheet.binding.kind !== 'cube' && activeSheet.binding.kind !== 'pivot');
  }

  function progress(done, total, msg) {
    el.progressMsg.textContent = msg;
    var pct = total > 0 && isFinite(total) ? Math.min(100, (done / total) * 100) : 0;
    el.progressFill.style.width = pct + '%';
  }

  function fmtNum(n) { return (n || 0).toLocaleString(); }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
})();
