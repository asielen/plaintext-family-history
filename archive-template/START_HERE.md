# Start here

This folder is a family history archive. Everything in it is ordinary files: text you can open in any text editor (Notepad, TextEdit), photos, and scans. You do not need any special program to read it. This page tells you what is here and how to read it.

## This family

<!-- FAMILY: the setup interview fills in this part, and the owner may rewrite it at any time.
     Tools never overwrite this file once it exists. -->

*Not written yet.* Whoever keeps this archive: say here, in two or three sentences, whose family this is, who the archive is centred on (the person named as `root_person` in `fha.yaml`), which surnames and places it covers, who has kept it, and, if you like, who should look after it next.

<!-- /FAMILY -->

## Where to begin

1. **Open `people/`.** Each person has one file, named surname first: `hartley__thomas_edward_p-de957bcda1.md`.
   - Folders numbered `002`, `004`, `010` … each hold one couple on the direct family line, with their other children. Folder `002` holds the parents of number 1, the generation the tree is counted from. To go up a generation, double a folder's number for the husband's parents, or double it and add 2 for the wife's (folder `004` holds the parents of the husband in `002`, folder `006` the parents of his wife).
   - `people/connections/` holds everyone else who matters: in-laws, cousins' lines, friends, neighbours.
   - `people/stubs/` holds people noted but not yet placed.
2. **Read a person's profile** (below), then follow its citations to the sources.
3. **Look at `sources/`** for the evidence: one file per document, photo, letter or interview, in a folder for its kind (`sources/census/`, `sources/photos/`, `sources/newspaper/` …).
4. If a `generated/site/` folder exists, open `generated/site/index.html` in a web browser for the same archive as web pages. It is made from the files and can always be made again.

## How to read a profile

The top of the file, between two `---` lines, is a short list of facts about the record: `name:`, `sex:`, `living:` (`true`, `false` or `unknown`), and sometimes rough `birth:` and `death:` dates that nobody has proved yet. Below that, most profiles open with summary lines (Born, Died, Married, Parents, Children), written by the tools from the archive's accepted facts and kept up to date; then come `## Sources`, `## Biography`, `## Stories`, `## Research Notes` and `## Friends & Family`. A short profile may have only some of these.

- A code in double brackets is a link: `[[s-b237895f31]]` points at a source, `[[p-cd795c61e0|Margaret Cole]]` at a person (the name after the `|` is just for reading). To follow one by hand, search the whole folder for the code: the file it names has that code in its file name.
- **A sentence that cites a source is a fact, and the citation says where it comes from. A sentence with no citation is a story or background, not a proven fact.**
- Text that ends with `<!-- AI-DRAFT … -->` was written by an AI and has not been checked by a person. `<!-- AI-ACCEPTED … -->` means a person checked it. Text between `<!-- private -->` and `<!-- /private -->` was meant to stay inside the family archive.

## How to read a source record

A source record (`sources/{kind}/a-short-name_s-xxxxxxxxxx.md`) describes one piece of evidence. Its top part says what it is: `title:`, `source_type:` (census, letter, photo …), `source_date:` (when it was made), `citation:` (a sentence saying exactly what it is), `people:` and `places:` (who and where it is about), and `files:`, the list of the actual scans, photos or recordings. The file marked `is_primary: true` is the one to open first. A recording's record may also have `speakers:`, the codes of the people heard on it, and `quotes:`, a few of their own words with the time they are said.

Under `## Claims` is a list of facts the source states, one block per fact. `## Stories` holds longer passages (often from interviews) and `## Notes` holds anything else. A record in `sources/proofs/` is different: it was written in the archive, and sets out the reasoning behind one conclusion where the records disagree or none says it outright.

## What the codes mean

Every record has a code: a letter, a dash, and ten letters and digits. The letter says what kind of record it is. The rest means nothing; it is only there so the code is unique.

| Starts with | It is | Where it lives |
|---|---|---|
| `p-` | a **person** | a file in `people/` |
| `s-` | a **source**: one document, photo, letter or interview | a file in `sources/`; its scans carry the same code in their file names, its photos inside the picture's keywords |
| `c-` | a **claim**: one fact taken from one source | inside its source's `## Claims` |
| `l-` | a **place** | an entry in `places/places.yaml` |
| `h-` | a **working answer**: a guess not yet proved, never a fact | `notes/questions.md`, or a person's research file |

## The claim keys, on one screen

<!-- claim-keys -->
| Key | What it says |
|---|---|
| `value` | the fact, in plain words |
| `id` | the claim's own `c-` code |
| `type` | what kind of fact: birth, death, marriage, residence, census, occupation, relationship, … |
| `persons` | who the fact is about |
| `roles` | what each person was: `child`, `parent`, `spouse`, `informant`, `witness`, … |
| `subtype` | more detail; on a relationship, its nature: biological, adoptive, step, … |
| `date` | when. `1850~` is about 1850, `1850?` is uncertain, `185X` is the 1850s, `[..1920]` is 1920 or before, `1871-02/1871-03` is between those dates |
| `place` | the place's `l-` code |
| `place_text` | the place exactly as the source wrote it |
| `status` | `accepted`: a person checked it against the source · `suggested`: proposed, not yet checked · `disputed`, `rejected`, `superseded`: doubted, wrong, or replaced by a later claim |
| `confidence` | how good the evidence is: `high`, `medium` or `low` |
| `reviewed` | the date a person made that decision |
| `notes` | the reasoning and context behind the fact |
| `anchor` | where in the source: a page, a line, or a time in a recording |
| `restricted` | keep this fact out of anything shared outside the family |
<!-- /claim-keys -->

## Where the files really are: `fha.yaml` and `roots:`

A source record names its files with short paths such as `photos/1880/portrait.jpg` or `documents/census/1880-census_s-7n4hp0wztb.pdf`. The first word (`photos`, `documents`, `inbox`) is a nickname for a folder. Look in `fha.yaml`, in this folder, under `roots:`:

```yaml
roots:
  photos: D:/Family Photos
  documents: documents
```

Here `photos/1880/portrait.jpg` is the file `D:/Family Photos/1880/portrait.jpg`, on whatever drive held the photo library, and `documents/…` is the `documents` folder inside this archive. A nickname that `roots:` does not list is the folder of that name inside this archive. Photos may have been kept on a separate drive; if one is missing, that drive is where to look.

## The other folders

- `places/places.yaml`: every place, with its `l-` code and, where known, its map position.
- `notes/`: research notes. `notes/questions.md` holds open questions and working answers; `notes/research-log.md` lists searches already made, including the ones that found nothing; `notes/surnames/`, where present, holds notes about a family name.
- `inbox/`: new material waiting to be filed.
- `docs/`: the manual. `docs/GETTING_STARTED.md` is the keeper's guide, `docs/CHEATSHEET.md` the one-page summary, and `docs/SPEC.md` the full rules this archive follows.
- `AGENTS.md`, `CLAUDE.md`, `fha`, `fha.cmd`, `serve.cmd`, and the hidden `.fha/` folder: the tools and their instructions. The archive does not need them to be read.
- `generated/` and `.cache/`: made by the tools from the files above, and can be made again. Nothing in them is the only copy of anything.
