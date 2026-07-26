# Updating Your Archive's Tools

**Who this is for:** you have a working archive that carries its own copy of the tools (set up
with `fha install`, or by copying them in), and a newer version of the project has come out.
This page is the whole ritual - it takes about two minutes, and your records are never part of it.

**The one promise to hold onto:** updating replaces *tools and instructions*, never *records*.
Your `sources/`, `people/`, `places/`, `notes/`, photos, and documents are not part of any
update. Your settings (`fha.yaml`), your place list (`places/places.yaml`), and your site styling
(`.fha/design/custom.css`) are written once at install and never overwritten by an update either.

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

**How to type `fha`.** `fha` is a launcher file sitting in the folder you are in — your archive,
or your workshop copy. The current folder is *not* on your PATH by default, so on **macOS or
Linux** type `./fha <command>`, and in Windows **PowerShell** `.\fha <command>`; the Windows
**Command Prompt** accepts a bare `fha <command>`. (Put the folder on your PATH once and a bare
`fha` works everywhere — that is the only way the bare form works on a Mac.) The commands below
are written bare for readability; add the `./` or `.\` your shell needs.

Whichever form you type, you never name the tool path yourself. In an installed archive
(`fha install`) the tools live tucked under a hidden `.fha/` folder rather than at the archive
root; the launcher finds them there, and `update-tools` refreshes them in place.

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

   (`.fha/` is where an installed archive keeps its tools.)

That's it. Records untouched, settings untouched, improvements in.

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
