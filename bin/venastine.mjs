#!/usr/bin/env node
/**
 * bin/venastine.mjs -- the npm entry point (batch 35).
 *
 * npm delivers this project as a FOLDER OF SOURCE, not a binary. This file
 * is the only thing npm can execute, so its whole job is to hand control to
 * the Python that actually implements the harness:
 *
 *   find a Python >= 3.11  ->  make sure its dependencies exist
 *   ->  spawn `python <root>/main.py` with the user's argv, cwd and stdio
 *
 * ZERO npm DEPENDENCIES, deliberately. Node stdlib only. The tarball is
 * first-party source and nothing else, which is what lets
 * THIRD_PARTY_NOTICES.md say the npm channel adds no licence surface. Do not
 * add a dependency here without changing that file too.
 *
 * WHAT THIS FILE MUST NEVER DO, and why each one is a real bug and not a
 * style preference:
 *
 *   * Never set PYTHONPATH. tools/isolation.py builds its child's PYTHONPATH
 *     from the PARENT's resolved sys.path -- "handing the child the paths the
 *     parent actually resolved is deterministic". A value injected here would
 *     be inherited into that computation and poison it.
 *   * Never set AGENT_WORKSPACE. WORKSPACE_DIR is a PERMISSION BOUNDARY, not
 *     a storage location: file_ops auto-approves writes inside it and demands
 *     approval outside. Redirecting it to a shared home directory would
 *     silently auto-approve writes there from every project.
 *   * Never install without consent. See ensureRuntime().
 */

import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const REQUIREMENTS = path.join(ROOT, 'requirements.txt');
const MAIN = path.join(ROOT, 'main.py');

/**
 * ~/.config/venastine ON EVERY PLATFORM, Windows included.
 *
 * Not %APPDATA%, however Node-idiomatic that would be. The Python half of
 * this directory is already fixed: core/config_loader.py, mcp_client/config.py
 * and core/workspace_trust.py all resolve their user tier with
 * os.path.expanduser("~/.config/venastine"). Splitting one config directory
 * across two locations by platform is the bug this constant exists to avoid.
 */
const CONFIG_HOME = path.join(os.homedir(), '.config', 'venastine');

const MIN_PYTHON = [3, 11];

/** Python floor, in the terms the project already states it in (#144). */
const PYTHON_FLOOR_MESSAGE =
  `Venastine needs Python ${MIN_PYTHON.join('.')} or newer.\n` +
  '\n' +
  'mcp_client/client.py uses asyncio.timeout (3.11+) and 27 sites use runtime\n' +
  '`X | Y` annotations (3.10+). On an older interpreter the harness installs\n' +
  'and then fails from inside the MCP connect path, which is why this refuses\n' +
  'here instead.\n' +
  '\n' +
  'Install Python 3.11+, or point VENASTINE_PYTHON at one:\n' +
  '  VENASTINE_PYTHON=/path/to/python3.12 venastine';

function readPackageVersion() {
  try {
    return JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8')).version;
  } catch {
    return '0.0.0';
  }
}

/**
 * Candidate interpreters, best first. The loop takes the first that satisfies
 * the floor, so an old `python3` earlier in the list simply falls through to a
 * newer explicit one rather than failing the launch.
 */
function pythonCandidates() {
  if (process.env.VENASTINE_PYTHON) {
    return [{ cmd: process.env.VENASTINE_PYTHON, args: [], explicit: true }];
  }
  if (process.platform === 'win32') {
    // `py -3` is the launcher's newest install; the explicit ones are the
    // fallback for machines without the py launcher on PATH.
    return [
      { cmd: 'py', args: ['-3'] },
      { cmd: 'python', args: [] },
      { cmd: 'python3', args: [] },
      { cmd: 'py', args: ['-3.13'] },
      { cmd: 'py', args: ['-3.12'] },
      { cmd: 'py', args: ['-3.11'] },
    ];
  }
  return [
    { cmd: 'python3', args: [] },
    { cmd: 'python3.13', args: [] },
    { cmd: 'python3.12', args: [] },
    { cmd: 'python3.11', args: [] },
    { cmd: 'python', args: [] },
  ];
}

const PROBE = 'import sys; print("%d.%d" % sys.version_info[:2]); print(sys.executable)';

function probe(candidate) {
  const r = spawnSync(candidate.cmd, [...candidate.args, '-c', PROBE], {
    encoding: 'utf8',
    // Windows resolves `py`/`python` through PATHEXT, which needs the shell
    // off but the extension search on -- spawnSync does that for us. What it
    // does NOT do is survive a missing binary, hence the status check below.
    windowsHide: true,
  });
  if (r.status !== 0 || !r.stdout) return null;
  const [version, executable] = r.stdout.trim().split(/\r?\n/);
  if (!version || !executable) return null;
  const [major, minor] = version.split('.').map(Number);
  return { ...candidate, version, executable, major, minor };
}

