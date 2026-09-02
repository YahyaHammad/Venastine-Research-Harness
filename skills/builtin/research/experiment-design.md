---
name: experiment-design
description: Designing a comparison that can come out either way — controls, confounds, and stating in advance what would falsify the hypothesis
additional_tools: [probability_stats, arxiv_search]
---

## Experiment design methodology

### Write the hypothesis and its negation

State what you expect, and state what the world looks like if you are
wrong. A hypothesis with no articulable negation is not being tested, it
is being illustrated. Write both before collecting anything, because after
the data arrives every result looks like it was predicted.

### Name the comparison

An experiment measures a difference, so say against what. "The new method
performs well" is not a result; "the new method beats the current one on
this metric, on this data, by this much" is. If there is no control
condition, there is no experiment — there is a demonstration, which is a
legitimate thing to run and a different thing to report.

### Enumerate the confounds before you run it

List everything that differs between conditions besides the variable you
care about: hardware, ordering, time of day, who ran it, which data
arrived first, warm caches. Then say, for each, whether you controlled it,
randomised it, or accepted it. The ones you accepted belong in the write
up, not in a footnote.

Ordering and warm state are the two that catch experienced people
repeatedly.

### Fix the analysis before the data exists

Decide the metric, the exclusion rule and the stopping rule in advance.
Every one of those chosen after seeing results is a degree of freedom that
inflates the apparent effect, and none of them feels like cheating at the
time — dropping "the obviously broken run" is exactly how it happens.

If you change the plan after looking, say that you did and report both
analyses.

### Estimate what the experiment can detect

Before running, work out roughly how large an effect this sample could
distinguish from noise. An underpowered experiment that returns "no
difference" has told you nothing, and it is routinely reported as though
it had told you something.

### Decide what a null result means

Say in advance what you will conclude, and do, if the effect is absent. An
experiment whose only useful outcome is confirmation will keep being rerun
with small variations until it confirms.

### Record the conditions, not just the numbers

Versions, seeds, hardware, data snapshot, command line. A result whose
conditions were not recorded cannot be replicated by you either, three
weeks later.
