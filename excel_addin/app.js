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
      'detailPanel', 'btnPivot', 'cubePanel', 'measure',
      'suppress', 'btnCube', 'cubeMsg', 'btnReload', 'wellAvail', 'wellRows',
      'wellCols', 'autoBuild', 'outline',
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
    el.btnFullPivot.addEventListener('click', function () { run(pivotFromFullDetail); });
    el.btnPushComments.addEventListener('click', function () { run(pushCommentsToSheet); });
    el.btnSaveComment.addEventListener('click', function () { run(saveSelectedComment); });
    el.btnDeleteComment.addEventListener('click', function () { run(deleteSelectedComment); });
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
    el.queryPanel.hidden = !ok;
    el.detailPanel.hidden = !ok;
    el.cubePanel.hidden = !ok;
    el.commentPanel.hidden = !ok;
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
      el.btnReload.disabled = true;
      el.btnSyncComments.disabled = true;
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
  var wells = { avail: [], rows: [], cols: [] };
  var MAX = { rows: 4, cols: 3 };

  function populateCube(cat) {
    DIMS = cat.dimensions || [];
    fill(el.measure, cat.measures || [], 'Amount', function (m) {
      return { value: m.key, label: m.label };
    });
    var ph = el.measure.querySelector('option[value=""]');
    if (ph) ph.remove();
    if (!el.measure.value) el.measure.value = 'amount';

    if (!wells.rows.length && !wells.cols.length) {
      wells.rows = ['account_class', 'account'];
      wells.cols = ['fin_year'];
    }
    reflowWells();
    wireWells();
  }

  function dimLabel(key) {
    for (var i = 0; i < DIMS.length; i++) if (DIMS[i].key === key) return DIMS[i].label;
    return key;
  }

  function reflowWells() {
    var used = wells.rows.concat(wells.cols);
    wells.avail = DIMS.map(function (d) { return d.key; })
      .filter(function (k) { return used.indexOf(k) === -1; });
    ['avail', 'rows', 'cols'].forEach(renderWell);
  }

  function renderWell(zone) {
    var host = zone === 'avail' ? el.wellAvail : (zone === 'rows' ? el.wellRows : el.wellCols);
    host.innerHTML = '';
    wells[zone].forEach(function (key, idx) {
      var chip = document.createElement('span');
      chip.className = 'chip';
      chip.dataset.key = key;
      chip.dataset.zone = zone;
      chip.dataset.idx = idx;

      var txt = document.createElement('span');
      txt.className = 'chip__t';
      txt.textContent = dimLabel(key);
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
    if (el.autoBuild.checked && wells.rows.length) run(buildCube);
  }

  /* Interaction deliberately does NOT use HTML5 drag-and-drop: dragstart/drop
     do not fire reliably in Excel's macOS webview, which left the wells looking
     interactive but inert. Every action has a button, and dragging is done with
     pointer events, which the webview does support. */
  var ptr = null;
  var wellsWired = false;

  function wireWells() {
    if (wellsWired) return;
    wellsWired = true;

    [el.wellAvail, el.wellRows, el.wellCols].forEach(function (host) {
      host.addEventListener('click', function (e) {
        var b = e.target.closest && e.target.closest('.chip__b');
        if (!b) return;
        var chip = b.closest('.chip');
        var key = chip.dataset.key, zone = chip.dataset.zone;
        var idx = parseInt(chip.dataset.idx, 10);
        var act = b.dataset.act;
        if (act === 'toRows') moveField(key, zone, 'rows', -1);
        else if (act === 'toCols') moveField(key, zone, 'cols', -1);
        else if (act === 'remove') moveField(key, zone, 'avail', -1);
        else if (act === 'left') reorder(zone, idx, idx - 1);
        else if (act === 'right') reorder(zone, idx, idx + 1);
      });

      host.addEventListener('pointerdown', function (e) {
        if (e.target.closest && e.target.closest('.chip__b')) return;
        var chip = e.target.closest && e.target.closest('.chip');
        if (!chip) return;
        ptr = { key: chip.dataset.key, from: chip.dataset.zone, moved: false };
        chip.classList.add('is-drag');
      });
    });

    document.addEventListener('pointermove', function () {
      if (ptr) ptr.moved = true;
    });

    document.addEventListener('pointerup', function (e) {
      if (!ptr) {
        return;
      }
      var over = document.elementFromPoint(e.clientX, e.clientY);
      var well = over && over.closest ? over.closest('.well') : null;
      if (well && ptr.moved) {
        var chip = over.closest('.chip');
        var at = (chip && chip.dataset.zone === well.dataset.zone)
          ? parseInt(chip.dataset.idx, 10) : -1;
        moveField(ptr.key, ptr.from, well.dataset.zone, at);
      }
      Array.prototype.forEach.call(document.querySelectorAll('.chip.is-drag'),
        function (c) { c.classList.remove('is-drag'); });
      ptr = null;
    });
  }

  function moveField(key, from, to, at) {
    if (from === to && at < 0) return;
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
    el.cubeMsg.textContent = '';
    el.cubeMsg.className = 'msg';
    if (el.autoBuild.checked && wells.rows.length) run(buildCube);
  }

  function readCubeSpec() {
    return {
      rows: wells.rows.slice(),
      cols: wells.cols.slice(),
      measure: el.measure.value || 'amount',
      suppress: el.suppress.checked,
      outline: el.outline.checked
    };
  }

  function applyCubeSpec(spec) {
    wells.rows = (spec.rows || []).slice();
    wells.cols = (spec.cols || []).slice();
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
        groups.forEach(function (g) {
          try {
            sheet.getRangeByIndexes(g.from, 0, g.to - g.from + 1, 1)
              .group(Excel.GroupOption.byRows);
          } catch (e) { /* host without outlining: rows just stay expanded */ }
        });
        await ctx.sync();
      }).catch(function () { /* outlining unsupported; the sheet is still correct */ });
    }

    lastCube[sheetId] = { cube: cube, firstDataRow: firstDataRow, nRowDims: nRowDims };
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
    var newPivot = null;
    try {
      await Excel.run(async function (ctx) {
        var src = ctx.workbook.worksheets.getItem(sourceId);
        var used = src.getUsedRange();
        used.load('address');
        var dest = ctx.workbook.worksheets.add(await uniqueSheetName(ctx, 'Pivot'));
        dest.load('name');
        await ctx.sync();

        var pivotName = 'KlikkPivot_' + dest.name.replace(/[^A-Za-z0-9]/g, '_');
        var pivot = dest.pivotTables.add(pivotName, used,
          dest.getRangeByIndexes(0, 0, 1, 1));
        pivot.rowHierarchies.add(pivot.hierarchies.getItem('Acct class'));
        pivot.rowHierarchies.add(pivot.hierarchies.getItem('Account'));
        pivot.columnHierarchies.add(pivot.hierarchies.getItem('Fin year'));
        pivot.dataHierarchies.add(pivot.hierarchies.getItem('Amount'));
        dest.load('id,name');
        dest.activate();
        await ctx.sync();
        newPivot = { sheetId: dest.id, name: dest.name, pivotName: pivotName };
      });
      await bindQuery(newPivot.sheetId, activeSheet.binding.query, 0, 'pivot',
        { pivotName: newPivot.pivotName, sourceSheet: sourceId });
    } catch (e) {
      throw new Error('Excel could not create a PivotTable here (' +
        (e && e.message ? e.message : e) + '). The cube view does the same job server-side.');
    }
    await inspectActiveSheet();
    el.countLine.textContent = 'PivotTable created on ' + newPivot.name
      + ' — drag fields freely; comments on it sync by meaning, not cell address.';
  }

  /* Re-run the pane's CURRENT filters into the sheet already in front, instead
     of spawning another tab. Refresh replays the sheet's stored query; this
     replaces it with what is in the pane now. */
  async function reloadThisSheet() {
    var b = activeSheet.binding;
    if (!b) throw new Error('Open a Klikk sheet first, then reload it.');
    var qy = readQuery();

    if (b.kind === 'cube') {
      var spec = readCubeSpec();
      var bad = validateCube(spec);
      if (bad) throw new Error(bad);
      progress(0, 1, 'Aggregating…');
      var cube = await fetchCube(qy, spec);
      if (cancelFlag.cancelled) return;
      await renderCube(activeSheet.id, cube, qy, spec);
      await inspectActiveSheet();
      el.cubeMsg.textContent = 'Reloaded ' + activeSheet.name + ' — '
        + fmtNum(cube.leaf_count) + ' leaf rows.';
      el.cubeMsg.className = 'msg msg--ok';
      return;
    }

    progress(0, 1, 'Querying…');
    var got = await fetchRows(qy);
    if (cancelFlag.cancelled) return;
    await renderRows(activeSheet.id, got.rows, qy);
    await inspectActiveSheet();
    el.countLine.innerHTML = 'Reloaded — <strong>' + fmtNum(got.rows.length) + '</strong> rows.';
  }

  /* ── comments pinned to a cube intersection ────────────────── */

  var COMMENT_API = '/xero/data/journals/pivot/comments/';

  function cellToIntersection(cube, rowIdx, colIdx) {
    var r = cube.rows[rowIdx];
    if (!r) return null;
    var nCols = cube.cols.length;
    var colPath, value;
    if (colIdx < 0) return null;
    if (colIdx < nCols) {
      colPath = cube.cols[colIdx];
      value = r.cells[colIdx];
    } else if (colIdx === nCols) {
      colPath = 'Total';
      value = r.cells.reduce(function (a, b) { return a + b; }, 0);
    } else {
      return null;
    }
    return {
      row_dims: cube.row_dims.slice(0, r.keys.length).map(function (d) { return d.key; }),
      row_path: r.keys,
      col_dims: cube.col_dims.map(function (d) { return d.key; }),
      col_path: colPath,
      cell_value: value
    };
  }

  function anchorId(x) {
    return x.row_path.join('\u001f') + '\u001e' + x.col_path;
  }

  async function syncComments() {
    var b = activeSheet.binding;
    if (!b) throw new Error('Open a Klikk cube or PivotTable sheet first.');
    if (b.kind === 'pivot') return syncPivotComments(b);
    if (b.kind !== 'cube') throw new Error('Open a cube or PivotTable sheet first.');
    if (!Office.context.requirements.isSetSupported('ExcelApi', '1.10')) {
      throw new Error('This Excel build has no comment API (needs ExcelApi 1.10).');
    }

    el.commentMsg.textContent = 'Rebuilding the cube to locate cells…';
    el.commentMsg.className = 'msg';

    // Re-derive the layout from the server rather than storing thousands of row
    // keys in the workbook; the sheet is a pure function of the spec anyway.
    var cube = await fetchCube(b.query, b.spec);
    var nRowDims = cube.row_dims.length;
    var FIRST_DATA_ROW = 4;
    var sheetId = activeSheet.id;

    var found = [];
    await Excel.run(async function (ctx) {
      var sheet = ctx.workbook.worksheets.getItem(sheetId);
      var comments = sheet.comments;
      comments.load('items/content');
      await ctx.sync();
      var locs = comments.items.map(function (c) {
        var rng = c.getLocation();
        rng.load('rowIndex,columnIndex');
        return rng;
      });
      await ctx.sync();
      comments.items.forEach(function (c, i) {
        found.push({ content: c.content, r: locs[i].rowIndex, c: locs[i].columnIndex });
      });
    });

    var author = (el.commentAuthor.value || '').trim();
    var posted = 0, skipped = 0;
    var onSheet = {};

    for (var i = 0; i < found.length; i++) {
      var f = found[i];
      var x = cellToIntersection(cube, f.r - FIRST_DATA_ROW, f.c - nRowDims);
      if (!x) { skipped += 1; continue; }
      onSheet[anchorId(x)] = true;
      await apiPost(COMMENT_API, {
        measure: b.spec.measure,
        row_dims: x.row_dims, row_path: x.row_path,
        col_dims: x.col_dims, col_path: x.col_path,
        filters: toParams(b.query),
        cell_value: x.cell_value,
        comment: f.content,
        author: author
      });
      posted += 1;
    }

    // Pull anything commented elsewhere back onto the sheet.
    var server = await apiGet(COMMENT_API, { status: 'all', limit: 2000 });
    var pulled = 0;
    var toAdd = [];
    (server.results || []).forEach(function (c) {
      if (c.measure !== b.spec.measure) return;
      var id = c.row_path.join('\u001f') + '\u001e' + c.col_path;
      if (onSheet[id]) return;
      for (var ri = 0; ri < cube.rows.length; ri++) {
        if (cube.rows[ri].keys.join('\u001f') !== c.row_path.join('\u001f')) continue;
        var ci = c.col_path === 'Total' ? cube.cols.length : cube.cols.indexOf(c.col_path);
        if (ci < 0) return;
        toAdd.push({ r: ri + FIRST_DATA_ROW, c: ci + nRowDims, text: c.comment });
        return;
      }
    });

    if (toAdd.length) {
      await Excel.run(async function (ctx) {
        var sheet = ctx.workbook.worksheets.getItem(sheetId);
        toAdd.forEach(function (a) {
          try { sheet.comments.add(sheet.getRangeByIndexes(a.r, a.c, 1, 1), a.text); } catch (e) { /* already there */ }
        });
        await ctx.sync();
      });
      pulled = toAdd.length;
    }

    el.commentMsg.textContent = posted + ' sent to Postgres, ' + pulled + ' pulled back'
      + (skipped ? ', ' + skipped + ' outside the data area ignored' : '') + '.';
    el.commentMsg.className = 'msg msg--ok';
  }

  /* Comments on a NATIVE PivotTable.
   *
   * Excel pins a comment to a cell ADDRESS. A PivotTable's cells move the
   * moment you expand a node, drag a field or refresh, so an address-anchored
   * comment silently ends up describing a different number — worse than no
   * comment at all on an audit. So we never store the address: each commented
   * cell is resolved to the pivot items that actually produce it, and THAT is
   * the anchor. Needs ExcelApi 1.12 (getPivotItems / getDataHierarchy); if the
   * host cannot do it we refuse rather than store something that will drift.
   */
  async function syncPivotComments(b) {
    if (!Office.context.requirements.isSetSupported('ExcelApi', '1.10')) {
      throw new Error('This Excel build has no comment API (needs ExcelApi 1.10).');
    }
    if (!Office.context.requirements.isSetSupported('ExcelApi', '1.12')) {
      throw new Error('This Excel build cannot resolve a PivotTable cell to its row '
        + 'and column items (needs ExcelApi 1.12). Anchoring on the cell address '
        + 'instead would drift as soon as the pivot is rearranged, so it is not '
        + 'offered. Comment on a cube sheet instead.');
    }

    el.commentMsg.textContent = 'Resolving pivot cells…';
    el.commentMsg.className = 'msg';

    var sheetId = activeSheet.id;
    var resolved = [];

    await Excel.run(async function (ctx) {
      var sheet = ctx.workbook.worksheets.getItem(sheetId);
      var pivot = sheet.pivotTables.getItem(b.spec.pivotName);
      var comments = sheet.comments;
      comments.load('items/content');
      await ctx.sync();

      if (!comments.items.length) return;

      var cells = comments.items.map(function (c) {
        var r = c.getLocation();
        r.load('address');
        return r;
      });
      await ctx.sync();

      var parts = cells.map(function (cell) {
        var rows = pivot.layout.getPivotItems(Excel.PivotAxis.row, cell);
        var cols = pivot.layout.getPivotItems(Excel.PivotAxis.column, cell);
        var data = pivot.layout.getDataHierarchy(cell);
        rows.load('items/name');
        cols.load('items/name');
        data.load('name');
        cell.load('values');
        return { rows: rows, cols: cols, data: data, cell: cell };
      });
      await ctx.sync();

      comments.items.forEach(function (c, i) {
        var pt = parts[i];
        var rowPath = pt.rows.items.map(function (x) { return x.name; });
        if (!rowPath.length) return;              // header or blank cell, not a value
        resolved.push({
          content: c.content,
          row_path: rowPath,
          col_path: pt.cols.items.map(function (x) { return x.name; }).join(' | ') || 'Total',
          measure: pt.data.name || 'Amount',
          value: (pt.cell.values && pt.cell.values[0]) ? pt.cell.values[0][0] : null
        });
      });
    });

    if (!resolved.length) {
      el.commentMsg.textContent = 'No comments found on a value cell of this PivotTable.';
      el.commentMsg.className = 'msg';
      return;
    }

    var author = (el.commentAuthor.value || '').trim();
    var posted = 0;
    for (var i = 0; i < resolved.length; i++) {
      var r = resolved[i];
      await apiPost(COMMENT_API, {
        measure: r.measure,
        // The pivot names its own levels; record them positionally so the anchor
        // still reads sensibly when the field list changes.
        row_dims: r.row_path.map(function (_, n) { return 'pivot_row_' + (n + 1); }),
        row_path: r.row_path,
        col_dims: ['pivot_col'],
        col_path: r.col_path,
        filters: toParams(b.query),
        cell_value: typeof r.value === 'number' ? r.value : null,
        comment: r.content,
        author: author
      });
      posted += 1;
    }

    el.commentMsg.textContent = posted + ' comment' + (posted === 1 ? '' : 's')
      + ' sent to Postgres, anchored to their row/column items. They stay on the sheet.';
    el.commentMsg.className = 'msg msg--ok';
  }

  /* ── the selected cell ─────────────────────────────────── */

  var lastCube = {};
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
    if (probe.count > 250000) {
      throw new Error(fmtNum(probe.count) + ' rows match. Narrow the filters — a '
        + 'PivotTable over that many rows will crawl on this machine.');
    }

    progress(0, 1, 'Pulling all ' + fmtNum(probe.count) + ' rows…');
    var got = await fetchRows(qy);
    if (cancelFlag.cancelled) return;
    await renderRows(null, got.rows, qy);
    await inspectActiveSheet();
    await addNativePivot();
    el.countLine.innerHTML = 'PivotTable over <strong>' + fmtNum(got.rows.length)
      + '</strong> rows — the complete result for these filters.';
  }

  function watchSelection() {
    Excel.run(function (ctx) {
      ctx.workbook.onSelectionChanged.add(function () { return readSelection(); });
      return ctx.sync();
    }).catch(function () { /* host without the event: the buttons still work */ });
  }

  async function readSelection() {
    try {
      var b = activeSheet.binding;
      if (!b) { return showSelection(null); }
      if (b.kind === 'pivot') return showSelection(await resolvePivotSelection(b));
      if (b.kind === 'cube') return showSelection(await resolveCubeSelection(b));
      return showSelection(null);
    } catch (e) {
      return showSelection(null);
    }
  }

  async function resolvePivotSelection(b) {
    if (!Office.context.requirements.isSetSupported('ExcelApi', '1.12')) return null;
    var out = null;
    await Excel.run(async function (ctx) {
      var sheet = ctx.workbook.worksheets.getItem(activeSheet.id);
      var pivot = sheet.pivotTables.getItem(b.spec.pivotName);
      var cell = ctx.workbook.getSelectedRange();
      cell.load('values,cellCount,rowIndex,columnIndex');
      await ctx.sync();
      if (cell.cellCount !== 1) return;

      var rows = pivot.layout.getPivotItems(Excel.PivotAxis.row, cell);
      var cols = pivot.layout.getPivotItems(Excel.PivotAxis.column, cell);
      var data = pivot.layout.getDataHierarchy(cell);
      rows.load('items/name'); cols.load('items/name'); data.load('name');
      await ctx.sync();

      var rp = rows.items.map(function (x) { return x.name; });
      if (!rp.length) return;
      out = {
        measure: data.name || 'Amount',
        row_dims: rp.map(function (_, i) { return 'pivot_row_' + (i + 1); }),
        row_path: rp,
        col_dims: ['pivot_col'],
        col_path: cols.items.map(function (x) { return x.name; }).join(' | ') || 'Total',
        value: (cell.values && cell.values[0]) ? cell.values[0][0] : null,
        r: cell.rowIndex, c: cell.columnIndex,
        query: b.query
      };
    });
    return out;
  }

  async function resolveCubeSelection(b) {
    var cached = lastCube[activeSheet.id];
    if (!cached) return null;                 // rebuilt in another session
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
    el.commentMsg.textContent = 'Saved against ' + selection.row_path.join(' / ')
      + ' × ' + selection.col_path + '.';
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
  async function writeCellComment(sheetId, r, c, text) {
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

        var hit = -1;
        for (var i = 0; i < locs.length; i++) {
          if (locs[i].rowIndex === r && locs[i].columnIndex === c) { hit = i; break; }
        }
        if (text) {
          if (hit >= 0) comments.items[hit].content = text;
          else comments.add(sheet.getRangeByIndexes(r, c, 1, 1), text);
        } else if (hit >= 0) {
          comments.items[hit].delete();
        }
        await ctx.sync();
      });
    } catch (e) {
      // Comment API missing (needs ExcelApi 1.10) — the comment is still safely
      // in Postgres, it just is not mirrored onto the grid.
    }
  }

  /* Paint every stored comment for this sheet back onto its cell. */
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
      var cached = lastCube[activeSheet.id];
      if (!cached) throw new Error('Rebuild this cube sheet first, then try again.');
      var writes = [];
      cached.cube.rows.forEach(function (r, i) {
        var nCols = cached.cube.cols.length;
        for (var ci = 0; ci <= nCols; ci++) {
          var x = cellToIntersection(cached.cube, i, ci);
          if (!x) continue;
          var txt = want[b.spec.measure + '\u001e' + x.row_path.join('\u001f') + '\u001e' + x.col_path];
          if (txt) writes.push({ r: cached.firstDataRow + i, c: cached.nRowDims + ci, t: txt });
        }
      });
      for (var w = 0; w < writes.length; w++) {
        await writeCellComment(activeSheet.id, writes[w].r, writes[w].c, writes[w].t);
        placed += 1;
      }
    } else {
      // A PivotTable's cells only reveal their meaning one at a time, so walk
      // the data body and resolve as we go. Bounded, and it reports what it
      // could not reach rather than pretending it covered everything.
      if (!Office.context.requirements.isSetSupported('ExcelApi', '1.12')) {
        throw new Error('Needs ExcelApi 1.12 to locate PivotTable cells by meaning.');
      }
      var found = [];
      await Excel.run(async function (ctx) {
        var sheet = ctx.workbook.worksheets.getItem(activeSheet.id);
        var pivot = sheet.pivotTables.getItem(b.spec.pivotName);
        var body = pivot.layout.getDataBodyRange();
        body.load('rowIndex,columnIndex,rowCount,columnCount');
        await ctx.sync();

        var total = body.rowCount * body.columnCount;
        if (total > 1500) {
          unplaced = total;
          return;
        }
        var probes = [];
        for (var rr = 0; rr < body.rowCount; rr++) {
          for (var cc = 0; cc < body.columnCount; cc++) {
            var cell = sheet.getRangeByIndexes(body.rowIndex + rr, body.columnIndex + cc, 1, 1);
            var pr = pivot.layout.getPivotItems(Excel.PivotAxis.row, cell);
            var pc = pivot.layout.getPivotItems(Excel.PivotAxis.column, cell);
            var pd = pivot.layout.getDataHierarchy(cell);
            pr.load('items/name'); pc.load('items/name'); pd.load('name');
            probes.push({ r: body.rowIndex + rr, c: body.columnIndex + cc, pr: pr, pc: pc, pd: pd });
          }
        }
        await ctx.sync();
        probes.forEach(function (pb) {
          var rp = pb.pr.items.map(function (x) { return x.name; });
          if (!rp.length) return;
          var key = (pb.pd.name || 'Amount') + '\u001e' + rp.join('\u001f') + '\u001e'
            + (pb.pc.items.map(function (x) { return x.name; }).join(' | ') || 'Total');
          if (want[key]) found.push({ r: pb.r, c: pb.c, t: want[key] });
        });
      });
      if (unplaced) {
        throw new Error('This PivotTable has ' + fmtNum(unplaced) + ' value cells — too many '
          + 'to resolve one by one. Collapse or filter it, then try again.');
      }
      for (var f = 0; f < found.length; f++) {
        await writeCellComment(activeSheet.id, found[f].r, found[f].c, found[f].t);
        placed += 1;
      }
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
