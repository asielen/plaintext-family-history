# The capture extension

This folder is a small browser add-on that saves a record page from a website
straight into your archive's inbox. It ships inside your archive, ready to load;
you never need to download anything else.

It is for the moment you are looking at a census page, an obituary, or a grave
memorial on a website and you want to keep it. One click saves the page, the
picture, and a note of where it came from, all together, so you can sort it out
later at your own pace.

Nothing is decided at capture time. No source record is created, no facts are
recorded, no names are matched to people in your tree. That all happens later,
when you sit down to review. Capturing is just gathering.

---

## Load it into your browser (once)

The add-on works in Chrome and Edge. You do this once per computer.

1. Open a new tab and go to `chrome://extensions` (in Edge:
   `edge://extensions`).
2. Turn on **Developer mode** - a switch in the top-right corner.
3. Click **Load unpacked**.
4. Choose the `browser-companion` folder that this file is sitting in. It is
   inside the `.fha` folder at the top of your archive.
5. Pin **Plaintext Family History (Capture)** to your toolbar so the button is
   always there.

**If the picker will not show the `.fha` folder:** folders whose name starts
with a dot are hidden. On Windows, type the full path into the dialog's
file-name box and press Enter, for example
`D:\Family Archive\.fha\browser-companion`; or turn on "Show hidden items" in
File Explorer's View menu first. On a Mac, press Shift-Command-. (period) in the
open dialog to show hidden folders.

The browser may say the add-on is "unpacked" or "in developer mode". That is
normal and expected: it is your own copy, not one from a store.

---

## Capture a page

1. Open the record on the website, and make sure the record's details are
   actually showing - not a search-results list, and not a "no record selected"
   panel. What is on screen is what gets saved.
2. Click the toolbar button. A panel opens at the side with the title, the date,
   and any names it noticed already filled in.
3. Correct anything that came out wrong, add a sentence of your own about why
   this page matters, and choose whether there is a specific file (an image or a
   PDF) that is the real record. Every one of these is optional - you can click
   straight through.
4. Click **Capture**. The page is saved into your browser's download folder, in
   a folder called `fha-inbox`. The panel then shows you the exact folder it
   used, and the exact command to run next.

You can keep going: capture as many pages as you like in one sitting. They queue
up in that folder and wait for you.

---

## Get the captures into your archive

Nothing moves on its own - your archive has no background programs watching
folders. When you are back at your archive, run one command from the archive's
own folder:

```sh
./fha capture --ingest      # macOS / Linux
.\fha capture --ingest      # Windows PowerShell
fha capture --ingest        # Windows Command Prompt
```

`fha` is the small launcher file sitting in your archive folder, not a program
your computer knows by name - so type the one line your own terminal wants, and
type it from inside the archive folder. (The
[everyday commands sheet](../../CHEATSHEET.md) has the same table.)

That sweeps everything waiting in the capture folder into your archive's
`inbox/` folder. Copy the command from the panel rather than typing it: if your
browser saves downloads somewhere other than the usual place, the panel's
version carries that folder, and the plain one would look in the wrong place. Nothing is thrown away: the swept-up copies are parked in a
folder named `.ingested/` in case you ever want them back.

If you forget, `fha doctor` reminds you - it counts the captures still waiting
and tells you the command to run.

Then ask your AI assistant to **process the inbox**. It reads each captured
page, files it as a source with its own record, and drafts what it thinks the
page says - all of it marked as suggestions for you to accept or reject. You
have the final word on every fact. The
[everyday commands sheet](../../CHEATSHEET.md) lists the rest of the
commands; [when something looks wrong](../../docs/TROUBLESHOOTING.md) is where
to turn if a step does not behave.

---

## Newspapers.com clippings

