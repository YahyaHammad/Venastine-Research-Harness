---
name: cybersecurity-research
description: Conventions for researching vulnerabilities, exploits and defensive posture
additional_tools: [web_search, fetch_url, arxiv_search]
---

## Cybersecurity research methodology

### Establish the authorization context first

Before analysing any specific system, state what kind of work this is:
defensive analysis, an authorized assessment, CTF, or study of published
research. If the request implies acting against a system the user has not
established authorization for, say so in one sentence and continue with
the defensive or educational framing rather than refusing outright.

### Name versions, not products

"Log4j is vulnerable" is not a finding. A finding names the affected
versions, the fixed version, and the condition required to reach the
vulnerable code path. A claim that omits the version range is unverified
by construction — treat it as a lead, not a result.

### Prefer primary sources, in this order

1. The vendor advisory or the maintainer's own commit and release notes.
2. The CVE record, and the NVD entry for the CVSS vector.
3. The original researcher's write-up.
4. Aggregators and news coverage — useful for discovering that something
   exists, never sufficient for asserting what it does.

When two sources disagree about a version range or a severity, report the
disagreement rather than picking. Disagreement between an advisory and NVD
is common and is itself a finding.

### Separate what is proven from what is plausible

Three distinct claims that get conflated constantly:

- **Exploitable** — a working technique exists and its preconditions are
  documented.
- **Vulnerable** — the flawed code is present and reachable.
- **Affected** — the component is present somewhere in the dependency
  tree, possibly on a path nothing calls.

State which one you mean. Escalating "affected" to "exploitable" in a
summary line is the most common failure in this domain.

### Score honestly

If you cite a CVSS score, cite the vector string with it, and say whose
score it is — vendor and NVD scores for the same CVE routinely differ.
Environmental factors (is it internet-facing, is authentication required
in this deployment) usually matter more than the base score, and you
generally do not know them; say so instead of implying the base score is
the risk.

### Writing up

Lead with what an operator should do, then the evidence for it. Include
the "how do I know I am affected" check — a version query, a config
inspection, a log signature — because a report that cannot be acted on is
a summary of someone else's work.
