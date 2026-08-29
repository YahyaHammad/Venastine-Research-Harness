# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| `main` (0.1.x) | Yes — security fixes ship on `main` first |

No long-term support branches yet. If you are on a fork, rebase on `main` before reporting.

## Reporting a Vulnerability

**Do not open a public issue, discussion, or pull request for a security vulnerability.**

**Email the maintainer at YahyaHammad@proton.me**, with the subject `[SECURITY] Venastine — <title>`.

Send a high-level summary first. Do not include full exploit code in plaintext until a secure transfer method has been agreed.

**Or use GitHub Private Vulnerability Reporting, where it is enabled:** *Security* tab → *Report a vulnerability*. It is the better channel when available, because the report, the fix and the advisory stay in one place — but it is a public-repository feature, so email is the channel that always works. Use email if you do not see the button.


### What to include

* Affected file(s) and line(s) — e.g. `security/sandbox.py:134`, `tools/registry.py:187`
* The concrete trigger — not "could be a problem" — the exact call, config (`ToolPermissions.shell=True`, `providers.json` entry), and filesystem/network/model state that reaches it
* Whether you can reproduce it offline (`python -m pytest -q` or a minimal script) and the commit you tested against
* Impact — what an attacker gains (exfiltration, code execution, bypass of approval, etc.) and whether it needs a local `shell` opt-in or is reachable on a default install

A minimal reproduction is not required, but it makes triage much faster.

## What to expect

* **Acknowledgment** within **2 business days**.
* **Triage and initial assessment** within **7 days** — we will confirm severity (`S1`–`S4` per `#15` tracker), whether it is in scope, and whether we need more info.
* **Fix and disclosure** under **coordinated disclosure with a 90-day window**:
  * We will develop and test a fix on a private branch.
  * We will ship the fix to `main` and tag a release as soon as it is ready — often well before day 90.
  * Public disclosure (advisory + `DEVLOG.md` entry + `tests/BREAKING_CHANGES.md` row) happens **7 days after a fix ships to main** (to allow users time to update), or **90 days after your initial report**, whichever comes first, unless extended by mutual agreement.
  * If you prefer, you will be credited in the advisory and `DEVLOG.md`; if not, you will remain anonymous.

We will keep the private thread updated at least every 14 days until resolution.

## Scope

In scope (examples):

* Bypass of `security/permissions.py` / `safety/policy_enforcement.py` (approval, `is_tool_allowed`, `requires_approval`, `check_output_policy`, blocked domains, secret redaction)
* Sandbox escape or network egress from a tier that should not have it (`security/sandbox.py`, `security/capability.py`)
* Workspace trust boundary bypass (`core/workspace_trust.py`, `core/config_loader.py` tier precedence)
* Credential exfiltration via tool arguments/results, logs, or `param_digest` display
* MCP `autoApprove` / `mcp.json` trust bypass

Out of scope: social engineering, physical access, compromise of a provider's own API, vulnerabilities in upstream dependencies without a harness-specific trigger, or issues that require `ToolPermissions.shell=True` together with `config.SHELL_APPROVAL_MODE = "never"` *and* are already documented as deliberate. `README.md` § *"`shell` is classified, not just approved"* records that `never` "is the old unbounded behaviour, kept on purpose" — in that mode `cat ~/.aws/credentials` runs on the host unprompted, and reaching it takes writing the word `never` in `config.py`. We still want such a report, but it will be closed as documented risk.

The same applies to `ALLOW_INSECURE_SANDBOX_FALLBACK = True` **together with** `AUTO_APPROVE_SANDBOX_FALLBACK = True`. That pair is unprompted arbitrary code on the host, with your own file access, including writes into the harness's own source tree — and the harness ships as a folder of Python, so a run can edit the code the next run executes. It is not defended against, and the reason is that it cannot be: an uncontained host shell cannot be bounded from inside the same process. A path check stops `echo x > ../config.py` and a `python -c` payload walks straight past it, which is a control that reads as safety without being it. Docker is the boundary. `README.md` § *"`shell` is classified, not just approved"* states the same thing at the point of use.

**This is the `unsafe-mode` branch, and it carries two settings no release has.**
`UNSAFE_NO_APPROVAL` removes every approval prompt, for every tool. `UNSAFE_NO_SANDBOX` runs shell
commands on the host whether or not Docker is available. Both ship `False`; checking the branch out
changes nothing until you set one. **Any vulnerability that requires either is a documented risk and
will be closed as one** — the same treatment `SHELL_APPROVAL_MODE = "never"` already gets below, and
for the same reason: the setting *is* the vulnerability, deliberately, and you chose it.

What we do still want reported from this branch: anything that reaches those settings without a
human at the machine setting them. They are read once at startup from `config.py` or the `--unsafe`
flag, and are unreachable from `settings.json` at either tier, from any environment variable, from a
tool call, and from a TUI command. A route past that is a real finding, on this branch as much as on
`main`.

And the thing worth saying plainly: under `UNSAFE_NO_APPROVAL` a prompt injection from a fetched
page, an MCP server or a project's `.venastine/` is arbitrary code on your host. The harness names
those tools at launch and shows a badge for the whole session. It cannot do more than tell you.

**Since batch 40 these settings are read ONCE, at startup, into a frozen posture
(`security/posture.py`).** Before that they were read live at every decision, so
`config.SHELL_APPROVAL_MODE = "never"` -- a single attribute assignment -- turned the shell gate off
for the rest of the process from anywhere in it, and `os.environ["VENASTINE_REDACT_OFF"] = "1"` did
the same for credential redaction. The posture now cannot be changed by a tool call, a settings
file, an environment variable, model output, or a TUI command; each of those is pinned by a test in
`tests/test_posture.py`.

It **can** be changed by arbitrary in-process Python, which can also rebind the accessor itself. A
frozen dataclass raises where a bare assignment succeeded, which raises the bar without closing the
class. In scope: any route from model output or repository content to a changed posture. Out of
scope: "I achieved arbitrary code execution and then changed it", which is a report about the
execution.

Note that all of these settings live in `config.py` (`SHELL_APPROVAL_MODE` at `config.py:245`, `ToolPermissions` at `config.py:640`, the two fallback flags at `config.py:221-222`) — none has a `settings.json` key or an environment override, by design. What IS in scope, and was fixed rather than documented: a sandboxed command reaching the harness's install tree or `~/.config/venastine/` through the workspace mount (`security/protected_paths.py`), and any argument spelling that makes the classifier and the executor disagree about where a token ends.

## No bounty

This is an independent research project with no funding — **we cannot offer monetary rewards, bounties, or swag.** We will credit you publicly if you want, and we will prioritize a fix. If you need a bounty, please use a third-party program that covers this repo, or treat the report as a voluntary disclosure.

## Disclosure and credit

* We will publish a GitHub Security Advisory (GHSA) 7 days after the fix ships to main (or at the 90-day mark), requesting a CVE if the severity warrants it.
* We will not disclose your identity without permission.
* Please do not disclose the issue publicly (including in a fork, gist, or write-up) before public disclosure or advisory publication — this protects users who have not yet updated.

## Questions

For non-sensitive questions about this policy, open a regular issue. For sensitive details, use the private channel above.