On Newspapers.com the add-on saves more than the page you see. Open a clipping
(anyone's: opening it is the choice), or a newspaper page that has your own
clippings on it, and click the toolbar button. The file-address box will say
"Newspapers.com page + clipping (saved automatically)".

When you click **Capture**, the add-on rebuilds the whole newspaper page at full
resolution from the site's own image pieces. It fetches them one at a time with
short random pauses, so a page takes about half a minute; the panel shows its progress.
Then it cuts out your clipping. Each clipping becomes its own source with three
pictures:

- `clipping.jpg` - the item itself, sharp enough to transcribe.
- `page.jpg` - the whole page, so the date and the neighbouring items are kept.
- `annotated.jpg` - the clipping on top and the page below, with the item boxed in
  red. The box is ours, not the newspaper's.

It also keeps `clipping-data.json`: who clipped it, its tags, the paper, place,
date and page, all four of the site's "Source Citation" styles, and the site's
machine-read text of the clipping. That text is often wrong, so it is kept out of
the archive's search and is for finding words only - transcribe from the picture.
The Genealogy citation and the tags also go into the source's notes.

On a clipping page, that clipping is saved, whoever made it; your own other
clippings on the same page appear as tick boxes you can add. On the page viewer,
only your own clippings are offered, and other members' clippings are skipped.
Each clipping you tick is saved as its own source. The copy of the web page (if
you keep one) goes only with the clipping whose page is open, since it shows that
clipping and not the others. A clipping that marks no area on the page cannot be
cut out; the panel names it and leaves it out.

When several clippings are saved at once, the message after Capture lists each one
and where it went. If one could not be saved, it says so and leaves just that one
ticked, so pressing **Capture** again tries it without saving the others twice.

If Newspapers.com asks the add-on to slow down, it waits and tries again, and
stops with a plain message if the site keeps refusing. Any other refusal stops at
once and says why. Your page's image pass lasts a day, so if a capture is refused,
reload the page and try again.

---

## Ancestry records

On an Ancestry record page (the page for one person in one collection - a
census entry, a death index entry), click the toolbar button and then
**Capture**. The add-on saves:

- `record.jpg` - the record's scan, when it has one, the same full-size image
  Ancestry's own Download button gives you.
- `record-data.json` - the record as Ancestry shows it: its labelled facts, the
  household, and its "Source Citation". This is Ancestry's index of the record,
  not a reading of the image.
- a copy of the page, if you keep one.

It also tells filing what the record is: a census entry is filed as a census, a
death index entry as a vital record dated by the death, and an index entry is
marked as an index rather than the original record. Whatever you type in the
form's title, date or kind boxes wins over that.

Everyone in a household has their own record, but they share one census page.
When you capture the next person on the same page while the panel is still open,
the add-on does not download the page again: it notes which capture already holds
it. If the Seen It add-on is
installed, the add-on also asks it whether your archive already has that page.

If Ancestry will not hand over the scan (you are signed out, or the collection
does not allow downloads), the record's facts and citation are still saved, and
the message says so. You can then use Ancestry's Download button, drop the image
into the panel and press **Capture** again: that capture holds the facts and your
image together.

Badges from the Seen It add-on ("Seen 2×", "Captured, not filed") are never saved
as part of a record or a page copy.

---

## What it will not do

- It only reads the page you are looking at, in your own browser session, when
  you click the button. It never browses on its own, never logs in for you, and
  never runs in the background.
- It saves only what you can already see. If a website will not hand over an
  image, the add-on says so plainly and you can download the file yourself and
  drop it into the panel.
- It records the page's web address, never a folder path from your computer.
- It sends nothing anywhere. Captures go to your Downloads folder and then into
  your archive - nowhere else, and never onto the public web. The one question it
  may ask the Seen It add-on, on your own computer, is whether the archive
  already holds an Ancestry census page. What is published
  is decided later, by you, when you export something.

---

## If something goes wrong

- **The toolbar button does nothing.** Reload the record page and try again. A
  page that was already open before you loaded the add-on may need one refresh.
- **The panel says the record detail looks empty.** Open or expand the record on
  the site so its details are on screen, then capture again.
- **The picture did not come through.** Some sites protect their images. Use the
  site's own download button, then drop the file into the panel's box and
  capture again.
- **A capture never showed up in the archive.** Check that you ran
  `fha capture --ingest` from inside your archive folder, and look in the folder
  the panel named after the capture to confirm it is there waiting. If your
  browser saves downloads to an unusual place (OneDrive, another drive), copy
  the command from the panel - it carries that folder - or put the folder in
  your `fha.yaml` as `capture_staging:`.

More help: [when something looks wrong](../../docs/TROUBLESHOOTING.md), and the
[plain-word glossary](../../docs/GLOSSARY.md) for any term here that is new.
