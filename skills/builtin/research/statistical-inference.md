---
name: statistical-inference
description: Reasoning from data to a claim without overstating it — effect sizes, uncertainty, multiplicity, and what a p-value does not say
additional_tools: [probability_stats, symbolic_math]
---

## Statistical inference methodology

### Report the effect, then the uncertainty, then the test

The size of the difference is the finding. The interval around it is how
much to trust the finding. A test statistic is the least informative of
the three and is routinely reported alone. Lead with "3.2 points better,
95% CI [0.4, 6.0]", not with "significant".

### Say what the p-value is about

It is the probability of data at least this extreme under the null
hypothesis. It is not the probability that the null is true, not the
probability the result will replicate, and not a measure of effect size. A
tiny p-value from a huge sample can accompany an effect too small to
matter, and that combination is one of the most common ways a true
statement misleads.

### Count every comparison you made

Testing twenty things at the 0.05 level and reporting the one that came
out is not a finding, whether the other nineteen were preregistered,
exploratory, or merely looked at. Say how many comparisons were made and
correct for them, or label the result exploratory and say it needs
confirmation on new data.

Subgroups, alternative metrics, and "we also tried" all count.

### Check the assumptions the method actually makes

Independence is the one that fails most often and is questioned least:
repeated measures on the same subject, runs on the same machine, samples
drawn from one crawl. Normality usually matters less than people fear;
independence usually matters more.

Say which assumptions you checked and which you assumed.

### Absence of significance is not evidence of absence

A non-significant result with a wide interval is compatible with a large
effect. Report the interval and say what the study could have detected. If
you mean "we can rule out an effect larger than X", say that — it is a
much stronger and much more useful claim, and it requires the interval to
support it.

### Keep the estimate separate from the decision

"How large is the effect" and "what should we do" are different questions
with different inputs. A threshold that decides an action belongs to the
decision, with its costs stated, not smuggled into the analysis as though
it were a property of the data.

### Distinguish measured, modelled and assumed

At each number, say which it is. A modelled quantity presented in the same
voice as a measured one is the most reliable way to make an analysis look
sturdier than it is.
