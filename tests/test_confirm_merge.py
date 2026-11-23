"""
test_confirm_merge.py - fha confirm merge: the SPEC §9 identity-merge write.

The merge verb is the highest-blast-radius operation in the suite (multi-file
mutation plus a rename), so the coverage here is deliberately thorough:

- tombstone: the four fields, the `MERGED-INTO-P-survivor__` rename grammar
  (checked against lint's own filename regex), the file kept forever, and
  `aliases:` reduced to the bare P-id;
- folds: name variants including the `{value:, restricted: true}` mapping form
  (round-tripped as a real boolean, and its value kept OUT of the survivor's
  plain aliases: list), the birth, married and other names folded by kind
  (an empty survivor field filled, a taken one sending the name to
  name_variants with a preview line, also_known_as appended in order),
  external-id folding with the same-key conflict kept on
  the survivor + warned + exit 1, relationship entries deduped by
  to+type+subtype with survivor-self edges skipped;
- claim relink across EVERY status and every taught person form (bare id,
  `[[P-id]]`, `[[P-id|Name]]`, a resolving name alias), survivor-already-listed
  dedupe, sibling claims byte-untouched, and the per-file guard turning a
  broken block into a refusal with zero writes anywhere;
- other-record relink (profile `relationships:` targets, source `people:`
  lists), prose mentions counted but never touched (the W107 contract);
- idempotent `already`, the full refusal matrix, a byte-identical `--dry-run`,
  rollback on an injected write failure and on a rename failure;
- the alias surface after a merge: the tombstone registers ONLY its bare
  P-id (unit + example-archive reproduction - the folded name resolves to
  the survivor only, no fresh W112, `[[old name]]` still resolves), and a
  fold onto a no-aliases survivor creates the list with the survivor's own
  id first (no instant W111);
- the tombstone is never written to after the merge: `fha confirm draft`
  refuses it naming the survivor (case-insensitive status, like set-living);
- the strict pre-write guard (`_lib.frontmatter_edit_problem` with the
  merge's intended-keys sets) and the shared FRONT_RE-exact fence grammar;
- generated companion views (timeline/sources-index/draft-queue) of the
  merged person are deleted (GENERATED-verified; human lookalikes kept and
  named), the tombstone's `tier:` is stripped so refresh does not regrow
  them, dry-run previews the deletions, rollback restores them;
- an end-to-end pass against a copy of `example-archive` proving the
  post-merge archive lints with no W115 and no new W107 (claims included).

Fixtures only (AGENTS_TOOLING §5): a synthetic mini-archive for the surgical
assertions (fast, byte-precise) and a temp copy of `example-archive` for the
lint/index integration - the real archive is never a test bed.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import confirm
from _lib import (EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS, normalize_id, person_alias_mirror,
                  person_restricted_names, read_record)

EXAMPLE = ROOT / 'example-archive'

SURVIVOR = 'P-aaaaaaaaaa'
MERGED = 'P-bbbbbbbbbb'
OTHER = 'P-cccccccccc'      # Nancy Webb, the profile whose edges get repointed
THIRD = 'P-dddddddddd'      # John Reed, target of the folded relationship
SOURCE = 'S-eeeeeeeeee'

SURVIVOR_FILE = '''---
id: P-aaaaaaaaaa
name: Thomas Edward Hartley
name_variants: [T. E. Hartley]
aliases: [P-aaaaaaaaaa, Thomas Edward Hartley, T. E. Hartley]
sex: M
living: false
external_ids:
  wikitree: Hartley-1
relationships:
  - to: "[[P-cccccccccc|Nancy Webb]]"
    type: spouse
    subtype: social
    claims: ["[[C-ff00000001]]"]
created: 2026-01-01
tier: curated
---

# Thomas Edward Hartley

A curated survivor fixture.
'''

MERGED_FILE = '''---
id: P-bbbbbbbbbb
name: Thos. Hartley
name_variants:
  - "T.E.H."
  - {value: Tommy Hartley, restricted: true}
aliases: [P-bbbbbbbbbb, Thos. Hartley, Tommy]
external_ids:
  wikitree: Hartley-2
  ancestry: "1234"
living: false
relationships:
  - to: "[[P-aaaaaaaaaa|Thomas Edward Hartley]]"
    type: sibling
    status: hypothesis
  - to: "[[P-cccccccccc|Nancy Webb]]"
    type: spouse
    subtype: social
    claims: ["[[C-ff00000001]]"]
  - to: "[[P-dddddddddd|John Reed]]"
    type: friend
    subtype: friend
    claims: ["[[C-ff00000004]]"]
created: 2026-01-02
tier: stub
---

# Thos. Hartley

A duplicate stub fixture.
'''

OTHER_FILE = '''---
id: P-cccccccccc
name: Nancy Webb
aliases: [P-cccccccccc, Nancy Webb]
living: false
relationships:
  - to: "[[P-aaaaaaaaaa|Thomas Edward Hartley]]"
    type: spouse
    subtype: social
    claims: ["[[C-ff00000001]]"]
  - to: "[[P-bbbbbbbbbb|Thos. Hartley]]"
    type: friend
    subtype: friend
    claims: ["[[C-ff00000004]]"]
created: 2026-01-01
tier: stub
---

# Nancy Webb

Nancy knew [[P-bbbbbbbbbb|Thos. Hartley]] from church.
'''

THIRD_FILE = '''---
id: P-dddddddddd
name: John Reed
aliases: [P-dddddddddd, John Reed]
living: false
created: 2026-01-01
tier: stub
---

# John Reed
'''

SOURCE_FILE = '''---
id: S-eeeeeeeeee
title: Test family notes
source_type: other
people:
  - "[[P-bbbbbbbbbb|Thos. Hartley]]"
  - "[[P-cccccccccc|Nancy Webb]]"
created: 2026-01-02
---

## Claims
```yaml
- value: "Thomas and Nancy: spouse"
  id: C-ff00000001
  type: relationship
  subtype: social
  persons: [P-aaaaaaaaaa, P-cccccccccc]
  roles:
    spouse: [P-aaaaaaaaaa, P-cccccccccc]
  status: accepted
  reviewed: 2026-01-03

- value: "birth suggested - bare id"
  id: C-ff00000002
  type: birth
  persons: [P-bbbbbbbbbb]
  status: suggested

- value: "death accepted - wikilink"
  id: C-ff00000003
  type: death
  persons: ["[[P-bbbbbbbbbb]]"]
  status: accepted
  reviewed: 2026-01-03

- value: "friendship - piped link plus third party"
  id: C-ff00000004
  type: relationship
  subtype: friend
  persons: ["[[P-bbbbbbbbbb|Thos. Hartley]]", P-dddddddddd]
  roles:
    friend: [P-bbbbbbbbbb, P-dddddddddd]
  status: accepted
  reviewed: 2026-01-03

- value: "occupation rejected - alias name"
  id: C-ff00000005
  type: occupation
  persons: ["[[Tommy]]"]
  status: rejected

- value: "note superseded - survivor already listed"
  id: C-ff00000006
  type: note
  persons: [P-bbbbbbbbbb, P-aaaaaaaaaa]
  status: superseded

- value: "event parked - roles block"
  id: C-ff00000007
  type: event
  persons: [P-bbbbbbbbbb]
  roles:
    witness: [P-bbbbbbbbbb]
  status: suggested
  reviewed: 2026-01-03

- value: "disputed relationship naming both - evidence"
  id: C-ff00000008
  type: relationship
  subtype: associate
  persons: [P-aaaaaaaaaa, P-bbbbbbbbbb]
  roles:
    associate: [P-aaaaaaaaaa, P-bbbbbbbbbb]
  status: disputed

- value: "sibling claim untouched"
  id: C-ff00000009
  type: birth
  persons: [P-cccccccccc]
  status: accepted
  reviewed: 2026-01-03
```
'''

BROKEN_SOURCE_FILE = '''---
id: S-ffffffffff
title: Broken block naming the merged person
source_type: other
created: 2026-01-02
---

## Claims
```yaml
- value: "broken
  id: C-ff00000010
  persons: [P-bbbbbbbbbb]
  status: suggested
```
'''

UNFENCED_SOURCE_FILE = '''---
id: S-gggggggggg
title: Unfenced claims naming the merged person
source_type: other
created: 2026-01-02
---

## Claims
- value: "unfenced claim"
  id: C-ff00000011
  type: note
  persons: [P-bbbbbbbbbb]
  status: suggested
'''

NOTES_FILE = '''# Research notes

- saw P-bbbbbbbbbb in the 1880 census index (chase this)
'''


def build_archive(tmp: Path, *, broken_source: bool = False,
                  unfenced_source: bool = False) -> Path:
    """Write the synthetic mini-archive the surgical tests run against."""
    root = tmp / 'arc'
    (root / 'people' / '040 Thomas').mkdir(parents=True)
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'people' / 'connections').mkdir(parents=True)
    (root / 'sources' / 'other').mkdir(parents=True)
    (root / 'notes').mkdir(parents=True)
    (root / 'fha.yaml').write_text('archive: merge-test\n', encoding='utf-8')
    (root / 'people' / '040 Thomas' / 'hartley__thomas_edward_P-aaaaaaaaaa.md'
     ).write_text(SURVIVOR_FILE, encoding='utf-8')
    (root / 'people' / 'stubs' / 'hartley__thos_P-bbbbbbbbbb.md'
     ).write_text(MERGED_FILE, encoding='utf-8')
    (root / 'people' / 'connections' / 'webb__nancy_P-cccccccccc.md'
     ).write_text(OTHER_FILE, encoding='utf-8')
    (root / 'people' / 'stubs' / 'reed__john_P-dddddddddd.md'
     ).write_text(THIRD_FILE, encoding='utf-8')
    (root / 'sources' / 'other' / 'test-notes_S-eeeeeeeeee.md'
     ).write_text(SOURCE_FILE, encoding='utf-8')
    (root / 'notes' / 'research.md').write_text(NOTES_FILE, encoding='utf-8')
    if broken_source:
        (root / 'sources' / 'other' / 'broken_S-ffffffffff.md'
         ).write_text(BROKEN_SOURCE_FILE, encoding='utf-8')
    if unfenced_source:
        (root / 'sources' / 'other' / 'unfenced_S-gggggggggg.md'
         ).write_text(UNFENCED_SOURCE_FILE, encoding='utf-8')
    return root


def tree_state(root: Path) -> dict[str, str]:
    """Path -> content-hash map of every file, for byte-identity assertions."""
    state = {}
    for p in sorted(root.rglob('*')):
        if p.is_file():
            state[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return state


def run_merge(root: Path, merged: str = MERGED, into: str = SURVIVOR,
              reason: str = 'same person: census and probate agree',
              dry_run: bool = False):
    return confirm.run_confirm_merge(
        root, person_merged=merged, into=into, reason=reason, dry_run=dry_run)


def _scandir_denying(unreadable: Path):
    """An os.scandir stand-in that refuses to list `unreadable`.

    The fault goes in at `os.scandir` because `os.walk` resolves it at call
    time on every supported Python - that is what makes the `onerror` seam
    observable here. chmod cannot produce this: CI runs as root, which ignores
    mode bits, and Windows has no equivalent.

    What this deliberately does NOT rely on: that pathlib's `rglob` reaches the
    disk the same way. It does on 3.11/3.12/3.14, but NOT on the 3.10 floor
    (pathlib routes through an accessor object that bound `os.scandir` at
    import time, so a later patch is invisible) and not on 3.13. So the
    injection does not reproduce the pre-fix `rglob` behaviour on every version
    we support - a regression back to `rglob` is still caught everywhere, but
    on the floor it is caught by the warning going missing rather than by the
    folder reading as empty.
    """
    real_scandir = os.scandir
    target = unreadable.resolve()

    def scandir(path='.'):
        try:
            denied = Path(path).resolve() == target
        except (TypeError, ValueError, OSError):
            denied = False
        if denied:
            err = PermissionError(13, 'Permission denied')
            err.filename = str(path)
            raise err
        return real_scandir(path)

    return scandir


class MergeUnreadableFolderTests(unittest.TestCase):
    """A merge planned from a partial walk is refused, not half-applied.

    The merge rewrites claims in every source that names the person, rewrites
    other profiles, deletes generated views and renames the record. All of it
    is decided by walking the tree, so a folder that will not list leaves
    claims pointing at a tombstone forever while the run reports how many it
    relinked - and the human has nothing telling him which."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_archive(Path(self._tmp.name))
        self.merged_path = (self.root / 'people' / 'stubs'
                            / 'hartley__thos_P-bbbbbbbbbb.md')
        self.source_path = (self.root / 'sources' / 'other'
                            / 'test-notes_S-eeeeeeeeee.md')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _snapshot(self) -> dict:
        return {str(q.relative_to(self.root)):
                hashlib.sha256(q.read_bytes()).hexdigest()
                for q in sorted(self.root.rglob('*')) if q.is_file()}

    def test_an_unreadable_source_folder_refuses_the_merge_untouched(self) -> None:
        before = self._snapshot()
        with mock.patch('os.scandir',
                        new=_scandir_denying(self.root / 'sources' / 'other')):
            result = run_merge(self.root)
        self.assertEqual(result['status'], 'unreadable-folders')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('was NOT made', text)
        self.assertIn('sources/other', text)
        self.assertIn('Nothing was changed', text)
        self.assertEqual(self._snapshot(), before)

    def test_a_person_hidden_by_a_shut_folder_is_not_reported_as_a_bad_id(self) -> None:
        # The worst wording failure this prevents: "No person record for
        # P-bbbbbbbbbb - check the ID" while the record sits in plain sight
        # inside the folder that would not open.
        with mock.patch('os.scandir',
                        new=_scandir_denying(self.merged_path.parent)):
            result = run_merge(self.root)
        self.assertEqual(result['status'], 'unreadable-folders')
        text = ' '.join(m.text for m in result.messages)
        self.assertNotIn('Check the ID', text)
        self.assertIn('people/stubs', text)

    def test_the_dry_run_refuses_identically(self) -> None:
        with mock.patch('os.scandir',
                        new=_scandir_denying(self.root / 'sources' / 'other')):
            result = run_merge(self.root, dry_run=True)
        self.assertEqual(result['status'], 'unreadable-folders')
        self.assertEqual(result.exit_code, EXIT_FAILURE)


