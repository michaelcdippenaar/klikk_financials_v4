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
  var SETTING_PREFIX = 'klikkJournalQuery::';

  // Column order for the sheet. `fmt` drives the Excel number format.
  var COLUMNS = [
    { key: 'date',                    label: 'Date',       fmt: 'date',  width: 11 },
    { key: 'journal_number',          label: 'Jrnl #',     fmt: 'int',   width: 8  },
    { key: 'journal_type',            label: 'Type',       fmt: null,    width: 13 },
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
      'detailPanel', 'btnPivot', 'cubePanel', 'measure', 'row1', 'row2', 'row3',
      'col1', 'col2', 'suppress', 'btnCube', 'cubeMsg',
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
    el.queryPanel.hidden = !ok;
    el.detailPanel.hidden = !ok;
    el.cubePanel.hidden = !ok;
    el.refreshPanel.hidden = !ok;
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
      amount: qy.amount, q: qy.q
    };
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

      if (matrix.length > 0) {
        var table = sheet.tables.add(body, true);
        table.name = 'Klikk_' + Date.now();
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

      sheet.freezePanes.freezeRows(1);
      await ctx.sync();
    });

    await bindQuery(sheetId, qy, rows.length, 'detail', null);
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

  async function inspectActiveSheet() {
    try {
      await Excel.run(async function (ctx) {
        var sheet = ctx.workbook.worksheets.getActiveWorksheet();
        sheet.load('id,name');
        await ctx.sync();
        activeSheet.id = sheet.id;
        activeSheet.name = sheet.name;
        activeSheet.binding = readBinding(sheet.id);
      });
    } catch (e) {
      activeSheet = { id: null, name: '', binding: null };
    }
    paintRefreshPanel();
  }

  function paintRefreshPanel() {
    var b = activeSheet.binding;
    if (!b) {
      el.sheetInfo.innerHTML = activeSheet.name
        ? '<strong>' + esc(activeSheet.name) + '</strong> has no Klikk query bound to it. '
          + 'Load a query to a new sheet first.'
        : 'No Klikk query is bound to the active sheet.';
      el.btnRefresh.disabled = true;
      el.btnRestore.disabled = true;
      el.btnPivot.disabled = true;
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

  var DIM_SELECTS = ['row1', 'row2', 'row3', 'col1', 'col2'];

  function populateCube(cat) {
    var dims = cat.dimensions || [];
    fill(el.measure, cat.measures || [], 'Amount', function (m) {
      return { value: m.key, label: m.label };
    });
    el.measure.querySelector('option[value=""]').remove();
    if (!el.measure.value) el.measure.value = 'amount';

    DIM_SELECTS.forEach(function (id) {
      fill(el[id], dims, '—', function (d) { return { value: d.key, label: d.label }; });
    });
    // A default that reads like a trial balance the moment you open the pane.
    if (!el.row1.value) el.row1.value = 'account_type';
    if (!el.row2.value) el.row2.value = 'account';
    if (!el.col1.value) el.col1.value = 'year';
  }

  function readCubeSpec() {
    var rows = ['row1', 'row2', 'row3'].map(function (id) { return el[id].value; })
      .filter(Boolean);
    var cols = ['col1', 'col2'].map(function (id) { return el[id].value; }).filter(Boolean);
    return {
      rows: rows, cols: cols,
      measure: el.measure.value || 'amount',
      suppress: el.suppress.checked
    };
  }

  function applyCubeSpec(spec) {
    ['row1', 'row2', 'row3'].forEach(function (id, i) { el[id].value = (spec.rows || [])[i] || ''; });
    ['col1', 'col2'].forEach(function (id, i) { el[id].value = (spec.cols || [])[i] || ''; });
    el.measure.value = spec.measure || 'amount';
    el.suppress.checked = !!spec.suppress;
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
    var params = Object.assign({}, toParams(qy), {
      rows: spec.rows.join(','),
      cols: spec.cols.join(','),
      measure: spec.measure,
      suppress: spec.suppress ? '1' : '0'
    });
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
    var firstDataRow = 4;

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

    await bindQuery(sheetId, qy, cube.rows.length, 'cube', spec);
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

  async function addNativePivot() {
    if (!activeSheet.binding || activeSheet.binding.kind !== 'detail') {
      throw new Error('Open a detail sheet first, then add the PivotTable.');
    }
    var sourceId = activeSheet.id;
    try {
      await Excel.run(async function (ctx) {
        var src = ctx.workbook.worksheets.getItem(sourceId);
        var used = src.getUsedRange();
        used.load('address');
        var dest = ctx.workbook.worksheets.add(await uniqueSheetName(ctx, 'Pivot'));
        dest.load('name');
        await ctx.sync();

        var pivot = dest.pivotTables.add('KlikkPivot_' + Date.now(), used,
          dest.getRangeByIndexes(0, 0, 1, 1));
        pivot.rowHierarchies.add(pivot.hierarchies.getItem('Acct type'));
        pivot.rowHierarchies.add(pivot.hierarchies.getItem('Account'));
        pivot.dataHierarchies.add(pivot.hierarchies.getItem('Amount'));
        dest.activate();
        await ctx.sync();
      });
    } catch (e) {
      throw new Error('Excel could not create a PivotTable here (' +
        (e && e.message ? e.message : e) + '). The cube view does the same job server-side.');
    }
    el.countLine.textContent = 'PivotTable created — drag fields in Excel to rearrange it.';
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
