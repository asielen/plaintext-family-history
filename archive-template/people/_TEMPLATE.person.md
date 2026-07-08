---
# A PERSON page - one per individual you're writing about. Copy this file, fill
# it in, and rename the copy to:   surname__given_P-yourcode.md
# (note the DOUBLE underscore between surname and given, e.g.
#  hartley__thomas_edward_P-de957bcda1.md). You can't break anything.
# No surname? (a mononym, a single given name, a foundling) Start the filename with the
# double underscore: __caesar_P-yourcode.md. Two surnames or surname-first? Put the name you
# want to sort by before the __, and write the full name in the "name" field below.

id: P-__________   # OPTIONAL - LINT WILL CREATE FOR YOU LATER IF MISSING: Make up a 10-character
                   # code: digits and a-z, but NOT i, l, o, or u (e.g. P-de957bcda1). Pick at
                   # random; just don't reuse one. A tool can also generate one for you later.
aliases:           # OPTIONAL - the code, repeated, so [[P-...]] and [[name]] links resolve
  - P-__________   # paste the same code here too

name: Full Name Here       # display name. CONVENTION: use their name AT BIRTH, so the record
                           #   files under the birth surname (a tool can still show a married name).
                           #   Set it to whatever reads best, though - it's your call.
# name_at_birth: Margaret Cole     # OPTIONAL - birth / maiden name, if different from `name`
# married_name: Margaret Hartley   # OPTIONAL - married / later name
# also_known_as:                   # OPTIONAL - nicknames, alternate spellings, other names;
#   - Peggy                        #   these become aliases, so [[Peggy]] resolves to this person
#   - Maggie Cole
sex: M                     # M / F / intersex / unknown - used only for grammar in generated text
living: false              # true / false / unknown - WHEN UNSURE, write unknown. Living people
                           # are kept private in any public output.

# gender: woman            # OPTIONAL - identity, in plain words. Use it only if you want to;
                           # "sex" above is enough for most records.
# restricted: by-request   # OPTIONAL - keep this person out of anything you share. Plain
                           # "restricted: true" can be re-included in a family packet on purpose;
                           # "by-request" is left out everywhere, no exceptions.
# tags: [brick-wall, priority]   # OPTIONAL - your own labels for finding / grouping people
                           # (research status, a project). NOT for facts - a job or a war record is
                           # a claim in a source, not a tag. ("tier: stub" below is one such label.)

# OPTIONAL provisional dates - an honest guess is fine here; a tool will remind
# you to add a source later. Uncomment and fill in what you know:
# birth: 1840              # a year, "about 1840", or 184X for "the 1840s"
# death: 1909

# OPTIONAL relationships - who this person connects to, and how. List parents,
# spouse, children, even non-family ties. Type [[ and pick a name. Note the NATURE
# with "subtype" when it matters (an adopted vs. a birth parent); leave it off for
# an ordinary birth parent. Link the source that proves it, or jot it as a hunch.
# relationships:
#   - to: "[[Caleb Hartley]]"     # the other person, by name
#     type: parent                # parent / child / spouse / sibling
#     subtype: biological         # the nature: biological (default), adoptive, step, foster, guardian, ...
#     source: "[[S-__________]]"  # the source that backs it (optional; a tool reminds you later)
#   - to: "[[Robert Hartley]]"    # a second, equally-real parent of a different nature
#     type: parent
#     subtype: adoptive

created: 2026-01-01        # the date you added this (any date is fine)
tier: stub                 # stub (a placeholder) -> curated (you've written them up). stub is fine.
---

# Full Name Here

<!-- A quick summary. Point at the source for each fact with its code in double
     brackets: [[S-...]]. Fill in what you know; delete the rest. -->
**Born:** about 1840 - New York [[S-__________]]
**Died:** 1909 [[S-__________]]
**Married:** Spouse Name [[S-__________]]
**Parents:** Father Name · Mother Name
**Children:** Child One · Child Two

<!-- Recording a job, home, school, military service, immigration, or a church /
     lodge / tribe membership? Those are CLAIMS - add them in the source that
     proves them (see the source template's "## Claims"), and mention them in the
     Biography below. -->

## Biography
Write their story in plain sentences. Uncited prose is welcome - it's story and
context, never treated as proven fact. Mark anything you mean to back up later
with `(TODO: import source)` and a tool will keep it on a gentle to-do list.

## Stories
*(none yet)*

## Research Notes
Open questions, hunches, and brick walls - where to look next. (Delete this line
as you add your own.) This section is public by default; to keep a single note out
of a shared copy, wrap it in a private fence like the example below.

<!-- private -->
A hunch you're not ready to publish - say, a possible tie to a living relative.
This block stays in your local `--linked` preview but is dropped from the shared
(standalone) site.
<!-- /private -->

## Friends & Family
People connected to them who aren't blood relatives - neighbors, business
partners, the family they boarded with. Type [[ and pick a name to link.
