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

Note that both settings live in `config.py` (`SHELL_APPROVAL_MODE` at `config.py:245`, `ToolPermissions` at `config.py:640`) — neither has a `settings.json` key or an environment override, by design.

## No bounty

This is an independent research project with no funding — **we cannot offer monetary rewards, bounties, or swag.** We will credit you publicly if you want, and we will prioritize a fix. If you need a bounty, please use a third-party program that covers this repo, or treat the report as a voluntary disclosure.

## Disclosure and credit

* We will publish a GitHub Security Advisory (GHSA) 7 days after the fix ships to main (or at the 90-day mark), requesting a CVE if the severity warrants it.
* We will not disclose your identity without permission.
* Please do not disclose the issue publicly (including in a fork, gist, or write-up) before public disclosure or advisory publication — this protects users who have not yet updated.

## Questions

For non-sensitive questions about this policy, open a regular issue. For sensitive details, use the private channel above.