function findPython() {
  const rejected = [];
  for (const candidate of pythonCandidates()) {
    const found = probe(candidate);
    if (!found) continue;
    if (found.major > MIN_PYTHON[0] ||
        (found.major === MIN_PYTHON[0] && found.minor >= MIN_PYTHON[1])) {
      return found;
    }
    rejected.push(`${found.executable} (${found.version})`);
  }
  const detail = rejected.length
    ? `\n\nFound, but too old: ${rejected.join(', ')}`
    : '\n\nNo Python interpreter was found on PATH.';
  fail(PYTHON_FLOOR_MESSAGE + detail);
}

function requirementsHash() {
  return crypto.createHash('sha256')
    .update(fs.readFileSync(REQUIREMENTS))
    .digest('hex')
    .slice(0, 12);
}

/**
 * The runtime directory is keyed by VERSION AND requirements hash together.
 *
 * Either alone is not enough: a version bump with unchanged pins should reuse
 * the environment, and an edited pin within one version must NOT. Keying on
 * both means every distinct dependency set gets a clean venv instead of a
 * half-migrated one, and switching back to an older install finds its own
 * environment still intact.
 */
function runtimePaths() {
  const dir = path.join(CONFIG_HOME, 'runtime', `${readPackageVersion()}-${requirementsHash()}`);
  return {
    dir,
    python: process.platform === 'win32'
      ? path.join(dir, 'Scripts', 'python.exe')
      : path.join(dir, 'bin', 'python'),
    // Written LAST, and it -- not the directory -- is what "ready" means. A
    // venv whose pip install died half way exists on disk and would otherwise
    // read as installed, then fail on a missing import at the first model call.
    stamp: path.join(dir, 'venastine-runtime.json'),
  };
}

function isReady(rt) {
  return fs.existsSync(rt.stamp) && fs.existsSync(rt.python);
}

function countPins() {
  return fs.readFileSync(REQUIREMENTS, 'utf8')
    .split(/\r?\n/)
    .filter((line) => line.trim() && !line.trim().startsWith('#'))
    .length;
}

function ask(question) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.question(question, (answer) => {
      rl.close();
      resolve(/^y(es)?$/i.test(answer.trim()));
    });
  });
}

/**
 * Build the Python environment, only ever with the user's say-so.
 *
 * The consent prompt is not ceremony. This is the one action the launcher
 * takes on its own initiative -- it runs a network install of 17 pinned
 * distributions -- and the project's own rule for that case is D17/D31:
 * show what will run and confirm it. That is also why the bootstrap is here
 * and not in an npm `postinstall` hook, where it would run unannounced and be
 * skipped entirely under `--ignore-scripts`.
 *
 * NOT A TTY AND NO --venastine-yes => REFUSE AND PRINT THE COMMAND. A CI job
 * or a piped invocation must not be able to trigger an unattended install
 * just by being unable to answer.
 */
async function ensureRuntime(python, rt, { assumeYes }) {
  if (isReady(rt)) return;

  const summary =
    'Venastine needs a Python environment (first run).\n' +
    `  python  : ${python.version}  (${python.executable})\n` +
    `  venv    : ${rt.dir}\n` +
    `  install : ${countPins()} pinned packages from requirements.txt\n`;

  if (!assumeYes) {
    if (!process.stdin.isTTY) {
      fail(
        summary +
        '\nRefusing to install unattended (no terminal to ask in).\n' +
        'Re-run interactively, pass --venastine-yes, or build it yourself:\n' +
        `  ${python.executable} -m venv "${rt.dir}"\n` +
        `  "${rt.python}" -m pip install -r "${REQUIREMENTS}"`
      );
    }
    process.stdout.write(summary);
    const ok = await ask('Proceed? [y/N] ');
    if (!ok) fail('Cancelled. Nothing was installed.');
  } else {
    process.stdout.write(summary);
  }

  try {
    run(python.executable, ['-m', 'venv', rt.dir]);
    run(rt.python, ['-m', 'pip', 'install', '--disable-pip-version-check',
                    '-r', REQUIREMENTS]);
  } catch (e) {
    // Leave nothing half-built behind: the next run must start clean rather
    // than inherit a venv that is missing an unknown subset of its packages.
    fs.rmSync(rt.dir, { recursive: true, force: true });
    fail(`Environment setup failed: ${e.message}`);
  }

  fs.writeFileSync(rt.stamp, JSON.stringify({
    version: readPackageVersion(),
    requirementsHash: requirementsHash(),
    python: python.executable,
    pythonVersion: python.version,
    createdAt: new Date().toISOString(),
  }, null, 2));
}

function run(cmd, args) {
  const r = spawnSync(cmd, args, { stdio: 'inherit', windowsHide: true });
  if (r.error) throw r.error;
  if (r.status !== 0) throw new Error(`${path.basename(cmd)} ${args[0]} exited ${r.status}`);
}

