---
id: s-4f5f215e60
aliases: [s-4f5f215e60, 1880-census-fairview-hartley]
title: 1880 U.S. Census - Hartley household, Fairview, Kansas
source_type: census
source_date: 1880-06
source_class: original
repository: National Archives microfilm, via FamilySearch
citation: >
  1880 U.S. Federal Census, Fairview City, Breton County, Kansas, enumeration district 41, page 12, dwelling 96, Thomas Hartley household (fictional example record).
people:
  - "[[p-de957bcda1|Thomas Edward Hartley]]"
  - "[[p-cd795c61e0|Margaret A. Cole]]"
  - "[[p-c4b26bb4bc|Ethel Hartley]]"
  - "[[p-83e768cacb|Frances Hartley]]"
places:
  - "[[l-7c1a9f4e22|Fairview]]"
files:
  - file: documents/census/1880-census-fairview-hartley-household_s-4f5f215e60.txt
    is_primary: true
    original_filename: 1880-census-fairview-hartley.txt
ai_passes:
  - date: '2026-06-12'
    by: claude-sonnet-4-6
    task: claims
    note: drafted claims from the census transcription
created: 2026-06-12
---

> **This source's record - yours to write.** The citation and claims for one piece
> of evidence. `fha process` scaffolded this file; everything below is yours to
> correct and add to.

## Claims
```yaml
- value: Hartley household residing in Fairview, Kansas (1880)
  id: c-a63ebf9152
  place: l-7c1a9f4e22
  type: residence
  persons:
    - "[[p-de957bcda1|Thomas Edward Hartley]]"
    - "[[p-cd795c61e0|Margaret A. Cole]]"
    - "[[p-c4b26bb4bc|Ethel Hartley]]"
    - "[[p-83e768cacb|Frances Hartley]]"
  date: 1880-06
  status: accepted
  reviewed: 2026-06-12
  reviewed_by: human
  confidence: medium
  notes: >
    Head-of-household enumeration; the standard residence claim for the family in 1880.
  roles:
    head: p-de957bcda1
    household_member: [p-cd795c61e0, p-c4b26bb4bc, p-83e768cacb]
  place_text: Fairview City, Breton, Kansas
  information: primary
  evidence: direct

- value: 'Thomas Hartley - occupation: bookkeeper, Plains Junction Railroad'
  id: c-d22ce0ac77
  place: l-7c1a9f4e22
  type: occupation
  persons:
    - "[[p-de957bcda1|Thomas Edward Hartley]]"
  date: 1880-06
  status: accepted
  reviewed: 2026-06-12
  reviewed_by: human
  confidence: medium
  notes: >
    Occupation as recorded by the enumerator: "Book Keeper, Plains Junction RR".
  place_text: Fairview City, Breton, Kansas
  information: primary
  evidence: direct

- value: Thomas Hartley born about 1840 in New York
  id: c-1b9d4e7a30
  type: birth
  persons:
    - "[[p-de957bcda1|Thomas Edward Hartley]]"
  date: 1840~
  status: accepted
  corroborates: [c-2d5g8j1k4m]
  reviewed: 2026-06-12
  reviewed_by: human
  confidence: medium
  notes: >
    Worked out from his age (40) and birthplace column in June 1880; no birth record
    located.
  place_text: New York
  information: secondary
  evidence: indirect

- value: Ethel Hartley is a child of Thomas Hartley and Margaret Cole
  id: c-bdfbce2a11
  type: relationship
  persons:
    - "[[p-c4b26bb4bc|Ethel Hartley]]"
    - "[[p-de957bcda1|Thomas Edward Hartley]]"
    - "[[p-cd795c61e0|Margaret A. Cole]]"
  date: 1880-06
  status: accepted
  reviewed: 2026-06-12
  reviewed_by: human
  confidence: medium
  notes: >
    The 1880 schedule names Ethel as daughter of the head; Margaret is listed as his wife.
  subtype: biological
  roles:
    child: p-c4b26bb4bc
    parent: [p-de957bcda1, p-cd795c61e0]
  information: primary
  evidence: direct

- value: Frances Hartley is a child of Thomas Hartley and Margaret Cole
  id: c-77a0c5e218
  type: relationship
  persons:
    - "[[p-83e768cacb|Frances Hartley]]"
    - "[[p-de957bcda1|Thomas Edward Hartley]]"
    - "[[p-cd795c61e0|Margaret A. Cole]]"
  date: 1880-06
  status: accepted
  reviewed: 2026-06-12
  reviewed_by: human
  confidence: medium
  notes: >
    The 1880 schedule names Frances as daughter of the head; Margaret is listed as his wife.
  subtype: biological
  roles:
    child: p-83e768cacb
    parent: [p-de957bcda1, p-cd795c61e0]
  information: primary
  evidence: direct
```

## Notes
Fictional census household used throughout the example. The 1880 schedule is the first federal census to name each person's relationship to the head.
