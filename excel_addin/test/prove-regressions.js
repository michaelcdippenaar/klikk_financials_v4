#!/usr/bin/env node
/* prove-regressions.js — proof that the harness would have caught 2026-09-03.
 *
 * A test that has never failed has proved nothing. This reconstructs each of
 * the bugs that shipped that day in a throwaway copy of the add-in, runs
 * the check that is supposed to catch it, and requires the check to FAIL —
 * then requires it to pass on the tree as it stands. A check that passes on
 * both is not a check; this script says so and exits non-zero.
 *
 *   node test/prove-regressions.js        (or: npm run prove)
 *
 * The mutations are the historic code, not inventions: each one names the
 * commit that removed it.
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const ADDIN = path.resolve(__dirname, '..');
const BUNDLE = ['app.js', 'lib.js', 'taskpane.html', 'styles.css', 'eslint.config.mjs', 'package.json'];

/* Each regression: what shipped, which file carried it, and which check is
   claimed to catch it. `find` must appear exactly once, or the mutation has
   drifted away from the code and the proof is worthless — that is an error,
   not a skip. */
const REGRESSIONS = [
  {
    name: 'null-listener',
    story: 'wireEvents() used bare el.X.addEventListener; one missing id killed '
         + 'every listener after it while the pane still rendered (fixed 8a527bd, 0689246)',
    file: 'app.js',
    find: `  function on(id, ev, fn) {
    var node = el[id] || document.getElementById(id);
    if (!node || typeof node.addEventListener !== 'function') {
      if (missingControls.indexOf(id) < 0) missingControls.push(id);
      return;
    }
    node.addEventListener(ev, fn);
  }`,
    replace: `  function on(id, ev, fn) {
    (el[id] || document.getElementById(id)).addEventListener(ev, fn);
  }`,
    check: ['test', 'test/boot.test.js']
  },
  {
    name: 'sheet-per-build',
    story: 'buildCube() added a worksheet every call — Cube 2 … Cube 5 in eleven '
         + 'seconds of dragging (fixed 1bcfb4f)',
    file: 'app.js',
    find: `    var target = (opts && opts.newSheet) ? null : cubeTargetId();`,
    replace: `    var target = null;`,
    check: ['test', 'test/cube.test.js']
  },
  {
    name: 'run-merge-indent',
    story: 'runs keyed on Math.min(depth, 2), so the first child of every parent '
         + 'was indented at its parent\'s level (fixed 1b3421b)',
    file: 'lib.js',
    find: `      var kind = (r.is_total ? 'T' : 'L') + r.depth;`,
    replace: `      var kind = (r.is_total ? 'T' : 'L') + Math.min(r.depth, 2);`,
    check: ['test', 'test/lib.test.js']
  },
  {
    name: 'widths-reset-on-rebuild',
    story: 'the in-place rebuild re-applied the default widths afterwards, so '
         + 'hand-sizing the columns survived only until the next Build '
         + '("when I rebuild the cube it resets my column spacing")',
    file: 'app.js',
    find: `      applyColumnWidths(sheet, headerRowIdx,
        LIB.cubeWidths({ nRowDims: nRowDims, nCols: nCols }, prevWidths));`,
    replace: `      if (nRowDims > 1) {
        sheet.getRangeByIndexes(headerRowIdx, 0, 1, nRowDims - 1).format.columnWidth = 130;
      }
      sheet.getRangeByIndexes(headerRowIdx, nRowDims - 1, 1, 1).format.columnWidth = 330;
      sheet.getRangeByIndexes(headerRowIdx, nRowDims, 1, width - nRowDims)
        .format.columnWidth = 104;`,
    check: ['test', 'test/widths.test.js']
  },
  {
    name: 'dead-handler',
    story: 'reloadThisSheet was deleted on 2026-08-20 (d0efa8f) while its click '
         + 'handler kept calling it — a live ReferenceError that shipped for two weeks',
    file: 'app.js',
    find: `    on('btnReload', 'click', function () { run(refreshActiveSheet); });`,
    replace: `    on('btnReload', 'click', function () { run(reloadThisSheet); });`,
    // Two gates see this one: the linter reads it, and clicking finds it.
    check: ['lint'],
    alsoCheck: ['test', 'test/boot.test.js']
  }
];

