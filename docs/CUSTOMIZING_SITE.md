# Customizing your family site

`fha site` turns your archive into a website you can open in any browser, put on a USB stick or give to a relative. This guide covers making it your family's site (its name, its welcome, its look) without breaking anything.

## Edit the source files, not the web pages

The site is **built from your archive every time you run `fha site`.** The tool reads your records and a few settings files and writes a fresh set of web pages, the same way each time, from scratch. That's why you can rebuild it safely after any change, and why you never edit the web pages themselves.

> If you open one of the generated `.html` files and change it by hand, your change disappears the next time you build. The tool rebuilds every page from your files and never looks at what was there before.

To customize the site, change the **source**: the few plain files below that the tool reads. Change one, run `fha site`, and the new pages include your change every time.

If you've already edited a page by hand and want to keep the change, see [Keeping a change you made to a page](#keeping-a-change-you-made-to-a-page) at the end.

## What you can change, and where

There are five places. All are plain files you edit in a normal text editor (Notepad or TextEdit, not Word), or ask the assistant to edit for you.

### 1. The homepage welcome: `notes/home.md`

Your archive comes with a `notes/home.md` file holding some starter text. Rewrite it in your own words: who the archive is for, where the family comes from, what a visitor is looking at. It's ordinary Markdown, so you can use headings, links and paragraphs, and you can put a **photo** in the text (see below). Whatever you write here appears as the introduction at the top of the homepage.

The assistant can draft or polish it with you. It's the first thing a visitor reads, so put it in your own voice.

### 2. The name and the homepage banner: `fha.yaml`

Your archive's settings file, `fha.yaml`, holds the site's name and the banner across the top of the homepage. You edit it like any text file:

```yaml
site:
  archive_name: "The Hartley Family Archive"   # the name shown across the top of every page
  hero:
    title: "The Hartley Family"
    tagline: "Six generations in Breton County, Ohio, 1798 to today"
    image: s-ea61339378          # a photo to feature in the banner (by its source ID)
  home_person: p-de957bcda1            # who the homepage family chart is centered on
  home_pedigree_generations: 5         # how many generations of ancestors it shows
  fan_degrees: 180                     # how far each person's ancestor fan opens: 180, 270 or 360
  fan_generations: 5                   # how many generations the ancestor fan draws (1 to 6)
  picture_workers: auto                # how many web pictures to make at once
  completeness_rings: true             # a "how well documented" ring on each person's page
  calendar: true                       # "This month in the family" on the homepage
  questions_page: true                 # a "Questions for the family" page
  search: true                         # the search box on every page (on unless set to false)
  whats_new: true                      # a "What's new" page for relatives who come back (off unless set to true)
  surname_pages: true                  # a page for each family name (on unless set to false)
  statistics: true                     # a "Family statistics" page (off unless set to true)
```

