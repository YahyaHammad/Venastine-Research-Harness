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
const problems = [];

/** Files whose absence would be a licensing defect, not a packaging one. */
const REQUIRED = [
  'LICENSE',
  'NOTICE',
  'THIRD_PARTY_NOTICES.md',
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

/** Anything matching these must never leave this machine. */
const FORBIDDEN = [
  { label: 'provider credentials', test: (f) => f === 'providers.json' },
  { label: 'tool secrets', test: (f) => f === '.env' || f.endsWith('/.env') },
  { label: 'local database', test: (f) => f.endsWith('.db') },
  { label: 'logs', test: (f) => f === 'logs' || f.startsWith('logs/') },
  { label: 'research output', test: (f) => f === 'output' || f.startsWith('output/') },
  { label: 'compiled python', test: (f) => f.endsWith('.pyc') || f.includes('__pycache__') },
  { label: 'notebook checkpoints', test: (f) => f.includes('.ipynb_checkpoints') },
  { label: 'security scan artifacts', test: (f) => f.startsWith('CLAUDE-SECURITY') },
  { label: 'the unsafe-mode branch marker', test: (f) => f === 'UNSAFE_BRANCH' },
];

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
 * Two independent detectors, because either alone is one rename away from
 * silence: a marker file the branch carries, and the config constants
 * themselves. The FORBIDDEN entry above catches the marker if it is ever
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

const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
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
