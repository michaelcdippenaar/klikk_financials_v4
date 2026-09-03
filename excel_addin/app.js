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

  /* The cell context menu ("Klikk: Show transactions") opens the pane at
     #drill. That is not a section: it lands on Comments -- where the
     selection readout and the drill live -- and queues a drill of the cell
     under the cursor, run once the pane is connected and has read the sheet
     in front. The hash is then put back to #comments so the next
     right-click is a URL change again; a same-URL ShowTaskpane may not
     re-navigate the webview, and without a change there is no hashchange. */
  var DRILL_HASH = 'drill';
  var pendingDrill = false;

  function sectionFromHash() {
    var h = (window.location.hash || '').replace(/^#/, '');
    if (h === DRILL_HASH) { pendingDrill = true; return 'comments'; }
    return SECTIONS[h] ? h : null;
  }

  function settleDrillHash() {
    try {
      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, '', '#comments');
      }
    } catch (e) { /* a webview that refuses replaceState just keeps #drill */ }
  }

  /* Runs the queued context-menu drill if the pane is in a state to do it:
     connected, and nothing else in flight. Called from the hashchange
     listener (pane already open) and from the end of connect() (pane opened
     cold by the menu item, or connected by hand afterwards). */
  function runPendingDrill() {
    if (!pendingDrill) return;
    if (!connected) return;          // connect() calls back in when it succeeds
    pendingDrill = false;
    settleDrillHash();
    showSection('comments');
    run(drillActiveCell);
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
    // Populated on a drill ("show transactions"); blank on a plain detail
    // load, which does not resolve documents. ~7% of ledger lines have a
    // receipt at all — a blank cell means no document, not a broken link.
    { key: 'receipt_url',             label: 'Receipt',    fmt: 'link',  width: 12 },
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

  /* Accounting convention: parentheses for negatives, a dash for zero.

     On an income statement the sign flips by account class, so a column is a
     mix of both — and a bracket is far quicker to scan than a leading minus,
     which is why every set of statutory accounts uses it. MC formatted a built
     cube this way by hand, which is the clearest possible statement of intent;
     doing it in the render means it survives the next rebuild, which his manual
     formatting would not have.

     Whole rands by default: at twenty-odd columns the cents cost width and buy
     nothing. Decimals stay available for the cases that need them. */
  var MONEY_FMT_0 = '#,##0;(#,##0);"–"';
  var MONEY_FMT_2 = '#,##0.00;(#,##0.00);"–"';
  var MONEY_FMT = MONEY_FMT_2;              // detail sheets keep their cents

  /* Sheet palette. Deliberately a light theme with a dark header band rather
     than following the pane's dark mode: a worksheet is printed, shared and
     screenshotted, and Excel does not tell an add-in which theme the workbook
     will be read under. */
  /* Header colour is a CHOICE, not a constant.

     A built cube is cleared and rewritten on every refresh, so anything
     formatted by hand is lost the next time it is built. Making the palette
     part of the spec means a preference survives the rebuild — and travels
     with a saved view. */
  var HEAD_THEMES = {
    navy:   { bg: '#1F2836', bg2: '#2E3948' },
    purple: { bg: '#6F2F6A', bg2: '#8A4785' },
    plum:   { bg: '#5B2545', bg2: '#7A3A61' },
    forest: { bg: '#1E4634', bg2: '#2F5F49' },
    slate:  { bg: '#3A4551', bg2: '#4E5A68' },
    black:  { bg: '#22252A', bg2: '#343941' }
  };

  var SHEET = {
    headBg:    '#1F2836',   // replaced per build from the chosen theme
    headBg2:   '#2E3948',
    headInk:   '#FFFFFF',
    accent:    '#C8912A',   // Klikk gold, for the title and the grand total rule
    /* Consolidation greys: DARKEST AT THE HIGHEST LEVEL, getting lighter as
       you go down. The steps are deliberately wide enough to tell apart on a
       laptop screen at 100% -- three tints that are nearly the same shade look
       like a rendering artefact rather than a hierarchy. Neutral grey rather
       than a tinted one so it sits under any header colour. */
    total0:    '#C7CBD2',   // top-level rollup
    total1:    '#DCDFE4',
    total2:    '#EDEFF2',   // and anything deeper
    grand:     '#B3B8C1',
    rule:      '#9BA2AD',
    subtle:    '#6B7280'
  };
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
      'typeHint', 'typeMirrorHint', 'dateFrom', 'dateTo', 'account', 'accountList', 'contact', 'reference',
      'contactList', 'description', 'amount', 'q', 'maxRows', 'countLine', 'btnLoad', 'btnCount',
      'detailPanel', 'btnPivot', 'cubePanel', 'measure', 'btnResetComments',
      'queryPanel', 'refreshPanel', 'commentPanel', 'settingsPanel',
      'suppress', 'btnCube', 'btnCubeNew', 'cubeTarget', 'cubeMsg', 'btnDrill', 'headTheme', 'showDecimals', 'btnReload', 'wellAvail', 'wellRows',
      'wellCols', 'wellFilt', 'autoBuild', 'outline',
      'pickerModal', 'pickerTitle', 'pickerClose', 'pickerSearch',
      'pickerAvail', 'pickerSel', 'pickerAvailCount', 'pickerSelCount',
      'btnPickAdd', 'btnPickAddAll', 'btnPickRemove', 'btnPickRemoveAll',
      'pickerApply', 'pickerCancel', 'pickerMsg',
      'btnPickUp', 'btnPickDown', 'btnPickSortAz',
      'subsetPick', 'subsetName', 'btnSubsetLoad', 'btnSubsetSave', 'btnSubsetDelete',
      'viewPick', 'viewName', 'btnViewLoad', 'btnViewRebuild', 'btnViewSave', 'btnViewDelete',
      'commentPanel', 'commentAuthor', 'btnSyncComments', 'commentMsg',
      'btnFullPivot', 'selNone', 'selHas', 'selPath', 'selVal', 'selComment',
      'btnSaveComment', 'btnDeleteComment', 'selBox', 'markCells', 'btnPushComments',
      'bulkTags', 'bulkNote', 'btnBulkFlag', 'bulkCount',
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
      reportMissingControls();
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

    /* Read the sheet in front BEFORE connecting. connect() ends in
       populateCube(), which points the wells at the active sheet's cube spec,
       and that needs the binding already read. Firing both at once left it to
       a race the network usually -- not always -- lost, and the pane came up
       showing defaults on top of a cube it should have recognised. Capped so
       a host that never answers cannot hold the connect. */
    Promise.race([
      inspectActiveSheet(),
      new Promise(function (r) { setTimeout(r, 4000); })
    ]).then(function () {
      if (settings.token) connect(true);
      else el.settingsPanel.hidden = false;
    });
  });


  /* Listener registration that cannot take its neighbours down.

     wireEvents() and wirePicker() register dozens of listeners in sequence.
     With bare el.x.addEventListener, ONE control missing from the page --
     a renamed id, an HTML/JS pair from different releases -- throws, and
     every listener after it is never attached: the pane renders and half
     its buttons are dead, with nothing on screen saying why. Register each
     one on its own, remember what was missing, and say so. */
  var missingControls = [];

  function on(id, ev, fn) {
    var node = el[id] || document.getElementById(id);
    if (!node || typeof node.addEventListener !== 'function') {
      if (missingControls.indexOf(id) < 0) missingControls.push(id);
      return;
    }
    node.addEventListener(ev, fn);
  }

  function reportMissingControls() {
    window.__klikkMissing = missingControls.slice();
    if (!missingControls.length || !el.errorMsg) return;
    el.errorMsg.textContent = 'This page is missing ' + missingControls.length
      + ' control' + (missingControls.length === 1 ? '' : 's') + ' the add-in expects ('
      + missingControls.join(', ') + '). Those buttons are inert; the rest work. '
      + 'Close and reopen the pane to fetch a matching page.';
    el.errorMsg.hidden = false;
  }

  function wireEvents() {
    on('btnSettings', 'click', function () {
      el.settingsPanel.hidden = !el.settingsPanel.hidden;
    });
    on('btnConnect', 'click', function () { connect(false); });
    on('btnForget', 'click', forget);
    on('btnLoad', 'click', function () { run(loadToNewSheet); });
    on('btnCount', 'click', function () { run(showCount); });
    on('btnCube', 'click', function () { run(buildCube); });
    on('btnCubeNew', 'click', function () { run(buildCubeToNewSheet); });
    on('btnPivot', 'click', function () { run(addNativePivot); });
    on('btnReload', 'click', function () { run(refreshActiveSheet); });
    on('btnSyncComments', 'click', function () { run(syncComments); });
    on('btnResetComments', 'click', function () { run(resetSheetComments); });
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
      runPendingDrill();
    });
    on('btnFullPivot', 'click', function () { run(pivotFromFullDetail); });
    on('btnPushComments', 'click', function () { run(pushCommentsToSheet); });
    on('btnSaveComment', 'click', function () { run(saveSelectedComment); });
    on('btnDeleteComment', 'click', function () { run(deleteSelectedComment); });
    on('btnDrill', 'click', function () { run(drillSelection); });
    on('btnBulkFlag', 'click', function () { run(flagSelection); });
    on('btnViewSave', 'click', function () { run(saveCurrentView); });
    on('btnViewLoad', 'click', function () { run(openSavedView); });
    if (el.btnViewRebuild) {
      on('btnViewRebuild', 'click', function () { run(rebuildSavedView); });
    }
    on('btnViewDelete', 'click', function () {
      run(async function () {
        var n = el.viewPick.value;
        if (!n) throw new Error('Pick a saved view to delete.');
        await apiDelete('/xero/data/journals/pivot/views/', { name: n });
        await loadViewList();
        el.cubeMsg.textContent = 'Deleted saved view "' + n + '". Sheets built from it are untouched.';
        el.cubeMsg.className = 'msg';
      });
    });
    watchSelection();
    on('btnRefresh', 'click', function () { run(refreshActiveSheet); });
    on('btnRestore', 'click', restoreFiltersFromSheet);
    on('btnCancel', 'click', function () { cancelFlag.cancelled = true; });
    on('journalType', 'change', updateTypeHint);

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
      // The boot path read the active sheet before calling connect(), so a
      // drill queued by the context menu has its binding available now.
      runPendingDrill();
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
    fill(el.journalType, (opts.journal_types || []).map(function (t) { return t; }),
      'Ledger (excludes legacy mirror)',
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
    // The blank type is now a real control, not a trap: search and pivot both
    // drop the frozen 'journal' mirror when no type is asked for, so the
    // default ties to the trial balance. The only unsafe selection left is the
    // mirror itself, chosen deliberately.
    var t = (el.journalType.value || '').toLowerCase();
    el.typeHint.hidden = !!t;
    el.typeMirrorHint.hidden = t !== 'journal';
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

  async function apiDelete(path, params) {
    var url = settings.baseUrl + path;
    var qs = Object.keys(params || {})
      .filter(function (k) { return params[k] !== '' && params[k] != null; })
      .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]); })
      .join('&');
    if (qs) url += '?' + qs;
    var res;
    try {
      res = await fetch(url, {
        method: 'DELETE',
        headers: { Authorization: 'Token ' + settings.token, Accept: 'application/json' }
      });
    } catch (e) {
      throw new Error('Cannot reach ' + settings.baseUrl + '.');
    }
    if (res.status === 401 || res.status === 403) throw new Error('Token rejected (' + res.status + ').');
    if (!res.ok) throw new Error('Server returned ' + res.status + ' ' + res.statusText + '.');
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

  /* rtotals / ctotals — which fields carry a total.

     Each is sent ONLY when it differs from the server's default, so an
     unchanged cube produces the same request it always did. rtotals is
     omitted while every row level still shows its subtotal; ctotals is
     omitted while no column field asks for one. */
  function totalsParams(spec) {
    var t = (spec && spec.totals) || {};
    var rows = spec.rows || [], cols = spec.cols || [];
    function on(k, zone) {
      return Object.prototype.hasOwnProperty.call(t, k) ? !!t[k] : zone === 'rows';
    }
    var out = {};
    var parents = rows.slice(0, -1);
    if (parents.some(function (k) { return !on(k, 'rows'); })) {
      var keep = parents.filter(function (k) { return on(k, 'rows'); });
      // An empty value is meaningful here: "no row subtotals at all".
      out.rtotals = keep.length ? keep.join(',') : '__none__';
    }
    var ct = cols.slice(0, -1).filter(function (k) { return on(k, 'cols'); });
    if (ct.length) out.ctotals = ct.join(',');
    return out;
  }

  /* {dimf: '{"fin_year":["FY2026"]}'} — omitted entirely when nothing is
     narrowed, so an unfiltered cube's URL and comment anchors stay as they
     were before filters existed. */
  function dimfParam(spec) {
    var f = spec && spec.filters ? spec.filters : {};
    var live = {};
    Object.keys(f).forEach(function (k) {
      // NOT sorted. The subset's order is the layout order -- the server lays
      // rows and columns out in exactly this sequence -- so sorting here would
      // silently throw away the arrangement the user made.
      if (f[k] && f[k].length) live[k] = f[k].slice();
    });
    return Object.keys(live).length ? { dimf: JSON.stringify(live) } : {};
  }

  function describe(qy) {
    var bits = [];
    if (qy.tenant) {
      var opt = el.tenant.querySelector('option[value="' + qy.tenant + '"]');
      bits.push(opt ? opt.textContent : 'one entity');
    }
    // A saved sheet has to say which ledger it holds. The blank type is not
    // "everything" any more -- both endpoints drop the frozen 'journal' mirror
    // by default -- so name the cut either way.
    bits.push(qy.journal_type ? qy.journal_type : 'ledger (no legacy mirror)');
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
        /* A clickable link without a per-cell Range.hyperlink call, which would
           be one Office round trip per row and make a large drill crawl.
           HYPERLINK() rides along in the same values write as everything else.
           Quotes are doubled so a stray one cannot terminate the formula. */
        if (c.fmt === 'link') {
          return v ? '=HYPERLINK("' + String(v).replace(/"/g, '""') + '","Receipt")' : '';
        }
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
    paintCubeTarget();
  }

  /* Build is in place when a cube sheet is in front -- the way a PivotTable
     refreshes, rather than sprouting "Cube 2", "Cube 3", ... on every change
     -- so the button must say which of the two it is about to do. */
  function cubeTargetId() {
    var b = activeSheet.binding;
    return (b && b.kind === 'cube' && b.spec && activeSheet.id) ? activeSheet.id : null;
  }

  function paintCubeTarget() {
    if (!el.btnCube) return;
    var inPlace = !!cubeTargetId();
    el.btnCube.textContent = inPlace ? 'Rebuild ' + activeSheet.name : 'Build cube view';
    el.btnCube.title = inPlace
      ? 'Rewrite ' + activeSheet.name + ' from the wells above'
      : 'Write the cube to a new sheet';
    if (el.btnCubeNew) el.btnCubeNew.hidden = !inPlace;
    if (el.cubeTarget) {
      el.cubeTarget.textContent = inPlace
        ? 'The wells show the layout of ' + activeSheet.name + '. Change them and Rebuild '
          + 'rewrites that sheet in place; New sheet leaves it alone.'
        : 'No cube sheet is in front, so Build writes a new sheet. Select a cube sheet '
          + 'to edit that one in place.';
    }
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
  /* Inline SVG icons.

     Text glyphs (R, C, F, the angle quotes) were doing the work of icons and
     doing it badly: they carry no meaning to anyone who has not been told what
     they stand for, and they render at whatever the font decides. These are
     drawn on a 24-unit grid and inherit currentColor, so they follow the
     theme -- including Excel's dark mode -- without a second palette. */
  var ICON = {
    rows:    '<path d="M3 5h18M3 12h18M3 19h12"/>',
    cols:    '<path d="M5 3v18M12 3v18M19 3v12"/>',
    filter:  '<path d="M3 5h18l-7 8v6l-4 2v-8z"/>',
    subset:  '<path d="M6 9l6 6 6-6"/>',
    left:    '<path d="M15 6l-6 6 6 6"/>',
    right:   '<path d="M9 6l6 6-6 6"/>',
    up:      '<path d="M12 19V5M6 11l6-6 6 6"/>',
    down:    '<path d="M12 5v14M6 13l6 6 6-6"/>',
    remove:  '<path d="M18 6L6 18M6 6l12 12"/>',
    add:     '<path d="M9 6l6 6-6 6"/>',
    addAll:  '<path d="M6 6l6 6-6 6M13 6l6 6-6 6"/>',
    del:     '<path d="M15 6l-6 6 6 6"/>',
    delAll:  '<path d="M11 6l-6 6 6 6M18 6l-6 6 6 6"/>',
    sortAz:  '<path d="M7 4v16M4 17l3 3 3-3M13 5h7M13 10h5M13 15h3"/>',
    search:  '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
    check:   '<path d="M5 13l4 4L19 7"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
    table:   '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/>',
    cube:    '<path d="M12 3l9 5v8l-9 5-9-5V8z"/><path d="M12 21V13M3 8l9 5 9-5"/>',
    comment: '<path d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    key:     '<circle cx="8" cy="14" r="4"/><path d="M11 11l9-9M17 5l3 3M14 8l3 3"/>',
    save:    '<path d="M5 3h11l3 3v15H5z"/><path d="M8 3v6h8M8 21v-6h8v6"/>',
    trash:   '<path d="M4 7h16M9 7V4h6v3M6 7l1 14h10l1-14"/>',
    play:    '<path d="M6 4l14 8-14 8z"/>',
    total:   '<path d="M18 4H6l7 8-7 8h12"/>'   // sigma
  };

  function svgIcon(name, size) {
    var d = ICON[name];
    if (!d) return '';
    var px = size || 14;
    return '<svg class="ic" viewBox="0 0 24 24" width="' + px + '" height="' + px
      + '" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
      + 'stroke-linejoin="round" aria-hidden="true" focusable="false">' + d + '</svg>';
  }

  var DIMS = [];
  var wells = { avail: [], rows: [], cols: [], filt: [] };
  /* rows is capped at 8 because that is Excel's own limit — a sheet supports 8
     outline levels, and "Collapsible groups in the sheet" spends one per row
     dimension. Past 8 the grouping silently stops nesting rather than erroring,
     which reads as a broken cube. The server has no cap: it happily groups by
     7 dimensions (4,287 rows, verified), so 4 was only ever a client guess. */
  var MAX = { rows: 8, cols: 3, filt: 6 };
  // dimension key -> array of selected labels. Empty array = the field is
  // on Filters but not yet narrowed, which passes everything through.
  var filterVals = {};
  /* Which fields show a total.

     Rows default ON (every level has always had a subtotal) and columns
     default OFF (stacked columns never had one), so an unset field behaves
     exactly as it did before this option existed. Only an explicit choice is
     stored, which is also what keeps a saved view meaning the same thing after
     the defaults are read again. */
  var totalVals = {};
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
    reportMissingControls();
    loadViewList();   // async on purpose: the wells must not wait on it
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
      /* A subset belongs to the DIMENSION, not to the well it is sitting in.
         Financial year restricted to FY2024-FY2026 means the same thing
         whether the field is on rows, on columns, or filtering from the side
         -- so the count is shown wherever the chip is, and dragging a field
         between wells carries its subset along. */
      var sub = filterVals[key] || [];
      if (zone === 'avail') {
        txt.textContent = dimLabel(key);
      } else {
        txt.textContent = dimLabel(key) + (sub.length
          ? ' · ' + (sub.length === 1 ? sub[0] : sub.length + ' selected')
          : '');
      }
      chip.appendChild(txt);

      var acts = document.createElement('span');
      acts.className = 'chip__acts';
      function btn(act, iconName, title) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'chip__b';
        b.dataset.act = act;
        b.innerHTML = svgIcon(iconName, 13);
        b.title = title;
        b.setAttribute('aria-label', title);
        return b;
      }
      if (zone === 'avail') {
        acts.appendChild(btn('toRows', 'rows', 'Move to Rows'));
        acts.appendChild(btn('toCols', 'cols', 'Move to Columns'));
        acts.appendChild(btn('toFilt', 'filter', 'Move to Filters'));
      } else if (zone === 'filt') {
        acts.appendChild(btn('pick', 'subset', 'Choose values'));
        acts.appendChild(btn('remove', 'remove', 'Remove'));
      } else {
        acts.appendChild(btn('left', 'left', 'Move earlier'));
        acts.appendChild(btn('right', 'right', 'Move later'));
        acts.appendChild(btn('pick', 'subset', 'Subset — choose which values appear'));
        /* A total on the innermost field would repeat the field itself, so the
           toggle only appears where it means something -- on a field that has
           another field stacked beneath it. */
        if (idx < wells[zone].length - 1) {
          var on = totalOn(key, zone);
          var tb = btn('total', 'total', on
            ? 'Total for each ' + dimLabel(key) + ' — on. Click to hide it.'
            : 'Total for each ' + dimLabel(key) + ' — off. Click to show it.');
          if (on) tb.className += ' chip__b--on';
          acts.appendChild(tb);
        }
        acts.appendChild(btn('remove', 'remove', 'Remove'));
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
        if (act === 'total') toggleTotal(key, zone);
        else if (act === 'pick') openPicker(key);
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

  /* Rows have always shown a subtotal per level; stacked columns never had
     one. Keep both defaults so nobody's existing sheet changes shape until
     they ask it to. */
  function totalOn(key, zone) {
    if (Object.prototype.hasOwnProperty.call(totalVals, key)) return !!totalVals[key];
    return zone === 'rows';
  }

  function toggleTotal(key, zone) {
    totalVals[key] = !totalOn(key, zone);
    reflowWells();
    rememberCubeSpec();
    if (el.autoBuild.checked && wells.rows.length) run(buildCube);
  }

  function moveField(key, from, to, at) {
    if (from === to && at < 0) return;
    /* Dropping a field back into Fields removes it from the view entirely, so
       its subset goes too -- otherwise an invisible constraint would survive
       on a dimension that is no longer anywhere on the sheet. Moving between
       rows, columns and filters KEEPS it: those are all ways of showing the
       same restricted set of members. */
    if (to === 'avail') delete filterVals[key];
    else if (!filterVals[key]) filterVals[key] = [];
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

  /* Subset editor for one filtered dimension.

     Two panes, the way Planning Analytics does it: what exists on the left,
     what you are keeping on the right. Members come from the server under the
     CURRENT journal filters, so the list is what is really in the data rather
     than a catalogue of everything that ever existed.

     Edits are held in `working` and only committed on Apply. A filter that
     rebuilt the sheet on every click would make assembling a five-value subset
     five rebuilds of a cube that can take seconds.

     Native <select multiple> rather than a custom list: ctrl/shift range
     selection, type-to-jump and keyboard navigation come from the host and
     work in Excel's webview, which a hand-rolled list would have to
     reimplement and get wrong. */
  var pickerKey = null;
  var working = [];

  async function openPicker(key) {
    pickerKey = key;
    working = (filterVals[key] || []).slice();
    el.pickerTitle.textContent = dimLabel(key);
    el.pickerSearch.value = '';
    el.pickerMsg.textContent = '';
    el.pickerModal.hidden = false;
    el.pickerAvail.innerHTML = '';
    el.pickerSel.innerHTML = '';
    el.pickerAvailCount.textContent = '(loading…)';

    var qy = readQuery();
    var ck = key + '::' + JSON.stringify(toParams(qy));
    try {
      if (!memberCache[ck]) {
        memberCache[ck] = await apiGet('/xero/data/journals/pivot/members/',
          Object.assign({}, toParams(qy), { dim: key }));
      }
      renderPicker();
      await loadSubsetList();
    } catch (e) {
      el.pickerAvailCount.textContent = '';
      el.pickerMsg.textContent = e.message;
      el.pickerMsg.className = 'msg msg--err';
    }
  }

  function pickerData() {
    return memberCache[pickerKey + '::' + JSON.stringify(toParams(readQuery()))];
  }

  function fillList(sel, items) {
    sel.innerHTML = '';
    var frag = document.createDocumentFragment();
    items.forEach(function (m) {
      var o = document.createElement('option');
      o.value = m.value;
      o.textContent = m.lines != null
        ? m.value + '  (' + fmtNum(m.lines) + ')'
        : m.value;
      frag.appendChild(o);
    });
    sel.appendChild(frag);
  }

  function renderPicker() {
    var data = pickerData();
    if (!data) return;
    var term = (el.pickerSearch.value || '').toLowerCase();
    var chosen = {};
    working.forEach(function (v) { chosen[v] = true; });

    var avail = data.members.filter(function (m) {
      return !chosen[m.value] && (!term || m.value.toLowerCase().indexOf(term) !== -1);
    });
    // Right-hand pane keeps the order values were added in, and shows line
    // counts where we know them -- a value can be in the subset without being
    // in the current member list if the filters moved under it.
    var byVal = {};
    data.members.forEach(function (m) { byVal[m.value] = m; });
    var picked = working.map(function (v) { return byVal[v] || { value: v, lines: null }; });

    fillList(el.pickerAvail, avail);
    fillList(el.pickerSel, picked);
    el.pickerAvailCount.textContent = '(' + fmtNum(avail.length)
      + (term ? ' shown' : '') + (data.truncated ? ', list capped' : '') + ')';
    el.pickerSelCount.textContent = working.length
      ? '(' + fmtNum(working.length) + ')'
      : '(none — no filter)';
  }

  function chosenIn(sel) {
    return Array.prototype.filter.call(sel.options, function (o) { return o.selected; })
      .map(function (o) { return o.value; });
  }

  function addValues(vals) {
    vals.forEach(function (v) { if (working.indexOf(v) === -1) working.push(v); });
    renderPicker();
  }

  function removeValues(vals) {
    working = working.filter(function (v) { return vals.indexOf(v) === -1; });
    renderPicker();
  }

  function closePicker() {
    el.pickerModal.hidden = true;
    pickerKey = null;
  }

  /* Reordering. The subset is a sequence, and that sequence IS the layout
     order -- the server lays rows and columns out in exactly this order -- so
     moving an item here moves the row or column on the next build. */
  function movePicked(delta) {
    var chosen = chosenIn(el.pickerSel);
    if (!chosen.length) {
      el.pickerMsg.textContent = 'Pick an item on the right first, then move it.';
      el.pickerMsg.className = 'msg';
      return;
    }
    var idx = chosen.map(function (v) { return working.indexOf(v); })
      .filter(function (i) { return i >= 0; })
      .sort(function (a, b) { return delta < 0 ? a - b : b - a; });
    // Walking from the leading edge means a block of selected items slides
    // together instead of collapsing into each other.
    for (var n = 0; n < idx.length; n++) {
      var i = idx[n], j = i + delta;
      if (j < 0 || j >= working.length) continue;
      if (chosen.indexOf(working[j]) !== -1) continue;   // swapping with itself
      var t = working[i]; working[i] = working[j]; working[j] = t;
    }
    renderPicker();
    // Keep the moved items selected so the button can be pressed repeatedly.
    Array.prototype.forEach.call(el.pickerSel.options, function (o) {
      o.selected = chosen.indexOf(o.value) !== -1;
    });
  }

  /* Saved subsets and saved views live on the server, not in this workbook.
     A subset is shared analytical vocabulary: "the trading entities" must mean
     the same three entities to MC, to the bookkeeper and to an agent, rather
     than three private definitions that drift apart. */
  async function loadSubsetList() {
    if (!pickerKey) return;
    fillSelect(el.subsetPick, [], '— none saved —');
    try {
      var d = await apiGet('/xero/data/journals/pivot/subsets/', { dimension: pickerKey });
      var names = (d.results || []).map(function (r) {
        return { value: r.name, label: r.name + ' (' + r.members.length + ')' };
      });
      savedSubsets = {};
      (d.results || []).forEach(function (r) { savedSubsets[r.name] = r.members; });
      fillSelect(el.subsetPick, names, names.length ? '— pick one —' : '— none saved —');
    } catch (e) { /* the editor still works without saved subsets */ }
  }

  function fillSelect(sel, items, placeholder) {
    sel.innerHTML = '';
    var o0 = document.createElement('option');
    o0.value = ''; o0.textContent = placeholder;
    sel.appendChild(o0);
    items.forEach(function (it) {
      var o = document.createElement('option');
      o.value = it.value; o.textContent = it.label;
      sel.appendChild(o);
    });
  }

  var savedSubsets = {};
  var savedViews = {};

  async function loadViewList() {
    try {
      var d = await apiGet('/xero/data/journals/pivot/views/', {});
      savedViews = {};
      (d.results || []).forEach(function (r) { savedViews[r.name] = r; });
      fillSelect(el.viewPick, (d.results || []).map(function (r) {
        return { value: r.name, label: r.name };
      }), (d.results || []).length ? '— pick a view —' : '— none saved —');
    } catch (e) { /* saved views are optional; the cube still builds */ }
  }

  function savedViewNames() {
    return Array.prototype.map.call(el.viewPick.options, function (o) { return o.value; })
      .filter(function (v) { return v; });
  }

  async function saveCurrentView() {
    /* Overwriting is the common case, not the exception: you open a view,
       adjust a subset, and want the same view to keep the change. Requiring
       the exact name to be retyped to do that is what produced "Default2"
       sitting next to "Default".

       Leave the name box empty and Save replaces the view currently selected.
       Type a name that already exists and it replaces that one -- the server
       has always upserted by name, so a "new" save under an existing name was
       a silent replace anyway. The message says which of the two happened
       rather than leaving it to be discovered later. */
    var typed = (el.viewName.value || '').trim();
    var name = typed || (el.viewPick.value || '').trim();
    if (!name) throw new Error('Name the view, or pick one to overwrite.');
    var replacing = savedViewNames().indexOf(name) >= 0;
    var spec = readCubeSpec();
    var bad = validateCube(spec);
    if (bad) throw new Error(bad);
    // The journal filters travel with it. A view that renders different numbers
    // depending on what the pane happened to be filtered to is not a saved view.
    await apiPost('/xero/data/journals/pivot/views/', {
      name: name, spec: spec, query: readQuery(),
      author: (el.commentAuthor.value || '').trim()
    });
    el.viewName.value = '';
    await loadViewList();
    el.viewPick.value = name;
    el.cubeMsg.textContent = (replacing ? 'Replaced view "' : 'Saved view "')
      + name + '" — layout, subsets and filters.';
    el.cubeMsg.className = 'msg msg--ok';
  }

  async function openSavedView() {
    var name = el.viewPick.value;
    if (!name || !savedViews[name]) throw new Error('Pick a saved view first.');
    var v = savedViews[name];
    applyCubeSpec(v.spec || {});
    if (v.query) applyQuery(v.query);
    rememberCubeSpec();
    el.cubeMsg.textContent = 'Opened "' + name + '". Build to write it to a sheet.';
    el.cubeMsg.className = 'msg';
    if (wells.rows.length) await buildCube();
  }


  /* Rebuild the selected view from its SAVED definition.

     Open applies whatever definition was cached when the list was last
     fetched, and only builds when rows happen to be populated. That is wrong
     twice over once a view can be edited: the definition may have changed on
     the server since — by the overwrite path, or by an agent — and the wells
     may have drifted locally since you opened it.

     Rebuild re-fetches the list first, so what lands on the sheet is what is
     actually saved rather than what this pane remembers, and then builds
     unconditionally instead of silently doing nothing. It DISCARDS local
     well edits by design: that is the point of asking for the saved view. */
  async function rebuildSavedView() {
    var name = el.viewPick.value;
    if (!name) throw new Error('Pick a saved view to rebuild.');
    await loadViewList();
    el.viewPick.value = name;
    var v = savedViews[name];
    if (!v) throw new Error('"' + name + '" is no longer saved — the list has been refreshed.');
    applyCubeSpec(v.spec || {});
    if (v.query) applyQuery(v.query);
    rememberCubeSpec();
    if (!wells.rows.length) {
      throw new Error('"' + name + '" has no row fields saved, so there is nothing to build.');
    }
    var onto = cubeTargetId() ? activeSheet.name : null;
    await buildCube();
    el.cubeMsg.textContent = 'Rebuilt "' + name + '" from its saved definition'
      + (onto ? ' onto ' + onto + '.' : ' on a new sheet.');
    el.cubeMsg.className = 'msg msg--ok';
  }

  var pickerWired = false;

  function wirePicker() {
    if (pickerWired) return;
    pickerWired = true;

    on('pickerSearch', 'input', renderPicker);
    on('btnPickAdd', 'click', function () { addValues(chosenIn(el.pickerAvail)); });
    on('btnPickRemove', 'click', function () { removeValues(chosenIn(el.pickerSel)); });

    on('btnPickAddAll', 'click', function () {
      // "All" means all VISIBLE: with a search term active that is the useful
      // meaning, and the only one that matches what is on screen.
      addValues(Array.prototype.map.call(el.pickerAvail.options, function (o) { return o.value; }));
    });
    on('btnPickRemoveAll', 'click', function () { working = []; renderPicker(); });

    on('pickerAvail', 'dblclick', function () { addValues(chosenIn(el.pickerAvail)); });
    on('pickerSel', 'dblclick', function () { removeValues(chosenIn(el.pickerSel)); });

    on('pickerApply', 'click', function () {
      if (!pickerKey) return closePicker();
      var key = pickerKey;
      filterVals[key] = working.slice();
      closePicker();
      reflowWells();
      rememberCubeSpec();
      if (el.autoBuild.checked && wells.rows.length) run(buildCube);
    });

    on('btnPickUp', 'click', function () { movePicked(-1); });
    on('btnPickDown', 'click', function () { movePicked(1); });
    on('btnPickSortAz', 'click', function () {
      working.sort(function (a, b) { return a.localeCompare(b); });
      renderPicker();
    });

    on('btnSubsetLoad', 'click', function () {
      var n = el.subsetPick.value;
      if (!n || !savedSubsets[n]) return;
      working = savedSubsets[n].slice();
      el.subsetName.value = n;
      renderPicker();
    });

    on('btnSubsetSave', 'click', function () {
      run(async function () {
        var n = (el.subsetName.value || '').trim();
        if (!n) throw new Error('Name the subset first.');
        if (!working.length) throw new Error('An empty subset is the same as no subset.');
        await apiPost('/xero/data/journals/pivot/subsets/', {
          dimension: pickerKey, name: n, members: working.slice(),
          author: (el.commentAuthor.value || '').trim()
        });
        await loadSubsetList();
        el.subsetPick.value = n;
        el.pickerMsg.textContent = 'Saved subset "' + n + '" (' + working.length + ' members, in this order).';
        el.pickerMsg.className = 'msg msg--ok';
      });
    });

    on('btnSubsetDelete', 'click', function () {
      run(async function () {
        var n = el.subsetPick.value;
        if (!n) throw new Error('Pick a saved subset to delete.');
        await apiDelete('/xero/data/journals/pivot/subsets/',
          { dimension: pickerKey, name: n });
        await loadSubsetList();
        el.pickerMsg.textContent = 'Deleted saved subset "' + n + '". The subset in front of you is untouched.';
        el.pickerMsg.className = 'msg';
      });
    });

    on('pickerCancel', 'click', closePicker);
    on('pickerClose', 'click', closePicker);

    // Clicking the backdrop cancels; clicking the card must not.
    on('pickerModal', 'click', function (e) {
      if (e.target === el.pickerModal) closePicker();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !el.pickerModal.hidden) closePicker();
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
      totals: JSON.parse(JSON.stringify(totalVals)),
      headTheme: el.headTheme ? el.headTheme.value : 'purple',
      decimals: el.showDecimals ? el.showDecimals.checked : false,
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
    totalVals = spec.totals ? JSON.parse(JSON.stringify(spec.totals)) : {};
    if (el.headTheme && spec.headTheme) el.headTheme.value = spec.headTheme;
    if (el.showDecimals && typeof spec.decimals === 'boolean') {
      el.showDecimals.checked = spec.decimals;
    }
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
    }, dimfParam(spec), totalsParams(spec));
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
    /* A caveat that lives only in the task pane is lost the moment the sheet is
       shared, printed or screenshotted — and this one changes the numbers by a
       factor of four. It goes in the sheet. */
    head.push([cube.mirror_hint ? '\u26a0 ' + cube.mirror_hint : ''].concat(blanks(width - 1)));

    /* Stacked column headers, the way a PivotTable reads.

       Two column dimensions used to flatten into one row of
       "FY2019 | 2018-07", repeating the year in every single cell. Now each
       column dimension gets its own header row and a parent is written once,
       above the span of children it covers -- Financial year across the top,
       period beneath it.

       Row-dimension labels sit on the LAST header row, level with the leaf
       column labels, because that is the row the data actually lines up with.

       cube.cols (the joined form) is untouched and still what comment anchors
       are keyed on, so nothing about existing comments moves. */
    var theme = HEAD_THEMES[(spec && spec.headTheme) || 'purple'] || HEAD_THEMES.purple;
    SHEET.headBg = theme.bg;
    SHEET.headBg2 = theme.bg2;
    // The rules under the header and above the grand total take the header
    // colour, so a sheet reads as one palette instead of navy-plus-gold.
    SHEET.accent = theme.bg;
    var moneyFmt = (spec && spec.decimals) ? MONEY_FMT_2 : MONEY_FMT_0;

    var colPaths = cube.col_paths
      || cube.cols.map(function (c) { return [c]; });
    var nLevels = Math.max(1, (cube.col_dims || []).length);
    var merges = [];

    for (var lv = 0; lv < nLevels; lv++) {
      var isLast = lv === nLevels - 1;
      var row = isLast
        ? cube.row_dims.map(function (d) { return d.label; })
        : blanks(nRowDims);

      for (var ci = 0; ci < nCols; ci++) {
        var label = (colPaths[ci] || [])[lv];
        label = (label === undefined || label === null) ? '' : String(label);
        // Write a parent only where its run STARTS; the cells it spans stay
        // blank and are merged under it.
        var prev = ci > 0 ? (colPaths[ci - 1] || []) : null;
        var sameRun = prev !== null && samePrefix(colPaths[ci] || [], prev, lv);
        row.push(sameRun ? '' : label);
        if (!sameRun && !isLast) {
          var span = 1;
          while (ci + span < nCols && samePrefix(colPaths[ci + span] || [], colPaths[ci] || [], lv)) span++;
          if (span > 1) merges.push({ row: 3 + lv, col: nRowDims + ci, span: span });
        }
      }
      row.push(isLast ? 'Total' : '');
      head.push(row);
    }

    var firstDataRow = 3 + nLevels;

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
    var headerRowIdx = firstDataRow - 1;   // the leaf header row

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
        /* clear() leaves outline groups and frozen panes behind, so a rebuild
           with fewer row levels kept stale +/- buttons on rows that no longer
           head a group. Peel every level off, then clear. */
        try { sheet.freezePanes.unfreeze(); await ctx.sync(); } catch (e) { /* none set */ }
        for (var lvl = 0; lvl < 8; lvl++) {
          try {
            sheet.getRange().ungroup(Excel.GroupOption.byRows);
            await ctx.sync();
          } catch (e) { break; }
        }
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

      var title = sheet.getRangeByIndexes(0, 0, 1, 1);
      title.format.font.bold = true;
      title.format.font.size = 14;
      title.format.font.color = SHEET.headBg;
      sheet.getRangeByIndexes(1, 0, 1, 1).format.font.color = SHEET.subtle;
      sheet.getRangeByIndexes(1, 0, 1, 1).format.font.italic = true;
      if (cube.mirror_hint) {
        var warn = sheet.getRangeByIndexes(2, 0, 1, Math.max(1, Math.min(width, 8)));
        warn.format.font.color = '#9C2B21';
        warn.format.font.bold = true;
      }

      /* The header is a solid band, not bold text on white. It has to stay
         readable when the grid scrolls under it and when the sheet is printed
         or pasted into a document, which bold-on-white does not. */
      var allHead = sheet.getRangeByIndexes(3, 0, nLevels, width);
      allHead.format.font.bold = true;
      allHead.format.font.color = SHEET.headInk;
      allHead.format.fill.color = SHEET.headBg;
      allHead.format.horizontalAlignment = 'Right';
      allHead.format.verticalAlignment = 'Center';

      // Parent levels sit one shade lighter, so a stacked header reads as a
      // hierarchy rather than one thick slab.
      if (nLevels > 1) {
        sheet.getRangeByIndexes(3, 0, nLevels - 1, width).format.fill.color = SHEET.headBg2;
      }

      var hdr = sheet.getRangeByIndexes(headerRowIdx, 0, 1, width);
      hdr.format.borders.getItem('EdgeBottom').style = 'Continuous';
      hdr.format.borders.getItem('EdgeBottom').color = SHEET.accent;
      hdr.format.borders.getItem('EdgeBottom').weight = 'Medium';
      sheet.getRangeByIndexes(headerRowIdx, 0, 1, nRowDims).format.horizontalAlignment = 'Left';
      // Long column labels wrap instead of being clipped by the next column.
      allHead.format.wrapText = true;

      /* Merge each parent across the children it spans, and centre it over
         them. Non-fatal: on a host without merge the labels still sit at the
         start of their run, which is how a PivotTable in compact form looks
         anyway -- the sheet is correct either way. */
      merges.forEach(function (m) {
        try {
          var r = sheet.getRangeByIndexes(m.row, m.col, 1, m.span);
          r.merge(true);
          r.format.horizontalAlignment = 'Center';
        } catch (e) { /* merge unsupported on this host */ }
      });

      if (body.length) {
        var nums = sheet.getRangeByIndexes(firstDataRow, nRowDims, body.length, nCols + 1);
        nums.numberFormat = [[isCount ? '#,##0' : moneyFmt]];
      }

      /* Consolidations are shaded by DEPTH, so the level a subtotal belongs to
         is visible without counting indents -- the top-level rollups are the
         darkest and the eye lands on them first.

         Applied in RUNS of consecutive rows that share a depth, not row by
         row. A cube can be thousands of rows and one range call per row is the
         difference between a sheet that renders and one that appears to hang. */
      var TOTAL_FILL = [SHEET.total0, SHEET.total1, SHEET.total2];
      var run = null;
      var runs = [];
      cube.rows.forEach(function (r, i) {
        /* Key on the REAL depth. Clamping it here (it was Math.min(depth, 2))
           made a depth-3 row look identical to a depth-2 one, so the first
           supplier under an account joined the ACCOUNT's run and was indented
           with the account's depth -- into column C, leaving its own column D
           flush left. The second supplier onwards began a fresh run and
           indented correctly, which is why only the first child of each parent
           looked wrong. Shading still clamps, below, so the colours are
           unchanged; only the run boundaries move. */
        var kind = (r.is_total ? 'T' : 'L') + r.depth;
        if (run && run.kind === kind && run.to === i - 1) {
          run.to = i;
        } else {
          run = { kind: kind, from: i, to: i, isTotal: r.is_total, depth: r.depth };
          runs.push(run);
        }
      });

      runs.forEach(function (g) {
        var n = g.to - g.from + 1;
        var range = sheet.getRangeByIndexes(firstDataRow + g.from, 0, n, width);
        if (g.isTotal) {
          range.format.font.bold = true;
          range.format.fill.color = TOTAL_FILL[Math.min(g.depth, TOTAL_FILL.length - 1)];
          if (g.depth === 0) {
            range.format.borders.getItem('EdgeTop').style = 'Continuous';
            range.format.borders.getItem('EdgeTop').color = SHEET.rule;
          }
        }
        if (g.depth > 0) {
          sheet.getRangeByIndexes(firstDataRow + g.from, Math.min(g.depth, nRowDims - 1), n, 1)
            .format.indentLevel = Math.min(g.depth, 5);
        }
      });

      var gt = sheet.getRangeByIndexes(firstDataRow + body.length + 1, 0, 1, width);
      gt.format.font.bold = true;
      gt.format.fill.color = SHEET.grand;
      gt.numberFormat = [[isCount ? '#,##0' : moneyFmt]];
      gt.getCell(0, 0).numberFormat = [['General']];
      gt.format.borders.getItem('EdgeTop').style = 'Double';
      gt.format.borders.getItem('EdgeTop').color = SHEET.accent;

      // Row-dimension columns get the room; value columns are uniform so the
      // eye can compare down a column without re-reading its width.
      /* Row-label columns are not all the same job. The outer ones hold short
         codes (REVENUE, OVERHEADS); the innermost holds the long account name.
         Giving all of them 200 wasted a screen of width on the outer ones and
         still clipped the inner one. */
      if (nRowDims > 1) {
        sheet.getRangeByIndexes(headerRowIdx, 0, 1, nRowDims - 1).format.columnWidth = 130;
      }
      sheet.getRangeByIndexes(headerRowIdx, nRowDims - 1, 1, 1).format.columnWidth = 330;
      // One call for every value column rather than one call per column: a wide
      // cube can be fifty columns, and each of those was a round trip.
      sheet.getRangeByIndexes(headerRowIdx, nRowDims, 1, width - nRowDims)
        .format.columnWidth = 104;
      // Autofit rather than a fixed height: wrapText with a fixed height
      // clips a long column label instead of showing it.
      try { sheet.getRangeByIndexes(3, 0, nLevels, width).format.autofitRows(); }
      catch (e) { /* host without autofit: wrapped text still shows on 1 line */ }

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

  /* Do two column paths agree on every level ABOVE the given one?
     That is what makes them part of the same parent's span. */
  function samePrefix(a, b, level) {
    for (var i = 0; i <= level; i++) {
      if ((a[i] === undefined ? '' : a[i]) !== (b[i] === undefined ? '' : b[i])) return false;
    }
    return true;
  }

  function blanks(n) {
    var a = [];
    for (var i = 0; i < n; i++) a.push('');
    return a;
  }

  async function buildCube(opts) {
    /* The sheet in front is the target when it is a cube of ours. Every build
       used to open a new sheet, so "Rebuild on every change" produced
       Cube 2 ... Cube 5 in eleven seconds of dragging (bindings 08:03:33,
       :42, :44 in one workbook) and the layout could never be edited. */
    var target = (opts && opts.newSheet) ? null : cubeTargetId();
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

    var out = await renderCube(target, cube, qy, spec);
    await inspectActiveSheet();

    if (cube.balancing_hint) {
      el.cubeMsg.textContent = cube.balancing_hint;
      el.cubeMsg.className = 'msg msg--err';
      return;
    }
    // Louder than the row count, because it is the difference between a real
    // figure and one several times too big.
    if (cube.mirror_hint) {
      el.cubeMsg.textContent = cube.mirror_hint;
      el.cubeMsg.className = 'msg msg--err';
      return;
    }
    var note = fmtNum(cube.leaf_count) + ' leaf rows × ' + fmtNum(cube.cols.length)
      + (target ? ' columns rebuilt in place on ' : ' columns written to ') + out.sheetName + '.';
    if (cube.zero_rows && cube.spec !== null) {
      note += ' ' + fmtNum(cube.zero_rows) + ' zero rows suppressed.';
    }
    if (cube.truncated_rows) note += ' Row cap hit — narrow the filters.';
    if (cube.truncated_cols) note += ' Column cap hit — use a coarser column dimension.';
    el.cubeMsg.textContent = note;
    el.cubeMsg.className = cube.truncated_rows || cube.truncated_cols ? 'msg msg--err' : 'msg msg--ok';
  }

  function buildCubeToNewSheet() {
    return buildCube({ newSheet: true });
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
  /* Where the data starts, given how deep the column header stacks.

     Title, filter line, blank, then ONE HEADER ROW PER COLUMN DIMENSION. It
     was a constant 4 while headers were a single flattened row; with stacked
     headers it moves, and anything that reads a cell's meaning from its
     position has to move with it -- otherwise comments on a two-deep cube
     would resolve one row out. */
  function cubeFirstDataRow(cube) {
    return 3 + Math.max(1, ((cube && cube.col_dims) || []).length);
  }
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
      firstDataRow: cubeFirstDataRow(cube),
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

  /* Context-menu entry point: resolve the cell under the cursor right now
     rather than trusting the last debounced onSelectionChanged read, which
     may not have fired for the cell that was right-clicked (a right-click on
     an unselected cell moves the selection and the pane may still be
     loading). Then drill it exactly as the button does. */
  async function drillActiveCell() {
    await inspectActiveSheet();
    var b = activeSheet.binding;
    if (!b || (b.kind !== 'cube' && b.kind !== 'pivot')) {
      throw new Error('Show transactions works on a cube view or PivotTable sheet built by '
        + 'this add-in. The sheet in front (' + (activeSheet.name || 'unnamed') + ') is not one '
        + '\u2014 build a cube view first, then right-click one of its figures.');
    }
    var sel = b.kind === 'cube' ? await resolveCubeSelection(b) : await resolvePivotSelection(b);
    if (!sel || !sel.row_path) {
      await showSelection(sel);
      throw new Error('Right-click a single value cell inside the '
        + (b.kind === 'cube' ? 'cube' : 'PivotTable') + ' \u2014 a figure, not a heading or a blank.');
    }
    await showSelection(sel);
    await drillSelection();
  }

  async function drillSelection() {
    var sel = selection;
    if (!sel || !sel.row_path) throw new Error('Select a value cell first.');
    var coords = selCoords(sel);
    progress(0, 1, 'Finding the transactions behind this figure…');
    var data = await apiGet('/xero/data/journals/pivot/drill/',
      Object.assign({}, toParams(sel.query), { coords: JSON.stringify(coords), limit: 5000 }));

    if (!data.count) {
      /* Do not blame the data. This said "the ledger may have changed", which
         was a guess presented as a diagnosis -- and the first time it fired the
         real cause was a bug in this add-in, not a change in the ledger. State
         what is true and let the number speak. */
      el.commentMsg.textContent = 'No journal lines came back for that cell, even though it '
        + 'shows ' + (typeof sel.value === 'number' ? fmtNum(sel.value) : 'a value')
        + '. That should not happen — the cell was built from those lines. '
        + 'Worth reporting rather than working around.';
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

  /* Flag every cell in the selection at once.

     Only cube sheets: a cube resolves its cells from the grid already held in
     memory, so a hundred of them cost nothing. A PivotTable would need the
     rendered grid re-read per area, which is doable but is not what this is
     for -- the point is speed while reviewing.

     One request, not one per cell. Sixty round trips would be slow and would
     fail halfway through often enough to matter. */
  var BULK_MAX = 500;

  async function flagSelection() {
    var b = activeSheet.binding;
    if (!b || b.kind !== 'cube') {
      throw new Error('Bulk flagging works on a cube sheet. Open or rebuild one first.');
    }
    var cached = await ensureCube(activeSheet.id, b);
    if (!cached) throw new Error('Rebuild this cube sheet first, then try again.');

    var tags = (el.bulkTags.value || '').split(',')
      .map(function (t) { return t.trim(); }).filter(Boolean);
    var note = (el.bulkNote.value || '').trim();
    if (!note && !tags.length) {
      throw new Error('Give a tag or a note — a flag with neither says nothing.');
    }
    if (!note) note = 'Flagged: ' + tags.join(', ');

    var picked = await selectedCells();
    if (!picked.length) throw new Error('Select the cells to flag first.');

    var cells = [], outside = 0;
    for (var i = 0; i < picked.length; i++) {
      if (cells.length >= BULK_MAX) break;
      var x = cellToIntersection(cached.cube,
        picked[i].r - cached.firstDataRow, picked[i].c - cached.nRowDims);
      // Labels, headers and blank cells have no figure behind them. Counted
      // and reported rather than silently ignored.
      if (!x) { outside++; continue; }
      cells.push({
        measure: b.spec.measure,
        row_dims: x.row_dims, row_path: x.row_path,
        col_dims: x.col_dims, col_path: x.col_path,
        cell_value: x.cell_value,
        filters: toParams(b.query),
        // Carried on the cell itself. Cells that hold no figure are skipped,
        // so an index into `cells` is NOT an index into `picked` — pairing
        // them positionally would write each note onto the wrong cell.
        _r: picked[i].r, _c: picked[i].c
      });
    }
    if (!cells.length) {
      throw new Error('None of the selected cells hold a figure — select value cells.');
    }

    progress(0, cells.length, 'Flagging ' + fmtNum(cells.length) + ' cells…');
    var res = await apiPost(COMMENT_API + 'bulk/', {
      cells: cells.map(function (c) {
        var o = {}; for (var k in c) if (k.charAt(0) !== '_') o[k] = c[k];
        return o;
      }),
      comment: note, tags: tags,
      author: (el.commentAuthor.value || '').trim()
    });
    commentCache = null;

    if (el.markCells.checked) {
      await writeCellComments(activeSheet.id, cells.map(function (c) {
        return { r: c._r, c: c._c, t: note };
      }));
    }

    var msg = 'Flagged ' + fmtNum(res.saved) + ' cell' + (res.saved === 1 ? '' : 's');
    if (res.tags && res.tags.length) msg += ' as ' + res.tags.join(', ');
    msg += '.';
    if (outside) msg += ' ' + fmtNum(outside) + ' selected cell'
      + (outside === 1 ? '' : 's') + ' held no figure and were left alone.';
    if (picked.length > BULK_MAX) {
      msg += ' Only the first ' + fmtNum(BULK_MAX) + ' were taken — narrow the selection.';
    }
    el.commentMsg.textContent = msg;
    el.commentMsg.className = 'msg msg--ok';
  }

  /* Every cell of the selection, across all areas.

     Excel supports a discontiguous selection (ctrl-click), which is exactly
     how someone picks out the dozen figures that look wrong -- so a single
     getSelectedRange would miss most of what they chose. */
  async function selectedCells() {
    var out = [];
    await Excel.run(async function (ctx) {
      var areas = null;
      try {
        if (Office.context.requirements.isSetSupported('ExcelApi', '1.9')) {
          areas = ctx.workbook.getSelectedRanges();
          areas.load('areas/items/rowIndex,areas/items/columnIndex,areas/items/rowCount,areas/items/columnCount');
        }
      } catch (e) { areas = null; }

      var single = ctx.workbook.getSelectedRange();
      single.load('rowIndex,columnIndex,rowCount,columnCount');
      await ctx.sync();

      var list = (areas && areas.areas && areas.areas.items && areas.areas.items.length)
        ? areas.areas.items : [single];
      list.forEach(function (a) {
        for (var r = 0; r < a.rowCount; r++) {
          for (var c = 0; c < a.columnCount; c++) {
            out.push({ r: a.rowIndex + r, c: a.columnIndex + c });
          }
        }
      });
    });
    return out;
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

  /* Everything between busy = true and the finally sits inside the try:
     a throw before the await (a missing button in setButtons, say) used to
     leave busy stuck at true, after which every click returned at the first
     line -- the whole pane dead with no message. */
  async function run(fn) {
    if (busy) return;
    busy = true;
    try {
      cancelFlag.cancelled = false;
      if (el.errorMsg) el.errorMsg.hidden = true;
      if (el.progressPanel) el.progressPanel.hidden = false;
      setButtons(false);
      await fn();
    } catch (e) {
      if (el.errorMsg) {
        el.errorMsg.textContent = e && e.message ? e.message : String(e);
        el.errorMsg.hidden = false;
      }
    } finally {
      busy = false;
      if (el.progressPanel) el.progressPanel.hidden = true;
      if (el.progressFill) el.progressFill.style.width = '0';
      setButtons(true);
    }
  }

  function setDisabled(id, off) {
    if (el[id]) el[id].disabled = !!off;
  }

  function setButtons(on) {
    var b = activeSheet.binding;
    var commentable = b && (b.kind === 'cube' || b.kind === 'pivot');
    setDisabled('btnLoad', !on);
    setDisabled('btnCount', !on);
    setDisabled('btnCube', !on);
    setDisabled('btnCubeNew', !on);
    setDisabled('btnRefresh', !on || !b);
    setDisabled('btnRestore', !on || !b);
    setDisabled('btnPivot', !on || !b || b.kind !== 'detail');
    setDisabled('btnReload', !on || !b);
    setDisabled('btnSyncComments', !on || !commentable);
    setDisabled('btnFullPivot', !on);
    setDisabled('btnPushComments', !on || !commentable);
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
