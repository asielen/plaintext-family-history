# Glossary

**Affiliation:** a person's membership in an organization, such as a tribe, a military unit, a lodge, an employer or a church. It's recorded as a relationship (the person *belongs to* the group), with the group's name written in plain text. The membership can be searched and connected; the organization itself stays a name, not a separate record.
*Example: "Enrolled member, Cherokee Nation (1902 Dawes Roll)" is a membership relationship on the person, with "Cherokee Nation" as the organization.*

**Alias:** any name that points at a record: its ID, its filename, a person's name and nicknames, a place's other names. Aliases are what let you link by name: type `[[Grandpa Joe]]` and the archive knows which person you mean. The tools keep each record's aliases up to date so your name links keep working after files are renamed.
*Example: a person named "Margaret A. Cole" also answers to `[[Margaret Cole]]`; both lead to the same record.*

**Archive:** your folder of records: plain text and standard file formats on disk. It is the record of your research; everything else is built from it.
*Example: the `my-family-archive` folder on your computer. Every record in it opens in Notepad, today or in fifty years.*

**Asset:** an actual file: a photo, scan, recording or transcript. Once processed it shares its source's ID. Photos are never renamed while they're in your photo library.
*Example: `rose-wedding-1955.jpg` keeps that filename; once processed it belongs to source `s-7f3a9c2b1d`.*

**Bloodline:** the genetic line the family-tree numbers follow. An adoptive parent, a step-parent or a guardian is shown everywhere they should be, but the tree's *numbering* counts only the genetic parents, so an adopted child isn't numbered into the wrong branch. Both kinds of parent stay visible; only the numbering follows blood.
*Example: Ruth was adopted into the Hartley line. Her adoptive parents show on her page and in the family folder, but the Ahnentafel numbers run through her birth parents.*

**Claim** (`c-`): one statement a source makes (a date, place, relationship or other fact). It lives inside its source record and moves through review (see *Status*).
*Example: "born 12 March 1898 in Leeds," read off a birth certificate and marked `accepted`.*

**Doctor:** `fha doctor`, the archive's health check. It looks for the common problems (an out-of-date index, a drive that isn't plugged in, a missing helper program, an old backup) and names the fix for each. Run it first whenever something seems wrong.

**EDTF:** Extended Date/Time Format (ISO 8601-2), the way all dates are written, so approximate and partial dates are as valid as exact ones (`1850~`, `185X`, `1871-02/1871-03`).
*Example: you say "about 1880" and the assistant writes `1880~`. You say "the 1880s" and it writes `188X`.*

