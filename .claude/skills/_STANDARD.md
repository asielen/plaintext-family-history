# Skill-authoring standard

**This is the shared contract every `SKILL.md` in this folder conforms to.** It exists so eight skills
written by different sessions and models don't drift in shape, in how they gate `accepted`, in how they
record their work, or in voice. When you write a new skill, copy [`today/SKILL.md`](today/SKILL.md) — the
reference skill — and obey the rules below. Design lives in
[`TOOLING_INTERFACE.md`](../../TOOLING_INTERFACE.md); the build sequence in
[`BUILD_INTERFACE.md`](../../BUILD_INTERFACE.md); the operating law in [`AGENTS.md`](../../AGENTS.md).

---

## 1. What a skill is

A skill is `.claude/skills/{name}/SKILL.md`: **portable instructions plus `fha` invocations — nothing
else.** No harness APIs (`chrome.*`, MCP, slash-command internals), no Python, no shelling any tool but
`fha`. A skill orchestrates the deterministic tools and adds the *judgment* a tool cannot have — which
claim to draft, which name resolves to which person, where to look next. It never reimplements what a
tool already owns (minting IDs, indexing, xref, bracket math, packet building, coordinate writes — all
tool calls).

The split is the whole architecture, stated once and obeyed everywhere:
**deterministic work is in `fha` tools; judgment is in skills; the human is the only gate to `accepted`.**

Portability is load-bearing (TOOLING_INTERFACE.md §1): the SKILL.md standard is adopted beyond Claude
Code, so switching harnesses must cost nothing. Anything that would make the harness load-bearing —
session memory relied on as a store of record, a harness-only cache, an MCP config — is forbidden.

## 2. Frontmatter shape

Keep frontmatter to two keys, both required, so the file drives any conforming harness unchanged:

```yaml
---
name: today
description: >
  Run at session start or when the human asks "what should I work on?" (also the `/today` wrapper).
  Reads `fha report` and narrates it discoveries-first, then offers one concrete next action.
  Read-only: writes nothing on its own.
---
```

- **`name`** — the folder name, kebab-case.
- **`description`** — one paragraph that (a) states the **trigger** in the human's words ("review the
  census claims", "draft Margaret's bio", "are these the same person?") so the harness can route to it,
  and (b) says in one clause what the skill does and whether it writes. The trigger phrasing *is* the
  routing surface — write it the way the genealogist would actually ask.

Do **not** add `allowed-tools`, model pins, or any harness-specific key: those bind the file to one
harness and break portability. The body is GitHub-flavored markdown: prose instructions with fenced
`fha …` command examples.

## 3. The contract, restated for skills (non-negotiable — AGENTS.md §"The contract")

1. **Suggested-only.** Every claim a skill drafts is `status: suggested`. A skill **never** writes
   `status: accepted`.
2. **The human is the only gate to `accepted`,** and it happens **only** through `fha claim <C-id>
   --status accepted`, which stamps `reviewed:` (lint E006 fails on an accepted claim with no
   `reviewed:`). Directing that tool *is* the human's accept — the skill presents evidence and captures a
   decision the human actually stated in the session; it never accepts on his behalf, never infers
   consent from silence.
3. **Record every AI pass** in the source's `## AI Passes` block — a YAML list entry
   `- {date, model, harness, task, outputs: […], human_reviewed: bool}` (SPEC §14). This is how the
   archive remembers what a machine touched; write it before you hand back.
4. **Draft prose lives behind markers.** New profile/story prose a skill writes is wrapped in
   `<!-- AI-DRAFT {date} {model} - {note} -->` until the human accepts it, at which point
   `fha confirm draft <P-id>` flips it to `<!-- AI-ACCEPTED … -->` (provenance kept). A skill never
   hand-edits a marker.
5. **Never edit below a `<!-- GENERATED … -->` header** and **never overwrite human-written text** —
   draft *around* existing prose; regenerate generated files with their tool, don't patch them.
6. **Respect privacy flags** (AGENTS.md §"The contract" 6): `living`, `restricted` and its no-override
   types stay out of external-facing output; a skill drafting for export honors them.

Every skill states which of these apply to it, near the top, so a reader sees the gate before the flow.

## 4. Voice (AGENTS.md §"Who you serve" — this binds like the contract)

The human is a **non-technical genealogist with a paper-filing mental model** — a careful cousin, not a
sysadmin. Write every skill's *output* accordingly:

- **Speak plainly.** He never has to understand the machinery to make the thing work. The index, the
  IDs, the tool flags — yours to operate, not his to learn.
- **The next-step rule.** Anything he sees that went wrong must name the fix in plain words. No raw
  tracebacks, no bare error codes, no jargon (EDTF, `source_type`, anchor, FTS, C-id) without a plain
  gloss **and** an example. "That date needs the archive's form — `1923` for the year, or `1923-06` for
  June 1923" beats "invalid EDTF."
