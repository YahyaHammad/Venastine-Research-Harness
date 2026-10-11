#!/usr/bin/env node
/**
 * scripts/prepublish-check.mjs -- the gate on `npm publish` (batch 35).
 *
 * Runs from package.json's `prepublishOnly`, so it fires before anything is
 * uploaded and a non-zero exit aborts the publish. `npm pack` triggers
 * `prepack`, not `prepublishOnly`, so calling pack from in here does not
 * recurse.
 *
 * It checks the two things that fail SILENTLY and are unrecoverable once
 * published, because a version on the npm registry cannot be replaced:
 *
 *   1. The two version numbers agree. package.json and pyproject.toml are a
 *      hand-maintained duplicate of one fact, introduced by this batch. The
 *      suite pins it too (tests/test_docs_consistency.py); this is the copy
 *      that runs on a machine that just typed `npm publish`.
 *
 *   2. No secret or local artifact is in the tarball, and every legally
 *      required file IS. `files` in package.json is an allowlist for exactly
 *      this reason -- without it npm falls back to .gitignore and
 *      providers.json would be excluded only by accident. This is the third
 *      copy of that rule: .gitignore covers `git push`, .gitattributes
 *      export-ignore covers `git archive` (H3/C2), `files` covers npm.
 *
 * LICENSE and NOTICE are checked as hard requirements, not niceties.
 * Publishing to npm is distribution of the Work under Apache-2.0 section 4,
 * where running from a checkout was not: 4(a) wants the licence to travel
 * with it and 4(d) wants the NOTICE to. npm auto-includes LICENSE and does
 * NOT auto-include NOTICE -- the same trap as `license-files` in pyproject,
 * one channel over.
 */

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
// Read once, at the top: FORBIDDEN below is derived from `neverDistribute`
// and the version check further down uses the same object.
const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
const problems = [];

/** Files whose absence would be a licensing defect, not a packaging one. */
const REQUIRED = [
  'LICENSE',
  'NOTICE',
  'docs/THIRD_PARTY_NOTICES.md',
  'README.md',
  'requirements.txt',
  'main.py',
  'bin/venastine.mjs',
];

/**
 * Root-level Python modules that must NOT ship, with the reason.
 *
 * Everything else at the root that ends in `.py` is production source the
 * harness imports, and `files` in package.json lists those INDIVIDUALLY --
 * so adding a module to the repo and forgetting the allowlist ships a
 * package that ImportErrors on its first run, silently, with nothing here
 * saying so. That happened in batch 45 with `json_store.py`, which is why
 * this check exists: the claim earned it by drifting.
 */
const ROOT_PY_NOT_SHIPPED = new Set([
  // Test bootstrap. pyproject sets packages=[]/py-modules=[] for the same
  // reason: installed as a top-level name it collides with other projects'.
  'conftest.py',
]);

/**
 * Anything matching these must never leave this machine.
 *
 * DERIVED FROM package.json's `neverDistribute`, which is the one list all
 * three channels read (batch 140, S18-01 and S18-08). It used to be a literal
 * here, and that was the FOURTH copy of "keep local secrets out of a
 * distribution" -- `.gitignore` for push, `.gitattributes export-ignore` for
 * archive, this, and `never_named` in tests/test_docs_consistency.py. Two
 * documents claimed three copies. No two of the five agreed, and the
 * disagreements were not cosmetic: `secrets.json` was in both git channels
 * and in neither npm-side list, and the database test was
 * `f.endsWith('.db')`, which does not match `app.db-wal` -- the very defect
 * both git channels had already fixed and written a comment about.
 *
 * Keeping the ALLOWLIST as the first line of defence is unchanged: `files`
 * names none of these, so nothing here can reach a tarball through it. This
 * is the second layer, and publish.yml states why it exists in its own words
 * -- "it cannot happen because of a list somewhere else" is exactly the
 * assumption worth not making about a credential file.
 */
