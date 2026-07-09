---
# A SOURCE is one piece of evidence: a photo, a certificate, a census page, a
# letter. Copy this file, fill it in, and rename the copy to:
#     a-short-name_S-yourcode.md      (e.g.  1880-census_S-7n4hp0wztb.md)
# Delete any line you don't need. You can't break anything.

id: S-__________   # OPTIONAL - LINT WILL CREATE FOR YOU LATER IF MISSING: Make up a 10-character
                   # code: digits and a-z, but NOT i, l, o, or u (e.g. S-7n4hp0wztb). Pick at
                   # random; just don't reuse one. A tool can also generate one for you later -
                   # same format either way.
aliases:           # OPTIONAL - the code, repeated, plus any nickname you like to type
  - S-__________   # paste the same code here too - it's what makes [[S-...]] links work
  # - grandmas-album   # ...and add any short nickname you like; both keep working

title: A short name for this source   # e.g. "1880 census, Hartley household"
source_type: photo                    # one of: photo, census, vital-record, newspaper, letter,
                                      # interview, military-record, land-record, probate, directory,
                                      # dna, book, website, artifact, proof-argument, other
source_date: 1880          # OPTIONAL - when the source was made. A year is fine; "about 1880" too.
repository: unknown        # OPTIONAL - where the original lives (an attic, a courthouse, a website)
citation: >                # OPTIONAL - a sentence saying exactly what this is, in your own words
  1880 U.S. census, Hartley household, Fairview, Kansas.

# restricted: true   # OPTIONAL - keep this source out of anything you share publicly. DNA is
                     # always restricted automatically.

# WHO and WHERE this source is about - this is your family graph.
# In Obsidian, type [[ and pick the name: it links them and shows up in your graph.
# A cleanup pass tidies these later (`fha normalize-links`); you never need the ID.
people:
  - "[[Ken Smith]]"        # type [[ and pick the person; a tidy pass settles it to [[P-...|Ken Smith]]
places:                    # OPTIONAL - where this source is set
  - "[[Fairview]]"         # settles to [[L-...|Fairview]]

# original_language: de   # OPTIONAL - the language of the source itself, if not English (de, fr, la, ...)
files:                     # OPTIONAL - the photo or scan this source points at
  - file: documents/put-your-file-here.jpg
    role: primary         # if there's just one file for this source, leave this as "primary"
    # language: de        # OPTIONAL - the language of THIS file
  # - file: documents/put-your-file-here-translation.md
  #   role: translation   # an English version of a foreign record, filed beside the original
  #   language: en
  #   derived: true

created: 2026-01-01        # the date you added this (any date is fine)
---

## Claims
<!-- A "claim" is one fact this source states about someone - one block per fact.
     Just taking notes? DELETE this whole ## Claims block: a source with only
     notes is completely valid. To point at a source in your writing, use its
     code in double brackets: [[S-...]] (searching the code finds it).
     INSIDE this block, IDs are written plainly, no brackets - it's the machine's copy. -->
```yaml
- value: "Thomas Hartley, bookkeeper, living in Fairview"   # the fact in plain words (the important part)
  type: residence          # what kind of fact: birth, death, marriage, residence, census, occupation, ...
  persons: [P-__________]  # whose fact this is - the person's code, no brackets
  id: C-__________         # this claim's own 10-character code (same dice-roll; a tool can fill it)
  status: suggested        # suggested -> needs-review -> accepted, as you confirm it
  confidence: medium       # high / medium / low - how sure you are

  # --- You can ignore everything below at first. ---
  date: 1880               # OPTIONAL - when it happened (1880, 1880-06-15, or 188X for "the 1880s")
  # place_text: Fairview, Kansas   # OPTIONAL - the place as written
  # place: L-__________            # OPTIONAL - a registered place code, no brackets (run `fha places`)
  # information: primary           # OPTIONAL (advanced) - primary / secondary: was the informant there?
  # evidence: direct               # OPTIONAL (advanced) - direct / indirect: does it state the fact outright?

# A RELATIONSHIP claim - who is related to whom, and how. "roles" is required here.
# - value: "Thomas Hartley, son of Caleb Hartley"
#   type: relationship
#   persons: [P-__________, P-__________]   # both people, no brackets
#   id: C-__________
#   subtype: biological        # the nature: biological (default), adoptive, step, foster, ...
#   roles: {child: P-__________, parent: P-__________}
#   status: suggested
#   confidence: high

# A MEMBERSHIP - someone belonged to a group: a regiment, a tribe, a lodge, an
# employer, or a CHURCH / faith community. (Religion is recorded this way, as a
# membership - there is no separate "religion" field.)
# - value: "Enrolled member, Cherokee Nation (1902 Dawes Roll #4471)"
#   type: relationship
#   persons: [P-__________]
#   id: C-__________
#   subtype: member-of           # or "employer" for a workplace
#   roles: {member: P-__________}
#   value_org: "Cherokee Nation" # the organization, in plain text (a church, unit, lodge, ...)
#   status: suggested
#   confidence: high

# MORE LIFE EVENTS - each is a claim with a different "type" (occupation, military,
# immigration, education, census, divorce, ...). Copy the shape above, change type + value:
# - value: "Bookkeeper, Plains Junction Railroad"
#   type: occupation
#   persons: [P-__________]
#   id: C-__________
#   date: 1874
#   status: suggested
#   confidence: medium
# - value: "Enlisted, Union Army, 15th Kansas Cavalry"
#   type: military
#   persons: [P-__________]
#   id: C-__________
#   date: 1863
#   status: suggested
#   confidence: medium
# - value: "Immigrated from Cork aboard the SS Britannia"
#   type: immigration
#   persons: [P-__________]
#   id: C-__________
#   date: 1851
#   status: suggested
#   confidence: low
```

## Notes
Anything else worth remembering about this source - the story behind it, context,
or where the original is kept.
