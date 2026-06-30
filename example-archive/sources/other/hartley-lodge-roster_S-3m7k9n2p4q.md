---
id: S-3m7k9n2p4q
aliases:
  - S-3m7k9n2p4q
title: Fairview Masonic Lodge roster, 1882
source_type: other
source_date: 1882
repository: family collection
citation: >
  Membership roster, Fairview Masonic Lodge No. 12, Fairview, Kansas, 1882
  (fictional fixture).
people:
  - "[[P-de957bcda1|Thomas Edward Hartley]]"
  - "[[P-ajnng40q36|Charley Layng]]"
files:
  - file: documents/other/hartley-lodge-roster-1882_S-3m7k9n2p4q.md
    role: transcript
    status: missing-fixture
    derived: true
created: 2026-06-29
---

## Claims
```yaml
- value: "Member, Fairview Masonic Lodge No. 12 (1882 roster)"
  id: C-4q2p9k3n7m
  type: relationship
  subtype: member-of
  persons: [P-de957bcda1]
  roles: {member: P-de957bcda1}
  value_org: "Fairview Masonic Lodge No. 12"
  date: 1882
  status: accepted
  reviewed: 2026-06-29
  confidence: high
  information: secondary
  evidence: direct
  notes: >
    Listed on the 1882 lodge roster. A membership/affiliation modeled as a
    relationship claim (subtype: member-of); the organization stays a value
    (value_org), not a record. Fictional fixture.

- value: "Charley Layng, member, Fairview Masonic Lodge No. 12 (1882 roster)"
  id: C-4dx3aydmwk
  type: relationship
  subtype: member-of
  persons: [P-ajnng40q36]
  roles: {member: P-ajnng40q36}
  value_org: "Fairview Masonic Lodge No. 12"
  date: 1882
  status: accepted
  reviewed: 2026-06-30
  confidence: high
  information: secondary
  evidence: direct
  notes: >
    A second member from the same roster, so the example carries a real
    connections-tier person (P-ajnng40q36) anchored to couple 040.

- value: "Thomas Hartley and Charley Layng were fellow lodge members (associates)"
  id: C-bfsjthr6sy
  type: relationship
  subtype: associate
  persons: [P-de957bcda1, P-ajnng40q36]
  roles: {associate: [P-de957bcda1, P-ajnng40q36]}
  date: 1882
  status: accepted
  reviewed: 2026-06-30
  confidence: medium
  information: secondary
  evidence: indirect
  notes: >
    A non-kin "FAN club" tie (subtype: associate) inferred from shared membership
    on the same roster - the kind of collateral connection that opens research
    leads. Fictional fixture.
```

## Notes
Fictional fixture demonstrating an affiliation/membership claim (gap 10c): a
`relationship` claim with `subtype: member-of` and the organization carried as
`value_org`, the lodge itself never becoming its own record.
