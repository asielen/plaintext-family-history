# Customizing your family site

`fha site` turns your archive into a browsable website you can open in any browser,
put on a USB stick, or hand to a relative. This guide is about making that site feel
like *your* family's site — its name, its welcome, its look — without breaking
anything.

There is one rule to learn, and it makes everything else simple.

## The one rule: edit the source, not the site

The site is **built from your archive every time you run `fha site`.** The tool reads
your records and a few settings files, and writes out a fresh set of web pages. It does
this the same way every time, from scratch. That is what lets you rebuild it safely
after any change — and it is why you never edit the web pages themselves.

> If you open one of the generated `.html` files and change it by hand, your change
> disappears the next time you build. The tool rebuilds every page from your source and
> never looks at what was there before.

So the way to customize the site is to change the **source** — the handful of plain
files below that the tool reads on the way in. Change the source, run `fha site`, and
your change is baked into the new pages, every time.

If you accidentally hand-edit a page and want to keep the change, there is a rescue path
— see "The rescue hatch" at the end.

## What you can change, and where

Four places, from "the family's words" to "the fine print." All are plain files you edit
in a normal text editor (Notepad, TextEdit — not Word), or just ask the assistant to
edit for you.

### 1. The homepage welcome — `notes/home.md`

Your archive comes with a `notes/home.md` file already filled in with friendly starter
text. Rewrite it in your own words: who this archive is for, where the family comes from,
what a visitor is looking at. It is ordinary Markdown, so you can use headings, links,
and paragraphs — and you can drop a **photo** right into the text (see below). Whatever
you write here appears as the introduction at the top of the homepage.

The assistant is happy to draft or polish this with you — it is your family's front door,
so it is worth getting into your own voice.

### 2. The name and the hero banner — `fha.yaml`

Your archive's one settings file, `fha.yaml`, holds the site's name and the banner across
the top of the homepage. You edit it like any text file:

```yaml
site:
  archive_name: "The Hartley Family Archive"   # the name shown across the top of every page
  hero:
    title: "The Hartley Family"
    tagline: "Six generations in Breton County, Ohio — 1798 to today"
    image: S-ea61339378          # a photo to feature in the banner (by its source ID)
```

- **`archive_name`** is the title on the masthead of every page.
- **`hero`** is the big welcome banner on the homepage: a title, a one-line tagline, and
  an optional lead photo. Leave any of them out and the site uses a sensible default.

The photo you name in `image` is handled like every other photo on the site: a
web-friendly copy is made, its hidden location/camera data is stripped, and if it happens
to show a living person it is left out of the shareable version automatically. You never
have to think about that — it just happens.

### 3. The look — `design/custom.css`

If you (or a helper) know a little CSS, `design/custom.css` restyles the **entire** site —
colours, fonts, spacing, every page and the family tree — from one file. Most changes are
a line or two:

```css
:root {
  --paper:  #f4f1ea;    /* a cooler page colour        */
  --accent: #3e4a3a;    /* bottle-green links           */
}
```

You do not need to touch this file to have a good-looking site — it comes with a complete
design already (a printed family-register look; see [`DESIGN.md`](DESIGN.md) if you are
curious). `custom.css` is there for when you want to make it yours. It is loaded last, so
whatever you put here wins.

### 4. The facts themselves — your records

Most of the site is simply a rendering of your archive. The best way to change what a
person's page *says* is to improve that person's record: accept a claim, write a
biography paragraph, choose their portrait photo, add a place's history. That is just
ordinary genealogy work — and the site shows it the next time you build. There is no
separate "website content" to maintain; the archive **is** the content.

## Putting a photo in your writing

In `notes/home.md` (and, over time, other prose), you can place a specific photo inline
using the same double-bracket style the archive uses everywhere, with a `!` in front to
mean "show the picture here":

```
![[S-ea61339378|Margaret and the children on the porch, about 1901]]
```

The part before the `|` is the photo's source ID; the part after is the caption. When you
build, the site shows the photo with that caption. Like the hero image, it is made
web-safe automatically, and a photo showing a living person is left out of the shareable
version. If you are not sure of a photo's ID, ask the assistant — "put the porch photo on
the homepage" is enough.

## Building the site after a change

Whenever you have changed any of the above, rebuild:

```
fha site
```

That reads your records and settings and writes a fresh site (by default into
`generated/site/`). Open its `index.html` in a browser to see your change. Building is safe
to repeat as often as you like — it always produces the same site from the same source.

When you are ready to share it, `fha site` already produces the **safe-to-share** version
by default: living people and anything you have marked private are left out. Hand out that
folder, not your archive.

## The rescue hatch: "I edited a page by hand and want to keep it"

It happens — you open a generated page, tweak the wording or a colour, and *then* remember
the change will vanish on the next build. You do not have to redo it from scratch. Ask the
assistant:

> "Regenerate the site but keep the edit I made to that page."

It runs the **reconcile-site-edits** skill: it reads your hand-edited page, works out what
you changed, and moves that change into the right source file for you — a colour into
`design/custom.css`, homepage words into `notes/home.md`, a person's biography into that
person's record, a title or banner into `fha.yaml`. It shows you each change and asks
before saving it. Then it rebuilds the site the normal way, so your edit now comes from
the source and will survive every future build. (If your edit was actually a new *fact*
about a person, it is walked through the usual review so it gets a source, like any other
fact.)

After that, you will know where that kind of change lives — so next time you can edit the
source directly and skip the rescue.

## What this is not

The site is a faithful, offline snapshot of your archive — not a website builder. There is
no page designer, no plugins, no server, no theme store. Everything you customize stays a
plain file you can read and edit yourself, and the site stays something you can rebuild,
carry on a stick, and open with no internet. That plainness is the point: it is what keeps
your family history readable for a very long time.
