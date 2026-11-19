<%*
// Plaintext source record (Obsidian Templater): one source = one piece of evidence
// (a photo, a certificate, a census page, a letter). Leave the ID out;
// `fha lint --fix-ids` adds it later. Drop the original file in your documents/ or
// photos/ tree and list it under files: when you have it; a notes-only source is valid too.
const title = await tp.system.prompt("Short title (e.g. 1880 census, Hartley household)");
-%>
---
title: <% JSON.stringify(title) %>
source_type: other       # photo | census | vital-record | newspaper | letter | interview | ...
# source_date:           # optional, when the source was made (a year is fine)
# repository:            # optional, where the original lives
# original_language:     # optional, if the source is not in English (de, fr, la, ...)
# restricted: true       # optional, keep this source out of anything you share publicly
people: []               # who this source is about: type [[ and pick names
created: <% tp.date.now("YYYY-MM-DD") %>
---

## Claims
<!-- One block per fact, in a fenced ```yaml block. A notes-only source (no
     ## Claims) is completely valid. The assistant drafts claims for you. -->

## Notes
