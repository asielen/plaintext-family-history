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
4. Click **Capture**. The page is saved into your Downloads folder, in a folder
   called `fha-inbox`.

You can keep going: capture as many pages as you like in one sitting. They queue
up in that folder and wait for you.

---

## Get the captures into your archive

Nothing moves on its own - your archive has no background programs watching
folders. When you are back at your archive, run one command from the archive's
own folder:

```sh
fha capture --ingest
```

On Windows that is `fha capture --ingest` in a Command Prompt window, or
`.\fha capture --ingest` in PowerShell; on a Mac or Linux,
`./fha capture --ingest`.

That sweeps everything waiting in `Downloads/fha-inbox/` into your archive's
`inbox/` folder. Nothing is thrown away: the swept-up copies are parked in a
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

## What it will not do

- It only reads the page you are looking at, in your own browser session, when
  you click the button. It never browses on its own, never logs in for you, and
  never runs in the background.
- It saves only what you can already see. If a website will not hand over an
  image, the add-on says so plainly and you can download the file yourself and
  drop it into the panel.
- It records the page's web address, never a folder path from your computer.
- It sends nothing anywhere. Captures go to your Downloads folder and then into
  your archive - nowhere else, and never onto the public web. What is published
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
  `fha capture --ingest` from inside your archive folder, and look in `Downloads/fha-inbox/`
  to confirm the capture is there waiting.

More help: [when something looks wrong](../../docs/TROUBLESHOOTING.md), and the
[plain-word glossary](../../docs/GLOSSARY.md) for any term here that is new.
