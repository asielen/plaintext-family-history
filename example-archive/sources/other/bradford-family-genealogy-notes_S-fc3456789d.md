---
id: S-fc3456789d
aliases: [S-fc3456789d]
title: Bradford family genealogy notes - handwritten, undated
source_type: other
source_date: 1980~
source_class: derivative
repository: example collection
citation: >
  Bradford, Carol A. "Bradford Family Genealogy Notes," handwritten manuscript, ca. 1980
  (fictional example document; photocopy held in example collection).
people:
  - "[[P-4d5e6f7g8h|George Arthur Bradford]]"
  - "[[P-5e6f7g8h9j|Edith Louise Hartley]]"
  - "[[P-6f7g8h9jka|Warren Calvin Hartley]]"
  - "[[P-7g8h9jkamb|Clara Mabel Frost]]"
  - "[[P-2b3c4d5e6f|James Robert Bradford]]"
  - "[[P-3c4d5e6f7g|Carol Anne Simmons]]"
  - "[[P-0a1b2c3d4e|Alex James Bradford]]"
files:
  - file: documents/transcripts/bradford-family-genealogy-notes_S-fc3456789d.txt
    role: transcription
    status: missing-fixture   # SPEC: allowed only in example-archive/ and tests/fixtures/
created: 2026-06-14
---

## Claims
```yaml
- value: "Edith Louise Hartley is a child of Warren Calvin Hartley and Clara Mabel Frost"
  id: C-fc0000001a
  type: relationship
  subtype: biological
  persons: [P-5e6f7g8h9j, P-6f7g8h9jka, P-7g8h9jkamb]
  roles:
    child: P-5e6f7g8h9j
    parent: [P-6f7g8h9jka, P-7g8h9jkamb]
  status: accepted
  reviewed: 2026-06-14
  confidence: medium
  information: secondary
  evidence: indirect
  notes: Stated in Carol Bradford's handwritten notes; Edith's birth not yet verified by independent record.

- value: "James Robert Bradford is a child of George Arthur Bradford and Edith Louise Hartley"
  id: C-fc0000002b
  type: relationship
  subtype: biological
  persons: [P-2b3c4d5e6f, P-4d5e6f7g8h, P-5e6f7g8h9j]
  roles:
    child: P-2b3c4d5e6f
    parent: [P-4d5e6f7g8h, P-5e6f7g8h9j]
  status: accepted
  reviewed: 2026-06-14
  confidence: high
  information: secondary
  evidence: direct
  notes: Both James's birth certificate (S-fd4567890e) and these family notes agree on parentage.

- value: "Alex James Bradford is a child of James Robert Bradford and Carol Anne Simmons"
  id: C-fc0000003c
  type: relationship
  subtype: biological
  persons: [P-0a1b2c3d4e, P-2b3c4d5e6f, P-3c4d5e6f7g]
  roles:
    child: P-0a1b2c3d4e
    parent: [P-2b3c4d5e6f, P-3c4d5e6f7g]
  status: accepted
  reviewed: 2026-06-14
  confidence: high
  information: secondary
  evidence: direct
  notes: Noted by Carol Bradford; corroborated by the obituary (S-fe5678901f) naming Alex as survivor.

- value: "James Robert Bradford married Carol Anne Simmons, 14 June 1979, Fairview, Kansas"
  id: C-fc0000004d
  type: marriage
  persons: [P-2b3c4d5e6f, P-3c4d5e6f7g]
  roles:
    spouse: [P-2b3c4d5e6f, P-3c4d5e6f7g]
  date: 1979-06-14
  place: L-7c1a9f4e22
  place_text: "Fairview, Breton County, Kansas"
  status: accepted
  reviewed: 2026-06-14
  confidence: high
  information: secondary
  evidence: direct
  notes: >
    Date and place noted by Carol Bradford. Marriage license (not separately located) would
    confirm; treated as high-confidence given that the author was the bride.

- value: "P-4d5e6f7g8h and P-5e6f7g8h9j: neighbor (co-occurrence confirmed)"
  id: C-sz2t3memxt
  type: relationship
  subtype: neighbor
  persons: [P-4d5e6f7g8h, P-5e6f7g8h9j]
  roles:
    neighbor: [P-4d5e6f7g8h, P-5e6f7g8h9j]
  status: accepted
  reviewed: 2026-07-16
  confidence: medium
  information: secondary
  evidence: indirect
  notes: >
    Social tie (neighbor) suggested by co-occurrence in shared sources and
    confirmed by a human from this source.
```

## Notes
Fictional handwritten notes compiled by Carol Bradford, ca. 1980, covering three generations
of the Bradford family. Used to exercise derivative-source schema and multi-person claims.
