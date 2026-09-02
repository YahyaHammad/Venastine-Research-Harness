---
name: technical-writing
description: Writing up a result so a reader can act on it — the claim first, the evidence attached, and the limitation stated rather than implied
additional_tools: [read, web_search]
---

## Technical writing methodology

### Lead with the finding

The first sentence should be the thing you learned, not the thing you did.
"Compaction folded a 1M-window thread at 4% of its window" is a finding;
"we investigated compaction behaviour" is a table of contents. A reader
who stops after one paragraph should still have the result.

### Attach the evidence to the claim

Every non-obvious assertion carries, in the same place, what supports it:
the measurement, the file and line, the citation, the command. Evidence
gathered into a section at the end forces the reader to reconstruct which
support belongs to which claim, and most will not.

### State limitations where they bite

A limitation section at the end is read as a formality. The same sentence
placed beside the claim it qualifies changes how the claim is read, which
is the point of writing it at all. "On this dataset only" belongs in the
sentence, not on page nine.

### Distinguish measured, inferred and assumed

Use different words for them and use them consistently. A modelled number
in the same voice as a measured one is the most reliable way to make work
look sturdier than it is, and it is almost always accidental.

### Write the negative results

What did not work, what was tried and abandoned, and why. It is the part
most often cut and the part most useful to the next person, who will
otherwise spend their time rediscovering it. A short paragraph is enough.

### One idea per paragraph, and put it first

The reader is skimming. If the first sentence of each paragraph carries
its point, skimming works and the document survives being read properly
later. If the point arrives at the end of the paragraph, the skim returns
nothing.

### Define a term once, then be boring

Pick one name for each thing and reuse it. Elegant variation — calling the
same object three different names across four paragraphs — reads well in
prose and is actively harmful in technical writing, because the reader
cannot tell whether you meant something different.

### Say what you would need to be more confident

Ending with the specific thing that would settle the remaining doubt is
more useful than a summary that restates what the reader just read.