# ── Pure-text helpers ────────────────────────────────────────────────────────────

class MergeHelperTests(unittest.TestCase):
    def test_split_flow_items_respects_brackets_and_quotes(self) -> None:
        items = confirm._split_flow_items('P-aaaaaaaaaa, "[[P-bbbbbbbbbb|Smith, John]]", [P-cccccccccc]')
        self.assertEqual(items, ['P-aaaaaaaaaa', '"[[P-bbbbbbbbbb|Smith, John]]"', '[P-cccccccccc]'])

    def test_rewrite_list_value_replaces_and_dedupes(self) -> None:
        # The engine passes NORMALIZED (lowercase) ids; the display form is
        # re-uppercased on write via fmt_id_display.
        token_re = confirm._person_token_re(MERGED.lower())
        new, changed = confirm._rewrite_person_list_value(
            f'[{MERGED}, {SURVIVOR}]', MERGED.lower(), SURVIVOR.lower(), {}, token_re)
        self.assertTrue(changed)
        self.assertEqual(new, f'[{SURVIVOR.lower()}]')

    def test_rewrite_list_value_pins_a_name_alias(self) -> None:
        token_re = confirm._person_token_re(MERGED.lower())
        alias_map = {'tommy': MERGED.lower()}
        new, changed = confirm._rewrite_person_list_value(
            '["[[Tommy]]"]', MERGED.lower(), SURVIVOR.lower(), alias_map, token_re)
        self.assertTrue(changed)
        self.assertEqual(new, f'["[[{SURVIVOR.lower()}|Tommy]]"]')

    def test_rewrite_ref_item_escapes_a_quote_in_the_display_name(self) -> None:
        # Sweep of PR #30's YAML-quoting review fixes: `display` comes from
        # the item's OWN existing text - a hand-typed, UNQUOTED wikilink
        # naming a nickname-in-quotes ("Tommy \"Slim\" Hartley") is not
        # unusual in genealogy prose, and was never validated by the merge.
        # Spliced unescaped into the hand-built double-quoted `"[[...]]"`
        # scalar this produces, an embedded `"` used to corrupt the result.
        token_re = confirm._person_token_re(MERGED.lower())
        survivor_disp = confirm.fmt_id_display(SURVIVOR.lower())
        new = confirm._rewrite_ref_item('[[Tommy "Slim" Hartley]]', token_re, survivor_disp)
        self.assertEqual(new, f'"[[{SURVIVOR.lower()}|Tommy \\"Slim\\" Hartley]]"')
        # And the result is genuinely valid YAML, not just accepted by this
        # codebase's own hand-rolled text scanners.
        import yaml
        parsed = yaml.safe_load(f'x: [{new}]')
        self.assertEqual(parsed['x'], [f'[[{SURVIVOR.lower()}|Tommy "Slim" Hartley]]'])

    def test_render_scalar_round_trips_a_restricted_mapping(self) -> None:
        # read_record coerces booleans to 'true'; the fold must write the real
        # boolean back so the mapping form survives verbatim.
        rendered = confirm._render_scalar({'value': 'Tommy Hartley', 'restricted': 'true'})
        self.assertEqual(rendered, '{value: Tommy Hartley, restricted: true}')

    def test_rename_grammar_matches_lint(self) -> None:
        import lint
        stem = f'MERGED-INTO-{SURVIVOR}__hartley__thos_{MERGED}'
        self.assertIsNotNone(lint._PERSON_FILENAME_RE.match(stem))


