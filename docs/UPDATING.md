# Updating Your Archive's Tools

**Who this is for:** you have a working archive that carries its own copy of the tools (set up
with `fha install`, or by copying them in), and a newer version of the project has come out.
This page is the whole ritual - it takes about two minutes, and your records are never part of it.

**The one promise to hold onto:** updating replaces *tools and instructions*, never *records*.
Your `sources/`, `people/`, `places/`, `notes/`, photos, and documents are not part of any
update. Your settings (`fha.yaml`), your place list (`places/places.yaml`), and your site styling
(`.fha/design/custom.css`) are written once at install and never overwritten by an update either -
not even by the one that introduced the `.fha/` folder, which carries your stylesheet across.

---

## The one rule that prevents lost work

**Improvements flow one way: from the project (the "workshop") into your archive - never the
other way.** If you (or your assistant) fix or improve a tool or a skill file *inside your
archive*, that copy is now "customized": the next update will carefully back it up and replace
it with the stock version, and your fix silently drops out of use. If a tool needs fixing, fix
it in the workshop copy of the project first, then update the archive from there. (The update
never *loses* the customized file - it lands in `.plaintext-backup/` - but it stops being the
one that runs.)

---

## The ritual

Run everything from a terminal *inside your archive folder*. `PATH-TO-WORKSHOP` below is the
project folder the update comes from - your git clone, or the freshly unzipped new download
(zip users: [SETUP_FROM_ZIP.md](SETUP_FROM_ZIP.md) covers getting that folder).

The bare `fha` command works whatever your layout. In an installed archive (`fha install`) the
tools live tucked under a hidden `.fha/` folder, not at the archive root; the launcher finds them
there, and `update-tools` refreshes them in place. You never name the tool path yourself.

1. **Freshen the workshop.** Git users: `git pull` in the workshop clone. Zip users: unzip the
   new download beside the old one.

2. **Preview - nothing is written yet:**

   ```
   fha update-tools --dry-run --repo "PATH-TO-WORKSHOP"
   ```

   Read the plan: which files are new, which are unchanged, which will be updated, and which of
   your files it considers *customized* (edited since install - those get backed up, then
   replaced with stock).

3. **Apply it** - same command without `--dry-run`.

4. **Review what it reports.** If anything landed in `.plaintext-backup/{date}/`, look at it:
   that's your edited copy, preserved. Salvage anything you meant to keep (by porting it to the
   workshop - see the rule above), then delete the backup folder when you're done with it.

5. **If your archive is a git repository, commit the update** as its own commit, so tool
   updates never mix with record changes:

   ```
   git add -A
   git commit -m "Update tools from workshop"
   ```

6. **Health-check:**

   ```
   fha doctor
   ```

   Doctor confirms the new tools and your archive agree. If any tool mentions the index needs a
   rebuild, `fha index` does it - the index is a disposable cache, so this is
   always safe. If the update brought new helper packages, re-run the installer once, from
   inside your archive folder:

   ```
   python -m pip install -r .fha/tools/requirements.txt
   ```

   (`.fha/` is where an installed archive keeps its tools. If your archive still has a plain
   `tools/` folder at its root, drop the `.fha/` and use `tools/requirements.txt` - or move to
   the tidier layout first with `fha migrate-layout`, below.)

That's it. Records untouched, settings untouched, improvements in.

---

## One-time: tidying an older archive's folders (`fha migrate-layout`)

**Skip this if your archive already has a `.fha` folder in it, or you set it up recently.**

Newer archives keep their machinery — the `tools/` program folder and the `design/` stylesheets —
inside a single hidden folder called `.fha`, so that when you open your archive you see *your
family history* and not a pile of program files. Archives set up before that change have `tools/`
and `design/` sitting at the top level. One command moves them:

1. **Preview first** — this writes nothing:

   ```
   fha migrate-layout --root "PATH-TO-YOUR-ARCHIVE" --dry-run
   ```

2. **Do it** — the same command without `--dry-run`.

3. **Then run the normal update** (step 2 of the ritual above) to pick up everything else.

Run it from your **workshop** folder with `--root` pointing at your archive, as shown. That way
the tools doing the moving aren't the ones being moved.

**What it does and doesn't touch.** Your `tools/` and `design/` folders *move* into `.fha/` —
they are not copied, replaced, or reset, so anything you or your assistant edited stays exactly
as it was, including your `design/custom.css` styling. Your records, `docs/`, the rulebooks, and
`fha.yaml` do not move at all. Running it twice is harmless: the second run says there's nothing
to do. If it finds your archive half-moved already, it stops and says so rather than guessing.

**Two things it will tell you about, if they apply:**

- **The launcher files.** An older archive's `serve.cmd` points straight at the old `tools/`
  folder, and it may not have an `fha` launcher at all. The command replaces them for you when it
  can. If it says it couldn't, run the update in the ritual above and they'll be installed.
- **Browser capture.** If you set up the browser clipper (`fha capture --install-host`), the
  browser remembers where the tools *used* to be. The command tells you when this applies — just
  re-run `fha capture --install-host` with the same browser and settings as before.

A brand-new archive from `fha install` is already laid out this way and never needs this command.

---

## Questions this page gets asked

**How do I know an update is available?** Git users: `git pull` says so. Zip users: a new zip on
the project page. There's no auto-update and no phoning home - nothing changes until you run the
command.

**What if I skipped several versions?** Nothing special - the update compares files, not
version numbers. One run brings you current.

**Something's off after updating.** The `.plaintext-backup` entry in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers the common cases (the backup folder, and
`update-tools` not finding your tools); `fha doctor` names anything else with its fix.

**Can I undo an update?** Your records were never touched, so there's nothing to undo there. To
roll the tools themselves back: git users check out the older workshop commit and re-run
`update-tools` from it; zip users re-run it from the older unzipped folder.
