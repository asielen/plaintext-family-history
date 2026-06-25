# BACKLOG.md

Deferred engineering items surfaced by the SPEC/README accuracy + philosophy
audit. None is a regression: each **errs safe today**, which is why it was not
fixed inline. They are recorded here with enough context — file references, the
hazard, and a proposed approach — to be picked up later.

This is a **repo-development tracking doc**, like `RELEASE_CHECKLIST.md`: it is
deliberately *not* part of the operating layer (`_ROOT_OPERATING_DOCS` in
`tools/scaffold.py`) and is never installed into a user's archive.

For unbuilt *features* on the spec roadmap (e.g. working-copy mode, M10), see
`BUILD.md` and the `README.md` roadmap instead — this file is only for
deferred fixes/cleanups in already-shipped tools.

---

## 1. GEDCOM export silently drops accepted-but-un-sourced vitals

**Where:** `tools/gedcom.py` — `_load_vitals` (the `JOIN sources s ON s.id = c.source_id`, ~line 345) and the marriage query (~line 395).

**Issue:** Both queries inner-join `claims` to `sources`, so an `accepted` vital
or marriage claim that carries **no `source_id`** is dropped from the export. An
accepted claim is human-asserted truth (SPEC §21 derives the export from
relationship/vital claims, not only sourced ones), so the export loses real
facts. This is inconsistent with `fha site`, which publishes un-sourced accepted
facts. It currently *errs safe* (under-exports; never leaks).

**Why deferred — privacy hazard in the naive fix:** simply switching to
`LEFT JOIN` would also let through a fact whose **only** source is `restricted`
/ DNA: with the restricted source filtered out, that fact would look
"un-sourced" and emit — a privacy regression. The current inner join is exactly
what keeps restricted-only facts out.

**Proposed approach:** distinguish "no source at all" from "only restricted
sources." Emit a fact when it is either un-sourced **or** has at least one
publication-eligible source; suppress it only when every source it has is
restricted/DNA/`publication_ok: false`. Add tests covering: un-sourced accepted
vital emits; restricted-only-sourced vital does **not** emit; mixed
public+restricted emits with the public `SOUR`. Mirror the same logic for
marriages.

**Severity:** low (data completeness, not correctness or privacy — as long as
the fix preserves the restricted-only exclusion).

---

## 2. Place `within:` "settlement cannot nest" check over-flags legitimate hierarchies

**Where:** `tools/places.py` — the `within:`-on-a-settlement lint check
(`_lint_within_on_settlement`; see the module docstring, "a `within:` link whose
source is itself a settlement … cannot also point [onward]").

**Issue:** the heuristic assumes a place that is already the *target* of some
other place's `within:` link (i.e. it has children) cannot itself have a
`within:` *parent*. Legitimate multi-level geography breaks this — a city is both
the target of `neighborhood → city` and the source of `city → county`. The check
flags valid nesting.

**Why deferred — needs a schema change:** doing this correctly requires the
place record to declare its level/kind (settlement vs. administrative region vs.
micro-place), which `places.yaml` (SPEC §15) does not currently carry. Adding a
`class:`/`kind:` field is a `LOCKED` spec amendment (logged decision), not a
tool-only fix.

**Proposed approach (pick one):**
- (a) Add a place `class:` field to SPEC §15 and validate containment by class
  (a settlement may sit `within:` a region; a region may not sit `within:` a
  settlement), or
- (b) relax the check to a **warning** until (a) lands, so legitimate
  hierarchies stop erroring.

**Severity:** low–medium (false errors on valid data; blocks deep place trees).

---

## 3. Minor / informational

- **`fha confirm cooccur` hard-codes `confidence: medium`** for the minted
  relationship claim (`tools/confirm.py`, ~line 708) instead of defaulting from
  the confirming source's `source_type` per SPEC §8.5. Defensible for social
  ties (a relationship asserted from a record is reasonably "medium"), so this is
  a consistency nicety, not a bug. If addressed, derive the default from
  `source_type` (vital-record → high, census/newspaper → medium, interview →
  low) with `medium` as the fallback.

- **W101 vitals-gap does not accept proxies.** The completeness check reports a
  missing `birth`/`death` even when a `baptism`/`burial` claim exists for the
  person. Genealogically a baptism/burial is strong evidence of the vital; the
  check could treat them as partial satisfaction (or emit a softer note).

- **Confirmed-absence (negative) claims have no authoring tool.** SPEC §8.6
  negative facts ("no military service found") must currently be hand-written;
  there is no `fha`-tool path to mint one. A future `fha confirm absence` (or a
  flag on an existing verb) would close the gap.

---

*Source: the spec-accuracy + philosophy audit (see git history around the
`Docs:`/`lint:`/`photoindex:` audit-fix commits). Items 1–3 here were the
audit's "deferred / needs-a-decision" findings; the rest of the audit's findings
were fixed inline.*
