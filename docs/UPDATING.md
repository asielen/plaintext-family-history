# Updating Your Archive's Tools

**Who this is for:** you have an archive set up with `fha install`, so it carries its own copy of the tools, and a newer version of the project has come out. The update takes about two minutes and your records are never part of it.

Updating replaces *tools and instructions*, never *records*. Your `sources/`, `people/`, `places/`, `notes/`, photos and documents are not part of any update. Your settings (`fha.yaml`), your place list (`places/places.yaml`) and your site styling (`.fha/design/custom.css`) are written once at install and never overwritten by an update either. (One exception: the first time, the update asks whether you want the Seen It browser add-on kept up to date, and where. Your answer is added as one line at the end of `fha.yaml`; nothing else in it changes.)

---

## Make tool changes in the project, not the archive

Changes flow one way: from your copy of the project (the "workshop") into your archive. If you or your assistant fix a tool or a skill file *inside your archive*, the next update backs that copy up and replaces it with the standard version, and your fix stops being used. If a tool needs fixing, fix it in your copy of the project first, then update the archive from there. (The update never loses the edited file; it goes into `.plaintext-backup/`, but it is no longer the one that runs.)

---

## The steps

Run everything from a terminal *inside your archive folder*. `PATH-TO-WORKSHOP` below is the project folder the update comes from: your git clone, or the new download, unzipped (zip users: [SETUP_FROM_ZIP.md](SETUP_FROM_ZIP.md) covers getting that folder).

Commands are written bare; on macOS or Linux type `./fha`, in Windows PowerShell `.\fha`, and in the Command Prompt `fha`. The [cheat sheet](CHEATSHEET.md#typing-fha) has the details. You never type the path to the tools: in an installed archive they live in a hidden `.fha/` folder, and the launcher finds them there.

1. **Get the new version of the project.** Git users: `git pull` in the workshop clone. Zip users: unzip the new download beside the old one.

2. **Preview.** Nothing is written yet:

   ```
   fha update-tools --dry-run --repo "PATH-TO-WORKSHOP"
   ```

   Read the plan: which files are new, which are unchanged, which will be updated, and which of your files it considers *customized* (edited since install; those get backed up, then replaced with the standard version).

3. **Apply it:** the same command without `--dry-run`.

4. **Review what it reports.** If anything went into `.plaintext-backup/{date}/`, look at it: that's your edited copy. Keep anything you meant to keep by making the same change in the workshop copy (see above), then delete the backup folder. A file the project has *retired* goes to the same place and is reported the same way: moved aside, never deleted.

   If your archive was installed before the manual moved into `docs/`, you'll see one more set of retired files once: the old root `README.md` (a page about the public project, not your archive) and the old root copies of `SPEC.md`, `TOOLING.md`, `GETTING_STARTED.md` and `CHEATSHEET.md`. The update puts the new copies in `docs/` and sets the old ones aside. The archive's top folder then holds only `START_HERE.md`, `fha.yaml`, `AGENTS.md`, `CLAUDE.md` and the launchers.

   **One folder can disappear.** If `fha.yaml` points `documents:`, `photos:` or `inbox:` somewhere outside the archive, the matching empty folder inside the archive is *removed* the next time you update (not moved to `.plaintext-backup/`), since it would never hold anything. It only happens to a folder holding nothing but the starting files the tools put there, checked byte for byte first. A file you added, or an edit to the starting note, leaves the folder untouched. Point the setting back inside the archive and the next `fha update-tools` brings the folder back.

   **Browser add-ons.** If `fha.yaml` names a `browser_extensions:` folder, the update also refreshes the Seen It add-on there (your old copy goes to that folder's `_backups/` first). When it says so, open `chrome://extensions` and click the **reload arrow** on the add-on. **Do not remove it** and load it again: removing an add-on deletes everything it remembers. The same goes for the capture add-on when the update says it changed.

5. **If your archive is a git repository, commit the update** as its own commit, so tool updates never mix with record changes:

   ```
   git add -A
   git commit -m "Update tools from workshop"
   ```

6. **Check the archive:**

   ```
   fha doctor
   ```

   It confirms the new tools and your archive agree. If any tool says the index needs rebuilding, run `fha index`; the index is rebuilt from your files, so this is always safe. If the update brought new helper packages, install them once from inside your archive folder:

   ```
   python -m pip install -r .fha/tools/requirements.txt
   ```

Your records and settings are unchanged and the new tools are in place.

---

## Common questions

**How do I know an update is available?** Git users: `git pull` says so. Zip users: a new zip appears on the project page. Nothing updates itself and the tools never report back to anyone; nothing changes until you run the command.

**What if I skipped several versions?** Nothing special: the update compares files, not version numbers, so one run brings you up to date.

**Something's wrong after updating.** The `.plaintext-backup` entry in [TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers the common cases (the backup folder, and `update-tools` not finding your tools); `fha doctor` names anything else with its fix.

**Can I undo an update?** Your records were never touched, so there's nothing to undo there. To roll the tools back, git users check out the older workshop commit and run `update-tools` from it; zip users run it from the older unzipped folder.
