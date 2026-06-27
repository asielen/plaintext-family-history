---
id: S-fe5678901f
aliases: [S-fe5678901f]
title: Obituary — James Robert Bradford, Fairview Gazette, November 2015
source_type: newspaper
source_date: 2015-11-07
source_class: original
repository: example collection
citation: >
  "James Robert Bradford, 1955–2015," Fairview Gazette, 7 November 2015, p. 4.
  Fictional example record.
people:
  - "[[P-2b3c4d5e6f|James Robert Bradford]]"
  - "[[P-3c4d5e6f7g|Carol Anne Simmons]]"
  - "[[P-0a1b2c3d4e|Alex James Bradford]]"
files:
  - file: documents/newspapers/fairview-gazette-bradford-obituary-2015_S-fe5678901f.txt
    role: clipping
    status: missing-fixture   # SPEC: allowed only in example-archive/ and tests/fixtures/
created: 2026-06-14
---

## Claims
```yaml
- value: "James Robert Bradford died 3 November 2015, Topeka, Kansas"
  id: C-fe0000001a
  type: death
  persons: [P-2b3c4d5e6f]
  date: 2015-11-03
  place_text: "Topeka, Kansas"
  status: accepted
  reviewed: 2026-06-14
  confidence: high
  information: secondary
  evidence: direct
  notes: Obituary states date and place of death; secondary source but near-primary for an obituary.

- value: "James Bradford — occupation: history teacher, then principal, Fairview Consolidated High School"
  id: C-fe0000002b
  type: occupation
  persons: [P-2b3c4d5e6f]
  date: 1978/2012
  place: L-7c1a9f4e22
  place_text: "Fairview, Breton County, Kansas"
  status: accepted
  reviewed: 2026-06-14
  confidence: medium
  information: secondary
  evidence: direct
  notes: >
    Obituary states he taught history for 20 years, then served as principal until 2012.
    Dates are derived from the obituary narrative; an independent personnel record would
    confirm exact start year.

- value: "James Bradford — education: BA History, Kansas State University, 1977"
  id: C-fe0000003c
  type: education
  persons: [P-2b3c4d5e6f]
  date: 1977~
  place_text: "Manhattan, Kansas"
  status: accepted
  reviewed: 2026-06-14
  confidence: medium
  information: secondary
  evidence: direct
  notes: Stated in obituary; degree type and year as reported, not independently verified.
```

## Notes
Fictional newspaper obituary used to exercise the schema for secondary biographical sources —
death, occupation, and education claims derived from published newspaper text.
