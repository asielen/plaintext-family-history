"""
test_lib_person_kind.py - what a file under people/ IS, decided in one place.

SPEC §13's person grammar is `{primary_sort_name}__{given_names}[_{kind}]_{P-id}.md`
and underscores are legal inside given names, so the optional companion-kind
slot and the last given-name segment are the SAME slot. The grammar cannot
separate them: `hartley__marie_timeline_P-…` is either a generated timeline or
the profile of Marie Timeline Hartley.

Reading it the wrong way is silent data loss - the person gets no `persons` row
at all and disappears from `fha find`, every view, every count, the site,
GEDCOM and every packet while her file sits untouched on disk. So content
outranks the filename, and the rule lives in `_lib` where index, lint, report
and views all read the same answer (index and lint drifting on this exact
question is what produced the defect).

These pin the shared rule itself:
  - `carries_person_record_fields` - key presence, not truthiness
  - `person_file_kind` - content promotes to profile, never demotes
  - `parse_filename`'s `kind_ambiguous` flag - the parser admitting it guessed
  - `is_person_file_kind(path, kind, meta)` - the same rule at every call site
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from _lib import (
    carries_person_record_fields,
    is_person_file_kind,
    parse_filename,
    person_file_kind,
)

PID = 'P-de957bcda1'

# A person record carrying the SPEC §9 required set.
PERSON_META = {'id': PID, 'name': 'Marie Timeline Hartley', 'living': False,
               'tier': 'curated'}

# The SPEC §16 research companion exactly as `fha person promote` scaffolds it
# (_lib.RESEARCH_TEMPLATE_FALLBACK): an id and a created date, nothing else.
# This is why `id:` can never be part of the person-record test - every
# research file in every archive carries one.
RESEARCH_META = {'id': PID, 'created': '2026-06-12'}

# A generated view: no frontmatter at all.
GENERATED_META: dict = {}


class CarriesPersonRecordFieldsTests(unittest.TestCase):
    def test_a_name_marks_a_person_record(self) -> None:
        self.assertTrue(carries_person_record_fields(PERSON_META))

    def test_living_false_alone_marks_a_person_record(self) -> None:
        # `living: false` is the commonest value the field takes and is falsy
        # in Python: a truthiness test would read every long-dead ancestor's
        # record as carrying nothing.
        self.assertTrue(carries_person_record_fields({'living': False}))

    def test_the_scaffolded_research_companion_carries_nothing(self) -> None:
        self.assertFalse(carries_person_record_fields(RESEARCH_META))

    def test_empty_and_null_values_do_not_count(self) -> None:
        self.assertFalse(carries_person_record_fields({'name': '', 'living': None}))
        self.assertFalse(carries_person_record_fields({}))


class PersonFileKindTests(unittest.TestCase):
    def test_content_promotes_a_companion_named_file_to_a_profile(self) -> None:
        self.assertEqual(
            person_file_kind(f'hartley__marie_timeline_{PID}.md', PERSON_META),
            'profile')

    def test_a_real_generated_companion_stays_a_companion(self) -> None:
        self.assertEqual(
            person_file_kind(f'hartley__marie_timeline_{PID}.md', GENERATED_META),
            'timeline')

    def test_the_scaffolded_research_companion_stays_research(self) -> None:
        self.assertEqual(
            person_file_kind(f'hartley__marie_research_{PID}.md', RESEARCH_META),
            'research')

    def test_content_never_demotes_a_profile(self) -> None:
        # A stub carries nothing but its id, and a stub is a profile - the
        # rule is one-directional on purpose.
        self.assertEqual(
            person_file_kind(f'hartley__marie_{PID}.md', {'id': PID}), 'profile')


class KindAmbiguousTests(unittest.TestCase):
    def test_a_kind_suffixed_person_file_is_flagged_ambiguous(self) -> None:
        parsed = parse_filename(f'hartley__marie_timeline_{PID}.md')
        self.assertTrue(parsed['kind_ambiguous'])
        self.assertTrue(parsed['is_companion'])

    def test_a_plain_profile_name_is_not_ambiguous(self) -> None:
        parsed = parse_filename(f'hartley__marie_{PID}.md')
        self.assertFalse(parsed['kind_ambiguous'])

    def test_a_source_filename_carries_the_key_too(self) -> None:
        # Every parse result has the same shape, so no caller has to guess
        # whether the key is there.
        parsed = parse_filename('census_1880_S-de957bcda1.md')
        self.assertIn('kind_ambiguous', parsed)
        self.assertFalse(parsed['kind_ambiguous'])


class IsPersonFileKindWithMetaTests(unittest.TestCase):
    """The live symptom: a research companion name over a person record.

    `smith__anne_research_P-….md` carrying full person frontmatter is Anne
    Research Smith's own record. Read as a research file, her whole file went
    into lint's E009 research scope and her `## Open Questions` block into the
    report's question scope - and SPEC §16 homes neither in a profile.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / f'smith__anne_research_{PID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_person_frontmatter_makes_it_a_profile_not_research(self) -> None:
        self.assertFalse(is_person_file_kind(self.path, 'research', PERSON_META))
        self.assertTrue(is_person_file_kind(self.path, 'profile', PERSON_META))

    def test_the_scaffolded_research_companion_is_still_research(self) -> None:
        self.assertTrue(is_person_file_kind(self.path, 'research', RESEARCH_META))
        self.assertFalse(is_person_file_kind(self.path, 'profile', RESEARCH_META))

    def test_callers_that_cannot_see_the_record_are_unchanged(self) -> None:
        # The parameter is additive: every existing call site keeps its
        # filename-only answer.
        self.assertTrue(is_person_file_kind(self.path, 'research'))
        self.assertTrue(is_person_file_kind(self.path, 'research', None))


class FindPersonRecordPathTests(unittest.TestCase):
    """Locating a person's record by scanning obeys the same rule.

    `fha claim`, `fha person set-living` and `fha confirm merge` find a record
    by walking people/ rather than trusting the index. Excluding every
    companion-NAMED file left Marie Timeline Hartley unreachable by all of
    them - "no record found" for a file sitting in plain sight.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.people = self.root / 'people'
        self.people.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, filename: str, text: str) -> Path:
        path = self.people / filename
        path.write_text(text, encoding='utf-8')
        return path

    def test_a_person_record_in_the_companion_slot_is_found(self) -> None:
        from _lib import find_person_record_path
        path = self._write(
            f'hartley__marie_timeline_{PID}.md',
            f'---\nid: {PID}\nname: Marie Timeline Hartley\nliving: false\n---\n\n# M\n')
        self.assertEqual(find_person_record_path(self.root, PID), path)

    def test_a_real_generated_companion_is_still_not_a_record(self) -> None:
        from _lib import find_person_record_path
        self._write(
            f'hartley__marie_timeline_{PID}.md',
            '<!-- GENERATED by fha views timeline on 2026-06-30'
            ' - do not edit; regenerate instead -->\n\n# Timeline\n')
        self.assertIsNone(find_person_record_path(self.root, PID))

    def test_a_plain_profile_still_wins(self) -> None:
        from _lib import find_person_record_path
        profile = self._write(
            f'hartley__marie_{PID}.md',
            f'---\nid: {PID}\nname: Marie Hartley\nliving: false\n---\n\n# M\n')
        self._write(
            f'hartley__marie_timeline_{PID}.md',
            '<!-- GENERATED by fha views timeline on 2026-06-30'
            ' - do not edit; regenerate instead -->\n\n# Timeline\n')
        self.assertEqual(find_person_record_path(self.root, PID), profile)


class ViewMarkerCompanionParsingTests(unittest.TestCase):
    """#77: the three GENERATED companion kinds now carry a `view_` marker
    immediately before the kind word (`_out_path_for` in views.py), so a
    folder listing sorts the profile and the research file first and the
    three generated companions together after them, instead of a kind word
    alone deciding the order (`draft-queue` used to sort ahead of the
    person's own record because 'd' < 'p').

    `parse_filename` detects the kind with a suffix test -
    `before_id.endswith(f'_{kind}')` - which only looks at the tail, so it is
    satisfied whether the text right before the kind word is nothing (the
    old shape) or `_view` (the new one). That is what makes the issue's
    migration story ("clean, then regenerate") complete with no parser
    change: an old-style leftover file left on disk after an update keeps
    parsing exactly as it always did, forever.
    """

    def test_new_style_timeline_companion_parses_correctly(self) -> None:
        parsed = parse_filename('hartley__thomas_edward_view_timeline_P-de957bcda1.md')
        self.assertEqual(parsed['kind'], 'timeline')
        self.assertTrue(parsed['is_companion'])
        self.assertTrue(parsed['kind_ambiguous'])

    def test_new_style_sources_index_and_draft_queue_also_parse(self) -> None:
        for kind in ('sources-index', 'draft-queue'):
            with self.subTest(kind=kind):
                parsed = parse_filename(
                    f'hartley__thomas_edward_view_{kind}_P-de957bcda1.md')
                self.assertEqual(parsed['kind'], kind)
                self.assertTrue(parsed['is_companion'])

    def test_old_style_leftover_still_parses_exactly_as_before(self) -> None:
        # A pre-#77 file left on disk after an archive updates its tools
        # (before `fha views clean` + regenerate migrates it away) must keep
        # reading as the companion it always was - the issue's migration
        # promise, pinned here at the parser level.
        parsed = parse_filename('hartley__thomas_edward_timeline_P-de957bcda1.md')
        self.assertEqual(parsed['kind'], 'timeline')
        self.assertTrue(parsed['is_companion'])

    def test_new_style_content_still_promotes_to_a_profile(self) -> None:
        # Content-first still applies with the marker present: a real person
        # record parked at a `_view_`-shaped name is read as her own record,
        # not a companion - the same rule KindAmbiguousTests/PersonFileKindTests
        # pin for the old shape, extended to the new one.
        self.assertEqual(
            person_file_kind(
                'hartley__marie_view_timeline_P-de957bcda1.md', PERSON_META),
            'profile')

    def test_new_style_generated_companion_stays_a_companion(self) -> None:
        self.assertEqual(
            person_file_kind(
                'hartley__marie_view_timeline_P-de957bcda1.md', GENERATED_META),
            'timeline')


if __name__ == '__main__':
    unittest.main()
