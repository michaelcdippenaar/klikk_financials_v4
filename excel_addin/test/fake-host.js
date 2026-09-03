/* fake-host.js — boot the task pane headlessly.
 *
 * The add-in is a web page. Everything that made it fail on 2026-09-03 was
 * reachable from the page: a listener that never attached, a Build that added
 * a sheet instead of rewriting one, a handler calling a deleted function.
 * None of it needed Excel — only something Excel-shaped to talk to.
 *
 * So: jsdom loads taskpane.html, this file supplies Office, Excel and fetch,
 * and the tests click the pane's real buttons. The Excel stub is deliberately
 * shallow. Sheets, tables and settings are modelled, because the tests assert
 * on them; ranges and formats are a Proxy that swallows every call, because
 * "did .format.font.bold get set" is not a bug this harness is for. If a test
 * ever needs a range property, model that property — do not deepen the Proxy.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ADDIN_DIR = process.env.KLIKK_ADDIN_DIR
  ? path.resolve(process.env.KLIKK_ADDIN_DIR)
  : path.resolve(__dirname, '..');

/* Anything a range or a format can be asked to do, it does, and returns
   itself. Property writes are recorded so a test can look, not so it must. */
function chainable(rec, label) {
  const target = function () {};
  return new Proxy(target, {
    get(_t, prop) {
      // Never look thenable: an accidental `await range` would hang.
      if (prop === 'then' || prop === 'catch' || prop === 'finally') return undefined;
      if (typeof prop === 'symbol') return undefined;
      if (prop === 'items') return [];
      return chainable(rec, label + '.' + String(prop));
    },
    set(_t, prop, value) {
      rec.rangeWrites.push({ path: label + '.' + String(prop), value: value });
      return true;
    },
    apply() { return chainable(rec, label + '()'); }
  });
}

function makeExcel(rec) {
  const wb = {
    sheets: [{ id: 'sheet-1', name: 'Sheet1' }],
    activeId: 'sheet-1',
    seq: 1
  };

  function sheetApi(s) {
    return {
      get id() { return s.id; },
      get name() { return s.name; },
      load() {},
      activate() { wb.activeId = s.id; },
      delete() { wb.sheets = wb.sheets.filter(function (x) { return x !== s; }); },
      tables: { load() {}, items: [] },
      pivotTables: { load() {}, items: [] },
      charts: { load() {}, items: [] },
      comments: { load() {}, items: [], add() {} },
      freezePanes: chainable(rec, 'freezePanes'),
      getRange() { return chainable(rec, 'range'); },
      getUsedRange() { return chainable(rec, 'usedRange'); },
      getRangeByIndexes(row, col, rows, cols) {
        rec.ranges.push({ sheet: s.id, row: row, col: col, rows: rows, cols: cols });
        return chainable(rec, 'range[' + row + ',' + col + ']');
      }
    };
  }

  const worksheets = {
    load() {},
    get items() { return wb.sheets.map(sheetApi); },
    onActivated: { add() { return {}; } },
    add(name) {
      rec.sheetsAdded.push(name);
      wb.seq += 1;
      const s = { id: 'sheet-' + wb.seq, name: name || ('Sheet' + wb.seq) };
      wb.sheets.push(s);
      return sheetApi(s);
    },
    getItem(id) {
      const s = wb.sheets.find(function (x) { return x.id === id || x.name === id; });
      if (!s) throw new Error('ItemNotFound: ' + id);
      return sheetApi(s);
    },
    getActiveWorksheet() {
      const s = wb.sheets.find(function (x) { return x.id === wb.activeId; }) || wb.sheets[0];
      return sheetApi(s);
    }
  };

  const Excel = {
    ClearApplyTo: { all: 'All', contents: 'Contents', formats: 'Formats' },
    GroupOption: { byRows: 'ByRows', byColumns: 'ByColumns' },
    async run(fn) {
      const ctx = {
        workbook: {
          worksheets: worksheets,
          onSelectionChanged: { add() { return {}; } },
          getSelectedRange() { return chainable(rec, 'selection'); },
          getSelectedRanges() { return chainable(rec, 'selections'); }
        },
        sync() { return Promise.resolve(); },
        trackedObjects: { add() {}, remove() {} }
      };
      return fn(ctx);
    }
  };
  return { Excel: Excel, workbook: wb };
}