class MergedAliasFilterTests(unittest.TestCase):
    """A tombstone registers ONLY its bare P-id on the alias surface: the
    tombstone keeps its `name:` for readability, but the merge folded that
    name into the survivor, and registering it from both records would clash
    the folded name out of every resolve map - the merge itself would break
    the `[[Name]]` links it just preserved (plus a fresh W112 per merge)."""

    def test_tombstone_registers_only_its_bare_id(self) -> None:
        from _lib import alias_clashes, build_alias_map
        records = [
            {'id': SURVIVOR.lower(), 'name': 'Thomas Edward Hartley',
             'name_variants': ['Thos. Hartley'],
             'aliases': [SURVIVOR, 'Thomas Edward Hartley', 'Thos. Hartley']},
            # The tombstone: status casing is deliberately hand-edited-shaped -
            # the filter must normalize, exactly like every merged guard.
            {'id': MERGED.lower(), 'name': 'Thos. Hartley',
             'aliases': [MERGED], 'status': 'Merged',
             'merged_into': SURVIVOR.lower()},
        ]
        amap = build_alias_map(records)
        # The folded name resolves to the SURVIVOR only; no clash anywhere.
        self.assertEqual(amap.get('thos. hartley'), SURVIVOR.lower())
        self.assertEqual(alias_clashes(records), {})
        # The tombstone's bare id still resolves (readers walk merged_into).
        self.assertEqual(amap.get(MERGED.lower()), MERGED.lower())

    def test_unmerged_records_still_clash_on_a_shared_name(self) -> None:
        # The filter is scoped to merged tombstones: two LIVING same-name
        # records remain a clash (SPEC §7 - never guess between them).
        from _lib import alias_clashes
        records = [
            {'id': SURVIVOR.lower(), 'name': 'John Smith'},
            {'id': MERGED.lower(), 'name': 'John Smith'},
        ]
        self.assertIn('john smith', alias_clashes(records))


class MergeGuardTests(unittest.TestCase):
    """The merge's pre-write guard is the shared `_lib.frontmatter_edit_problem`
    with the merge's intended-keys sets - the same strictness `fha person
    set-living` applies, scoped to the keys the merge means to touch."""

    BEFORE = (
        f'---\nid: {SURVIVOR}\nname: Thomas Edward Hartley\nliving: false\n'
        f'created: 2026-01-01\n---\n\n# Thomas\n'
    )

    def test_guard_refuses_an_unintended_field_change(self) -> None:
        tampered = self.BEFORE.replace('name: Thomas Edward Hartley',
                                       'name: Someone Else')
        problem = confirm._merge_fm_problem(
            self.BEFORE, tampered, confirm._MERGE_TOMBSTONE_KEYS)
        self.assertIsNotNone(problem)
        self.assertIn("'name'", problem)

    def test_guard_refuses_an_unintended_field_disappearing(self) -> None:
        tampered = self.BEFORE.replace('living: false\n', '')
        problem = confirm._merge_fm_problem(
            self.BEFORE, tampered, confirm._MERGE_SURVIVOR_KEYS)
        self.assertIsNotNone(problem)
        self.assertIn('appear or disappear', problem)

    def test_guard_refuses_an_id_change(self) -> None:
        tampered = self.BEFORE.replace(SURVIVOR, MERGED)
        problem = confirm._merge_fm_problem(
            self.BEFORE, tampered, confirm._MERGE_TOMBSTONE_KEYS)
        self.assertEqual(problem, 'the id: field would change')

    def test_guard_accepts_exactly_the_tombstone_intent(self) -> None:
        after = (
            f'---\nid: {SURVIVOR}\nname: Thomas Edward Hartley\nliving: false\n'
            f'created: 2026-01-01\naliases: [{SURVIVOR}]\nstatus: merged\n'
            f'merged_into: {MERGED}\nmerge_reason: duplicate\n'
            f'merged_date: 2026-07-10\n---\n\n# Thomas\n'
        )
        self.assertIsNone(confirm._merge_fm_problem(
            self.BEFORE, after, confirm._MERGE_TOMBSTONE_KEYS))

    def test_fence_grammar_matches_read_record(self) -> None:
        # The shared fence locator only accepts what FRONT_RE/read_record
        # parse as frontmatter: exactly `---` at column zero. An indented or
        # trailing-space fence is prose to the readers, so no surgical editor
        # may treat it as frontmatter either (person.py and confirm.py used
        # to disagree here).
        from _lib import frontmatter_fence_span
        self.assertEqual(frontmatter_fence_span(['---', 'id: x', '---']), (0, 2))
        self.assertEqual(frontmatter_fence_span(['---\r', 'id: x', '---\r']), (0, 2))
        self.assertIsNone(frontmatter_fence_span(['  ---', 'id: x', '---']))
        self.assertIsNone(frontmatter_fence_span(['---   ', 'id: x', '---']))
        # A decorated `---` is skipped, not treated as the close - the span
        # runs to the next EXACT fence, exactly as FRONT_RE's grammar does.
        self.assertEqual(
            frontmatter_fence_span(['---', 'id: x', '---   ', 'more', '---']),
            (0, 4))


