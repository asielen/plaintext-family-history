---
# A SOURCE is one piece of evidence: a certificate, a census page, a photo, a
# letter. COPY this file for each new source and rename the copy to match the
# document it describes, e.g. "Jane Doe birth certificate.md".
# Delete any line you don't need.

title:            # a short name, e.g. "1950 census, Brooks household"
source_type:      # photo, census, vital-record, newspaper, letter, interview,
                  # military-record, land-record, probate, directory, book,
                  # website, artifact, other
source_date:      # when it was made - a year is fine
repository:       # where the original lives (an attic, a courthouse, a website)
citation:         # one sentence, in your own words, saying exactly what this is

# WHO and WHERE this source is about - this is your family graph.
# In Obsidian, type [[ and pick the name.
people:
  - "[[Name Here]]"
places:
  - "[[Place Name Here]]"

files:            # the scan or photo this points at (put the file in documents/ or photos/)
  - file: documents/put-your-file-here.jpg
    role: primary   # if there's just one file for this source, leave this as "primary"
---

## Claims
<!-- A "claim" is one fact this source states about someone - one block per fact.
     Just taking notes? Delete this whole ## Claims block: a source with only notes
     is completely valid. -->
```yaml
- value: "the fact in plain words"     # e.g. "Jane Doe born 14 June 1950, Millbrook"
  type: birth                          # birth, death, marriage, residence, census, occupation, ...
  persons: ["[[Name Here]]"]           # whose fact this is - link them with [[ ]]
  status: suggested                    # suggested -> accepted, once you're confident
  confidence: medium                   # high / medium / low
  date:                                # when it happened (1950, 1950-06-14, or 195X)
```

## Notes
The story behind this source, or where the original is kept.
