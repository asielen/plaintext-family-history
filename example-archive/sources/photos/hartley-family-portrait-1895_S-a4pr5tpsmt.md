---
id: S-a4pr5tpsmt
aliases: [S-a4pr5tpsmt]
title: Hartley family portrait, circa 1895
source_type: photo
source_date: 1895~
source_class: original
repository: family collection
citation: >
  Studio portrait of the Thomas Hartley family, Fairview, Kansas, circa 1895
  (fictional fixture).
people:
  - "[[P-de957bcda1|Thomas Edward Hartley]]"
  - "[[P-cd795c61e0|Margaret A. Cole]]"
  - "[[P-c4b26bb4bc|Ethel Hartley]]"
  - "[[P-83e768cacb|Frances Hartley]]"
  - "[[P-fa7541e871|Calvin Hartley]]"
places:
  - "[[L-7c1a9f4e22|Fairview]]"
files:
  - file: photos/1895/hartley-family-portrait.jpg
    role: front
    status: missing-fixture   # SPEC: allowed only in example-archive/ and tests/fixtures/
created: 2026-06-30
---

<!-- A PHOTO source (SPEC §12.1, §13). Photos-root files are NEVER renamed by us -
     so this record points at the photo's existing path as a HINT, and the durable
     identity lives in the photo's embedded `SOURCE: S-a4pr5tpsmt` keyword (written
     via exiftool at processing), not in the filename. A fictional fixture; the image
     itself is absent (this example ships no binaries). -->

## Claims
```yaml
- value: "Studio portrait depicting Thomas Hartley, Margaret Cole, and children Ethel, Frances, and Calvin"
  id: C-r6r1xk7aym
  type: note
  subtype: depiction
  persons: [P-de957bcda1, P-cd795c61e0, P-c4b26bb4bc, P-83e768cacb, P-fa7541e871]
  date: 1895~
  place: L-7c1a9f4e22
  place_text: "Fairview, Kansas"
  status: accepted
  reviewed: 2026-07-16
  confidence: medium
  information: secondary
  evidence: direct
  notes: >
    Who-is-depicted recorded as a note claim (subtype: depiction). Left as
    `status: suggested` - identification of the children is the family's, not yet
    confirmed against a labeled copy. A photo can carry claims like any other source.
```

## Notes
Fictional photo source demonstrating the photos-root conventions: never-rename,
the `SOURCE:` keyword as the identity carrier, the record's `files:` inventory as
a last-known-path hint, and a `note`/depiction claim for the people pictured.