class MergeTombstoneLivingTests(unittest.TestCase):
    """#304 round 5 backstop: the tombstone `fha confirm merge` writes says
    what the survivor says about being alive when it said nothing itself
    (`unknown`, the stub default, or no line) - so an ordinary stub merge
    leaves nothing that could hold a dead ancestor back as living. An
    explicit `living: true` on the merged record is someone's word and stays."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_archive(Path(self._tmp.name))
        self.merged_path = self.root / 'people' / 'stubs' / 'hartley__thos_P-bbbbbbbbbb.md'
        self.tombstone_path = (self.root / 'people' / 'stubs'
                               / 'MERGED-INTO-p-aaaaaaaaaa__hartley__thos_P-bbbbbbbbbb.md')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _merge_with(self, living_line: str) -> str:
        text = self.merged_path.read_text(encoding='utf-8')
        self.merged_path.write_text(text.replace('living: false\n', living_line), encoding='utf-8')
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok', [m.text for m in result.messages])
        return str(read_record(self.tombstone_path)['meta'].get('living')).lower()

    def test_unknown_takes_the_survivors_value(self) -> None:
        self.assertEqual(self._merge_with('living: unknown\n'), 'false')

    def test_a_missing_line_takes_the_survivors_value(self) -> None:
        self.assertEqual(self._merge_with(''), 'false')

    def test_an_explicit_true_stays(self) -> None:
        self.assertEqual(self._merge_with('living: true\n'), 'true')

    def test_a_living_survivors_true_is_not_copied(self) -> None:
        # A cousin alive at the merge dies later and is marked `living: false`.
        # A `true` copied onto the tombstone would keep hiding him, and only a
        # hand edit of the merged file would release him. Only `false` copies.
        survivor = next(p for p in (self.root / 'people').rglob('*_P-aaaaaaaaaa.md')
                        if not p.name.startswith('MERGED-INTO'))
        text = survivor.read_text(encoding='utf-8')
        self.assertIn('living: false\n', text)
        survivor.write_text(text.replace('living: false\n', 'living: true\n'), encoding='utf-8')
        self.assertEqual(self._merge_with('living: unknown\n'), 'unknown')


# ── The synthetic-archive matrix ─────────────────────────────────────────────────

class MergeArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_archive(Path(self._tmp.name))
        self.survivor_path = self.root / 'people' / '040 Thomas' / 'hartley__thomas_edward_P-aaaaaaaaaa.md'
        self.merged_path = self.root / 'people' / 'stubs' / 'hartley__thos_P-bbbbbbbbbb.md'
        self.tombstone_path = self.root / 'people' / 'stubs' / \
            'MERGED-INTO-p-aaaaaaaaaa__hartley__thos_P-bbbbbbbbbb.md'
        self.other_path = self.root / 'people' / 'connections' / 'webb__nancy_P-cccccccccc.md'
        self.source_path = self.root / 'sources' / 'other' / 'test-notes_S-eeeeeeeeee.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_restricted_nickname_on_the_merged_record_never_joins_the_survivor(self) -> None:
        # A hand edit had copied a private nickname into the merged record's
        # aliases:; its marker sits in also_known_as:. The survivor gets the
        # nickname with its marker, never as plain text in aliases:.
        text = self.merged_path.read_text(encoding='utf-8')
        text = text.replace('aliases: [P-bbbbbbbbbb, Thos. Hartley, Tommy]',
                            'aliases: [P-bbbbbbbbbb, Thos. Hartley, Tommy, Quenby]\n'
                            'also_known_as:\n  - {value: Quenby, restricted: true}')
        self.merged_path.write_text(text, encoding='utf-8')
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        aliases = [str(a) for a in read_record(self.survivor_path)['meta']['aliases']]
        self.assertIn('Tommy', aliases)
        self.assertNotIn('Quenby', aliases)
        aka = read_record(self.survivor_path)['meta']['also_known_as']
        self.assertEqual(aka[0]['value'], 'Quenby')
        self.assertEqual(str(aka[0]['restricted']), 'true')

    # Full round trip -----------------------------------------------------------

    def test_merge_round_trip(self) -> None:
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        # external-id conflict (and evidence warnings) => exit 1, merge landed
        self.assertEqual(result.exit_code, EXIT_WARNINGS)

        # Tombstone: renamed and kept, never deleted.
        self.assertFalse(self.merged_path.exists())
        self.assertTrue(self.tombstone_path.exists())
        rec = read_record(self.tombstone_path)
        meta = rec['meta']
        self.assertEqual(meta['status'], 'merged')
        self.assertEqual(normalize_id(str(meta['merged_into'])), SURVIVOR.lower())
        self.assertEqual(meta['merge_reason'], 'same person: census and probate agree')
        self.assertTrue(str(meta['merged_date']))
        # aliases reduced to the bare P-id; folded keys stripped.
        self.assertEqual([normalize_id(str(a)) for a in meta['aliases']], [MERGED.lower()])
        self.assertNotIn('name_variants', meta)
        self.assertNotIn('relationships', meta)
        # the conflicting external id stays on the tombstone; the folded one goes
        self.assertEqual(meta.get('external_ids'), {'wikitree': 'Hartley-2'})
        self.assertEqual(meta['name'], 'Thos. Hartley')   # identity stays readable

        # Survivor folds.
        srec = read_record(self.survivor_path)
        smeta = srec['meta']
        variants = smeta['name_variants']
        self.assertIn('T. E. Hartley', variants)
        self.assertIn('Thos. Hartley', variants)
        self.assertIn('T.E.H.', variants)
        restricted = [v for v in variants if isinstance(v, dict)]
        self.assertEqual(len(restricted), 1)
        self.assertEqual(restricted[0]['value'], 'Tommy Hartley')
        self.assertEqual(str(restricted[0]['restricted']), 'true')
        # restricted value stays OUT of the plain aliases list; public names join it
        aliases = [str(a) for a in smeta['aliases']]
        self.assertIn('Thos. Hartley', aliases)
        self.assertIn('Tommy', aliases)
        self.assertNotIn('Tommy Hartley', aliases)
        # external ids: survivor's value kept on conflict; missing key folded
        self.assertEqual(smeta['external_ids'],
                         {'wikitree': 'Hartley-1', 'ancestry': '1234'})
        # relationships: friend edge folded; duplicate spouse edge not doubled;
        # survivor-self (sibling) edge skipped
        rels = smeta['relationships']
        self.assertEqual(len(rels), 2)
        types = sorted(str(e['type']) for e in rels)
        self.assertEqual(types, ['friend', 'spouse'])

        # Claims: every status relinked; no reference to the merged id remains.
        claims = {c['id']: c for c in read_record(self.source_path)['claims']}
        source_text = self.source_path.read_text(encoding='utf-8')
        self.assertNotIn(MERGED.lower(), source_text.lower())
        self.assertEqual(claims['C-ff00000002']['persons'], ['p-aaaaaaaaaa'])
        self.assertEqual(claims['C-ff00000003']['persons'], ['[[p-aaaaaaaaaa]]'])
        self.assertEqual(claims['C-ff00000004']['persons'][0], '[[p-aaaaaaaaaa|Thos. Hartley]]')
        self.assertEqual(claims['C-ff00000005']['persons'], ['[[p-aaaaaaaaaa|Tommy]]'])
        # survivor-already-listed dedupe (persons and roles)
        self.assertEqual(claims['C-ff00000006']['persons'], ['p-aaaaaaaaaa'])
        self.assertEqual(claims['C-ff00000007']['roles']['witness'], ['p-aaaaaaaaaa'])
        self.assertEqual(claims['C-ff00000008']['persons'], ['P-aaaaaaaaaa'])
        self.assertEqual(claims['C-ff00000008']['roles']['associate'], ['P-aaaaaaaaaa'])
        self.assertEqual(result['relinked_claims'], 7)

        # Source frontmatter people: repointed, display kept.
        speople = read_record(self.source_path)['meta']['people']
        self.assertEqual(speople[0], '[[p-aaaaaaaaaa|Thos. Hartley]]')

        # Other profile: relationships target repointed; prose untouched.
        otext = self.other_path.read_text(encoding='utf-8')
        orec = read_record(self.other_path)
        targets = [str(e['to']) for e in orec['meta']['relationships']]
        self.assertNotIn(MERGED.lower(), ' '.join(targets).lower())
        self.assertIn('Nancy knew [[P-bbbbbbbbbb|Thos. Hartley]] from church.', otext)
        self.assertEqual(result['relinked_profiles'], 2)

        # Prose mentions counted, not touched.
        self.assertEqual(result['prose_refs_remaining'], 2)
        notes = (self.root / 'notes' / 'research.md').read_text(encoding='utf-8')
        self.assertIn('P-bbbbbbbbbb', notes)

        # changed[] lists exactly the files written/renamed.
        changed = {Path(p).name for p in result.changed}
        self.assertEqual(changed, {
            self.survivor_path.name, self.tombstone_path.name,
            self.source_path.name, self.other_path.name,
        })

        # Warnings name both conflicting external-id values and the evidence claim.
        warnings = ' | '.join(m.text for m in result.messages if m.level == 'warning')
        self.assertIn('Hartley-1', warnings)
        self.assertIn('Hartley-2', warnings)
        self.assertIn('c-ff00000008', warnings)
        self.assertIn('sibling', warnings)     # the skipped survivor-self edge

    # One entry per person and role (SPEC §9) -----------------------------------

    def test_a_tie_both_records_list_becomes_one_entry_with_both_claims(self) -> None:
        # The duplicate record backs the same spouse tie with a different
        # claim: the survivor keeps ONE spouse entry for Nancy, listing both.
        text = self.merged_path.read_text(encoding='utf-8')
        self.merged_path.write_text(
            text.replace('    claims: ["[[C-ff00000001]]"]', '    claims: ["[[C-ff00000009]]"]'),
            encoding='utf-8')
        run_merge(self.root)
        rels = read_record(self.survivor_path)['meta']['relationships']
        spouse = [e for e in rels if str(e['type']) == 'spouse']
        self.assertEqual(len(spouse), 1)
        self.assertEqual(spouse[0]['claims'], ['[[c-ff00000001]]', '[[c-ff00000009]]'])
        friend = [e for e in rels if str(e['type']) == 'friend']
        self.assertEqual(friend[0]['claims'], ['[[c-ff00000004]]'])

    def test_a_repointed_entry_folds_into_the_one_already_naming_the_survivor(self) -> None:
        # Nancy's record lists Thomas as a friend AND (through the duplicate)
        # Thos. as a friend: after the merge that is one friend, once.
        text = self.other_path.read_text(encoding='utf-8')
        self.other_path.write_text(text.replace(
            '  - to: "[[P-bbbbbbbbbb|Thos. Hartley]]"',
            '  - to: "[[P-aaaaaaaaaa|Thomas Edward Hartley]]"\n'
            '    type: friend\n'
            '    subtype: friend\n'
            '    claims: ["[[C-ff00000010]]"]\n'
            '  - to: "[[P-bbbbbbbbbb|Thos. Hartley]]"'), encoding='utf-8')
        run_merge(self.root)
        rels = read_record(self.other_path)['meta']['relationships']
        friend = [e for e in rels if str(e['type']) == 'friend']
        self.assertEqual(len(friend), 1)
        self.assertEqual(friend[0]['claims'], ['[[c-ff00000010]]', '[[c-ff00000004]]'])

    def test_sibling_claims_and_records_byte_untouched(self) -> None:
        before = self.source_path.read_text(encoding='utf-8')
        run_merge(self.root)
        after = self.source_path.read_text(encoding='utf-8')
        for untouched in ('- value: "Thomas and Nancy: spouse"',
                          '  persons: [P-aaaaaaaaaa, P-cccccccccc]',
                          '- value: "sibling claim untouched"',
                          '  persons: [P-cccccccccc]'):
            self.assertIn(untouched, before)
            self.assertIn(untouched, after)
        third = self.root / 'people' / 'stubs' / 'reed__john_P-dddddddddd.md'
        self.assertEqual(third.read_text(encoding='utf-8'), THIRD_FILE)

    def test_fold_onto_no_aliases_survivor_writes_its_own_id_first(self) -> None:
        # A survivor with no aliases: key gets one created by the fold. A
        # non-empty aliases: list must carry the record's own ID (lint W111),
        # so the merge writes it first instead of minting an instant warning.
        no_alias = SURVIVOR_FILE.replace(
            'aliases: [P-aaaaaaaaaa, Thomas Edward Hartley, T. E. Hartley]\n', '')
        self.survivor_path.write_text(no_alias, encoding='utf-8')
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        smeta = read_record(self.survivor_path)['meta']
        entries = [str(a).strip() for a in smeta['aliases']]
        self.assertEqual(normalize_id(entries[0]), SURVIVOR.lower())
        self.assertIn('Tommy', entries)   # the merged stem still folded
        import lint
        self.assertTrue(lint._self_alias_ok(smeta, SURVIVOR))

    def test_survivor_written_before_the_mirror_gets_its_own_names_too(self) -> None:
        # A survivor whose aliases: holds only its ID (written before the tools
        # mirrored names) ends the merge with its own name and every public
        # variant listed (SPEC §9), the ID still first, so Obsidian's [[ list
        # finds the survivor by all of them.
        id_only = SURVIVOR_FILE.replace(
            'aliases: [P-aaaaaaaaaa, Thomas Edward Hartley, T. E. Hartley]\n',
            'aliases: [P-aaaaaaaaaa]\n')
        self.survivor_path.write_text(id_only, encoding='utf-8')
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        smeta = read_record(self.survivor_path)['meta']
        entries = [str(a).strip() for a in smeta['aliases']]
        self.assertEqual(normalize_id(entries[0]), SURVIVOR.lower())
        for name in ('Thomas Edward Hartley', 'T. E. Hartley', 'Thos. Hartley'):
            self.assertIn(name, entries)
        self.assertNotIn('Tommy Hartley', entries)   # restricted: never mirrored
        import lint
        self.assertEqual(lint._person_alias_gaps(smeta, SURVIVOR), ([], False))

    def test_accept_draft_refuses_a_merged_tombstone_naming_survivor(self) -> None:
        run_merge(self.root)
        # Give the tombstone an AI-DRAFT marker and a hand-edited status
        # casing: the refusal must come first and must be case-insensitive.
        text = self.tombstone_path.read_text(encoding='utf-8')
        text = text.replace('status: merged', 'status: Merged')
        text += '\n<!-- AI-DRAFT 2026-07-10 model=test -->\ndrafted prose\n'
        self.tombstone_path.write_text(text, encoding='utf-8')
        before = self.tombstone_path.read_bytes()
        r = confirm.run_accept_draft(self.root, person_id=MERGED)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'merged')
        msg = ' '.join(m.text for m in r.messages)
        self.assertIn(f'fha confirm draft {SURVIVOR.lower()}', msg)
        self.assertEqual(self.tombstone_path.read_bytes(), before)
        self.assertEqual(r.changed, [])

    def test_accept_draft_names_the_find_step_when_pointer_is_broken(self) -> None:
        run_merge(self.root)
        text = self.tombstone_path.read_text(encoding='utf-8')
        text = text.replace(f'merged_into: {SURVIVOR.lower()}', 'merged_into: nowhere')
        text += '\n<!-- AI-DRAFT 2026-07-10 model=test -->\ndrafted prose\n'
        self.tombstone_path.write_text(text, encoding='utf-8')
        r = confirm.run_accept_draft(self.root, person_id=MERGED)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'merged')
        self.assertIn(f'fha find {MERGED.lower()}', ' '.join(m.text for m in r.messages))

    # Companion views ------------------------------------------------------------

    GEN_TIMELINE = ('<!-- GENERATED by fha views timeline - do not edit -->\n'
                    '\n# Timeline\n')
    GEN_SOURCES = ('<!-- GENERATED by fha views sources-index - do not edit -->\n'
                   '\n# Sources\n')

    def _write_companions(self) -> tuple[Path, Path, Path, Path]:
        """Two GENERATED views + one human lookalike for the merged person,
        plus a GENERATED view for the SURVIVOR (which must never be touched)."""
        t = self.merged_path.with_name(f'hartley__thos_timeline_{MERGED}.md')
        s = self.merged_path.with_name(f'hartley__thos_sources-index_{MERGED}.md')
        h = self.merged_path.with_name(f'hartley__thos_draft-queue_{MERGED}.md')
        keep = self.survivor_path.with_name(
            f'hartley__thomas_edward_timeline_{SURVIVOR}.md')
        t.write_text(self.GEN_TIMELINE, encoding='utf-8')
        s.write_text(self.GEN_SOURCES, encoding='utf-8')
        h.write_text('# My hand-written notes at a companion filename\n',
                     encoding='utf-8')
        keep.write_text(self.GEN_TIMELINE, encoding='utf-8')
        return t, s, h, keep

    def test_merge_deletes_generated_views_and_strips_tier(self) -> None:
        t, s, h, keep = self._write_companions()
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        # GENERATED views of the merged person are deleted; the human
        # lookalike and the survivor's own view are untouched.
        self.assertFalse(t.exists())
        self.assertFalse(s.exists())
        self.assertTrue(h.exists())
        self.assertTrue(keep.exists())
        changed = {Path(p).name for p in result.changed}
        self.assertIn(t.name, changed)
        self.assertIn(s.name, changed)
        self.assertNotIn(h.name, changed)
        self.assertEqual({Path(p).name for p in result['deleted_views']},
                         {t.name, s.name})
        msgs = ' | '.join(m.text for m in result.messages)
        self.assertIn(t.name, msgs)
        # The human lookalike is named as a loose end, never deleted.
        warnings = ' | '.join(m.text for m in result.messages
                              if m.level == 'warning')
        self.assertIn(h.name, warnings)
        # The tombstone keeps no tier: a redirect is not a curated profile,
        # so `fha views refresh` will not regenerate views for it.
        meta = read_record(self.tombstone_path)['meta']
        self.assertNotIn('tier', meta)

    def test_dry_run_previews_view_deletion_and_deletes_nothing(self) -> None:
        t, s, h, keep = self._write_companions()
        before = tree_state(self.root)
        result = run_merge(self.root, dry_run=True)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(tree_state(self.root), before)
        text = '\n'.join(m.text for m in result.messages if m.level == 'info')
        self.assertIn('Would delete the generated companion view', text)
        self.assertIn(t.name, text)
        self.assertIn(s.name, text)
        self.assertNotIn(h.name, text)

    def test_rollback_restores_deleted_views_on_rename_failure(self) -> None:
        t, s, h, keep = self._write_companions()
        before = tree_state(self.root)
        with mock.patch.object(Path, 'rename', side_effect=OSError('locked')):
            r = run_merge(self.root)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r.changed, [])
        self.assertEqual(tree_state(self.root), before)

    # Dry-run --------------------------------------------------------------------

    def test_dry_run_writes_nothing_and_previews_everything(self) -> None:
        before = tree_state(self.root)
        result = run_merge(self.root, dry_run=True)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result.changed, [])
        self.assertEqual(tree_state(self.root), before)
        infos = [m.text for m in result.messages if m.level == 'info']
        text = '\n'.join(infos)
        self.assertIn('[dry-run] Would merge', text)
        self.assertIn('(before)', text)
        self.assertIn('(after)', text)
        self.assertIn('Would rename', text)
        self.assertIn(self.tombstone_path.name, text)
        self.assertIn('No file written', text)
        # the warning tier carries through the preview (conflict => exit 1)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)

    def test_dry_run_previews_exactly_the_live_write_set(self) -> None:
        dry = run_merge(self.root, dry_run=True)
        previewed = set()
        for m in dry.messages:
            if m.text.startswith('--- ') and m.text.endswith('(before)'):
                previewed.add(Path(m.text[4:-len(' (before)')]).name)
        live = run_merge(self.root)
        written = {Path(p).name for p in live.changed} - {self.tombstone_path.name}
        written.add(self.merged_path.name)   # the tombstone was edited at its old name
        self.assertEqual(previewed, written)

    # Idempotence ----------------------------------------------------------------

    def test_rerun_same_merge_is_a_clean_already_noop(self) -> None:
        run_merge(self.root)
        state = tree_state(self.root)
        again = run_merge(self.root)
        self.assertEqual(again['status'], 'already')
        self.assertEqual(again.exit_code, EXIT_CLEAN)
        self.assertEqual(again.changed, [])
        self.assertEqual(tree_state(self.root), state)

    # Refusals -------------------------------------------------------------------

    def test_refuses_self_merge(self) -> None:
        r = run_merge(self.root, merged=SURVIVOR, into=SURVIVOR)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'same-person')

    def test_refuses_malformed_and_unknown_ids(self) -> None:
        bad = run_merge(self.root, merged='P-nope')
        self.assertEqual(bad.exit_code, EXIT_FAILURE)
        self.assertEqual(bad['status'], 'invalid-id')
        missing = run_merge(self.root, merged='P-9999999999')
        self.assertEqual(missing.exit_code, EXIT_FAILURE)
        self.assertEqual(missing['status'], 'not-found')

    def test_refuses_empty_reason(self) -> None:
        r = run_merge(self.root, reason='   ')
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertIn('merge_reason', ' '.join(m.text for m in r.messages))

    def test_refuses_merge_into_a_tombstone_naming_the_final_survivor(self) -> None:
        run_merge(self.root)   # b -> a; b is now a tombstone
        r = run_merge(self.root, merged=OTHER, into=MERGED)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'merged-survivor')
        msg = ' '.join(m.text for m in r.messages)
        self.assertIn(SURVIVOR.lower(), msg)   # the chain's final survivor is named

    def test_refuses_remerge_into_a_different_survivor(self) -> None:
        run_merge(self.root)
        r = run_merge(self.root, merged=MERGED, into=OTHER)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'already-merged-elsewhere')

    def test_refuses_rename_collision_before_any_write(self) -> None:
        self.tombstone_path.write_text('in the way\n', encoding='utf-8')
        before = tree_state(self.root)
        r = run_merge(self.root)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'rename-collision')
        self.assertEqual(tree_state(self.root), before)

    def test_scan_never_lets_a_frontmatterless_lookalike_shadow_the_record(self) -> None:
        # A junk file whose name ends with the same _{P-id}.md suffix must not
        # become the P-id's profile. The decoy sorts before the real record on
        # every platform (Path order is case-insensitive on Windows only, which
        # is how the rename-collision test above passed here and failed on CI).
        decoy = self.merged_path.with_name(f'a__decoy_{MERGED}.md')
        decoy.write_text('in the way\n', encoding='utf-8')
        profiles = confirm._scan_person_profiles(self.root)
        self.assertEqual(profiles[MERGED.lower()][0], self.merged_path)

    def test_guard_refusal_on_broken_claims_block_writes_nothing(self) -> None:
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_archive(Path(self._tmp.name), broken_source=True)
        before = tree_state(self.root)
        r = run_merge(self.root)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'refused')
        msg = ' '.join(m.text for m in r.messages)
        self.assertIn('broken_S-ffffffffff.md', msg)
        self.assertIn('Nothing was written', msg)
        self.assertEqual(tree_state(self.root), before)

    def test_unfenced_claims_naming_merged_person_refuse(self) -> None:
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_archive(Path(self._tmp.name), unfenced_source=True)
        before = tree_state(self.root)
        r = run_merge(self.root)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'refused')
        self.assertIn('fix-claims-fence', ' '.join(m.text for m in r.messages))
        self.assertEqual(tree_state(self.root), before)

    # Rollback -------------------------------------------------------------------

    def test_rollback_on_injected_write_failure(self) -> None:
        before = tree_state(self.root)
        # The merge writes (and its undo journal restores) through the atomic
        # writer; this injection proves the JOURNAL replays the earlier,
        # already-completed writes. The atomic writer covers the other half -
        # the file being written when the failure lands - which
        # tests/test_atomic_record_writes.py pins directly.
        real_write = confirm.write_text_exact_atomic
        state = {'n': 0}

        def flaky(path, text):
            state['n'] += 1
            if state['n'] == 2:
                raise OSError('simulated disk full')
            return real_write(path, text)

        with mock.patch.object(confirm, 'write_text_exact_atomic', flaky):
            r = run_merge(self.root)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r.changed, [])
        self.assertIn('rolled back', ' '.join(m.text for m in r.messages))
        self.assertEqual(tree_state(self.root), before)

    def test_rollback_on_rename_failure(self) -> None:
        before = tree_state(self.root)
        with mock.patch.object(Path, 'rename', side_effect=OSError('locked')):
            r = run_merge(self.root)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r.changed, [])
        self.assertIn('rolled back', ' '.join(m.text for m in r.messages))
        self.assertEqual(tree_state(self.root), before)

    # CLI ------------------------------------------------------------------------

    def test_cli_dry_run_and_required_reason(self) -> None:
        code = confirm._standalone_main([
            'merge', MERGED, '--into', SURVIVOR,
            '--reason', 'duplicate stub', '--dry-run', '--root', str(self.root)])
        self.assertEqual(code, EXIT_WARNINGS)   # the external-id conflict warns
        with self.assertRaises(SystemExit):     # --reason is argparse-required
            confirm._standalone_main([
                'merge', MERGED, '--into', SURVIVOR, '--root', str(self.root)])


# ── Birth, married and other names (SPEC §9 single and list name fields) ─────────

def _add_fm_lines(text: str, lines: list[str], after: str = 'name:') -> str:
    """Insert frontmatter lines right after the first line starting `after`."""
    out = []
    done = False
    for line in text.split('\n'):
        out.append(line)
        if not done and line.startswith(after):
            out.extend(lines)
            done = True
    assert done, after
    return '\n'.join(out)


class MergeNameFieldTests(unittest.TestCase):
    """After a merge the survivor still says which name was the birth name,
    which the married name and which the nicknames: the merged record's
    `name_at_birth`, `married_name` and `also_known_as` land in the same
    fields, and a single field the survivor already fills differently sends
    the merged one to `name_variants`, said in the preview."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_archive(Path(self._tmp.name))
        self.survivor_path = self.root / 'people' / '040 Thomas' / 'hartley__thomas_edward_P-aaaaaaaaaa.md'
        self.merged_path = self.root / 'people' / 'stubs' / 'hartley__thos_P-bbbbbbbbbb.md'
        self.tombstone_path = self.root / 'people' / 'stubs' / \
            'MERGED-INTO-p-aaaaaaaaaa__hartley__thos_P-bbbbbbbbbb.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _edit(self, path: Path, lines: list[str], after: str = 'name:') -> None:
        path.write_text(_add_fm_lines(path.read_text(encoding='utf-8'), lines, after),
                        encoding='utf-8')

    def _survivor(self) -> dict:
        return read_record(self.survivor_path)['meta']

    def _infos(self, result) -> str:
        return '\n'.join(m.text for m in result.messages if m.level == 'info')

    # Single fields: every row of the rule, for both fields -----------------------

    def test_an_empty_survivor_field_takes_the_merged_name(self) -> None:
        for field in ('name_at_birth', 'married_name'):
            with self.subTest(field=field):
                self.tearDown()
                self.setUp()
                self._edit(self.merged_path, [f'{field}: Thomas E. Hartlee'])
                result = run_merge(self.root)
                self.assertEqual(result['status'], 'ok')
                smeta = self._survivor()
                self.assertEqual(smeta[field], 'Thomas E. Hartlee')
                self.assertNotIn('Thomas E. Hartlee',
                                 [str(v) for v in smeta['name_variants']])
                # The public name joins the survivor's aliases mirror.
                self.assertIn('Thomas E. Hartlee', [str(a) for a in smeta['aliases']])
                self.assertEqual(result['folded'][field], 1)
                self.assertIn(f'{field}: Thomas E. Hartlee (from p-bbbbbbbbbb)',
                              self._infos(result))

    def test_the_same_name_already_there_changes_nothing(self) -> None:
        for field in ('name_at_birth', 'married_name'):
            with self.subTest(field=field):
                self.tearDown()
                self.setUp()
                self._edit(self.survivor_path, [f'{field}: Thomas E. Hartlee'])
                self._edit(self.merged_path, [f'{field}: thomas e. HARTLEE'])
                result = run_merge(self.root)
                self.assertEqual(result['status'], 'ok')
                smeta = self._survivor()
                self.assertEqual(smeta[field], 'Thomas E. Hartlee')
                variants = [str(v).lower() for v in smeta['name_variants']]
                self.assertNotIn('thomas e. hartlee', variants)
                self.assertEqual(result['folded'][field], 0)

    def test_a_different_name_already_there_goes_to_name_variants(self) -> None:
        for field in ('name_at_birth', 'married_name'):
            with self.subTest(field=field):
                self.tearDown()
                self.setUp()
                self._edit(self.survivor_path, [f'{field}: Thomas Edward Hartlee'])
                self._edit(self.merged_path, [f'{field}: Tom Hartlee'])
                result = run_merge(self.root, dry_run=True)
                infos = self._infos(result)
                self.assertIn('name_variants: Tom Hartlee (from p-bbbbbbbbbb', infos)
                self.assertIn(f'{field} is already Thomas Edward Hartlee', infos)
                result = run_merge(self.root)
                self.assertEqual(result['status'], 'ok')
                smeta = self._survivor()
                self.assertEqual(smeta[field], 'Thomas Edward Hartlee')
                self.assertIn('Tom Hartlee', [str(v) for v in smeta['name_variants']])
                self.assertEqual(result['folded'][field], 0)
                self.assertIn(f'{field} is already Thomas Edward Hartlee',
                              self._infos(result))

    def test_an_empty_key_on_the_survivor_is_filled_in_place(self) -> None:
        self._edit(self.survivor_path, ['married_name:'], after='name_variants:')
        self._edit(self.merged_path, ['married_name: Tom Hartlee'])
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        lines = self.survivor_path.read_text(encoding='utf-8').split('\n')
        i = lines.index('married_name: Tom Hartlee')
        self.assertTrue(lines[i - 1].startswith('name_variants:'))
        self.assertEqual(sum(1 for ln in lines if ln.startswith('married_name')), 1)

    def test_a_new_single_field_lands_in_person_key_order(self) -> None:
        self._edit(self.merged_path, ['married_name: Tom Hartlee',
                                      'name_at_birth: Thomas Hartlee'])
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        keys = [ln.split(':', 1)[0] for ln in
                self.survivor_path.read_text(encoding='utf-8').split('\n')
                if ln and not ln.startswith((' ', '-', '#'))]
        nv = keys.index('name_variants')
        self.assertEqual(keys[nv + 1:nv + 3], ['name_at_birth', 'married_name'])

    # also_known_as: the list field ------------------------------------------------

    def test_also_known_as_appends_new_names_after_the_survivors_own(self) -> None:
        self._edit(self.survivor_path, ['also_known_as: [Ned, Teddy]'],
                   after='name_variants:')
        self._edit(self.merged_path, ['also_known_as: [Tad, teddy, Edd, T. E. Hartley]'])
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        # The survivor's two stay first, in his order; the new ones follow in
        # the merged record's order; the repeat and the name the survivor
        # already shows as a variant are skipped.
        self.assertEqual(self._survivor()['also_known_as'], ['Ned', 'Teddy', 'Tad', 'Edd'])
        self.assertEqual(result['folded']['also_known_as'], 2)
        aliases = [str(a) for a in self._survivor()['aliases']]
        self.assertIn('Tad', aliases)

    def test_also_known_as_block_list_keeps_its_form(self) -> None:
        self._edit(self.survivor_path, ['also_known_as:', '  - Ned'],
                   after='name_variants:')
        self._edit(self.merged_path, ['also_known_as:', '  - Tad'])
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        text = self.survivor_path.read_text(encoding='utf-8')
        self.assertIn('also_known_as:\n  - Ned\n  - Tad\n', text)

    def test_a_survivor_without_also_known_as_gets_it_in_key_order(self) -> None:
        self._edit(self.survivor_path, ['married_name: Tom Hartley'],
                   after='name_variants:')
        self._edit(self.merged_path, ['also_known_as: [Tad]'])
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        lines = self.survivor_path.read_text(encoding='utf-8').split('\n')
        i = lines.index('also_known_as: [Tad]')
        self.assertEqual(lines[i - 1], 'married_name: Tom Hartley')

    def test_a_typed_name_keeps_its_kind_over_the_plain_name(self) -> None:
        # The merged record's own name is also its married name: it arrives
        # as the survivor's married_name, not as a bare variant as well.
        self._edit(self.merged_path, ['married_name: Thos. Hartley'])
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        smeta = self._survivor()
        self.assertEqual(smeta['married_name'], 'Thos. Hartley')
        self.assertNotIn('Thos. Hartley', [str(v) for v in smeta['name_variants']])

    def test_a_name_shown_in_another_survivor_field_is_skipped(self) -> None:
        self._edit(self.merged_path, ['name_at_birth: "[[T. E. Hartley]]"',
                                      'also_known_as: [Thomas Edward Hartley]'])
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        smeta = self._survivor()
        self.assertNotIn('name_at_birth', smeta)
        self.assertNotIn('also_known_as', smeta)

    def test_the_merged_name_is_not_repeated_when_the_survivor_has_it_as_married_name(self) -> None:
        self._edit(self.survivor_path, ['married_name: thos. hartley'])
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        variants = [str(v).lower() for v in self._survivor()['name_variants']]
        self.assertNotIn('thos. hartley', variants)

    def test_the_tombstone_keeps_its_own_name_fields_as_written(self) -> None:
        lines = ['name_at_birth: Tom Hartlee', 'married_name: Thomas Hartley-Webb',
                 'also_known_as: [Tad]']
        self._edit(self.merged_path, lines)
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        text = self.tombstone_path.read_text(encoding='utf-8')
        for line in lines:
            self.assertIn(line + '\n', text)

    # Restricted names (SPEC §9, §19) ------------------------------------------------

    def test_restricted_names_travel_as_mappings_and_stay_out_of_aliases(self) -> None:
        self._edit(self.merged_path, [
            'name_at_birth: {value: Thomas Quenby, restricted: true}',
            'married_name: {value: Tom Quenby, restricted: true}',
            'also_known_as:', '  - {value: Quen, restricted: true}'])
        # Also mirrored by hand into the merged record's aliases:.
        text = self.merged_path.read_text(encoding='utf-8').replace(
            'aliases: [P-bbbbbbbbbb, Thos. Hartley, Tommy]',
            'aliases: [P-bbbbbbbbbb, Thos. Hartley, Tommy, Thomas Quenby, Tom Quenby, Quen]')
        self.merged_path.write_text(text, encoding='utf-8')
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        smeta = self._survivor()
        for field, value in (('name_at_birth', 'Thomas Quenby'),
                             ('married_name', 'Tom Quenby')):
            self.assertEqual(smeta[field]['value'], value)
            self.assertEqual(str(smeta[field]['restricted']), 'true')
        self.assertEqual(smeta['also_known_as'][0]['value'], 'Quen')
        self.assertEqual(str(smeta['also_known_as'][0]['restricted']), 'true')
        aliases = {str(a).lower() for a in smeta['aliases']}
        for value in ('thomas quenby', 'tom quenby', 'quen'):
            self.assertNotIn(value, aliases)
        self.assertIn('{value: Thomas Quenby, restricted: true}',
                      self.survivor_path.read_text(encoding='utf-8'))

    def test_a_restricted_name_moved_to_name_variants_keeps_its_marker(self) -> None:
        self._edit(self.survivor_path, ['name_at_birth: Thomas Edward Hartlee'])
        self._edit(self.merged_path, ['name_at_birth: {value: Tom Quenby, restricted: true}'])
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        smeta = self._survivor()
        moved = [v for v in smeta['name_variants']
                 if isinstance(v, dict) and v.get('value') == 'Tom Quenby']
        self.assertEqual(len(moved), 1)
        self.assertEqual(str(moved[0]['restricted']), 'true')
        self.assertNotIn('tom quenby', {str(a).lower() for a in smeta['aliases']})

    def test_a_name_private_in_one_field_stays_private_when_another_lists_it_plain(self) -> None:
        # The merged record marks "Tommy Hartley" private in name_variants
        # (the fixture) and also lists it as a plain nickname. On the merged
        # record that keeps it private everywhere; the merge must not hand
        # the survivor a plain copy that its aliases and exports read as
        # public.
        for field, line in (('also_known_as', 'also_known_as: [Tommy Hartley]'),
                            ('name_at_birth', 'name_at_birth: Tommy Hartley'),
                            ('married_name', 'married_name: Tommy Hartley')):
            with self.subTest(field=field):
                self.tearDown()
                self.setUp()
                self._edit(self.merged_path, [line])
                result = run_merge(self.root)
                self.assertEqual(result['status'], 'ok')
                smeta = self._survivor()
                self.assertIn('tommy hartley', person_restricted_names(smeta))
                self.assertNotIn('tommy hartley',
                                 {str(a).lower() for a in smeta['aliases']})
                self.assertNotIn('tommy hartley',
                                 {v.lower() for v in person_alias_mirror(smeta)})
                self.assertIn('Tommy Hartley (from p-bbbbbbbbbb, restricted)',
                              self._infos(result))

    def test_a_name_the_survivor_shows_plain_keeps_the_merged_records_marker(self) -> None:
        # The survivor lists "Tommy Hartley" as a plain nickname; the merged
        # record marks it private (the fixture's name_variants). The merge
        # strips the tombstone's name_variants, so without a private entry on
        # the survivor the marker would be gone from both records.
        for field, line in (('also_known_as', 'also_known_as: [Tommy Hartley]'),
                            ('married_name', 'married_name: Tommy Hartley')):
            with self.subTest(field=field):
                self.tearDown()
                self.setUp()
                self._edit(self.survivor_path, [line])
                result = run_merge(self.root, dry_run=True)
                self.assertIn('name_variants: Tommy Hartley (from p-bbbbbbbbbb, '
                              'restricted; the survivor lists it without the marker',
                              self._infos(result))
                result = run_merge(self.root)
                self.assertEqual(result['status'], 'ok')
                smeta = self._survivor()
                self.assertIn('tommy hartley', person_restricted_names(smeta))
                self.assertNotIn('tommy hartley',
                                 {v.lower() for v in person_alias_mirror(smeta)})
                self.assertNotIn('tommy hartley',
                                 {str(a).lower() for a in smeta['aliases']})

    def test_a_one_name_also_known_as_says_how_to_write_it_as_a_list(self) -> None:
        # `also_known_as: Ned` typed by hand cannot be extended in place; the
        # merge stops with nothing written and tells him the exact line to
        # write so it can run.
        self._edit(self.survivor_path, ['also_known_as: Ned'])
        self._edit(self.merged_path, ['also_known_as: [Tad]'])
        before = self.survivor_path.read_bytes()
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'refused')
        errors = '\n'.join(m.text for m in result.messages)
        self.assertIn('also_known_as: [Ned]', errors)
        self.assertIn('run the merge again', errors)
        self.assertEqual(self.survivor_path.read_bytes(), before)
        self.assertTrue(self.merged_path.exists())

    # Aliases: a mirrored name is not a hand-typed stem -------------------------------

    def test_a_mirrored_married_name_does_not_create_aliases_on_the_survivor(self) -> None:
        no_alias = SURVIVOR_FILE.replace(
            'aliases: [P-aaaaaaaaaa, Thomas Edward Hartley, T. E. Hartley]\n', '')
        self.survivor_path.write_text(no_alias, encoding='utf-8')
        text = self.merged_path.read_text(encoding='utf-8').replace(
            'aliases: [P-bbbbbbbbbb, Thos. Hartley, Tommy]',
            'aliases: [P-bbbbbbbbbb, Thos. Hartley, Tom Hartlee]')
        self.merged_path.write_text(text, encoding='utf-8')
        self._edit(self.merged_path, ['married_name: Tom Hartlee'])
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        smeta = self._survivor()
        self.assertEqual(smeta['married_name'], 'Tom Hartlee')
        self.assertNotIn('aliases', smeta)

    # Preview, CRLF, undo ---------------------------------------------------------------

    def test_dry_run_lists_every_name_and_writes_nothing(self) -> None:
        self._edit(self.survivor_path, ['name_at_birth: Thomas Edward Hartlee'])
        self._edit(self.merged_path, ['name_at_birth: Tom Hartlee',
                                      'married_name: Thomas Hartley-Webb',
                                      'also_known_as: [Tad]'])
        before = tree_state(self.root)
        result = run_merge(self.root, dry_run=True)
        self.assertEqual(tree_state(self.root), before)
        infos = self._infos(result)
        self.assertIn('[dry-run] Would add these names to', infos)
        self.assertIn('married_name: Thomas Hartley-Webb (from p-bbbbbbbbbb)', infos)
        self.assertIn('also_known_as: Tad (from p-bbbbbbbbbb)', infos)
        self.assertIn("name_variants: Tom Hartlee (from p-bbbbbbbbbb's name_at_birth", infos)
        self.assertIn('name_variants: Thos. Hartley (from p-bbbbbbbbbb)', infos)

    def test_a_crlf_survivor_keeps_crlf(self) -> None:
        crlf = SURVIVOR_FILE.replace('\n', '\r\n')
        self.survivor_path.write_bytes(crlf.encode('utf-8'))
        self._edit(self.merged_path, ['married_name: Tom Hartlee', 'also_known_as: [Tad]'])
        result = run_merge(self.root)
        self.assertEqual(result['status'], 'ok')
        raw = self.survivor_path.read_bytes()
        self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))
        self.assertIn(b'married_name: Tom Hartlee\r\n', raw)
        self.assertIn(b'also_known_as: [Tad]\r\n', raw)

    def test_a_failure_after_the_survivor_write_puts_it_back_byte_for_byte(self) -> None:
        self._edit(self.merged_path, ['married_name: Tom Hartlee', 'also_known_as: [Tad]'])
        survivor_bytes = self.survivor_path.read_bytes()
        before = tree_state(self.root)
        real_write = confirm.write_text_exact_atomic
        written: list[Path] = []

        def flaky(path, text):
            written.append(Path(path))
            if len(written) == 2:   # the survivor is written first; fail on the next file
                raise OSError('simulated disk full')
            return real_write(path, text)

        with mock.patch.object(confirm, 'write_text_exact_atomic', flaky):
            r = run_merge(self.root)
        self.assertEqual(written[0], self.survivor_path)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(self.survivor_path.read_bytes(), survivor_bytes)
        self.assertEqual(tree_state(self.root), before)

    def test_the_summary_line_counts_the_name_fields(self) -> None:
        self._edit(self.merged_path, ['name_at_birth: Thomas Hartlee',
                                      'married_name: Tom Hartlee',
                                      'also_known_as: [Tad, Edd]'])
        result = run_merge(self.root)
        self.assertEqual(result['folded']['also_known_as'], 2)
        self.assertIn('1 birth name, 1 married name, 2 other name(s)', self._infos(result))