**Ephemera:** a clipping or note kept for its own interest that names nobody in your archive (period news, local history). It's filed as a source with `source_type: ephemera` and no people linked, so it is kept and searchable but appears on nobody's page. See the [FAQ](FAQ.md#does-every-photo-or-document-need-to-become-a-source).

**FAN club:** Friends, Associates and Neighbors: the people *around* a family. Researching them often finds a record the family's own papers don't.
*Example: the witness who signed a marriage record turns out to be the bride's uncle, which leads to her parents.*

**fha:** short for "family-history archive," the set of commands that work on the archive (`fha process`, `fha report`, `fha doctor` and the rest). They are described in `TOOLING.md`. You ask in plain English and the assistant runs them.

**Gender and sex:** two optional fields on a person. `sex` is what records usually state (`M`, `F`, `intersex` or `unknown`); `gender` is a person's identity, in plain words. Use either, both or neither. Drafted biographies take pronouns from `gender` when it's set, otherwise from `sex`. `sex` also decides which side of a couple a parent is numbered on when only one parent is known.
*Example: you note `sex: F` from a birth certificate and, where you know it, `gender: woman`, or you leave both blank.*

**Half (relationship):** two people who share one parent instead of two are half-siblings, and their children are half first cousins. `fha relate` says *half* only when both of the other parents are recorded and are different people, or when a source says so; with a parent missing it says the tie may be half.
*Example: Calvin's two children by different wives are half-siblings; a son of his with no mother recorded is their "brother", with a line saying he may be their half-brother.*

**Index:** a SQLite file the tools build from your records to make search, trees and reports fast. It is never the record of anything: delete it and `fha index` makes it again.
*Example: delete `.cache/index.sqlite` and rebuild it any time with `fha index`; nothing is lost.*

**Launcher:** the small `fha` file (and `fha.cmd` on Windows) in your archive folder that finds the tools and runs them. See the [cheat sheet](CHEATSHEET.md#typing-fha) for how to type it.

**Link** (`[[ ]]`): how writing connects to records: a name, ID or record file name in double brackets, like `[[Grandpa Joe]]` or `[[s-1a2b3c4d5e]]`. It's plain text you can read in any editor, the tools and the family website follow it, and it always leads to a stable ID underneath. (Obsidian follows a link by file name, so there you link by typing `[[` and picking the record from the list; the file name ends in the ID, so the tools follow that link too.) A citation on a fact is a link to its source.
*Example: "born in [[l-7c1a9f4e22|Fairview]]" links the place; "[[s-1a2b3c4d5e]]" after a sentence cites the evidence.*

**Lint:** `fha lint` (also called `fha check`), which checks that every record is shaped correctly and lists anything that needs attention, such as a provisional date still waiting for a source or a hand-made record still waiting for an ID. `fha lint --fix-ids` gives those records their IDs.

**Person** (`p-`): one human. The record holds their names, privacy flags and story; their sourced facts live in claims. It may also list their **relationships** in plain words (parents, spouse, children and the *nature* of each tie), each linked to the source that backs it or marked as a guess until one is found. It may hold a **provisional** birth or death date (see *Provisional date*) until a sourced claim replaces it.
*Example: Thomas Edward Hartley's record is the file `hartley__thomas_edward_p-9c2f4a8b1e.md`.*

**Place** (`l-`): a physical location, identified by coordinates, with its names and jurisdictions over time. Each real place has one record.
*Example: one `l-` record for Leeds, England, which also notes it was "Leeds, Yorkshire" in 1880.*

**Primary sort name:** the part of a person's filename that decides where they sort, so families land together. For most Western records that's the birth surname. Not everyone has a surname (mononyms, people recorded by a single given name, patronymics, foundlings), so the rule is "the name we sort by", and people with no surname sort in their own group. A person's full name is always written in the record itself; the filename is only a handle.
*Example: an ancestor recorded only as "Caesar" gets a file that sorts in the no-surname group; his page still reads "Caesar" in full.*

**Processing:** turning a file in the inbox into a source: giving it an ID, filing it, creating its record and drafting claims for you to review.
*Example: you drop a census scan in `inbox/` and say "process it"; back come suggested names, dates and places to approve.*

**Provisional date:** a birth or death date you write down before you've found the record that proves it. Recording what you know now is a normal starting point, not an error: the tools remind you it still needs a source, and a sourced birth or death claim replaces it when you add one.
*Example: you're fairly sure great-grandma was born around 1849, so you write `birth: 1849~` on her record, and it appears on the "still to source" list until a certificate turns up.*

**Relationship calculator:** asking "how are these two people related?" It answers two ways: the blood relationship (second cousin, great-grandfather) by following the genetic line, and the plain-language path between any two people, even when they aren't related by blood.
*Example: ask how two cousins connect and get "second cousin once removed"; ask about two acquaintances and get "your brother's friend's sister's father."*

**Relationship subtype:** the *nature* of a tie between two people. A father can be a *biological* father or an *adoptive* one; both are real, and recording which keeps the archive from treating two true things as a contradiction. The same idea covers step, foster and guardian ties, surrogacy and donor conception, and ties that aren't family at all: an employer, a fellow member of a unit or lodge, or an enslaver and the person they enslaved.
*Example: a child has two "parent" entries, `subtype: biological` for the father a DNA test confirms and `subtype: adoptive` for the father who raised him, and the tools count both as valid.*

**Research log:** `notes/research-log.md`, the list of searches you've already run, including the ones that found nothing, so neither you nor the assistant repeats them. It also notes the years each search covered, and the searches you couldn't run.

**Restricted:** a private flag you can put on almost anything: a whole person, a single sensitive fact, a source, or a name someone no longer uses. Restricted material stays in your archive but never leaves in anything you share publicly, and it's left out of family packets unless you choose to include it. Two kinds are always kept out: DNA, and anyone who asked to be left out entirely.
*Example: a relative asks not to appear in anything you share. You mark them `restricted: by-request`, and no export includes them, while their place in the tree is kept.*

**Source** (`s-`): one piece of evidence with its own record: its citation, a description, the list of its files, and the claims it makes.
*Example: the 1900 U.S. Census page for the Hartley household, recorded as `s-1a2b3c4d5e`.*

**Source stub:** an item sitting in the inbox with your notes about it, before it's processed into a source. It has no ID yet.
*Example: `inbox/grandmas-album/` holding the scans plus a `notes.md` of your hunches.*

**Status:** where a claim stands in review: `suggested`, then `accepted`, `disputed`, `rejected` or `superseded`. Only a person can move a claim to `accepted`, and everything the assistant drafts starts at `suggested`. A claim also stays `suggested` when you've looked at it and can't decide yet ("possible, but I want a second record first"); that stamps a `reviewed:` date, so a dated suggestion reads "looked at, still open" and an undated one reads "not looked at yet". `disputed` is different: it marks a claim your sources contradict.
*Example: the assistant's guess at a birthplace stays `suggested` until you say yes, and only then becomes `accepted`. Or you look, stay unsure, and leave it `suggested` with today's date.*

**Stub (person):** a person record for someone mentioned but not yet researched. It has the same shape as a full record (an ID, a name and every section) with little or nothing filled in, and it's filed in `people/stubs/` instead of a couple folder until it's written up. Many people stay stubs for good, and that's fine.
*Example: "Uncle Pat," named in a letter, gets a `p-` record with just his name and empty sections, to be filled in later or never.*

**Translation:** an English (or other-language) version of a source in another language, filed beside the original as its own file. The original stays exactly as it was; the language of each file is noted so nothing is mistaken for the original wording.
*Example: a German baptism record keeps its scan and a word-for-word German transcription, plus an English translation filed alongside, all under the same source.*

**Workshop:** your copy of the public project (a git clone or an unzipped download), where the tools and rules come from. Your archive gets its own copy of the tools from the workshop with `fha install`, and newer versions with `fha update-tools`. Your family records never go in the workshop.

**Working answer** (`h-`): your best guess at an open question, written on that question's entry (in `notes/questions.md`, or in the person's research file if they have one) with why you think so and what would settle it. It is never a fact. When a source proves it, the source's claim becomes the fact. (Older documents call this a *hypothesis*.)
*Example: "maybe the John Hartley in the 1881 census is our John," stored as an `h-` entry and never cited as if proven.*

---

*A note on line breaks: plain-text files can be written either way, with each paragraph on one long line or broken into shorter lines. Markdown shows a paragraph the same either way, and the tools don't care. This manual keeps each paragraph on one line. Write your own records however you like.*
