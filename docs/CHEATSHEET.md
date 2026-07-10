# Cheat Sheet - One Page

Print this and keep it by the keyboard. You talk to the **AI assistant** in plain English;
it runs the commands. The command names are here only so nothing feels like a black box.

---

## The daily loop

**Capture → file → process → review → report.** Every session is these five beats.

| Beat | What you say to the assistant | What it runs for you |
|---|---|---|
| **Capture** | "Pull this record into my inbox" (or just drop a scan in `inbox/`) | `fha capture` |
| **File & process** | "Process the new item in my inbox" | `fha process` |
| **Review** | "The name and date are right; leave the place as a suggestion" | `fha claim` (you decide; it records the decision) |
| **Report** | "What should I look at today?" | `fha report` |

You never have to type a command. The phrases above are the whole job.

---

## The handful of commands (if you ever want them)

Run from your workshop folder. Replace `my-family-archive` with your archive's folder name.

```
python tools/fha.py process "inbox/the-file-you-added.jpg" --root my-family-archive   # file one new inbox item
python tools/fha.py report   --root my-family-archive   # the review queue + research leads
python tools/fha.py find --text "Rose Hartley" --root my-family-archive   # search everything
python tools/fha.py doctor   --root my-family-archive   # health check - run this when stuck
python tools/fha.py lint     --root my-family-archive   # "is my archive shaped right?"
python tools/fha.py relate P-aaaa P-bbbb --root my-family-archive   # how are these two related?
python tools/fha.py views timeline P-aaaa --format html --root my-family-archive   # a printable one-page timeline (lands in generated/views/)
```

`--root` just names which archive folder to use. On a Mac, use `python3` if that's what answers.

---

## How to write an uncertain date

You don't need real dates. Say it the way you'd say it out loud - the tool stores the rest.

| You say | The tool stores |
|---|---|
| "about 1880" | `1880~` |
| "the 1880s" | `188X` |
| "sometime in 1898" | `1898` |
| "February or March 1871" | `1871-02/1871-03` |
| "no idea" | nothing - it stays blank, honestly |

A guess clearly marked as a guess is always better than a wrong exact date.
And it's fine to jot a birth or death date before you've found the record - write it down, and the
assistant keeps it on a gentle "still to source" list until the evidence turns up.

---

## How to link to a source or person

Write the name in **double brackets**. That's the whole trick.

| You write | It links to |
|---|---|
| `[[Grandpa Joe]]` | the person named Grandpa Joe (a nickname is fine) |
| `[[Hartley family bible]]` | that source record |
| `born in [[Fairview]]` | the place |
| `[[Caleb Hartley]]` in a person's relationships | his parent, spouse, or child - with its nature noted |

Don't worry about IDs - name your file something sensible, link to it by name, and if you ever run
`fha lint` it assigns the durable IDs and keeps your name-links working. You never have to make one.

Relationships work the same way: under a person, list who they connect to and how - a parent, a
spouse, an adoptive parent - by name. Mark a tie you're sure of with the source that proves it, or
just jot it as a hunch; the assistant keeps the unproved ones on the "still to source" list.

---

## Where things live

| Folder | What's in it |
|---|---|
| `inbox/` | New material waiting to be processed - your to-file pile. |
| `sources/` | One record per piece of evidence (a document, photo, interview). |
| `people/` | The people in your tree, in numbered family-couple folders. |
| `places/` | The list of places, with their locations. |
| `notes/` | Research in progress and your running questions. |
| `fha.yaml` | The one settings file - where your photos and documents live. |

Everything is plain text or standard image files. Open any of it with Notepad, TextEdit, or a
photo viewer - no tool required, now or in fifty years.

---

## Three rules that keep you safe

1. **Nothing becomes a fact until *you* accept it.** The assistant only ever *suggests*.
2. **Photos are never renamed.** Drop them in as-is; identity rides in hidden metadata, not the name.
3. **Mark anything private with `restricted`.** A person, a fact, a source, or an old name -
   restricted material stays in your archive but never leaves in anything you share.

---

*Stuck? See [TROUBLESHOOTING.md](TROUBLESHOOTING.md). New here? [GETTING_STARTED.md](GETTING_STARTED.md).
Every term defined: [GLOSSARY.md](GLOSSARY.md).*
