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

**The panel stays with its page.** Once open, the panel keeps describing the
page it was opened on, even while you look at other tabs, so nothing changes
under you while you type. When that page moves on to the next record, the panel
follows it. To capture a page in a different tab, click that tab and press
**↻ Re-read page** at the top of the panel: it clears everything (including
what you typed) and fills the panel in again from the page in front of you.
**↻ Re-read file**, beside the "specific file" question, looks at the same
page again for its file only and keeps your title, date, people and notes. Use it on
a viewer that turns to the next image without changing its address.

**What was saved automatically.** On sites where the add-on fetches the
record's file by itself (Ancestry, FamilySearch, Find a Grave, Newspapers.com,
and archives with IIIF images) there is no address box to fill in. If the
automatic download fails on an image viewer, the address box comes back, so you
can paste the right file's address; you can always drop the file in instead. On
Newspapers.com the box is there whenever none of the clippings on the page are
yours. A small box under "Yes, save the actual file" says what will be fetched.
After Capture it lists each download with a tick (and its size) or a cross (and
the site's reason), and says "Saved automatically" only once the capture really
is saved. A long list keeps every download that failed. If a picture is exactly the same as the one your last capture saved,
the list says so: check the tab really shows the record you meant.

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
clippings on it, and click the toolbar button. The box under "Yes, save the
actual file" will say the whole page and your clippings are saved automatically.

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
the message says so. A record saved without a scan always keeps a copy of the
web page with it, even if you unticked that box: the copy is what the record's
facts are filed with. You can then use Ancestry's Download button, drop the image
into the panel and press **Capture** again: that capture holds the facts and your
image together.

Badges from the Seen It add-on ("Seen 2×", "Captured, not filed") are never saved
as part of a record or a page copy.

### Photos other members shared on Ancestry

When a photo someone else shared to their Ancestry tree is open on its own page
(the photo fills the screen; the address has `mediaui-viewer` in it), the panel's
button reads **Capture this photo**. It saves:

- the photo itself, exactly as Ancestry shows it, named
  `ancestry-…-photo.jpg` after the photo's own number on Ancestry;
- `photo-data.json` - the photo's details as Ancestry shows them: its title,
  date, place, description, who first added it, and who it is linked to. These
  were written by whoever shared it, so treat them as their word, not a fact.

It does **not** keep a copy of Ancestry's page, whatever your "Keep a copy of the
whole page" setting: that page lists the other members who saved the photo, and
the archive keeps the photo and its details instead. The panel says so in one
line, and does not offer the page copy there.

When you file it, it becomes a **photo** source: the photo goes into your photo
library like any other, and the record keeps the photo's Ancestry address, so
the Seen It add-on knows it is already in your archive. The record's notes
repeat the date, place, description and the people it is linked to. The name of
the member who first added it is kept in `photo-data.json` only, never in the
record's notes. With no date in its
Details, the photo is filed undated - never with a year from elsewhere on the
page, such as a linked relative's birth.

Open the photo's **Details** on Ancestry before you open the panel: what is on
screen is what gets saved. The people the photo is linked to appear in the
panel's list of names, where you can untick anyone who does not belong; if the
Details are not open, the panel says so before you press Capture. When you press
**Capture this photo**, the panel looks at the page once more first. If the
Details now show something the panel had not listed (you opened them after the
panel read the page), it saves nothing yet: it adds those people to the list,
ticked, and says "I found more on the page — check the people below, then press
Capture this photo." Check them, then press it. Each photo is captured once.

The list of other members who saved the photo ("Saved by …") never reaches your
archive: with no copy of the page, the only words kept are the photo's details,
and reading them stops at that list. A sentence of the description that happens
to begin "Saved by …" is the photo's story, and is kept.

When the page's layout is unclear, the add-on leaves text out rather than file
someone else's words as yours. Anything left out can still be read on Ancestry
from the photo's saved address. It works by one rule: the photo's own Details
box is read whole; anywhere else on the page, a section is read only when the
first words it shows are one of the photo's labels - "Description", "Linked
to" / "Attached to", or "Originally added by" (and, when the page has no
Details box, also "Title", "Date" or "Place"). A section that starts with
anything else - a relative's name, in a heading, a link or bold type, "About
this photo", a member's name - is left out with everything in it. A line that
begins "Originally added by" is always taken as who uploaded the photo, never as
its description, and from a section headed that way only the first name is kept.

If Ancestry will not hand over the photo (you are signed out, say), **nothing is
saved**, and the panel says why: a photo source is never filed without its
picture. If you can download the photo yourself (right-click it, **Save image
as**), drop that file into the panel and press **Capture this photo**; your file
is then saved as the photo. To keep a larger copy than the one the viewer shows,
drop it in the same way before you press Capture.

