# Archive Template

Copy this folder's contents to start your own family archive.
This is the empty skeleton a real archive grows from - `inbox/` for new material, `sources/` for evidence records, `people/` for person records, `places/places.yaml`, `notes/`.

**Your archive is a separate, private repository** - never commit real family data to the public spec repo.
See the repo root `README.md` ("Repo, tools, and your archive") for how the public spec/tools and your private archive relate.

After copying:
1. Edit `fha.yaml` to point at where your photos and documents live (see the worked examples below).
2. Bring in the **operating layer** from the public repo. Run, once, from your clone or
   unzipped download of the public repo:
   `python tools/fha.py install <this-archive>` (add `--repo .` if it can't find the tools).
   It copies the `tools/` folder, the rulebooks (`SPEC.md`, `TOOLING.md`, `AGENTS.md`,
   `AGENTS_TOOLING.md`, `CLAUDE.md`, `BUILD.md`), and the `docs/`, then stamps the archive.
   Later, `fha update-tools --repo <updated-clone>` pulls improvements and backs up anything
   you've customized - never deleting, never touching your `fha.yaml` or `places.yaml`
   (`BUILD.md` M9.1-M9.2, TOOLING.md §13c).
3. Open in your AI agent and start processing `inbox/` items.

## Where your photos and documents live

`fha.yaml` has one job: tell the tools where to find your files. The first segment of any record
path (like `photos/1955/…`) is looked up here. Open `fha.yaml` in a plain text editor and use
whichever block below matches your setup - copy it in, edit the path, save.

**Starting with nothing yet?** Leave `fha.yaml` exactly as it ships. The defaults below work, and
you can point it at a real library later without redoing anything.

**1. Plain folders inside this archive** (the default - keeps everything in one place):

```yaml
roots:
  photos: photos
  documents: documents
```

**2. An external drive** (your photos live on a USB or backup drive). Use the drive's own path -
a drive letter on Windows, `/Volumes/…` on Mac:

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

**3. An existing photo library** (e.g. a Lightroom or Photos folder you already keep). Leave it
exactly where it is and point at it - the archive reads from it, never moves or renames it:

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

Notes that save trouble:

- **Always use forward slashes** (`/`), even on Windows. `D:/FamilyPhotos`, never `D:\FamilyPhotos`.
- **`documents:`** works the same way - point it at an external drive or a scans folder if yours
  doesn't live inside the archive. If your documents *are* inside the archive, leave it as
  `documents`.
- Photos under the photos root are **never renamed** - your existing folder structure and
  filenames stay untouched, so connecting a library you already curate is safe.