- **Translate for him, never quiz him.** Map his natural words to stored forms yourself: "around 1870"
  → `1870~`, "the 1880s" → `188X`, "June 1923" → `1923-06` (AGENTS.md §"Dates"). Show him a claim as a
  sentence, not a YAML blob. When a hedge is genuinely un-mappable, ask **one** short plain question —
  never a lecture, never a refusal.
- **Always leave a next step.** A skill ends by naming the one concrete thing to do next.

## 5. Forgiving, not fussy

Messy input is the normal condition of this work (AGENTS.md §"Who you serve"). Loose dates, informal
names, half-written notes, imperfect hand-edits — infer what he meant or ask one plain question. Never
hard-refuse because input isn't schema-clean. A skill that stalls on a fuzzy date has failed the human,
not protected the archive.

## 6. The stop-don't-improvise rule

If a skill needs a capability **no `fha` tool provides**, it must **halt and surface that as core tool
work (BUILD.md)** — never hand-roll it in prose. A skill does not shell `exiftool`, does not edit
`places.yaml` coordinates directly, does not compute Ahnentafel numbers, does not write the SQLite
index. Those are tool jobs. When you hit a missing capability while authoring a skill, stop and report it
as a spec-discovery; do not paper over it with logic a skill should not hold. (Worked example: the
`photo-context` skill blocked on a missing UserComment-write verb — see
[`photo-context/DESIGN.md`](photo-context/DESIGN.md).)

One narrow exception exists: the **owner** may explicitly decide that a skill enacts a missing verb by a
documented interim hand-edit — recorded in the skill's folder (a `GAP.md` naming the wanted verb) and in
BUILD_INTERFACE.md, never silently. `merge-identities` is the one current example (the SPEC §9 merge
write, pending `fha confirm merge`). Blocking, as `photo-context` did, remains the default; an interim
enactment is an owner decision, not an authoring choice.

## 7. Sessions are an interface, not memory

Anything worth keeping is written into archive records in SPEC formats **before the skill hands back** —
claims into source files, hypotheses into research files, discoveries via `fha confirm discovery`,
searches into the research log. Never rely on conversation history as a store of record; the human should
never have to re-explain himself next session.

## 8. Execution hygiene (AGENTS.md §"Tools")

- Run `fha` from the archive root. **Preview before any write:** every mutating verb takes `--dry-run` —
  use it, show the human what will change, then apply.
- **Check exit codes:** 0 clean · 1 warnings · 2 errors · 3 tool failure. Never proceed past a 2/3
  silently; read the tool's TOOLING.md section before retrying on unexpected behavior.
- **Query the index, not the tree** — person/claim/photo questions are `fha` calls, never bulk-reading
  `photos/` or `documents/` into context.
- Run `fha lint` after any batch of edits; it is the done-gate.

## 9. Verification is behavioral (there is no unit test)

A SKILL.md is prose, not code — there is no `python -m unittest` for it. A skill is verified by **running
it in a real session against `example-archive/`** and confirming three things:

1. It produces **exactly** the documented archive writes (suggested claims + anchors, recorded AI passes,
   view refreshes, confirm-driven entries) and **no** write the contract forbids — nothing reaches
   `accepted` without a human `fha claim`, nothing edits below a GENERATED header, no human text is
   overwritten.
2. It **degrades gracefully** on messy input (infer, or ask one plain question — never hard-fail).
3. **`fha lint --root example-archive` still exits 1 with only the documented baseline warnings**
   afterward — currently **W101** (Thomas Hartley's death record is deliberately absent) and **W102**
   (one suggested claim staged on the family-portrait source as review-demo material) — nothing the
   skill wrote introduced a new error or warning, and nothing "resolved" a staged item by accepting a
   claim without a human. This list is the canonical baseline for the skills layer (it mirrors
   TOOLING.md §15); each skill's "Done when" references it instead of restating the codes.

Capture the session transcript proving each "Done when" in the skill's PR description; that transcript
*is* the test evidence.

## 10. The copyable skeleton

```markdown
---
name: {kebab-name}
description: >
  {Trigger in the human's words}. {What it does in one clause}. {Whether it writes / what it never does}.
---

# {name}

{One or two sentences: what this skill is for and the split it honors.}

## When this runs
{The trigger, restated. Invoked-only skills say so explicitly.}

## The contract for this skill
{Which of §3's rules bite here — the gate stated before the flow.}

## Flow
1. {Step — the `fha` call, then the judgment around it.}
2. …

## Guardrails
- {The specific "never" lines for this skill.}

## Done when
- {The behavioral checks — the same shape as the plan's acceptance block.}
```

Every skill in this folder is an instance of this skeleton. If a new skill can't be written as one, that
is a signal it wants a capability a tool should own — see §6.
