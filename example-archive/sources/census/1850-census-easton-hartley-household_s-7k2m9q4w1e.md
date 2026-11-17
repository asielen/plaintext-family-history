---
id: s-7k2m9q4w1e
aliases: [s-7k2m9q4w1e, 1850-census-easton-hartley]
title: 1850 U.S. Census - Caleb Hartley household, Easton, New York
source_type: census
source_date: '1850-08-14'
source_class: original
repository: National Archives microfilm, via FamilySearch
citation: >
  1850 U.S. Federal Census, Easton, Washington County, New York, page 207, dwelling 312, Caleb C. Hartley household (fictional example record).
people:
  - "[[p-075114a0f8|Caleb Comstock Hartley]]"
  - "[[p-d00c678c1a|Chastina Augusta Reed]]"
  - "[[p-de957bcda1|Thomas Edward Hartley]]"
files:
  - file: documents/census/1850-census-easton-hartley-household_s-7k2m9q4w1e.txt
    is_primary: true
    original_filename: 1850-census-easton-hartley.txt
ai_passes:
  - date: '2026-07-02'
    by: claude-sonnet-4-6
    task: claims
    note: drafted claims from the census transcription
created: 2026-07-02
---

> **This source's record - yours to write.** The citation and claims for one piece
> of evidence. `fha process` scaffolded this file; everything below is yours to
> correct and add to.

## Claims
```yaml
- value: Caleb C. Hartley household residing in Easton, New York (1850)
  id: c-9r4t7y2b6h
  type: residence
  persons:
    - "[[p-075114a0f8|Caleb Comstock Hartley]]"
    - "[[p-d00c678c1a|Chastina Augusta Reed]]"
    - "[[p-de957bcda1|Thomas Edward Hartley]]"
  date: 1850-08-14
  status: accepted
  reviewed: 2026-07-02
  reviewed_by: human
  confidence: medium
  notes: >
    Caleb (40, farmer), Chastina A. (36), Thomas E. (10) and Mary J. (7) in one dwelling.
    The 1850 schedule does not state relationships.
  roles:
    head: p-075114a0f8
    household_member: [p-d00c678c1a, p-de957bcda1]
  place_text: Easton, Washington County, New York
  information: primary
  evidence: direct

- value: Thomas E. Hartley born about 1840 in New York
  id: c-2d5g8j1k4m
  type: birth
  persons:
    - "[[p-de957bcda1|Thomas Edward Hartley]]"
  date: 1840~
  status: accepted
  corroborates: [c-1b9d4e7a30]
  reviewed: 2026-07-02
  reviewed_by: human
  confidence: medium
  notes: >
    Age 10 and birthplace New York in August 1850 - the same year and state the 1880 census
    gives.
  place_text: New York
  information: secondary
  evidence: indirect
```

## Notes
Found while testing the working answer to "Who were Thomas Edward Hartley's parents?" (notes/questions.md). The 1850 schedule lists a household but not how its members are related, so it places Thomas with Caleb and Chastina without calling him their son.
