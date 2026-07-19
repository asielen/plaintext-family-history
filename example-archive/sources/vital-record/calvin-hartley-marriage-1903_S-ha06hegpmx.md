---
id: S-ha06hegpmx
aliases: [S-ha06hegpmx]
title: Marriage certificate - Calvin Hartley & Louisa Denton, 1903
source_type: vital-record
source_date: 1903-09-10
source_class: original
repository: example collection
citation: >
  Breton County Clerk, Marriage Register Vol. 5, p. 47 (Calvin Hartley & Louisa May
  Denton, 10 September 1903, Fairview, Kansas). Fictional example record.
people:
  - "[[P-fa7541e871|Calvin George Hartley]]"
  - "[[P-nf819w33dy|Louisa May Denton]]"
  - "[[P-htyn3mpg9g|Ada Jane Hartley]]"
files:
  - file: documents/vital-records/calvin-louisa-marriage-1903_S-ha06hegpmx.txt
    role: transcription
    status: missing-fixture   # SPEC: allowed only in example-archive/ and tests/fixtures/
created: 2026-07-17
---

## Claims
```yaml
- value: "Calvin George Hartley married Louisa May Denton, 10 September 1903, Fairview, Kansas"
  id: C-ks3xwbqnwk
  type: marriage
  persons: [P-fa7541e871, P-nf819w33dy]
  roles:
    spouse: [P-fa7541e871, P-nf819w33dy]
  date: 1903-09-10
  place: L-7c1a9f4e22
  place_text: "Fairview, Breton County, Kansas"
  status: accepted
  reviewed: 2026-07-17
  confidence: high
  information: primary
  evidence: direct
  notes: >
    Calvin's second marriage, two years after Harriet Webb's death; register entry
    recorded by the county clerk.

- value: "Ada Jane Hartley is a child of Calvin George Hartley and Louisa May Denton"
  id: C-5taq0e7251
  type: relationship
  subtype: biological
  persons: [P-htyn3mpg9g, P-fa7541e871, P-nf819w33dy]
  roles:
    child: P-htyn3mpg9g
    parent: [P-fa7541e871, P-nf819w33dy]
  status: accepted
  reviewed: 2026-07-17
  confidence: medium
  information: secondary
  evidence: indirect
  notes: >
    Ada named as daughter of this couple in the register's margin annotation
    (fictional); her birth itself is not yet backed by a record.
```

## Notes
Fictional second-marriage certificate. Together with S-fb2345678c (Calvin & Harriet, 1898)
it gives Calvin children by two spouses - the fixture case for the person-page family
chart's couples-first routing: each couple joins at its own junction before one line
splits to that couple's own children.