# ── End-to-end against the example archive ───────────────────────────────────────

DUP_STUB_ID = 'P-mm00000001'
DUP_SOURCE_ID = 'S-mm00000001'
DUP_CLAIM_ID = 'C-mm00000001'
THOMAS = 'P-de957bcda1'

DUP_STUB_FILE = f'''---
id: {DUP_STUB_ID}
name: Thos. E. Hartley
name_variants: ["T.E.H. of Fairview"]
aliases: [{DUP_STUB_ID}, Thos. E. Hartley]
living: false
created: 2026-07-10
tier: stub
---

# Thos. E. Hartley

A duplicate stub of Thomas Edward Hartley, for the merge session check.
'''

DUP_SOURCE_FILE = f'''---
id: {DUP_SOURCE_ID}
title: Merge-check ledger fragment
source_type: other
source_class: derivative
citation: >
  Fictional ledger fragment used by the merge round-trip test.
people:
  - "[[{DUP_STUB_ID}|Thos. E. Hartley]]"
created: 2026-07-10
---

## Claims
```yaml
- value: "Thos. E. Hartley paid dues, Fairview lodge"
  id: {DUP_CLAIM_ID}
  type: note
  persons: [{DUP_STUB_ID}]
  status: suggested
```

## Notes

A fixture source so the merged stub has a claim to relink.
'''