- **`archive_name`** is the title at the top of every page.
- **`hero`** is the large welcome banner on the homepage: a title, a one-line tagline and an optional photo. Leave any of them out and the site uses a default.
- **`home_person`** chooses who the homepage's family chart is built around. It also decides whose relative each person's page says they are ("Thomas Hartley's great-grandmother"), next to their number in the family numbering when they are a direct ancestor. That number counts from the same person, so it matches your folders when the home person is your root person. If the home person is still living, the shareable version names their nearest ancestor who has died instead, and counts from them. Leave it out and the site uses the "root person" you named when you set up the archive. If that person is still living, the shareable version still starts with them, on a card that just says "Private". Relatives see both sides of the family behind it. Living adults in the chart show the same way, with no name, dates or photo, and living children are left out entirely. A living person counts as an adult when they have a child in the archive, or when their recorded birth was 18 or more years ago. If the archive can't tell that the home person is an adult, the chart starts with their nearest ancestor who has died instead. People who have died show in full.
- **`home_pedigree_generations`** sets how many generations of ancestors the homepage chart shows (5 by default: parents, grandparents, great-grandparents and two more). You can click any ancestor's card **that already has its own page** to see the chart centered on them and go back further from there. An ancestor who is still a stub (a name in the tree with no page written up yet, which is normal while an archive is in progress) shows as a plain name with no link.
- **`fan_degrees`** and **`fan_generations`** shape the "Ancestor fan" on each person's page. The fan opens a half circle (`180`) by default, five generations deep. `270` opens it three quarters of the way round and `360` makes a full circle, which holds more names for the same size of page; `fan_generations` can go from 1 to 6. For a reunion poster, set `fan_degrees: 360` and `fan_generations: 6`, then print that person's page with the Ancestor fan open. Every name stays upright, including the ones along the bottom. A value that is not allowed is ignored with a warning, and the build uses the default.
- **`picture_workers`** is how many web-sized photo copies the site makes at once. The first build of a large archive spends most of its time on those copies, so by default it makes several at a time, as many as your computer's processors and free memory allow. You can leave this out. Set it to `1` to make them one at a time (slower, but easier on an older computer); the site comes out the same either way.
- **`completeness_rings`** (off unless set to `true`) puts a small ring on each person's page showing how well documented they are, in five parts: a birth record, the censuses they lived through, their parents, their marriage records, and a death or burial. A filled part means found, half filled means partly found, an empty outline means not found yet, and a faint dotted outline means it doesn't apply. A list beside the ring says the same in words. It counts only facts you've accepted, the same way `fha metrics` does. Nobody living, or whose living status is unknown, gets a ring, and a record you marked private doesn't count toward one on the shareable site. The private workbench (`fha serve`) always shows the ring, whatever this setting says, and names under each gap the kind of record that would fill it.
- **`calendar`** (off unless set to `true`) adds "This month in the family" to the homepage: the births, baptisms, marriages, deaths, burials and arrivals recorded to the month or the day, each name linked to that person's page. Every month is built into the page and the visitor sees their own current month, with all twelve listed below. It uses accepted facts only, and leaves out a living person's own birth, marriage or death, anything naming a private person, and anything resting on a private record. A grandfather's death stays when his living son reported it; the son is not named. The private workbench (`fha serve`) shows "This week in the family" on its home page instead, living relatives included.
- **`questions_page`** (off unless set to `true`) adds a "Questions for the family" page to the shareable site, so relatives can see what you are still trying to find out. Only the questions you mark yourself appear: add the line `- share: family` to a question that is still open. The page shows the question's heading and who it is about, never your guess, your reasons or your notes. A question is left off if it is about someone living, someone whose living status is unknown, or someone you marked private.
- **`search`** (on unless set to `false`) puts a search box at the top of every page. It finds people, places and records by name, forgives the usual spelling differences in old records (Kowalski, Kowalsky), and finds a woman by her married name. It only knows what the site already shows: nobody living, nothing private. It works from a USB stick with no internet.
- **`whats_new`** (off unless set to `true`) adds a "What's new" page, linked from the homepage, that lists what was added, month by month, using the date each fact was accepted, so relatives who come back can see what is new. It lists new facts by person, people who got a page, new records and your dated discoveries, newest month first, up to the last twelve months. It lists only what the shareable site already shows: nothing about a living person, a private record or a fact you marked private, and no count of what was left out.
- **`surname_pages`** (on unless set to `false`) gives each family name its own page: the people who carried it, from the first year to the last, the places they are most often found, and the records that mention them. A Polish or Czech feminine form (Kowalska, Nováková) shares the page of the masculine form when that form is on the site too. A name only living or private people carry gets no page, and no page says how many people are left out.
- **`statistics`** (off unless set to `true`) adds a "Family statistics" page, linked from the homepage under People. It says how long the family lived and how old they were when they married, by century of birth, how many children families had, the commonest first names and surnames, and, for the direct line from the person the homepage chart centres on, how many ancestors are known in each generation (when that person is you and you're living, the line starts with your parents). It uses the middle figure (the median), not an average, so one very long life doesn't skew it, and it counts only dates known to within a couple of years. A figure from fewer than three people says "Too few to say." Living relatives and anyone you marked private are never counted, even in the private preview.

The photo you name in `image` is handled like every other photo on the site: a web-sized copy is made, its hidden location and camera data is removed, and if it shows a living person it's left out of the shareable version.

### 3. The look: `.fha/design/custom.css`

If you (or a helper) know a little CSS, one stylesheet restyles the **entire** site (colours, fonts, spacing, every page and the family tree) from one file. Most changes are a line or two:

```css
:root {
  --paper:  #f4f1ea;    /* a cooler page colour        */
  --accent: #3e4a3a;    /* bottle-green links           */
}
```

You don't need to touch this file to have a good-looking site; it comes with a complete design that looks like a printed family register (see [`DESIGN.md`](https://github.com/asielen/plaintext-family-history/blob/master/docs/DESIGN.md) if you're curious). `custom.css` is for when you want to change it. It's loaded last, so whatever you put here takes priority.

