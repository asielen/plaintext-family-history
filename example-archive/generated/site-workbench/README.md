# Workbench-mode site (fha serve preview)

This folder is what `fha serve` serves for the example archive - the linked
(unredacted) site plus the workbench chrome: the serve bar, the edit
affordances on every record, and the dry-run -> confirm -> CLI-echo modals.

Browse the HTML files directly to see the look and the affordances. Two things
only work with the server actually running, by design (the front-door rule -
serve owns no state, every button is an `fha` command executed by the server):

- **Apply buttons** - they POST to the local server; without it, the dry-run
  and apply steps report that the server is not running.
- **Asset images** - served from `/root/photos/...` style URLs that only the
  server maps to the archive's asset folders.

For the real thing, from the repo root:

    py tools/fha.py serve --root example-archive

(or double-click `serve.cmd` in an installed archive). It binds 127.0.0.1
only - no network, no accounts - and stopping it loses nothing.

Regenerate this preview anytime; it is generated output, never the truth.
