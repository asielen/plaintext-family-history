---
# A SOURCE is one piece of evidence: a photo, a certificate, a census page, a
# letter. Copy this file, fill it in, and rename the copy to:
#     a-short-name_s-yourcode.md      (for example 1880-census_s-7n4hp0wztb.md)
# Delete any line you don't need. You can't break anything.

id: s-__________   # OPTIONAL. Leave it blank; `fha lint --fix-ids` fills it in. To make one yourself:
                   # s- and 10 characters, digits and a-z except i, l, o and u (e.g. s-7n4hp0wztb).
                   # Pick at random, and never reuse a code another record already has.
aliases:           # OPTIONAL: the same code again, plus any nickname you like to type
  - s-__________   # paste the same code here too; it's what makes [[s-...]] links work
  # - grandmas-album   # ...and any short nickname you like; both keep working

title: A short name for this source   # e.g. "1880 census, Hartley household"
source_type: photo                    # one of: photo, census, vital-record, newspaper, letter,
                                      # interview, military-record, land-record, probate, directory,
                                      # dna, book, website, artifact, proof-argument, ephemera, other
source_date: 1880          # OPTIONAL: when the source was made. 1880, or 1880~ for "about 1880".
# source_class: original   # OPTIONAL (advanced): original / derivative (a copy or transcript made
                           # later) / authored (your own write-up). Tools assume original.
# subtype: campaign card   # OPTIONAL: a few words of detail, mainly when source_type is "other"
# original_language: de    # OPTIONAL: the language of the source itself, if not English (de, fr, la, ...)
repository: unknown        # OPTIONAL: where the original is kept (an attic, a courthouse, a website)
citation: >                # OPTIONAL: a sentence saying exactly what this is, in your own words
  1880 U.S. census, Hartley household, Fairview, Kansas.

# WHO and WHERE this source is about: these links make your family graph.
# In Obsidian, type [[ and pick the name: it links them and shows up in your graph.
# `fha normalize-links` later adds the IDs to these links; you never need to type one.
# (Obsidian follows a link by file name, so it shows the ID form as unresolved.)
people:
  - "[[Ken Smith]]"        # type [[ and pick the person; later it becomes [[p-...|Ken Smith]]
places:                    # OPTIONAL: where this source is set
  - "[[Fairview]]"         # later it becomes [[l-...|Fairview]]

# restricted: true   # OPTIONAL: keep this source out of anything you share publicly. DNA is
                     # always restricted automatically.

files:                     # OPTIONAL: the photo or scan this source points at
  - file: documents/put-your-file-here.jpg
    is_primary: true      # the file to open first; with just one file, leave this as it is
  # - file: documents/put-your-file-here-translation.md
  #   role: translation   # an English version of a foreign record, filed beside the original
  #   derived: true

created: 2026-01-01        # the date you added this (any date is fine)
---

> **This source's record - yours to write.** The citation and claims for one piece
> of evidence. Everything below is yours to correct and add to.

## Claims
<!-- A "claim" is one fact this source states about someone, one block per fact.
     Just taking notes? DELETE this whole ## Claims block: a source with only
     notes is complete. To point at a source in your writing, use its
     code in double brackets: [[s-...]] (searching for the code finds it).
     Inside this block the people are written one per line as [[code|Name]];
     roles: and place: use the bare code. -->
```yaml
- value: "Thomas Hartley, bookkeeper, living in Fairview"   # the fact in plain words (the important part)
  id: c-__________         # this claim's own code: c- and 10 characters (or leave it for a tool to fill)
  type: residence          # what kind of fact: birth, death, marriage, residence, census, occupation, ...
  persons:                 # whose fact this is, one line per person
    - "[[p-__________|Thomas Hartley]]"
  date: 1880               # OPTIONAL: when it happened (1880, 1880-06-15, or 188X for "the 1880s")
  # place: l-__________    # OPTIONAL: a registered place code, no brackets (run `fha places`)
  status: suggested        # suggested until you confirm it (accepted). Not sure yet? Keep it suggested and add a reviewed: date
  confidence: medium       # high / medium / low: how good the evidence is
  # notes: >               # OPTIONAL, but recommended: the reasoning and context behind the fact
  #   Listed with his wife and two daughters in the Fairview enumeration.

  # ... You can ignore everything below at first. ...
  # OPTIONAL: the place exactly as the source wrote it:
  # place_text: Fairview, Kansas
  # information: primary           # OPTIONAL (advanced): primary / secondary. Was the informant there?
  # evidence: direct               # OPTIONAL (advanced): direct / indirect. Does it state the fact outright?

# A RELATIONSHIP claim: who is related to whom, and how. "roles" is required here.
# - value: "Thomas Hartley, son of Caleb Hartley"
#   id: c-__________
#   type: relationship
#   persons:                   # both people, one line each
#     - "[[p-__________|Thomas Hartley]]"
#     - "[[p-__________|Caleb Hartley]]"
#   status: suggested
#   confidence: high
#   subtype: biological        # the nature: biological (default), adoptive, step, foster, ...
#   roles: {child: p-__________, parent: p-__________}

# A MEMBERSHIP: someone belonged to a group: a regiment, a tribe, a lodge, an
# employer, or a CHURCH or faith community. (Religion is recorded this way, as a
# membership; there is no separate "religion" field.)
# - value: "Enrolled member, Cherokee Nation (1902 Dawes Roll #4471)"
#   id: c-__________
#   type: relationship
#   persons:
#     - "[[p-__________|Full Name]]"
#   status: suggested
#   confidence: high
#   subtype: member-of           # or "employer" for a workplace
#   roles: {member: p-__________}
#   value_org: "Cherokee Nation" # the organization, in plain text (a church, unit, lodge, ...)

# A DEATH or BURIAL record that names more than one person: say who died with
# "roles: deceased:". Most death and burial records name more than the person who
# died (a spouse, an informant, a grandchild who visited the grave), so roles:
# deceased: says which one it was. List more than one code if two people died or
# were buried together (a house fire, a shipwreck) with nobody else named.
# - value: "Thomas Hartley died 3 March 1912, Fairview, Kansas"
#   id: c-__________
#   type: death
#   persons:                           # everyone the record names, the deceased included
#     - "[[p-__________|Thomas Hartley]]"
#   status: suggested
#   confidence: high
#   roles: {deceased: [p-__________]}  # list more than one code for a shared death or burial

# MORE LIFE EVENTS: each is a claim with a different "type" (occupation, military,
# immigration, education, census, divorce, ...). Copy the shape above and change type and value:
# - value: "Bookkeeper, Plains Junction Railroad"
#   id: c-__________
#   type: occupation
#   persons:
#     - "[[p-__________|Thomas Hartley]]"
#   date: 1874
#   status: suggested
#   confidence: medium
# - value: "Enlisted, Union Army, 15th Kansas Cavalry"
#   id: c-__________
#   type: military
#   persons:
#     - "[[p-__________|Thomas Hartley]]"
#   date: 1863
#   status: suggested
#   confidence: medium
# - value: "Immigrated from Cork aboard the SS Britannia"
#   id: c-__________
#   type: immigration
#   persons:
#     - "[[p-__________|Thomas Hartley]]"
#   date: 1851
#   status: suggested
#   confidence: low
```

## Notes
Anything else to remember about this source: the story behind it, its context, or where the original is kept.
