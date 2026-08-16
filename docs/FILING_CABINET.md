# The Filing Cabinet - Paper You Know, Mapped to the System

If you've ever kept family research in a real filing cabinet, you already understand this archive.
It's the same shelf of folders, the same habits - just in plain files that won't fade, jam, or get
discontinued. Here's the whole thing, drawer by drawer.

---

## The drawers

| In the system | The paper equivalent | What it is |
|---|---|---|
| `inbox/` | **The pile on your desk** | Everything that just came in - scans, downloads, a photo a cousin mailed - waiting to be sorted and filed. Nothing here is filed yet. |
| `sources/` | **Evidence folders, by citation** | One folder per *piece of evidence*: a census page, a certificate, a letter, an interview. The original is the document; the folder is what you wrote *about* it. |
| `people/` | **Couple dividers** | A divider per ancestral couple, the way you'd separate "the Hartleys" from "the Bennetts." Each person gets a sheet; their facts aren't on the sheet - they're the underlined lines in the evidence folders. |
| `places/` | **The gazetteer card** | A single list of every place you've touched - where it is, and what it was called when. One card per real spot on the map, forever. |
| `notes/` | **Your research legal pad** | Open questions, half-formed leads, "check the 1901 census for this branch." Thinking in progress, not filed conclusions. |
| `photos/` | **The photo boxes** | Your actual pictures, kept in whatever order you already like. Never relabeled, never reshuffled by the system - it reads from the box, it doesn't rearrange it. |
| `documents/` | **The scans drawer** | The actual scan and recording files the `sources/` folders point at - certificates, clippings, audio, transcripts. Lay it out in any folders you like - by type, by family line, by decade - anything you place in a folder keeps its spot when it's processed (something dropped loose at the drawer's top level gets filed into a type folder for you), and you can rearrange the drawer whenever you like: the ID tag in each filed item's name ties it to its evidence folder, and one command (`fha reconcile`, or just ask the assistant) re-ties everything after a reshuffle. |
| `fha.yaml` | **The label that says where the boxes are** | One small card noting which drive or shelf the photo boxes and scans drawer actually live on (they're often too big for the cabinet itself). |

> `photos/`, `documents/`, and `inbox/` are often kept on an external drive instead of inside the
> cabinet - they're the big, heavy items. `fha.yaml` is the note that says where they went. The
> light, durable paper - `sources/`, `people/`, `places/`, `notes/` - stays in the cabinet.

**Photo box or scans drawer?** The split is about *management*, not content: if it lives (or
belongs) in your photo library, it's a photo; everything else - including a photograph *of* a
document - goes in the scans drawer. When you drop a scanned record that happens to be a `.jpg`
into the pile, say what it is in the note that travels with it (e.g. `source_type: census`) so it
files into the right drawer. And plenty of in-between things need no choice at all: a postcard can
keep its picture side in the photo box while its typed-up transcription sits in the scans drawer -
both filed under the same evidence folder - and if something does land in the wrong drawer,
`fha process refile` moves it to the other one and corrects the paperwork in one step.

A couple divider's label also lists that couple's children, and it quietly flags anything worth
seeing at a glance - a child who joined by adoption rather than birth (`Ruth (adopted)`), or a
person who belongs to more than one branch and is filed under another divider (`Thomas Hartley
(also #128 - see 040)`). The assistant keeps these labels fresh; you never edit them by hand.

---

## The two habits that make it work

These aren't system features - they're the filing discipline any good researcher already keeps.
The archive just makes them automatic.

**Underline what the evidence proves; pencil in your guesses.**

- A **claim** is a fact you'd *underline in ink* on the evidence: "this certificate says she was
  born 12 March 1898." It lives in the evidence folder, right next to the document that backs it,
  and it's marked the day you accepted it.
- A **hypothesis** is a *pencil note in the margin*: "maybe this is the same John as the one in the
  1881 census?" A guess, clearly a guess, never mistaken for a proven fact. When evidence turns the
  pencil note into ink, it becomes a real claim.

That single distinction - ink vs. pencil, proven vs. guessed - is the honesty the whole system is
built to protect.

---

## Where does a note go?

Research produces four kinds of paper, and each has a home:

- **Something someone asserted** - "Aunt Mary said the farm burned in 1922" - is *evidence*. It
  goes in the pile (`inbox/`), gets filed as a source, and its assertions become claims you can
  review. Even a half-page of jotted memories counts; the note itself is the document.
- **Something to find out** - "check the 1901 census for this branch" - is an *open question*. If
  it's about one person, it goes on that person's research sheet; only truly general questions go
  on the shared question log (`notes/questions.md`). That split is what keeps the question log
  readable even at hundreds of open questions.
- **Something you believe but can't prove yet** - "maybe this is the same John as the 1881
  census" - is a *hypothesis*: a pencil note on the person's research sheet, with a line about
  what evidence would settle it.
- **A search you already ran** - even one that found nothing - goes in the *research log*, so you
  (and the assistant) never re-run it blind.

Everything else - working notes that span people, draft write-ups - lives on the legal pad
(`notes/research/`). You can hand the assistant a pile of old notes and sort them into these homes
together, one chunk at a time.

---

## Why a cabinet of plain files instead of an app

A program can read your handwriting and sort your pile for you - and this one does, through the AI
assistant. But the *cabinet itself* is plain paper-equivalent files on your own disk, because that's
what lasts. Apps get discontinued; subscriptions lapse; file formats are abandoned. A drawer of
plain files opens with nothing but a text editor and an image viewer, this year and in fifty.

The tools are the helpful clerk who files, fetches, and reads things back to you. The cabinet is
yours, and it would still be a perfectly good cabinet if every tool vanished tomorrow.

---

*Want the formal version of any drawer? See [GLOSSARY.md](GLOSSARY.md) for every term and ID type,
or [../SPEC.md](../SPEC.md) for the full rulebook. Just want to start filing?
[GETTING_STARTED.md](../GETTING_STARTED.md).*
