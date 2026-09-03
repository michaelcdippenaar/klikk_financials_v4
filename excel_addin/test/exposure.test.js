/* What the public origin serves.
 *
 * excel_addin/ is published whole at /excel-addin/ by _excel_addin_asset in
 * klikk_business_intelligence/urls.py, so ANY file added to this folder is
 * public the moment it deploys. That is how the icon masters — the SVG
 * sources plus build.py and contact_sheet.py — ended up readable on the
 * internet: nobody chose to publish them, they were just put in the folder.
 *
 * So this is not a README line, it is a check. Every file here must be either
 * something Excel actually fetches, or explicitly denied in urls.py. A file
 * that is neither fails this test, which is the only moment anyone is going
 * to think about it.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const ADDIN_DIR = process.env.KLIKK_ADDIN_DIR
  ? path.resolve(process.env.KLIKK_ADDIN_DIR)
  : path.resolve(__dirname, '..');
const URLS_PY = path.resolve(ADDIN_DIR, '..', 'klikk_business_intelligence', 'urls.py');

/* Exactly what Office and the pane fetch. Everything else is working
   material. Keep this in step with taskpane.html and manifest.xml. */
const SERVED = [
  /^manifest\.xml$/,
  /^taskpane\.html$/,
  /^app\.js$/,
  /^lib\.js$/,
  /^styles\.css$/,
  /^assets\/[^/]+\.png$/
];

/* The denial rules as urls.py actually states them, read from the file rather
   than restated here — a copy would drift and pass while production leaked. */
function denyRules() {
  const src = fs.readFileSync(URLS_PY, 'utf8');
  const m = src.match(/_EXCEL_ADDIN_PRIVATE = \(([\s\S]*?)\)/);
  assert.ok(m, 'could not find _EXCEL_ADDIN_PRIVATE in urls.py');
  const prefixes = [...m[1].matchAll(/'([^']+)'/g)].map(x => x[1]);
  const kinds = (src.match(/normalised\.endswith\(\(([^)]*)\)\)/) || [null, ''])[1];
  const extensions = [...kinds.matchAll(/'([^']+)'/g)].map(x => x[1]);
  const dotfiles = /seg\.startswith\('\.'\)/.test(src);
  assert.ok(extensions.includes('.py'), 'urls.py no longer denies .py under the add-in origin');
  assert.ok(dotfiles, 'urls.py no longer denies dotfiles — an .env here would be public');
  return { prefixes, extensions, dotfiles };
}

function isDenied(rel, rules) {
  if (rules.extensions.some(ext => rel.endsWith(ext))) return true;
  if (rules.dotfiles && rel.split('/').some(seg => seg.startsWith('.'))) return true;
  return rules.prefixes.some(p => rel === p || rel.startsWith(p));
}

function walk(dir, base) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const rel = base ? base + '/' + entry.name : entry.name;
    if (entry.name === 'node_modules' || entry.name === '.git') return [];
    return entry.isDirectory() ? walk(path.join(dir, entry.name), rel) : [rel];
  });
}

test('every file in excel_addin is either fetched by Excel or denied by urls.py', () => {
  const rules = denyRules();
  const unclassified = walk(ADDIN_DIR, '')
    .filter(rel => !SERVED.some(re => re.test(rel)))
    .filter(rel => !isDenied(rel, rules));

  assert.deepStrictEqual(unclassified, [],
    'These files would be public at https://console.8-bit.space/backend/excel-addin/…\n'
    + 'Either Excel fetches them (add to SERVED here) or it does not '
    + '(add to _EXCEL_ADDIN_PRIVATE in klikk_business_intelligence/urls.py).');
});

test('the icon masters and their build scripts are denied', () => {
  // The specific leak of 2026-09-03: 16 files committed so the icon set could
  // be regenerated, published by the same rule that publishes the pane.
  const rules = denyRules();
  for (const rel of ['assets/src/build.py', 'assets/src/contact_sheet.py', 'assets/src/cube-32.svg']) {
    assert.ok(isDenied(rel, rules), rel + ' is publicly readable');
  }
});

test('the icons Excel actually shows are still served', () => {
  // Denying assets/ wholesale would take the ribbon icons down with it.
  const rules = denyRules();
  const png = 'assets/icon-32.png';
  assert.ok(SERVED.some(re => re.test(png)));
  assert.ok(!isDenied(png, rules), 'the manifest references this by URL');
});