**Where to find it.** In an archive set up with `fha install`, the site design lives in the hidden `.fha/` folder with the tools, so the file is `.fha/design/custom.css`. (An archive whose tools sit at the top level instead has it at `design/custom.css`.) An update never overwrites it.

- **Mac / Linux:** the leading dot makes `.fha` hidden. In Finder, press `Cmd-Shift-.` to show hidden folders (press it again to hide them), or use *Go*, then *Go to Folder…*, and paste the path.
- **Windows:** `fha install` marks the folder hidden, so tick *Hidden items* on the *View* tab in File Explorer to see it. On some drives (a USB stick or a network folder) the hidden mark doesn't stick and the folder is visible anyway; that's fine.

Or ask your assistant to open the stylesheet for you.

### 4. The facts themselves: your records

Most of the site shows your archive as it is. To change what a person's page *says*, improve that person's record: accept a claim, write a biography paragraph, choose their portrait photo, add a place's history (a person who lived there while it changed hands gets a grey "Place history" line on their timeline). That's ordinary genealogy work, and the site shows it the next time you build. There is no separate website content to maintain.

### 5. A page about a family name: `notes/surnames/`

To add your own words to a family name's page, make a file named for the name in plain lowercase letters, such as `notes/surnames/kowalski.md`, and write about the name: where it comes from, how it was spelled, what the family says about it. To put other spellings on the same page, add a line at the top between two `---` lines:

```
---
also: [Kowalsky, Kovalski]
---
The name comes from *kowal*, the Polish word for smith.
```

Private paragraphs and unaccepted drafts stay off the shared site, as everywhere else. If no one who may be shown carries the name, the file is left unused.

For a name written in another alphabet (Иванов, 李), name the file after its page instead: the last part of the page's address, such as `name-1a2b3c4d` in `surnames/name-1a2b3c4d.html`, gives `notes/surnames/name-1a2b3c4d.md`. The same goes for a name Windows keeps for itself, such as Con or Nul: its page is `con-family.html`, so its note is `notes/surnames/con-family.md`.

## Putting a photo in your writing

In `notes/home.md` (and other writing), you can place a specific photo using the same double-bracket style the archive uses everywhere, with a `!` in front to mean "show the picture here":

```
![[s-ea61339378|Margaret and the children on the porch, about 1901]]
```

The part before the `|` is the photo's source ID; the part after is the caption. When you build, the site shows the photo with that caption. Like the banner photo, it gets a web-sized copy with the hidden data removed, and a photo showing a living person is left out of the shareable version. If you don't know a photo's ID, ask the assistant: "put the porch photo on the homepage" is enough.

## Building the site after a change

Whenever you've changed any of the above, rebuild. Run these from your archive folder, one after the other (commands are written bare; the [cheat sheet](CHEATSHEET.md#typing-fha) shows the `./fha` or `.\fha` form your terminal needs):

```
fha index
fha site
```

`fha index` updates the archive index so the build sees your edits to `notes/home.md`, `fha.yaml` and any records; `fha site` then writes a fresh site (by default into `generated/site/`). Open its `index.html` in a browser to see your change. You can build as often as you like; the same files always produce the same site.

The site `fha site` builds is the **safe-to-share** version by default: living people and anything you've marked private are left out. Share that folder, not your archive.

## Keeping a change you made to a page

If you edited a generated page (some wording, a colour) and then remembered the change will be lost on the next build, you don't have to redo it. Ask the assistant:

> "Regenerate the site but keep the edit I made to that page."

It runs the **reconcile-site-edits** skill: it reads your edited page, works out what you changed, and moves that change into the right source file (a colour into `.fha/design/custom.css`, homepage words into `notes/home.md`, a person's biography into that person's record, a title or banner into `fha.yaml`). It shows you each change and asks before saving it. Then it rebuilds the site the normal way, so your edit comes from the source and stays through every future build. (If your edit was a new *fact* about a person, it goes through the usual review so it gets a source like any other fact.)

Next time you'll know which file that kind of change belongs in and can edit it there directly.

## What the site doesn't do

The site is an offline copy of your archive, not a website builder. There's no page designer, no plugins, no server and no theme store. Everything you customize stays a plain file you can read and edit, and the site stays something you can rebuild, carry on a USB stick and open with no internet.
