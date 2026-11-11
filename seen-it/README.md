# Seen It: Newspapers.com, Ancestry, FamilySearch, Find a Grave

A small Chrome add-on for genealogy browsing. It remembers what you have already looked at,
ruled out, clipped or attached to your tree, and marks it right on the sites. You stop
re-reading the same newspaper pages, re-opening the same records and re-running the same
searches.

It only reads. It never clicks, saves or changes anything on the sites.

**It works on its own.** You do not need a family archive, Python, or anything else: install
the add-on and it keeps everything in your browser. A family archive kept with
[Plaintext Family History](https://github.com/asielen/plaintext-family-history) can be
connected later, if you want one (see [Advanced mode](#advanced-mode-connect-a-family-archive)).

## Install

1. Get this `seen-it` folder - download the
   [project](https://github.com/asielen/plaintext-family-history) from GitHub (green **Code**
   button → **Download ZIP**) and unzip it - and put the folder somewhere it can stay (moving
   it later means loading it again).
2. Open `chrome://extensions` and turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose the `seen-it` folder.
4. Pin **Seen It** to the toolbar.

That is all. When it is installed it reads your Chrome history once for the Newspapers.com
pages you have already opened, so the "Seen" counts start out filled in.

It also loads in Microsoft Edge (`edge://extensions`) and other Chromium browsers the same way.

### Keeping it up to date

**If you keep a family archive** with these tools, let them do it. `fha install` and
`fha update-tools` ask you once whether you want Seen It and which folder to keep it in (press
Enter for an `extensions` folder inside the archive, or paste a folder of your own - your answer
is saved as one line in `fha.yaml`, `browser_extensions:`). After that, every
`fha update-tools`:

- finds your copy by its ID, even if you renamed its folder;
- copies your current one into `_backups` beside it first (named with its version and the date);
- writes the new files over it, in place - keeping any file it did not ship, and never touching
  anything else in that folder (a data folder you keep beside it is left alone);
- leaves it alone if yours is already newer.

Then one step is yours: in `chrome://extensions`, click the **reload arrow** on Seen It.
**Do not remove it** and load it again - removing it deletes everything it remembers. The
helper (`host`) is updated with it and needs nothing re-run.

**Without an archive,** download the new `seen-it` folder and copy its files over your old
folder (keep the folder where it is), then click the reload arrow the same way.

## What you see on the sites

| Pill | Means |
|---|---|
| **Seen 3× · last Oct 2** (yellow) | You have opened this page or record before. Hover for the time spent on it. |
| **Passed over 2×** (outline) | Newspapers.com: this result sat on your screen and you did not open it. |
| **New** (blue) | Newspapers.com: never opened, never on your screen. |
| **✂ 2 clippings of yours** (green) | Newspapers.com: you already clipped on this page. Click to open your clipping. |
| **🌳 Thomas Hartley +1** (green) | Ancestry / FamilySearch / Find a Grave: this record or memorial is attached to someone in your tree. Hover for everyone it is attached to. |
| **✓ Nothing here / ✗ Not ours** (grey) | You checked it and marked it as nothing for you. The result is faded. |
| **↻ Later** (purple) | You marked it to come back to. |
| **👥 Other member clipped your item** (teal) | Newspapers.com: someone else clipped the same item you did. |

**On a newspaper page or a record page,** a small badge shows the same pills plus two buttons
to mark it (**Nothing here / Not ours** and **Later**). Click again to clear.

**On an Ancestry person's Hints page,** each record and photo hint you have opened before shows
its pills beside its title. Opening a hint (its title or **Review**, which slides in Ancestry's
side panel) counts as opening that record or photo, and the badge with **Not ours** and
**Later** shows for it while the panel is open. Scrolling past hints counts for nothing. A
photo hint is the same photo wherever it is offered, so "Seen" follows it from one person's
hints to another's.

**Above Newspapers.com results,** if you have run the same search before, a banner says when,
how many results it had, and what you opened, clipped and ruled out from it.

## "In your tree" on Ancestry, FamilySearch and Find a Grave

Search results, record pages and memorials show **🌳** on records already attached to people in
your tree, and on Find a Grave memorials your tree people link to. Seen It learns your tree
two ways:

- **Open a person.** On an Ancestry person's Facts page, or a FamilySearch person page, Seen It
  notes the records attached to them. Every person you open adds to what it knows.
- **Load your Ancestry tree file** (fills in everyone at once). On Ancestry, export the tree
  (Trees → your tree → Tree settings → **Export tree**), download the `.ged` file, then in the
  Seen It window click **Load your tree (GEDCOM)…** and choose it. Only each person's name and
  the records and memorials attached to them are kept. Loading a newer file replaces the older one.

## The toolbar window

- **Counts:** pages and records you have opened, marks, searches logged, records attached to your
  tree; and the newspapers you have read in, with years, pages and how many you ruled out.
- **Export research log:** your Newspapers.com searches as dated research-log entries (what you
  searched, where, and whether it found anything, nothing, or something), ready to paste into an
  archive's `notes/research-log.md`. By default only what is new since the last export.
- **Back up… / Restore…:** save everything to a file, and load it back on this or another
  computer. Restoring merges and never counts a look twice.

## Your privacy

- **Nothing is sent anywhere.** Seen It has no server and talks to no one. What it knows stays in
  your browser's own storage on this computer.
- **Your browser history is read once,** when the add-on is installed (and again when it or
  Chrome is updated, or you press **Re-import browser history** - a visit already counted is
  never counted twice), and only for Newspapers.com page addresses. It never reads the rest of
  your history.
- **On the sites, it reads only pages you open yourself,** in your own signed-in session - plus
  your own Newspapers.com clippings list and a tree person's source list, the same lists the
  sites show you. It never clicks, saves or changes anything there.
- **Chrome Sync carries a small summary** (look counts, last-look dates and your marks, by page
  number - no titles, no searches) to your other computers if you are signed in to Chrome with
  sync on. That is Chrome's own sync to your own Google account; turn off "Extensions" in
  Chrome's sync settings to stop it.
- **Backups are files you save yourself,** into your Downloads folder. A backup, like the shared
  data-folder file, holds what you looked at and marked - never this computer's folder names.
- **In Advanced mode the helper only reads your archive.** It never writes to, moves or changes
  anything in it. It writes only its own settings folder (its settings and caches) and the data
  folder you choose.

## Advanced mode: connect a family archive

At the bottom of the Seen It window. Off by default, and **nobody needs it to use Seen It.** It
connects the add-on to a family archive kept with
[Plaintext Family History](https://github.com/asielen/plaintext-family-history) on this computer:

- marks what the archive already holds (**📚 in archive**, **📥 captured, not filed**,
  **○ not in archive**) on all four sites;
- shows, on each tree person and in the tree view, how many of their sources are saved; once you
  have opened an Ancestry person's **Gallery**, their gallery gets its own count beside it
  ("12 of 15 sources saved · 3 of 6 gallery items saved"), counting only the gallery pages you
  have opened;
- keeps the add-on's data in a folder you choose, so a sync tool (OneDrive, Dropbox, a network
  drive) can share it between your computers.

It needs a small local helper, which needs Python 3 ([python.org](https://www.python.org/downloads/);
on Windows tick **"Add python.exe to PATH"** when installing).

### Set it up (once per computer)

1. **Run the installer** in this folder's `host` folder:
   - **Windows:** double-click `host\install.cmd`.
   - **Mac or Linux:** open Terminal in the `host` folder and run `sh install.sh`.

   It asks where your family archive is: paste (or drag in) the archive's top folder - the one
   that holds `fha.yaml` - and press Enter. You can also press Enter to skip and set it later.
   It changes only your own browser settings (no administrator password), and it is safe to run
   again after moving a folder.
2. In `chrome://extensions`, press the reload arrow on **Seen It**.
3. In the Seen It window, tick **Advanced mode**. Chrome asks once for permission to talk to the
   helper; say yes.

Under **Folders** the window shows the archive and data folder in use, and lets you change
either one: paste a folder and press **Save**. The helper checks it before saving.

### How the archive is found

You name one folder: the archive's top folder, the one holding `fha.yaml`. Everything else is
read from that file, the same way the archive's own `fha` tools read it:

| What | Where it comes from |
|---|---|
| Filed sources | `fha.yaml` `roots:` `documents` (default: `documents` inside the archive). The small `*site-data*.json` files saved beside processed captures carry each source's S-id. |
| Source records | the archive's `sources` folder. The page address a record was captured from (its `external_links:` or `citation:`, as `fha capture` and `fha process` write them) marks that record filed. A Find a Grave memorial a source cites is noted too, but as "linked" - its photos may not be captured yet. |
| Captured, not filed | `fha.yaml` `roots:` `inbox` (default: `inbox`): capture bundles, and the `*.notes.md` stubs `fha capture --ingest` leaves. A `processed` folder inside it counts as done. |
| Waiting in Downloads | the capture add-on's download folder: `fha.yaml` `capture_staging:`, else `~/Downloads/fha-inbox`, plus any `fha-inbox-…` folders beside it. |

A page image or scan is marked saved only when the image itself is: the capture says it saved
it (`imageSaved` in its record file), or the image file sits beside the record file. A record
that only links to a scan - Ancestry would not hand it over, only a screen capture was kept,
or the page is saved with another household member - leaves the scan "not saved", so the
capture add-on still downloads it next time. A Find a Grave memorial whose photos did not all
save is "linked", not "captured" - and so is one known only by its address (a capture whose
memorial data is missing, an inbox note, a source that cites it). Each memorial photo whose file
is saved is remembered on its own, so the capture add-on does not download that photo again. A
download that stopped part-way counts for nothing until it finishes.

A root can be a full path (an external drive) or a path relative to the archive. If a drive is
not connected, the window says which folder it could not reach rather than showing zero, and
what that folder held the last time it was read stays marked until it can be read again.

To read more folders - old scans that are not in the archive yet, say - put a
`known-config.json` in the data folder; each list **adds** to what the archive gives:

```json
{
  "filed": ["C:\\Users\\you\\Old Scans"],
  "captured": ["C:\\Users\\you\\Downloads\\more-captures-*"],
  "sources": []
}
```

## The data folder

In Advanced mode each computer keeps its data in a file called `seen-it-<device>.json` in the
data folder, and merges in the files other computers left there. Because the files are a
record of everything you looked at, the data folder is **never** inside the add-on's folder, a
git checkout, the archive, any of the archive's photos, documents or inbox folders (wherever
they live), or the capture download folder - the helper refuses those, so your browsing history
can never be committed, filed or shared by accident.

By default it is a private per-user folder:

- **Windows:** `%LOCALAPPDATA%\SeenIt\data`
- **Mac and Linux:** `~/.local/share/seen-it`

**Sharing it between computers.** Pick one folder your sync tool carries between computers
(outside the archive), and point every computer's Seen It at it: paste it under
**Folders → Data folder** in the Seen It window and press **Save**. Each computer writes only
its own file and reads the others, so nothing is overwritten.

**Already have a data folder** from an earlier copy of Seen It? Point the setting at your
existing data folder the same way. Seen It merges everything already there on its next sync
(within a minute), and never counts a look twice.

### Where the settings live

The helper keeps its settings in one small file on each computer - never in the add-on's
folder (which you might share) and never in the archive:

- **Windows:** `%LOCALAPPDATA%\SeenIt\config.json`
- **Mac and Linux:** `~/.config/seen-it/config.json`

```json
{
  "archive": "C:\\Users\\you\\Documents\\Hartley Family Archive",
  "data_dir": "C:\\Users\\you\\OneDrive\\Seen It data",
  "capture_staging": "C:\\Users\\you\\Downloads\\fha-inbox"
}
```

Every line is optional: with no `data_dir` the default folder above is used, with no `archive`
nothing is marked, and `capture_staging` is needed only when the capture add-on downloads
somewhere other than what the archive's `fha.yaml` says. The same folder also holds the
two-line launcher the browser starts (`seenit_host.cmd` / `seenit_host`) and two caches: the
last archive read (`known-cache.json`), and what each archive file said when it was last read
(`known-files.json`), so a new visit re-reads only the files that changed. Both are safe to
delete; the next visit just takes longer.

**To remove the helper:** delete that settings folder. On Windows also delete the registry key
`HKEY_CURRENT_USER\Software\Google\Chrome\NativeMessagingHosts\com.genealogy.seenit`; on a
Mac or Linux delete `com.genealogy.seenit.json` from the browser's `NativeMessagingHosts`
folder (the installer printed where).

## Why the manifest has a "key"

`manifest.json` carries a `key`: the **public** half of a key pair, published on purpose. It
fixes the add-on's ID to `cennkaddolfbhiabacldomegncpdphed` for everyone who loads it, on every
computer. That matters three ways:

- the helper answers only that ID (its `allowed_origins`), so no other add-on can use it;
- Chrome Sync shares the summary between your computers only if the ID is the same on each;
- another add-on (a capture add-on, for one) can ask Seen It what the archive already holds
  by that ID.

It is not a secret and identifies no one: it is the same for every user, like the add-on's name.
(Only a private key could publish an add-on as this one, and there is none in this folder.)

The one question other add-ons may ask is "which item keys does the archive hold"; Seen It
never shares your looks, marks, searches or tree, and outside Advanced mode the answer is empty.
The question is `{type: "knownKeys", prefix}` (every key starting with `rec:`, `fsrec:`, …) or
`{type: "knownKeys", keys: [...]}` (only those keys; the answer's `sids` then names the
source each filed key is in). An add-on may ask for the archive to be
re-read first (`refresh: true`), but gets at most one fresh read every five minutes; the
answer's `missing` lists archive folders the last read could not reach.

## Files

| File | What it is |
|---|---|
| `manifest.json` | the add-on's description for Chrome |
| `background.js` | the one place data is written; history import, sync, backups, the helper link |
| `content.js`, `cliplist.js`, `searchrun.js`, `content.css` | Newspapers.com (`cliplist.js` keeps your clippings list; `searchrun.js` decides when a search counts as run again) |
| `content-ancestry.js`, `content-familysearch.js`, `content-findagrave.js`, `content-ancestry.css` | Ancestry, FamilySearch, Find a Grave |
| `researchlog.js` | the research-log export and newspaper coverage |
| `treefile.js` | reads an Ancestry tree file (GEDCOM) |
| `popup.html`, `popup.js` | the toolbar window |
| `host/` | the Advanced-mode helper (`seenit_host.py`) and its installers |
| `tests/`, `package.json` | tests (`npm test`; the helper's are `tests/test_seen_it.py` in the project repo) |