const MATCHERS = {
  exact: (vs) => (f) => vs.includes(f),
  // A basename anywhere in the tree: `.env` and `sub/.env` both count.
  basename: (vs) => (f) => vs.some((v) => f === v || f.endsWith(`/${v}`)),
  suffix: (vs) => (f) => vs.some((v) => f.endsWith(v)),
  prefix: (vs) => (f) => vs.some((v) => f.startsWith(v)),
  // A whole PATH SEGMENT, not a substring. `f.includes('__pycache__')` was
  // the old spelling and it also matches `notes__pycache__.txt`, which is
  // somebody's file rather than build residue.
  segment: (vs) => (f) => vs.some((v) => f === v || f.split('/').includes(v)),
  'suffix-or-segment': (vs) => (f) =>
    vs.some((v) => f.endsWith(v) || f.split('/').includes(v)),
  dir: (vs) => (f) => vs.some((v) => f === v || f.startsWith(`${v}/`)),
};

const FORBIDDEN = (pkg.neverDistribute || []).flatMap((entry) => {
  const make = MATCHERS[entry.match];
  // LOUD, not skipped. A match kind this file does not know is an entry the
  // gate would silently stop enforcing -- the defensive-default shape that
  // S18-02 files against the bandit baseline, and the one thing a denylist
  // must never do.
  if (!make) {
    throw new Error(
      `package.json neverDistribute: unknown match kind ${JSON.stringify(entry.match)} ` +
      `for ${JSON.stringify(entry.label)}. Teach MATCHERS about it or fix the entry; ` +
      `skipping it would turn this gate off for that pattern.`);
  }
  return [{ label: entry.label, test: make(entry.value) }];
});

if (FORBIDDEN.length === 0) {
  throw new Error(
    'package.json neverDistribute produced no npm-channel patterns. That ' +
    'would make the tarball check below pass unconditionally, which is the ' +
    'vacuity this gate exists to avoid.');
}

/**
 * The unsafe-mode branch must never reach npm (ROADMAP_v2 UN5).
 *
 * That branch carries UNSAFE_NO_APPROVAL / UNSAFE_NO_SANDBOX -- a
 * deliberate, documented reduction in security for researchers who accept
 * it. Regular users install from npm, so npm must only ever serve `main`.
 *
 * This check lives on MAIN, not on the branch, and that placement is the
 * point. It travels into the branch by merge, so the branch cannot lose it
 * by forgetting to add it or by a bad conflict resolution -- and it also
 * catches the reverse accident, unsafe code merged INTO main and published
 * from there. A published npm version can never be replaced, only
 * deprecated, so the reverse accident is the unrecoverable one.
 *
 * Three independent detectors, because any one alone is one rename away from
 * silence: a marker file the branch carries, and the config constants
 * themselves, in `config.py` and in `config.yaml`. The FORBIDDEN entry above catches the marker if it is ever
 * packed; this catches it in the working tree whether packed or not.
 */
function unsafeBranchProblems() {
  const found = [];
  if (fs.existsSync(path.join(ROOT, 'UNSAFE_BRANCH'))) {
    found.push('an UNSAFE_BRANCH marker file is present at the repo root');
  }
  try {
    const cfg = fs.readFileSync(path.join(ROOT, 'config.py'), 'utf8');
    if (/^\s*UNSAFE_NO_(APPROVAL|SANDBOX)\s*=/m.test(cfg)) {
      found.push('config.py declares UNSAFE_NO_APPROVAL / UNSAFE_NO_SANDBOX');
    }
  } catch {
    found.push('config.py could not be read to check for unsafe-mode flags');
  }
  // The values moved to config.yaml, so an unsafe-mode build declares them
  // there now. All three detectors are kept: config.py is still where a human
  // could add a constant back, and dropping any one is the rename away
  // from silence this function's own comment warns about.
  //
  // `[\r\n]` rather than `^` with /m is belt and braces, and the reason
  // this comment USED to give was wrong (S18-09, corrected batch 140): it
  // said these files are stored CRLF in the worktree. Measured -- config.py
  // is 95 LF lines and config.yaml 1030, neither containing one CRLF. `^`
  // with /m would also have worked on CRLF anyway, since `^` matches after
  // `\n` and the `\s*` that follows absorbs the `\r`. The construct is
  // correct either way; it is kept, and the config.py check four lines up
  // uses bare `^` with /m, so the two spellings differ harmlessly.
  try {
    const yml = fs.readFileSync(path.join(ROOT, 'config.yaml'), 'utf8');
    if (/(^|[\r\n])\s*unsafe_no_(approval|sandbox)\s*:/i.test(yml)) {
      found.push('config.yaml declares unsafe_no_approval / unsafe_no_sandbox');
    }
  } catch {
    found.push('config.yaml could not be read to check for unsafe-mode flags');
  }
  return found.map(
    (why) =>
      `Refusing to publish the unsafe-mode build: ${why}. ` +
      'npm serves regular users and must only ever be published from `main`. ' +
      'If you are on `main` and see this, unsafe-mode code has been merged in ' +
      'by mistake -- that is the accident this check exists for.'
  );
}

