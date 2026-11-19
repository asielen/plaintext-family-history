<%*
// Plaintext person record (Obsidian Templater). Leave the ID out: a record without
// one is fine (SPEC 10, 13), and `fha lint --fix-ids` adds it later.
// Name the note anything; `fha lint --fix-ids` renames it to {sort-name}__{given}_p-id.md
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
