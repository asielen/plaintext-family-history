<%*
// Plaintext person record (Obsidian Templater). Leave the ID out - `fha` mints it
// on first contact (a record with no ID is a valid pre-machine state, SPEC 10/13).
// Name the note anything; `fha lint` later renames it to {sort-name}__{given}_P-id.md
// and keeps your filename as an alias so existing [[links]] keep working.
const name = await tp.system.prompt("Full name (e.g. Thomas Edward Hartley)");
-%>
---
name: <% JSON.stringify(name) %>
# sex: M                 # optional: M | F | intersex | unknown
# gender:                # optional: identity, in plain words (only if relevant)
living: unknown          # true | false | unknown  (unknown is treated as living in exports)
# birth:                 # optional provisional estimate, e.g. 1840 or 184X
# death:
# restricted: by-request # optional: keep this person out of anything you share
created: <% tp.date.now("YYYY-MM-DD") %>
tier: stub
---

# <% name %>

## Biography

## Stories

## Friends & Family