function tempCopy() {
  // realpath: on macOS os.tmpdir() is a symlink, and ESLint resolves the real
  // path before comparing it with cwd — a mismatch makes it silently ignore
  // every file as "outside of base path".
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'klikk-addin-')));
  BUNDLE.forEach(f => fs.copyFileSync(path.join(ADDIN, f), path.join(dir, f)));
  fs.mkdirSync(path.join(dir, 'test'));
  fs.readdirSync(path.join(ADDIN, 'test')).forEach(f => {
    fs.copyFileSync(path.join(ADDIN, 'test', f), path.join(dir, 'test', f));
  });
  // The checks run from the real directory (that is where node_modules is);
  // KLIKK_ADDIN_DIR points them at the copy.
  return dir;
}

/* `kind` is 'lint' or 'test'. Returns true when the check PASSES. */
function runCheck(check, addinDir) {
  const env = Object.assign({}, process.env, { KLIKK_ADDIN_DIR: addinDir });
  let cmd, args;
  if (check[0] === 'lint') {
    cmd = path.join(ADDIN, 'node_modules', '.bin', 'eslint');
    // Relative to cwd, beside the config: ESLint 9 ignores files outside its
    // base path, which is how the skill's own lint-addin.sh does it too.
    args = ['-c', 'eslint.config.mjs', 'app.js', 'lib.js'];
  } else {
    cmd = process.execPath;
    args = ['--test', path.join(addinDir, 'test', path.basename(check[1]))];
  }
  const r = spawnSync(cmd, args, { cwd: addinDir, env: env, encoding: 'utf8' });
  return { ok: r.status === 0, out: (r.stdout || '') + (r.stderr || '') };
}

function checkLabel(check) {
  return check[0] === 'lint' ? 'npm run lint' : 'node --test ' + check[1];
}

/* One check, twice: on the reconstructed bug (must fail) and on HEAD (must
   pass). Anything else and the check is not doing its job. */
function prove(reg, check, mutatedDir) {
  const mutated = runCheck(check, mutatedDir);
  const head = runCheck(check, ADDIN);
  const caught = !mutated.ok;
  const clean = head.ok;
  console.log('    ' + checkLabel(check));
  console.log('      with the bug : ' + (caught ? 'FAILED   ✓ caught' : 'passed   ✗ BLIND'));
  console.log('      on HEAD      : ' + (clean ? 'passed   ✓' : 'FAILED   ✗ broken on HEAD'));
  if (!caught) {
    console.log('      ── the check did not notice the bug. Its output was:');
    console.log(mutated.out.split('\n').slice(0, 20).map(l => '        ' + l).join('\n'));
  }
  if (!clean) {
    console.log('      ── HEAD does not pass this check:');
    console.log(head.out.split('\n').filter(l => /not ok|error|Error/.test(l))
      .slice(0, 12).map(l => '        ' + l).join('\n'));
  }
  return caught && clean;
}

let allGood = true;
console.log('Reconstructing the ' + REGRESSIONS.length + ' 2026-09-03 regressions and '
  + 'checking that each check sees them.\n');

for (const reg of REGRESSIONS) {
  console.log('  ' + reg.name);
  console.log('    ' + reg.story);

  const dir = tempCopy();
  try {
    const target = path.join(dir, reg.file);
    const src = fs.readFileSync(target, 'utf8');
    const hits = src.split(reg.find).length - 1;
    if (hits !== 1) {
      console.log('    ✗ the code this mutation edits is not where it was ('
        + hits + ' matches in ' + reg.file + '). Update the fixture.');
      allGood = false;
      continue;
    }
    fs.writeFileSync(target, src.replace(reg.find, reg.replace));

    let ok = prove(reg, reg.check, dir);
    if (reg.alsoCheck) ok = prove(reg, reg.alsoCheck, dir) && ok;
    allGood = ok && allGood;
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
  console.log('');
}

if (allGood) {
  console.log('All ' + REGRESSIONS.length
    + ' regressions are caught by a check that passes on HEAD.');
  process.exit(0);
}
console.log('At least one check is blind, or does not pass on HEAD. See above.');
process.exit(1);