/** Walks up from cwd the way dotenv's find_dotenv(usecwd=True) now does. */
function findEnvFile() {
  let dir = process.cwd();
  for (;;) {
    const candidate = path.join(dir, '.env');
    if (fs.existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

/**
 * The child's environment: inherit everything, then fill in ONLY what is
 * missing. Every branch here defers to a value the user already set.
 */
function childEnv() {
  const env = { ...process.env };
  const cwd = process.cwd();

  // Credentials. cwd wins, because per-project keys are a real workflow; the
  // user-level file is the fallback that makes a globally-installed CLI
  // usable from directories that have no providers.json of their own.
  if (!env.AGENT_PROVIDERS_FILE && !fs.existsSync(path.join(cwd, 'providers.json'))) {
    const shared = path.join(CONFIG_HOME, 'providers.json');
    if (fs.existsSync(shared)) env.AGENT_PROVIDERS_FILE = shared;
  }

  // State. ONE decision drives BOTH variables -- a global database writing to
  // a local log is worse than either arrangement on its own.
  //
  // Why global at all: main.py sets project_path = os.getcwd(), and UserMemory
  // carries `scope` AND `project_path` (M12/D25) precisely so ONE database can
  // tell projects apart. Per-directory databases would put an accidental
  // scoping axis on top of that deliberate one -- `--thread <uuid>` would stop
  // resolving outside the directory it was created in, and a memory saved
  // scope="global" would be global only inside one folder.
  //
  // An existing ./app.db always wins, so running this launcher inside a
  // checkout keeps using the data already there instead of orphaning it.
  if (!env.APP_DB_PATH && !fs.existsSync(path.join(cwd, 'app.db'))) {
    env.APP_DB_PATH = path.join(CONFIG_HOME, 'app.db');
    if (!env.AGENT_LOG_FILE) env.AGENT_LOG_FILE = path.join(CONFIG_HOME, 'logs', 'app.log');
  }

  // AGENT_OUTPUT_DIR is deliberately untouched: research artifacts belong
  // beside the work that prompted them, and nothing reads them back -- the
  // report and claims live in the PipelineRunRecord row, not in output/.
  return env;
}

function doctor(python, rt) {
  const cwd = process.cwd();
  const env = childEnv();
  const localProviders = path.join(cwd, 'providers.json');
  const lines = [
    `venastine ${readPackageVersion()}`,
    `  package root  : ${ROOT}`,
    `  node          : ${process.version}`,
    `  python        : ${python.version}  (${python.executable})`,
    `  venv          : ${rt.dir}`,
    `  venv ready    : ${isReady(rt) ? 'yes' : 'no (first run will build it)'}`,
    `  cwd           : ${cwd}`,
    `  providers.json: ${env.AGENT_PROVIDERS_FILE
        || (fs.existsSync(localProviders) ? localProviders : '(none found)')}`,
    `  .env          : ${env.AGENT_ENV_FILE || findEnvFile() || '(none found)'}`,
    `  app.db        : ${env.APP_DB_PATH || path.join(cwd, 'app.db')}`,
    `  log file      : ${env.AGENT_LOG_FILE || path.join(cwd, 'logs', 'app.log')}`,
    `  output dir    : ${env.AGENT_OUTPUT_DIR || path.join(cwd, 'output')}`,
    `  workspace     : ${env.AGENT_WORKSPACE || path.join(cwd, 'workspace')}`,
  ];
  process.stdout.write(lines.join('\n') + '\n');
}

function fail(message) {
  process.stderr.write(message.replace(/\n?$/, '\n'));
  process.exit(1);
}

async function main() {
  const argv = process.argv.slice(2);

  // Launcher-owned flags are PREFIXED because main.py takes a positional
  // `query`: a bare `venastine doctor` would be sent to the model as a
  // research question rather than reaching this file.
  const assumeYes = argv.includes('--venastine-yes');
  const wantDoctor = argv.includes('--venastine-doctor');
  const wantReinstall = argv.includes('--venastine-reinstall');
  const passthrough = argv.filter((a) => !a.startsWith('--venastine-'));

  const python = findPython();
  const rt = runtimePaths();

  if (wantReinstall) {
    fs.rmSync(rt.dir, { recursive: true, force: true });
    process.stdout.write(`Removed ${rt.dir}\n`);
  }

  if (wantDoctor) {
    doctor(python, rt);
    return;
  }

  await ensureRuntime(python, rt, { assumeYes });

  // Let the child own the terminal's interrupt. Without this, Ctrl+C kills
  // the launcher first and the harness loses its chance to shut down.
  process.on('SIGINT', () => {});

  // main.py BY PATH, so its directory becomes sys.path[0]: the harness relies
  // on the project root being importable (AGENTS.md, "the project root is on
  // sys.path"). stdio:'inherit' is what gives the Textual TUI a real terminal.
  const child = spawnSync(rt.python, [MAIN, ...passthrough], {
    stdio: 'inherit',
    cwd: process.cwd(),
    env: childEnv(),
    windowsHide: false,
  });

  if (child.error) fail(`Could not start the harness: ${child.error.message}`);
  if (child.signal) process.exit(128 + (os.constants.signals[child.signal] || 0));
  process.exit(child.status ?? 1);
}

main().catch((e) => fail(e.stack || String(e)));
