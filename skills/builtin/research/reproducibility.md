---
name: reproducibility
description: Making a result something another person can obtain again — pinned environments, recorded seeds, and provenance for every input
additional_tools: [shell, read]
---

## Reproducibility methodology

### The bar is another person, on another machine, later

Not "it works here". Everything that would differ between your machine and
theirs is either pinned or is a reason the result will not reproduce:
package versions, interpreter version, operating system, hardware, locale,
environment variables, the data.

### Pin the environment by exact version

A range resolves differently next month, so a lockfile or an exact pin is
what makes the record real. Record the resolved versions that actually
ran, not the constraints you asked for — those are different documents,
and only the first one reproduces anything.

### Record the seed, and check that it is the only source of randomness

Set it and log it. Then confirm the run is actually deterministic by
running it twice: seeds are routinely set for one library while a second
source of randomness — hash ordering, thread scheduling, a GPU kernel, a
shuffled data loader — goes unseeded. A recorded seed that does not
determine the output is worse than none, because it looks like control.

### Give every input a provenance

For each dataset: where it came from, when it was retrieved, which version
or snapshot, and what was done to it before it reached the analysis.
"Downloaded from the project website" is not provenance if the file at
that URL changes. Hash it.

### Separate the raw data from every derived form

Never modify the original in place. Derived files are outputs, regenerable
from the raw input by a recorded command; if a derived file cannot be
regenerated, it has become raw data with no provenance.

### Make the whole path executable

One command that goes from inputs to results. A sequence of manual steps
in a README drifts from what was actually run, silently, and the drift is
discovered only when someone tries to reproduce the result and fails —
usually the author, months later.

### Report what did not reproduce

When a rerun gives a different number, that is a finding and belongs in
the write up with its magnitude. Quietly rerunning until it matches is the
failure mode this whole methodology exists to prevent.