If a photo does get captured twice, filing the second copy stops with a message
naming the source that already holds the photo, and leaves the copy in your inbox
for you to delete. If the second capture is a *different version* of the same
Ancestry photo (a larger download, say), the message gives you the one command
that keeps it with that photo's source, as a second print, and tells you to
delete the folder afterwards. A *different* photo
that only shares a filed photo's name is never called a copy, and the message
says how to rename it so it can be filed. Nothing is ever offered for deleting
on a matching name alone.

A capture of a shared photo made with an older version of this add-on may still
hold a copy of Ancestry's page. When you file it, that copy is left out (it
stays in your download folder) and the notes say why. If a capture of
a shared photo holds no photo at all (an older add-on saved only the page, or
only the address), nothing is filed. It is moved, whole, into a `.not-filed`
folder in your download folder, and the message - shown that once - tells you to
update the add-on, open the photo again and press **Capture this photo**. Once
you have, you can delete that folder.

From a person's **Hints** page, open the photo on its own page first (the
"Evaluate photo" side panel is not captured).

---

## FamilySearch records

On a FamilySearch record (the page for one person in one collection), or on the
image viewer you opened from one, click the toolbar button and then **Capture**.
You need to be signed in to FamilySearch in that tab. The add-on saves:

- `record.jpg` - the record's image, the full-size picture behind FamilySearch's
  viewer, not the small tiles the viewer draws.
- `record-data.json` - FamilySearch's own data for the record, with its
  citation. This is FamilySearch's index of the record, not a reading of the
  image.
- a copy of the page, if you keep one (always, when there is no image).

The title comes from FamilySearch's own name for the record ("Ethel Hartley -
California Death Index, 1940-1997") unless you type your own. As for Ancestry, a
census files as a census and a death index as a vital record dated by the death,
and whatever you type wins.

Many FamilySearch images can only be seen at a FamilySearch center or an
affiliate library. The panel checks before you press Capture: if the image
cannot be saved from where you are, the line under the file box turns amber and
says "the record's data only", and a line in the Notes box says which image it
was and why it could not be downloaded. You can edit that line; it is saved
with the record. If you have the image another way, drop it into the panel.

On other FamilySearch pages, if the file address the panel found will not
download (the viewer's image pieces often refuse), Capture tries once more
through FamilySearch itself for the same image.

On an image viewer that was not opened from a record, Capture saves the full
image the same way.

The add-on uses your FamilySearch sign-in only to talk to FamilySearch itself,
reads it from the page each time, and never keeps or saves it.

---

## Find a Grave memorials

On a Find a Grave memorial (the page for one person's grave), click the toolbar
button and then **Capture**. You do not need to be signed in: a memorial is
public, and the add-on sends Find a Grave no sign-in, cookie or password. It
saves:

- every photo on the memorial at full size - the gravestone, the person, the
  family, documents - each named for its kind: `grave-1.jpg`, `person-1.jpg`,
  `family-1.jpg`, `document-1.jpg`. Photos are taken only from Find a Grave's
  own picture server (images.findagrave.com), one at a time with a short pause.
- `record-data.json` - what the memorial says: names, birth and death dates and
  places, the cemetery and plot, the inscription and gravesite details, the
  family members it links to, the bio, who made the memorial, the list of its
  photos with their captions, and Find a Grave's own citation. A memorial is a
  volunteer's work from the stone, records and family knowledge - useful, but
  not the original record - so it files as a website and is marked derivative.
- a copy of the page, if you keep one (always, when the capture would otherwise
  hold no photo and no file of yours).

The gravestone photo is the source's main picture (else the person's photo).
The title is "Calvin George Hartley (1842-1918) - Find a Grave memorial" unless
you type your own, and the date is the death date the memorial gives. Find a
Grave gives no burial date, so none is made up, and the birth date never dates
the memorial.

A photo already saved is not downloaded again: one from an earlier capture in
the same sitting, or one the Seen It add-on says the archive holds. When not
every photo could be saved - Find a Grave asked the add-on to slow down, its
photo list could be read only in part ("Find a Grave shows 12 photos; 8 are
saved"), or the memorial has more than the 40 saved in one capture - the result
line turns amber and says so: press Capture again later for the rest. That
second capture is a capture of its own, not added to the first: when you file
it, add its photos to the memorial's source. If a memorial has been removed or
merged into another, Capture says so: search Find a Grave for the person and
open the memorial it shows now.

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
  already holds an Ancestry census page or a Find a Grave photo. What is published
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