function makeOffice(rec) {
  const store = {};
  return {
    onReady(cb) { rec.onReadyCb = cb; },
    HostType: { Excel: 'Excel' },
    context: {
      requirements: { isSetSupported() { return true; } },
      document: {
        settings: {
          get(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
          set(k, v) { store[k] = v; },
          remove(k) { delete store[k]; },
          saveAsync(cb) { if (cb) cb({ status: 'succeeded' }); }
        }
      }
    },
    _settings: store
  };
}

/* One fetch stub for the whole pane. `routes` maps a path fragment to a body
   (or a function of the URL). Anything unrouted answers 404, which is what a
   real deployment does for an endpoint that does not exist, and is enough for
   the non-fatal paths (comments) to take their catch. */
function makeFetch(rec, routes) {
  return function (url, opts) {
    rec.requests.push({ url: String(url), method: (opts && opts.method) || 'GET' });
    const hit = Object.keys(routes).find(function (frag) { return String(url).indexOf(frag) >= 0; });
    const body = hit ? (typeof routes[hit] === 'function' ? routes[hit](String(url)) : routes[hit]) : null;
    return Promise.resolve({
      ok: !!hit,
      status: hit ? 200 : 404,
      statusText: hit ? 'OK' : 'Not Found',
      json: function () { return Promise.resolve(body === null ? {} : body); },
      text: function () { return Promise.resolve(JSON.stringify(body === null ? {} : body)); }
    });
  };
}

/* Load the pane.
 *
 * options.removeControls — ids to delete from the HTML before boot, which is
 *   how an HTML/JS pair from two different releases actually presents.
 * options.mutateApp / mutateLib — (src) => src, used by the regression prover
 *   to reconstruct a historic bug in the file it shipped in.
 * options.routes — fetch routes (see makeFetch).
 */
async function loadPane(options) {
  const opts = options || {};
  const rec = {
    sheetsAdded: [],
    ranges: [],
    rangeWrites: [],
    requests: [],
    listeners: [],      // {id, event} for every addEventListener on an element
    pageErrors: [],
    onReadyCb: null
  };

  let html = fs.readFileSync(path.join(ADDIN_DIR, 'taskpane.html'), 'utf8');
  (opts.removeControls || []).forEach(function (id) {
    // Delete the element, keeping the rest of the page byte-identical.
    const re = new RegExp('<([a-zA-Z]+)([^>]*\\bid="' + id + '"[^>]*)>[\\s\\S]*?</\\1>');
    const before = html;
    html = html.replace(re, '');
    if (html === before) {
      const self = new RegExp('<[a-zA-Z]+[^>]*\\bid="' + id + '"[^>]*/?>');
      html = html.replace(self, '');
    }
  });

  const dom = new JSDOM(html, {
    url: 'https://console.8-bit.space/excel-addin/taskpane.html',
    runScripts: 'dangerously',
    pretendToBeVisual: true
    // No `resources`, so jsdom does NOT fetch office.js, lib.js or app.js.
    // We inject those ourselves, in order, below.
  });
  const win = dom.window;

  win.addEventListener('error', function (e) {
    rec.pageErrors.push(String((e.error && e.error.message) || e.message));
  });
  win.addEventListener('unhandledrejection', function (e) {
    rec.pageErrors.push('unhandledrejection: ' + String((e.reason && e.reason.message) || e.reason));
  });

  /* Which controls actually got a listener. This is the assertion that pins
     the on() helper: not "did the pane render" but "did the listeners AFTER
     the missing one still attach". */
  const origAdd = win.EventTarget.prototype.addEventListener;
  win.EventTarget.prototype.addEventListener = function (ev, fn, o) {
    if (this && this.id) rec.listeners.push({ id: this.id, event: ev });
    return origAdd.call(this, ev, fn, o);
  };

  const excel = makeExcel(rec);
  const office = makeOffice(rec);
  win.Excel = excel.Excel;
  win.Office = office;
  win.fetch = makeFetch(rec, opts.routes || {});
  rec.workbook = excel.workbook;
  rec.office = office;

  let libSrc = fs.readFileSync(path.join(ADDIN_DIR, 'lib.js'), 'utf8');
  let appSrc = fs.readFileSync(path.join(ADDIN_DIR, 'app.js'), 'utf8');
  if (opts.mutateLib) libSrc = opts.mutateLib(libSrc);
  if (opts.mutateApp) appSrc = opts.mutateApp(appSrc);
  if (!opts.skipLib) win.eval(libSrc);
  win.eval(appSrc);

  const api = {
    dom: dom,
    window: win,
    document: win.document,
    rec: rec,
    // Copied out of the jsdom realm: its Array is not this realm's Array,
    // and deepStrictEqual compares prototypes.
    diag: function () { return JSON.parse(JSON.stringify(win.__klikkDiag())); },
    $: function (id) { return win.document.getElementById(id); },
    click: function (id) {
      const node = win.document.getElementById(id);
      if (!node) throw new Error('no such control: ' + id);
      node.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    },
    /* Office fires onReady once the host is up; nothing before that runs. */
    boot: async function () {
      if (!rec.onReadyCb) throw new Error('app.js never called Office.onReady');
      await rec.onReadyCb({ host: 'Excel', platform: 'Mac' });
      await api.settle();
    },
    // The pane fires several promise chains off boot (inspect, connect,
    // view list). Give them the ticks they need before asserting.
    settle: async function (ticks) {
      for (let i = 0; i < (ticks || 12); i++) await new Promise(function (r) { setTimeout(r, 0); });
    },
    close: function () { win.close(); }
  };
  return api;
}

module.exports = { loadPane: loadPane, ADDIN_DIR: ADDIN_DIR };