function versionFromPyproject() {
  const text = fs.readFileSync(path.join(ROOT, 'pyproject.toml'), 'utf8');
  const matches = [...text.matchAll(/^\s*version\s*=\s*"([^"]+)"/gm)];
  if (matches.length !== 1) {
    problems.push(
      `pyproject.toml has ${matches.length} \`version = "..."\` lines; expected exactly 1. ` +
      'This check reads it by pattern, so two would make the comparison meaningless.'
    );
    return null;
  }
  return matches[0][1];
}

function packedFiles() {
  const r = spawnSync('npm', ['pack', '--dry-run', '--json'], {
    cwd: ROOT,
    encoding: 'utf8',
    shell: process.platform === 'win32',
  });
  if (r.status !== 0) {
    problems.push(`\`npm pack --dry-run\` failed:\n${r.stderr || r.stdout}`);
    return null;
  }
  // npm writes its progress notices to stderr, so stdout is the JSON. Slice
  // from the first bracket anyway: some npm versions prepend a blank line.
  const json = r.stdout.slice(r.stdout.indexOf('['));
  return JSON.parse(json)[0].files.map((f) => f.path);
}

/** Root-level `*.py` that production imports -- everything but the exempt set. */
function rootPythonModules() {
  return fs
    .readdirSync(ROOT, { withFileTypes: true })
    .filter((e) => e.isFile() && e.name.endsWith('.py'))
    .map((e) => e.name)
    .filter((name) => !ROOT_PY_NOT_SHIPPED.has(name));
}

const pyVersion = versionFromPyproject();

if (pyVersion && pyVersion !== pkg.version) {
  problems.push(
    `Version mismatch: package.json says ${pkg.version}, pyproject.toml says ${pyVersion}.\n` +
    '  They describe one release. Bump both, or the npm tarball and the Python\n' +
    '  metadata inside it disagree about what they are.'
  );
}

const files = packedFiles();
if (files) {
  for (const required of REQUIRED) {
    if (!files.includes(required)) {
      problems.push(`${required} is missing from the tarball. Add it to \`files\` in package.json.`);
    }
  }
  for (const module of rootPythonModules()) {
    if (!files.includes(module)) {
      problems.push(
        `${module} is a root Python module and is NOT in the tarball.\n` +
        '  `files` lists root modules one by one, so a new one is excluded by\n' +
        '  default and the package ImportErrors on first run. Add it to `files`\n' +
        '  in package.json, or to ROOT_PY_NOT_SHIPPED here with the reason.'
      );
    }
  }
  for (const { label, test } of FORBIDDEN) {
    const hits = files.filter(test);
    if (hits.length) {
      problems.push(
        `Tarball contains ${label}: ${hits.slice(0, 5).join(', ')}` +
        `${hits.length > 5 ? ` (+${hits.length - 5} more)` : ''}`
      );
    }
  }
}

problems.push(...unsafeBranchProblems());

if (problems.length) {
  process.stderr.write(
    `\nRefusing to publish -- ${problems.length} problem(s):\n\n` +
    problems.map((p) => `  * ${p}`).join('\n\n') +
    '\n\nA published npm version cannot be replaced, only deprecated.\n'
  );
  process.exit(1);
}

process.stdout.write(
  `prepublish check passed: v${pkg.version}, ${files ? files.length : '?'} files, ` +
  'LICENSE + NOTICE present, no secrets.\n'
);
