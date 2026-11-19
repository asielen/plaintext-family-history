# Archive Template

This folder is the empty starting archive: `inbox/` for new material, `sources/` for evidence records, `people/` for person records, `places/places.yaml`, `notes/`, and empty `photos/` and `documents/` folders that the default settings point at.

**To start an archive, don't copy this folder.** From your copy of the project, run `fha install` with the name of a new folder (`./fha install ../my-family-archive` on macOS or Linux, `.\fha install ..\my-family-archive` in Windows PowerShell). It builds this same skeleton and adds the tools, the manual and the assistant's instructions. [`docs/GETTING_STARTED.md`](../docs/GETTING_STARTED.md) walks through it.

## Building an archive by hand

If you want an archive with no tools at all, copy this folder, rename the copy, and delete this `README.md` from it (an archive's front page is `START_HERE.md`). Then fill in records with any text editor.

Each record folder has a template to copy:

- `people/_TEMPLATE.person.md`: a person record. It's the same file whether you're noting someone you only need to mention or writing a full profile. It starts as `tier: stub`; change that to `curated` once you've written them up.
- `sources/_TEMPLATE.source.md`: an evidence record with its claims.
- `places/places.yaml`: a commented `_TEMPLATE` place entry at the top.
- `inbox/_TEMPLATE.notes.md`: a note to put beside new material.

Copy a template, give the file a sensible name (`hartley-thomas.md`, `grandpas-letter.md`) and fill it in. **Link records by name:** in any profile or note, cite a source or point at a person by writing its name in double brackets, like `[[Grandpa Joe]]` or `[[Hartley family bible]]`; a nickname works too. **You don't need to make IDs.** `fha lint --fix-ids` adds them and keeps your filename as an alias so your `[[name]]` links keep working; plain `fha lint` only checks.

**Your archive is private.** Never put real family data in the public project's repository. `docs/GETTING_STARTED.md` ("Your archive and the project are separate") explains how the two relate.

## Adding the tools to a hand-built copy later

`fha install` accepts a copy of this folder as long as you haven't changed anything in it yet; then it adds the tools as usual. Once you've started filling the copy in (edited `fha.yaml`, added records), `fha install` stops, so it can never overwrite your work. In that case, copy the tools in by hand from your copy of the project:

- the `tools/` and `design/` folders, into a new `.fha/` folder in the archive;
- the `docs/` folder, plus `SPEC.md` and `TOOLING.md` copied into it;
- `AGENTS.md`, `CLAUDE.md`, the launchers `fha`, `fha.cmd` and `serve.cmd`, and the `.claude/skills/` and `.agents/skills/` folders, into the archive's top folder.

After that, `fha update-tools --repo PATH-TO-PROJECT` brings in newer versions. It backs up anything you've changed, never deletes, and never touches your `fha.yaml` or `places.yaml`.

## Where your photos and documents live

`fha.yaml` tells the tools where to find your files. The first part of any file path in a record (like `photos/1955/…`) is looked up here. Open `fha.yaml` in a plain text editor and use whichever block below matches your setup: copy it in, edit the path, save.

**Starting with nothing yet?** Leave `fha.yaml` as it is. The defaults below work, and you can point it at a real library later without redoing anything.

**1. Plain folders inside the archive** (the default, which keeps everything in one place):

```yaml
roots:
  photos: photos
  documents: documents
```

**2. An external drive** (your photos are on a USB or backup drive). Use the drive's own path: a drive letter on Windows, `/Volumes/…` on a Mac.

```yaml
roots:
  photos: D:/FamilyPhotos              # Windows: the drive letter, forward slashes
  documents: documents
```

```yaml
roots:
  photos: /Volumes/Archive/Photos      # Mac: external drives appear under /Volumes
  documents: documents
```

**3. An existing photo library** (for example a Lightroom or Photos folder you already keep). Leave it where it is and point at it; the archive reads from it and never reorganizes it.

```yaml
roots:
  photos: C:/Users/you/Pictures/Lightroom   # Windows
  documents: documents
```

```yaml
roots:
  photos: /Users/you/Pictures/Lightroom     # Mac
  documents: documents
```

A few things to know:

- **Always use forward slashes** (`/`), even on Windows: `D:/FamilyPhotos`, never `D:\FamilyPhotos`.
- **`documents:`** works the same way. Point it at an external drive or a scans folder if yours isn't inside the archive. If your documents *are* inside the archive, leave it as `documents`.
- Photos under the photos folder are **never renamed**. Your folder structure and filenames stay as they are, so connecting a library you already keep in order is safe.
