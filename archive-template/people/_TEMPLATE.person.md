---
# A PERSON page: one per individual you're writing about. Copy this file, fill
# it in, and rename the copy to:   surname__given_p-yourcode.md
# (note the DOUBLE underscore between surname and given, for example
#  hartley__thomas_edward_p-de957bcda1.md). You can't break anything.
# No surname? (a mononym, a single given name, a foundling) Start the filename with the
# double underscore: __caesar_p-yourcode.md. Two surnames or surname-first? Put the name you
# want to sort by before the __, and write the full name in the "name" field below.

id: p-__________   # OPTIONAL. Leave it blank; `fha lint --fix-ids` fills it in. To make one yourself:
                   # p- and 10 characters, digits and a-z except i, l, o and u (e.g. p-de957bcda1).
                   # Pick at random, and never reuse a code another record already has.
aliases:           # OPTIONAL: the same code again (the tools add the name below it)
  - p-__________   # paste the same code here too

name: Full Name Here       # display name. Usually their name AT BIRTH, so the record
                           #   files under the birth surname (a tool can still show a married name).
                           #   Use whatever reads best, though; it's your call.
# name_variants: [T. E. Hartley]   # OPTIONAL: other ways the name is written
# name_at_birth: Margaret Cole     # OPTIONAL: birth or maiden name, if different from `name`
# married_name: Margaret Hartley   # OPTIONAL: married or later name
# also_known_as:                   # OPTIONAL: nicknames, other spellings, other names;
#   - Peggy                        #   the tools read these too, so a [[Peggy]] link leads to this person
#   - Maggie Cole
# profile_photo: margaret-portrait.jpg   # OPTIONAL: their main portrait (a photo file name)
sex: M                     # M / F / intersex / unknown. Sets pronouns in drafted text (unless gender
                           # is set), and which side of a couple a lone known parent is numbered on.
living: false              # true / false / unknown. When unsure, write unknown. Living people
                           # are kept private in anything you share.

# OPTIONAL provisional dates: a guess is fine here; a tool will remind
# you to add a source later. Uncomment and fill in what you know:
# birth: 1840              # 1840, 1840~ for "about 1840", or 184X for "the 1840s"
# death: 1909

# tags: [brick-wall, priority]   # OPTIONAL: your own labels for finding or grouping people
                           # (research status, a project). NOT for facts: a job or a war record is
                           # a claim in a source, not a tag. ("tier: stub" below is one such label.)
# restricted: by-request   # OPTIONAL: keep this person out of anything you share. Plain
                           # "restricted: true" can be put back into a family packet on purpose;
                           # "by-request" is left out everywhere, no exceptions.

# OPTIONAL relationships: who this person connects to, and how. List parents,
# spouse, children, even non-family ties. Type [[ and pick a name. Note the NATURE
# with "subtype" when it matters (an adoptive vs. a birth parent); leave it off for
# an ordinary birth parent. Link the source that proves it, or mark it as a guess.
# relationships:
#   - to: "[[Caleb Hartley]]"     # the other person, by name
#     type: parent                # parent / child / spouse / sibling
#     source: "[[s-__________]]"  # the source that backs it (optional; a tool reminds you later)
#   - to: "[[Robert Hartley]]"    # a second, equally real parent of a different nature
#     type: parent
#     subtype: adoptive           # the nature: biological (default), adoptive, step, foster, guardian, ...
#   - to: "[[Walter Doe]]"        # a guess you are building on before you can prove it:
#     type: parent                #   drawn as a guess, left out of anything you share
#     status: hypothesis

created: 2026-01-01        # the date you added this (any date is fine)
tier: stub                 # stub (a placeholder), then curated once you've written them up. stub is fine.

# gender: woman            # OPTIONAL: identity, in plain words. Use it only if you want to;
                           # "sex" above is enough for most records.
---

# Full Name Here

> **This person's record - yours to write.** The main page for this person: summary,
> biography, relationships. The summary lines under this note and the `## Sources`
> list are generated - `fha views refresh` rewrites them, so never edit them by hand.
> Everything else here is yours; working notes go in `## Research Notes` below, or in
> a separate `_research` file if this person's research grows large enough to want one
> of its own.

<!-- The summary lines (Born, Died, Married, Parents, Children) go here.
     `fha views summary` writes them from your accepted claims between two
     marker lines and keeps them up to date. To write them yourself
     instead, type them here with no markers (each label in bold, each fact
     followed by its source code in double brackets) and the tools will
     leave them alone. -->

<!-- Recording a job, home, school, military service, immigration, or a church /
     lodge / tribe membership? Those are CLAIMS: add them in the source that
     proves them (see the source template's "## Claims"), and mention them in the
     Biography below. -->

## Sources
<!-- Generated by a tool (`fha views sources-index`), not written by hand. It
     lists every source that names this person. Leave this section for the tool
     to fill in once you have an id and some sources; nothing to do here now. -->

## Biography
Write their story in plain sentences. Uncited prose is welcome - it's story and
context, never treated as proven fact. Mark anything you mean to back up later
with `(TODO: import source)` and a tool will keep it on a gentle to-do list.

## Stories
*(none yet)*

## Research Notes
Open questions, hunches, and brick walls - where to look next. (Delete this line
as you add your own.) This section is public by default.
<!-- To keep one note out of anything you share, put it between two marker
     lines: before it, the word private inside marks like the ones around this
     note; after it, /private the same way. START_HERE.md shows both lines.
     The note stays in your own archive and your local preview. -->

## Friends & Family
People connected to them who aren't blood relatives - neighbors, business
partners, the family they boarded with. Type [[ and pick a name to link.
