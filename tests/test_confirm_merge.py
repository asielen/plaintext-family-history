"""
test_confirm_merge.py - fha confirm merge: the SPEC §9 identity-merge write.

The merge verb is the highest-blast-radius operation in the suite (multi-file
mutation plus a rename), so the coverage here is deliberately thorough:

- tombstone: the four fields, the `MERGED-INTO-P-survivor__` rename grammar
  (checked against lint's own filename regex), the file kept forever, and
  `aliases:` reduced to the bare P-id;
- folds: name variants including the `{value:, restricted: true}` mapping form
  (round-tripped as a real boolean, and its value kept OUT of the survivor's
  plain aliases: list), external-id folding with the same-key conflict kept on
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
- an end-to-end pass against a copy of `example-archive` proving the
  post-merge archive lints with no E016/W115 and no new W107.

Fixtures only (AGENTS_TOOLING §5): a synthetic mini-archive for the surgical
assertions (fast, byte-precise) and a temp copy of `example-archive` for the
lint/index integration - the real archive is never a test bed.
"""

import hashlib
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import confirm
from _lib import EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS, normalize_id, read_record

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
    claim: "[[C-ff00000001]]"
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
    claim: "[[C-ff00000001]]"
  - to: "[[P-dddddddddd|John Reed]]"
    type: friend
    subtype: friend
    claim: "[[C-ff00000004]]"
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
    claim: "[[C-ff00000001]]"
  - to: "[[P-bbbbbbbbbb|Thos. Hartley]]"
    type: friend
    subtype: friend
    claim: "[[C-ff00000004]]"
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

- value: "event needs-review - roles block"
  id: C-ff00000007
  type: event
  persons: [P-bbbbbbbbbb]
  roles:
    witness: [P-bbbbbbbbbb]
  status: needs-review

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
        self.assertEqual(new, f'[{SURVIVOR}]')

    def test_rewrite_list_value_pins_a_name_alias(self) -> None:
        token_re = confirm._person_token_re(MERGED.lower())
        alias_map = {'tommy': MERGED.lower()}
        new, changed = confirm._rewrite_person_list_value(
            '["[[Tommy]]"]', MERGED.lower(), SURVIVOR.lower(), alias_map, token_re)
        self.assertTrue(changed)
        self.assertEqual(new, f'["[[{SURVIVOR}|Tommy]]"]')

    def test_render_scalar_round_trips_a_restricted_mapping(self) -> None:
        # read_record coerces booleans to 'true'; the fold must write the real
        # boolean back so the mapping form survives verbatim.
        rendered = confirm._render_scalar({'value': 'Tommy Hartley', 'restricted': 'true'})
        self.assertEqual(rendered, '{value: Tommy Hartley, restricted: true}')

    def test_rename_grammar_matches_lint(self) -> None:
        import lint
        stem = f'MERGED-INTO-{SURVIVOR}__hartley__thos_{MERGED}'
        self.assertIsNotNone(lint._PERSON_FILENAME_RE.match(stem))


# ── The synthetic-archive matrix ─────────────────────────────────────────────────

class MergeArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_archive(Path(self._tmp.name))
        self.survivor_path = self.root / 'people' / '040 Thomas' / 'hartley__thomas_edward_P-aaaaaaaaaa.md'
        self.merged_path = self.root / 'people' / 'stubs' / 'hartley__thos_P-bbbbbbbbbb.md'
        self.tombstone_path = self.root / 'people' / 'stubs' / \
            'MERGED-INTO-P-aaaaaaaaaa__hartley__thos_P-bbbbbbbbbb.md'
        self.other_path = self.root / 'people' / 'connections' / 'webb__nancy_P-cccccccccc.md'
        self.source_path = self.root / 'sources' / 'other' / 'test-notes_S-eeeeeeeeee.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

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
        self.assertNotIn(MERGED, source_text)
        self.assertEqual(claims['C-ff00000002']['persons'], ['P-aaaaaaaaaa'])
        self.assertEqual(claims['C-ff00000003']['persons'], ['[[P-aaaaaaaaaa]]'])
        self.assertEqual(claims['C-ff00000004']['persons'][0], '[[P-aaaaaaaaaa|Thos. Hartley]]')
        self.assertEqual(claims['C-ff00000005']['persons'], ['[[P-aaaaaaaaaa|Tommy]]'])
        # survivor-already-listed dedupe (persons and roles)
        self.assertEqual(claims['C-ff00000006']['persons'], ['P-aaaaaaaaaa'])
        self.assertEqual(claims['C-ff00000007']['roles']['witness'], ['P-aaaaaaaaaa'])
        self.assertEqual(claims['C-ff00000008']['persons'], ['P-aaaaaaaaaa'])
        self.assertEqual(claims['C-ff00000008']['roles']['associate'], ['P-aaaaaaaaaa'])
        self.assertEqual(result['relinked_claims'], 7)

        # Source frontmatter people: repointed, display kept.
        speople = read_record(self.source_path)['meta']['people']
        self.assertEqual(speople[0], '[[P-aaaaaaaaaa|Thos. Hartley]]')

        # Other profile: relationships target repointed; prose untouched.
        otext = self.other_path.read_text(encoding='utf-8')
        orec = read_record(self.other_path)
        targets = [str(e['to']) for e in orec['meta']['relationships']]
        self.assertNotIn(MERGED, ' '.join(targets))
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
        self.assertIn('C-ff00000008', warnings)
        self.assertIn('sibling', warnings)     # the skipped survivor-self edge

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
        self.assertIn(SURVIVOR, msg)   # the chain's final survivor is named

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
        real_write = confirm.write_text_exact
        state = {'n': 0}

        def flaky(path, text):
            state['n'] += 1
            if state['n'] == 2:
                raise OSError('simulated disk full')
            return real_write(path, text)

        with mock.patch.object(confirm, 'write_text_exact', flaky):
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
    leaves lint with no E016, no W115 regression, and no new W107."""

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
            f'MERGED-INTO-{THOMAS}__hartley__thos_e_{DUP_STUB_ID}.md'
        self.assertTrue(tombstone.exists())

        # The claim followed the identity: it now names Thomas.
        src = read_record(self.root / 'sources' / 'other'
                          / f'merge-check-ledger_{DUP_SOURCE_ID}.md')
        claim = {c['id']: c for c in src['claims']}[DUP_CLAIM_ID]
        self.assertEqual(claim['persons'], [THOMAS])

        self.index.build_index(self.root, self.config)
        after = self._lint_codes()
        # E016: no claim may reference the merged person. W107: nothing new was
        # left pointing at it. W115: the relationships reconciliation is no
        # worse than the baseline.
        self.assertEqual(after.get('E016', 0), 0)
        self.assertEqual(after.get('W107', 0), baseline.get('W107', 0))
        self.assertEqual(after.get('W115', 0), baseline.get('W115', 0))
        self.assertEqual(after.get('E005', 0), baseline.get('E005', 0))


if __name__ == '__main__':
    unittest.main()
