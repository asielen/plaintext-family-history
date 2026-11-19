---
name: on-this-day
description: >
  Run when the human asks "what happened on this day?", "any family anniversaries this week?",
  "anything this month?", "who was born in June?", or "start our chat with something from the family's
  history". Runs `fha calendar` for today, the week ahead or a month and tells him, plainly, which
  births, marriages, deaths and arrivals in his own records fall then, with at most a sentence or two of
  general world history clearly labelled as such. Read-only: it writes nothing, invents no family
  detail, and cites nothing the tool did not give it.
---

# on-this-day

The archive's "on this day" column: a warm, honest way into the family's history. `fha calendar` finds the events (TOOLING.md §7b); this skill only chooses which to say and says them in a cousin's words. It is also the opener for a chat front end that starts a conversation with something from the family. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

"What happened on this day?", "any family anniversaries this week?", "anything this month?", "who in the family was born in June?", "start our chat with something from the family's history" — or a front end that asks for an opener. Safe anytime: it only reads.

## The contract for this skill

- **Read-only.** No claims, no records, no `ai_passes:` entry: nothing is written, so there is nothing to record.
- **Accepted claims only, by default.** The calendar reads accepted claims unless told otherwise, and that is what this skill narrates as family fact. If the human asks to include drafts (`--include-suggested`), every line the tool marks `[suggested, not yet reviewed]` is said as a draft waiting for his review, never as a fact (AGENTS.md "Speculation and storytelling").
- **Never invent lore.** Every family specific — a name, a date, a place, a relationship — comes from the tool's output. No weather, no mood, no motive, no "she must have…". A detail made up here becomes family truth in a generation.
- **Never cite what the tool did not give you.** The JSON lists each event's claims and sources; any `[[S-…]]` you name comes from there. No invented record, repository or collection.
- **Privacy.** Talking to the owner, everyone is named — this is his private view. If the words are going to anyone else (a family group chat, a newsletter, a shared front end), leave out every event that names a person the tool marks `"living": true` (living or unknown both count as living) and every event marked `"source_restricted": true`, and say that living people stay private (AGENTS.md contract 6). The calendar does not flag a person or a claim marked `restricted:`, so before naming anyone in shared words, open their record (`fha find <P-id>` prints its file) and the source behind the event, and leave the event out when either says `restricted`.

## Flow

1. **Pick the span from his words** and run the calendar with `--json` so you read the people, places, claims and sources as data:
   - "today", "on this day", "start our chat" → `fha calendar today --json`
   - "this week", "the next few days" → `fha calendar --week --json`
   - "this month", "in June", "anything in March?" → `fha calendar --json` (this month) or `fha calendar june --json`
   - a named day, "the 14th of June" → `fha calendar june 14 --json`

   Exit `2` means the words did not read as a day or month: the tool's message names the forms it takes — translate it, or ask one plain question ("the 14th of which month?"). Exit `3` means there is no index yet: run `fha index`, then the calendar again.

2. **Say the events plainly.** One sentence each, oldest first or the most striking first, in his words: *"On this day in 1979, James Robert Bradford and Carol Anne Simmons were married in Fairview — 47 years ago."* A marriage names the couple; an arrival is "arrived" or "came to", with the place only if the tool gives one. Say "on this day" only for today's date; a week or month reads "this week, in 1979…" or "on 14 June 1979…". For more than five or so events, say the few that stand out and the count of the rest ("and four more this month — want the whole list?"). Use the tool's own years-ago number; a line the tool says is dated in the future is a record to check, said as such.

3. **Optionally, one or two sentences of general history.** You may add what was happening in the wider world on that date or in that month and year — but label it as general history, not from his records, in the same breath: *"For context, from general history, not your records: the summer of 1979 was the second oil crisis, with long lines at American gas stations."* Never connect it to the family as something they did, saw, felt or took part in ("they would have heard…", "the family lived through…"). If you are not sure of a historical fact, leave it out. Never more than two sentences, and never instead of the family events.

4. **When nothing falls then, say so in one line** and offer the wider span: *"Nothing in your records falls on today's date. Want to see the rest of the week, or the month?"* Only dates recorded to the day or month can be placed; an "about 1880" or a year alone never appears. If he asks why someone is missing, that is the reason, and research-next or review-claims is where to fix it.

5. **Leave one next step:** the wider span, a person's page (`fha find <P-id>`), or the source behind a line ("want to open the 1880 census entry?").

### The opener (a chat front end's first message)

When a front end asks for an opener, run `fha calendar today --json` (fall back to `--week`, then the month, when today is empty) and write one short paragraph: a greeting, the one family event that falls nearest today in plain words, at most one labelled sentence of general history, and an open question inviting him in. *"Good morning. On this day in 1979, James Robert Bradford and Carol Anne Simmons were married in Fairview, 47 years ago. (From general history, not your records: that summer was the second oil crisis, with long lines at the gas pumps.) Would you like to look at what the archive holds about them?"* When the event comes from the week or the month rather than today, say so ("this week, in 1979…"). If nothing falls in the month either, skip the family line rather than invent one, and open with the question alone. A front end that anyone besides the owner reads follows the shared-output privacy rule above.

## Guardrails

- Any record ID written into prose is `[[ ]]`-wrapped, `[[ID|Name]]` preferred; bare IDs only in structured fields and tool arguments (_STANDARD.md §11). In speech, say the name, not the ID.
- Never add a family detail the tool's output does not carry; never present general history as something the family did or saw; never name a source the tool did not list.
- Never speak a suggested-only line as fact; never pass `--include-suggested` unless he asked for drafts.
- Never write anything: no claim, no note, no discovery. A win he wants kept is the `today` skill's `fha confirm discovery` path, on his say-so.
- Shared words leave out any event naming a living person, resting on a restricted source, or naming a restricted person, and say that living people stay private.

## Done when

- On `example-archive`, "any family anniversaries this week?" runs `fha calendar --week --json` and the reply says each event the tool returned and nothing it did not; "what happened in June?" runs `fha calendar june --json` and names the 14 June 1979 marriage of James Robert Bradford and Carol Anne Simmons in Fairview.
- Any general history is at most two sentences, labelled as not from his records, and tied to no one in the family.
- An empty day gets one line and the offer of the week or month, never a filler event.
- For shared output, that marriage is left out (Carol Anne Simmons counts as living: her record says `living: unknown`) and the reply says living people stay private.
- The skill makes **zero** archive writes; `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9).
