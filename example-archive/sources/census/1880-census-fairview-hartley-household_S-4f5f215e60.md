---
id: S-4f5f215e60
aliases: [S-4f5f215e60, C-d22ce0ac77]
title: 1880 U.S. Census - Hartley household, Fairview, Kansas
source_type: census
source_date: 1880-06~
source_class: original
repository: example collection
citation: >
  1880 U.S. Federal Census, Fairview, Breton County, Kansas (fictional example record).
people:
  - "[[P-de957bcda1|Thomas Edward Hartley]]"
  - "[[P-cd795c61e0|Margaret A. Cole]]"
  - "[[P-c4b26bb4bc|Ethel Hartley]]"
  - "[[P-83e768cacb|Frances Hartley]]"
places:
  - "[[L-7c1a9f4e22|Fairview]]"
files:
  - file: documents/census/1880-fairview-hartley_S-4f5f215e60.txt
    role: page-1
    status: missing-fixture   # SPEC: allowed only in example-archive/ and tests/fixtures/
created: 2026-06-12
---

## Claims
```yaml
- value: "Hartley household residing in Fairview, Kansas (1880)"
  id: C-a63ebf9152
  type: residence
  persons: [P-de957bcda1, P-cd795c61e0, P-c4b26bb4bc, P-83e768cacb]
  roles:
    head: P-de957bcda1
    household_member: [P-cd795c61e0, P-c4b26bb4bc, P-83e768cacb]
  date: 1880-06~
  place: L-7c1a9f4e22
  place_text: "Fairview City, Breton, Kansas"
  status: accepted
  reviewed: 2026-06-12
  confidence: medium
  information: primary
  evidence: direct
  notes: Head-of-household enumeration; the standard residence claim for the family in 1880.

- value: "Thomas Hartley - occupation: bookkeeper, Plains Junction Railroad"
  id: C-d22ce0ac77
  type: occupation
  persons: [P-de957bcda1]
  date: 1880-06~
  place: L-7c1a9f4e22
  place_text: "Fairview City, Breton, Kansas"
  status: accepted
  reviewed: 2026-06-12
  confidence: medium
  information: primary
  evidence: direct
  notes: Occupation as recorded by the enumerator.

- value: "Thomas Hartley born about 1840 in New York"
  id: C-1b9d4e7a30
  type: birth
  persons: [P-de957bcda1]
  date: 1840~
  place_text: "New York"
  status: accepted
  reviewed: 2026-06-12
  confidence: medium
  information: secondary
  evidence: indirect
  notes: Inferred from age and birthplace columns in the 1880 census; no birth record located.

- value: "Ethel Hartley is a child of Thomas Hartley and Margaret Cole"
  id: C-bdfbce2a11
  type: relationship
  subtype: biological
  persons: [P-c4b26bb4bc, P-de957bcda1, P-cd795c61e0]
  roles:
    child: P-c4b26bb4bc
    parent: [P-de957bcda1, P-cd795c61e0]
  date: 1880-06~
  status: accepted
  reviewed: 2026-06-12
  confidence: medium
  information: secondary
  evidence: indirect
  notes: Relationship inferred from household enumeration.

- value: "Frances Hartley is a child of Thomas Hartley and Margaret Cole"
  id: C-77a0c5e218
  type: relationship
  subtype: biological
  persons: [P-83e768cacb, P-de957bcda1, P-cd795c61e0]
  roles:
    child: P-83e768cacb
    parent: [P-de957bcda1, P-cd795c61e0]
  date: 1880-06~
  status: accepted
  reviewed: 2026-06-12
  confidence: medium
  information: secondary
  evidence: indirect
  notes: Relationship inferred from household enumeration.

- value: "Thomas Hartley is a child of Caleb Comstock Hartley and Chastina Augusta Reed"
  id: C-2e6b1f9c45
  type: relationship
  subtype: biological
  persons: [P-de957bcda1, P-075114a0f8, P-d00c678c1a]
  roles:
    child: P-de957bcda1
    parent: [P-075114a0f8, P-d00c678c1a]
  date: 1880-06~
  status: accepted
  reviewed: 2026-06-12
  confidence: low
  information: secondary
  evidence: indirect
  notes: Parentage placeholder for the example; in a real archive this would rest on a vital record or proof argument, not a census alone.

- value: Lived at 16 Lake Vista
  id: C-1q49bbb6r3
  type: residence
  persons: [P-de957bcda1]
  date: 1989
  place_text: daly City
  status: rejected
  confidence: medium
  reviewed: 2026-07-17
  notes: >
    Test entry typed during a live workbench review session (2026-07-16), not an
    assertion of this source - the 1989 date cannot belong to a person born about
    1840. Kept as rejected to preserve the review trail.
```

## Notes
Fictional census source used to exercise the schema (roles, place_text, Mills fields, negative/indirect evidence, accepted lifecycle).
