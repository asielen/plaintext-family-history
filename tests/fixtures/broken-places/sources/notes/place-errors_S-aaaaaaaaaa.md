---
id: S-aaaaaaaaaa
title: Place lint broken fixture
source_type: other
source_date: 1900
source_class: derivative
repository: fixture
created: 2026-06-21
---

## Claims
```yaml
- id: C-aaaaaaaaaa
  type: residence
  persons: [P-aaaaaaaaaa]
  value: "Test Person lived in Missing Place"
  place: L-cccccccccc
  place_text: "Missing Place"
  status: suggested
  information: secondary
  evidence: indirect
```

## Notes

Fixture for `fha places lint`: one dangling `within:` link and one orphan
claim `place:`.
