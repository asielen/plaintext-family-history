# The Filing Cabinet

If you've ever kept family research in a real filing cabinet, you already understand this archive. It's the same drawers and folders and the same habits, kept in plain files that won't fade or stop opening. This page goes through it drawer by drawer.

---

## The drawers

| In the archive | The paper equivalent | What it is |
|---|---|---|
| `inbox/` | **The pile on your desk** | Everything that just came in (scans, downloads, a photo a cousin mailed) waiting to be sorted and filed. Nothing here is filed yet. |
| `sources/` | **Evidence folders** | One folder per *piece of evidence*: a census page, a certificate, a letter, an interview. The original is the document; the folder is what you wrote *about* it. |
| `people/` | **Couple dividers** | A divider per ancestral couple, the way you'd separate "the Hartleys" from "the Bennetts." Each person gets a sheet. Their facts aren't on the sheet; they're the underlined lines in the evidence folders. |
| `places/` | **The gazetteer card** | A single list of every place in your research: where it is, and what it was called when. One card per real place on the map. |
| `notes/` | **Your research legal pad** | Open questions, leads, "check the 1901 census for this branch." Thinking in progress, not filed conclusions. |
| `photos/` | **The photo boxes** | Your pictures, kept in whatever order you already like. The archive reads from the boxes and never relabels or rearranges them. |
| `documents/` | **The scans drawer** | The scan and recording files the `sources/` folders point at: certificates, clippings, audio, transcripts. Arrange it in any folders you like; [GETTING_STARTED](GETTING_STARTED.md#where-things-live) explains what happens when you file into it or move things around. |
| `fha.yaml` | **The label that says where the boxes are** | One small card noting which drive or shelf the photo boxes and scans drawer are on (they're often too big for the cabinet itself). |

> `photos/`, `documents/` and `inbox/` are often kept on an external drive instead of inside the cabinet, because they're the large, heavy items. `fha.yaml` is the note that says where they went. The light paper (`sources/`, `people/`, `places/`, `notes/`) stays in the cabinet.

**Photo box or scans drawer?** Decide by where it's kept, not what it shows. If it lives (or belongs) in your photo library, it's a photo; everything else, including a photograph *of* a document, goes in the scans drawer. When you drop a scanned record that happens to be a `.jpg` into the pile, say what it is in the note that goes with it (for example `source_type: census`) so it's filed in the right drawer. A postcard can keep its picture side in the photo box and its typed transcription in the scans drawer, both under the same evidence folder. If something lands in the wrong drawer, `fha process refile` moves it and updates the paperwork in one step.

**One evidence folder, or two?** A folder holds every part of *one* piece of evidence: fronts, backs, extra scans, the pages of a letter written over several days, a ledger whose entries run for months. Two separately published things, such as two different newspaper clippings, get two folders, even if they're about the same event and arrived in the same envelope. When in doubt, ask whether each item could be cited on its own. If yes, it gets its own folder.

A couple divider's label also lists that couple's children and marks two things you'd want to see at a glance: a child who joined the family by adoption (`Ruth (adopted)`), and a person who belongs to more than one branch and is filed under another divider (`Thomas Hartley (also #128 - see 040)`). The assistant keeps these labels up to date; you never edit them by hand.

---

## The two habits that make it work

These are filing habits any careful researcher already has. The archive keeps them for you.

**Underline what the evidence proves; pencil in your guesses.**

- A **claim** is a fact you'd *underline in ink* on the evidence: "this certificate says she was born 12 March 1898." It lives in the evidence folder, next to the document that backs it, and it's dated the day you accepted it.
- A **working answer** is a *pencil note in the margin*: "maybe this is the same John as the one in the 1881 census?" It's clearly a guess and never mistaken for a proven fact. When evidence turns the pencil note into ink, it becomes a claim.

Keeping ink and pencil apart is what lets anyone reading the archive tell what's proven from what's guessed.

---

## Where does a note go?

Research produces four kinds of paper, and each has a place:

- **Something someone told you or wrote down** ("Aunt Mary said the farm burned in 1922") is *evidence*. It goes in the pile (`inbox/`), gets filed as a source, and what it says becomes claims you can review. Even half a page of jotted memories counts; the note itself is the document.
- **Something to find out** ("check the 1901 census for this branch") is an *open question*. Give it its own `## Q:` heading so the assistant can find it again and track it. If the person has a separate research file, it goes there; otherwise it goes in the shared question list, `notes/questions.md`. (The Research Notes section on a person's page is free-form; the assistant doesn't track questions written there.)
- **Something you believe but can't prove yet** ("maybe this is the same John as in the 1881 census") is a *working answer* to an open question. It goes on that question's entry, wherever the question lives, with why you think so and what evidence would settle it. The template in `notes/questions.md` shows the lines to fill in.
- **A search you already ran**, even one that found nothing, goes in the *research log* (`notes/research-log.md`), so neither you nor the assistant runs it again for nothing.

Everything else (working notes that cover several people, drafts) goes on the legal pad, `notes/research/`. You can hand the assistant a pile of old notes and sort them into these places together, a piece at a time.

---

## Why a cabinet of plain files instead of an app

A program can read your handwriting and sort your pile for you, and this one does, through the AI assistant. But the cabinet itself is plain files on your own disk, because those last. Apps get discontinued and subscriptions lapse; a drawer of plain files opens with a text editor and an image viewer, this year and in fifty.

The tools file, fetch and read things back to you. The cabinet is yours, and it would still be a complete cabinet if the tools were gone.

---

*Want the formal version of any drawer? See [GLOSSARY.md](GLOSSARY.md) for every term and ID type, or `SPEC.md`, in this same folder, for the full rules. Ready to start filing? See [GETTING_STARTED.md](GETTING_STARTED.md).*
