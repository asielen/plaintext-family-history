---
id: S-fa1234567b
aliases: [S-fa1234567b]
title: Hartley family notes — typescript, circa 1940
source_type: other
source_date: 1940~
source_class: derivative
repository: example collection
citation: >
  Hartley, unknown compiler. "Hartley Family Notes," typescript, ca. 1940 (fictional
  example document; transcription held in example collection).
people:
  - "[[P-de957bcda1|Thomas Edward Hartley]]"
  - "[[P-cd795c61e0|Margaret A. Cole]]"
  - "[[P-fa7541e871|Calvin George Hartley]]"
  - "[[P-8h9jkambne|Harriet Frances Webb]]"
  - "[[P-6f7g8h9jka|Warren Calvin Hartley]]"
files:
  - file: documents/transcripts/hartley-family-notes_S-fa1234567b.txt
    role: transcription
    status: fixture   # actual .txt created; fictional content
created: 2026-06-14
---

## Claims
```yaml
- value: "Calvin George Hartley is a child of Thomas Edward Hartley and Margaret A. Cole"
  id: C-fa0000001a
  type: relationship
  subtype: child-of
  persons: [P-fa7541e871, P-de957bcda1, P-cd795c61e0]
  roles:
    child: P-fa7541e871
    parent: [P-de957bcda1, P-cd795c61e0]
  status: accepted
  reviewed: 2026-06-14
  confidence: medium
  information: secondary
  evidence: indirect
  notes: >
    Compiler's typescript lists Calvin as a son of Thomas and Margaret; no independent
    vital record yet located to confirm. Consistent with approximate birth years.

- value: "Warren Calvin Hartley is a child of Calvin George Hartley and Harriet Frances Webb"
  id: C-fa0000002b
  type: relationship
  subtype: child-of
  persons: [P-6f7g8h9jka, P-fa7541e871, P-8h9jkambne]
  roles:
    child: P-6f7g8h9jka
    parent: [P-fa7541e871, P-8h9jkambne]
  status: accepted
  reviewed: 2026-06-14
  confidence: medium
  information: secondary
  evidence: indirect
  notes: >
    Same typescript source; Warren listed as Calvin's son by Harriet Webb. Marriage record
    (S-fb2345678c) corroborates the Calvin–Harriet union.
```

## Notes
Fictional typescript genealogy document used to exercise the schema for derivative sources
and multi-generation relationship chains. The compiler's identity and date are unknown;
the circa-1940 date is estimated from paper type and typeface.