class ExampleArchiveMergeTests(unittest.TestCase):
    """The acceptance bar: a verb-enacted merge on (a copy of) example-archive
    leaves lint with no W115 regression and no new W107."""

    @classmethod
    def setUpClass(cls) -> None:
        if not EXAMPLE.is_dir():
            raise unittest.SkipTest('example-archive not present')
        import index
        import lint
        from _lib import load_fha_yaml
        cls.index = index
        cls.lint = lint
        cls.load_fha_yaml = staticmethod(load_fha_yaml)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'arc'
        shutil.copytree(EXAMPLE, self.root)
        (self.root / 'people' / 'stubs' / f'hartley__thos_e_{DUP_STUB_ID}.md'
         ).write_text(DUP_STUB_FILE, encoding='utf-8')
        (self.root / 'sources' / 'other' / f'merge-check-ledger_{DUP_SOURCE_ID}.md'
         ).write_text(DUP_SOURCE_FILE, encoding='utf-8')
        self.config = self.load_fha_yaml(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _lint_codes(self) -> dict[str, int]:
        result = self.lint.run_lint(self.root, self.config)
        codes: dict[str, int] = {}
        for m in result.messages:
            if m.code:
                codes[m.code] = codes.get(m.code, 0) + 1
        return codes

    def test_merge_keeps_the_archive_lint_clean_of_merge_codes(self) -> None:
        baseline = self._lint_codes()
        result = confirm.run_confirm_merge(
            self.root, person_merged=DUP_STUB_ID, into=THOMAS,
            reason='duplicate stub of Thomas Edward Hartley')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result.exit_code, EXIT_CLEAN)   # no conflicts here

        tombstone = self.root / 'people' / 'stubs' / \
            f'MERGED-INTO-{THOMAS.lower()}__hartley__thos_e_{DUP_STUB_ID}.md'
        self.assertTrue(tombstone.exists())

        # The claim followed the identity: it now names Thomas.
        src = read_record(self.root / 'sources' / 'other'
                          / f'merge-check-ledger_{DUP_SOURCE_ID}.md')
        claim = {c['id']: c for c in src['claims']}[DUP_CLAIM_ID]
        self.assertEqual(claim['persons'], [THOMAS.lower()])

        self.index.build_index(self.root, self.config)
        after = self._lint_codes()
        # W107: nothing new was left pointing at the merged person - no claim
        # (the old E016, now part of W107) and no prose mention. W115: the
        # relationships reconciliation is no worse than the baseline.
        self.assertEqual(after.get('W107', 0), baseline.get('W107', 0))
        self.assertEqual(after.get('W115', 0), baseline.get('W115', 0))
        self.assertEqual(after.get('E005', 0), baseline.get('E005', 0))

    def test_merged_name_still_resolves_to_the_survivor_only(self) -> None:
        # The alias-surface reproduction: before the fix, the tombstone's kept
        # name: registered beside the survivor's freshly folded variant, so
        # the folded name became a two-record clash - dropped from every
        # resolve map (breaking existing [[Name]] links) and a fresh W112 on
        # every completed merge.
        import sqlite3
        baseline = self._lint_codes()
        result = confirm.run_confirm_merge(
            self.root, person_merged=DUP_STUB_ID, into=THOMAS,
            reason='duplicate stub of Thomas Edward Hartley')
        self.assertEqual(result['status'], 'ok')
        self.index.build_index(self.root, self.config)

        # No fresh W112: the merge must not mint an alias clash.
        after = self._lint_codes()
        self.assertEqual(after.get('W112', 0), baseline.get('W112', 0))
        self.assertEqual(after.get('W113', 0), baseline.get('W113', 0))

        conn = sqlite3.connect(self.root / '.cache' / 'index.sqlite')
        try:
            amap = self.index._resolve_map_from_aliases(conn)
            # [[Thos. E. Hartley]] (the folded name) resolves - to the
            # survivor, and only the survivor.
            self.assertEqual(amap.get('thos. e. hartley'), THOMAS.lower())
            # The folded variant resolves too.
            self.assertEqual(amap.get('t.e.h. of fairview'), THOMAS.lower())
            # The tombstone's bare P-id still resolves (readers walk
            # merged_into from there) - and it is ALL the tombstone registers.
            self.assertEqual(amap.get(DUP_STUB_ID.lower()), DUP_STUB_ID.lower())
            rows = conn.execute(
                'SELECT alias FROM aliases WHERE canonical_id=?',
                (DUP_STUB_ID.lower(),)).fetchall()
            self.assertEqual([r[0] for r in rows], [DUP_STUB_ID.lower()])
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
