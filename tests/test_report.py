"""
test_report.py - fha report (BUILD.md M5.1-M5.3).

Unlike xref/cooccur's synthetic-sqlite-index fixtures, `fha report` rebuilds
the index from on-disk record files (it calls `index.build_index` and
`lint._run_lint_core` directly - BUILD.md M5.1's "call tool logic directly"
design), so the fixture here is a tiny real archive tree rather than a
hand-built .cache/index.sqlite.
"""

import datetime
import json
import re
import shlex
import sys
import tempfile
import unittest
import unittest.mock
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib
import lint
import report
from _lib import shell_quote


_PERSON_MD = '''---
id: P-aaaaaaaaaa
name: Test Person
living: true
tier: curated
no_known_marriages: true
---

## Biography

Some text about Test Person.
'''

_SOURCE_ONE_NEEDS_REVIEW = '''---
id: S-1111111111
title: Source One
source_type: vital-record
---

## Claims
```yaml
- id: C-1111111111
  type: birth
  persons: [P-aaaaaaaaaa]
  value: Born 1900
  status: needs-review
```
'''

_SOURCE_ONE_ACCEPTED = '''---
id: S-1111111111
title: Source One
source_type: vital-record
---

## Claims
```yaml
- id: C-1111111111
  type: birth
  persons: [P-aaaaaaaaaa]
  value: Born 1900
  status: accepted
  reviewed: 2026-01-01
```
'''

_SOURCE_TWO_SUGGESTED = '''---
id: S-2222222222
title: Source Two
source_type: newspaper
---

## Claims
```yaml
- id: C-2222222222
  type: occupation
  persons: [P-aaaaaaaaaa]
  value: Worked as a clerk
  status: suggested
```
'''

_QUESTIONS_MD = '''# Open Questions (general)

## Q: When was Test Person born?
- origin: human
- status: open
- refs: [C-1111111111]
- context:
  - (human, 2026-01-01) Birth date still needs confirmation.
'''

_PERSON_NO_MARRIAGES_MD = '''---
id: P-bbbbbbbbbb
name: No Marriages Person
living: false
tier: curated
no_known_marriages: true
---

## Biography

Some text about No Marriages Person.
'''

_SOURCE_VITALS_MD = '''---
id: S-3333333333
title: Source Three
source_type: vital-record
---

## Claims
```yaml
- id: C-3333333333
  type: birth
  persons: [P-bbbbbbbbbb]
  value: Born 1900
  status: accepted
  reviewed: 2026-01-01
- id: C-3333333334
  type: death
  persons: [P-bbbbbbbbbb]
  value: Died 1970
  status: accepted
  reviewed: 2026-01-01
```
'''

_QUESTIONS_NO_MARRIAGES_MD = '''# Open Questions (general)

## Q: Is No Marriages Person fully documented?
- origin: human
- status: open
- refs: [P-bbbbbbbbbb]
- context:
  - (human, 2026-01-01) Check vitals completeness.
'''

_PERSON_PARTIAL_VITALS_MD = '''---
id: P-cccccccccc
name: Partial Birth Person
living: false
tier: curated
no_known_marriages: false
---

## Biography

Some text about Partial Birth Person.
'''

_SOURCE_BIRTH_ONLY_MD = '''---
id: S-4444444444
title: Source Four
source_type: vital-record
---

## Claims
```yaml
- id: C-4444444444
  type: birth
  persons: [P-cccccccccc]
  value: Born 1880
  status: accepted
  reviewed: 2026-01-01
```
'''

_QUESTIONS_PARTIAL_VITALS_MD = '''# Open Questions (general)

## Q: When was Partial Birth Person born?
- origin: human
- status: open
- refs: [P-cccccccccc]
- context:
  - (human, 2026-01-01) Birth date still needs confirmation.
'''

# Role Only Parent has no birth claim of their own; the only birth claim
# naming them is their CHILD's, where roles: casts them as `parent`, not the
# claim's subject. Guards against counting ANY claim naming pid toward pid's
# own vitals - the false-negative twin of #126, same root cause as #136.
_PERSON_PARENT_ROLE_ONLY_MD = '''---
id: P-dddddddddd
name: Role Only Parent
living: false
tier: curated
no_known_marriages: true
---

## Biography

Some text about Role Only Parent.
'''

_PERSON_CHILD_OF_ROLE_ONLY_MD = '''---
id: P-eeeeeeeeee
name: Role Only Child
living: false
tier: stub
---
'''

_SOURCE_CHILDS_BIRTH_NAMES_PARENT_MD = '''---
id: S-5555555555
title: Source Five
source_type: vital-record
---

## Claims
```yaml
- id: C-5555555555
  type: birth
  persons: [P-eeeeeeeeee, P-dddddddddd]
  roles:
    child: [P-eeeeeeeeee]
    parent: [P-dddddddddd]
  value: Role Only Child born 1925
  status: accepted
  reviewed: 2026-01-01
```
'''

_QUESTIONS_PARENT_ROLE_ONLY_MD = '''# Open Questions (general)

## Q: When was Role Only Parent born?
- origin: human
- status: open
- refs: [P-dddddddddd]
- context:
  - (human, 2026-01-01) Birth date still needs confirmation.
'''

# Three claims sharing one place_text and carrying no place_id - the
# smallest cluster that clears run_candidates()'s default threshold (3),
# for the §6b call-to-action test (issue #79 point 1).
_SOURCE_PLACE_CLUSTER_MD = '''---
id: S-6666666666
title: Source Six
source_type: vital-record
---

## Claims
```yaml
- id: C-6666666661
  type: residence
  persons: [P-aaaaaaaaaa]
  value: Lived in Topeka
  place_text: "Topeka, Kansas"
  status: accepted
  reviewed: 2026-01-01
- id: C-6666666662
  type: residence
  persons: [P-aaaaaaaaaa]
  value: Lived in Topeka
  place_text: "Topeka, Kansas"
  status: accepted
  reviewed: 2026-01-01
- id: C-6666666663
  type: residence
  persons: [P-aaaaaaaaaa]
  value: Lived in Topeka
  place_text: "Topeka, Kansas"
  status: needs-review
```
'''

# A place_text carrying a double quote and a comma - plausible free text
# lifted straight off a record (a building name quoted on a deed). Single-
# quoted YAML scalar so the embedded `"` needs no escaping in the fixture
# itself. Exercises the §6b command's shell-quoting (issue #79 point 1).
_SOURCE_PLACE_CLUSTER_QUOTED_MD = '''---
id: S-7777777777
title: Source Seven
source_type: vital-record
---

## Claims
```yaml
- id: C-7777777771
  type: residence
  persons: [P-aaaaaaaaaa]
  value: Lived at the old manse
  place_text: 'The "Old Manse", Springfield'
  status: accepted
  reviewed: 2026-01-01
- id: C-7777777772
  type: residence
  persons: [P-aaaaaaaaaa]
  value: Lived at the old manse
  place_text: 'The "Old Manse", Springfield'
  status: accepted
  reviewed: 2026-01-01
- id: C-7777777773
  type: residence
  persons: [P-aaaaaaaaaa]
  value: Lived at the old manse
  place_text: 'The "Old Manse", Springfield'
  status: needs-review
```
'''


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'people').mkdir(parents=True)
        (self.archive_root / 'sources').mkdir(parents=True)
        (self.archive_root / 'notes').mkdir(parents=True)

        (self.archive_root / 'people' / 'test__person_P-aaaaaaaaaa.md').write_text(
            _PERSON_MD, encoding='utf-8'
        )
        (self.archive_root / 'sources' / 'sourceone_S-1111111111.md').write_text(
            _SOURCE_ONE_NEEDS_REVIEW, encoding='utf-8'
        )
        (self.archive_root / 'notes' / 'questions.md').write_text(
            _QUESTIONS_MD, encoding='utf-8'
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_first_run_reports_vitals_gap_and_no_baseline_diff(self) -> None:
        result = report.run_report(self.archive_root, {}, full=True)
        self.assertEqual(result['status'], 'ok')
        md = result['markdown']
        self.assertIn('missing vital(s): birth', md)
        self.assertIn('No suggested claims awaiting review.', md)
        self.assertTrue((self.archive_root / '.cache' / 'last_report.json').exists())

    def test_accepted_claim_surfaces_as_discovery_and_closes_vitals_gap(self) -> None:
        report.run_report(self.archive_root, {}, full=True)

        # Source 2 (a suggested claim) lands on disk, and source 1's birth
        # claim flips from needs-review to accepted.
        (self.archive_root / 'sources' / 'sourcetwo_S-2222222222.md').write_text(
            _SOURCE_TWO_SUGGESTED, encoding='utf-8'
        )
        (self.archive_root / 'sources' / 'sourceone_S-1111111111.md').write_text(
            _SOURCE_ONE_ACCEPTED, encoding='utf-8'
        )

        result = report.run_report(self.archive_root, {}, full=False)
        md = result['markdown']

        self.assertIn('Claims newly accepted', md)
        self.assertIn('C-1111111111', md)
        self.assertIn('Profiles newly vital-complete', md)
        self.assertIn('Test Person', md)
        self.assertIn('No vitals gaps for curated persons.', md)
        self.assertIn('Source Two', md)
        self.assertIn('1 suggested claim(s)', md)
        self.assertIn('New sources (1):', md)
        self.assertIn('S-2222222222', md)
        self.assertIn('New claims (1):', md)
        self.assertIn('C-2222222222', md)
        self.assertIn('Changed claims (1):', md)
        # The answerable-questions proposal should cite the now-accepted claim.
        self.assertIn('now accepted', md)
        self.assertIn('answered [S-1111111111]', md)

    def test_unchanged_second_run_has_no_new_discoveries(self) -> None:
        report.run_report(self.archive_root, {}, full=True)
        result = report.run_report(self.archive_root, {}, full=False)
        md = result['markdown']
        self.assertIn('No discoveries since last session.', md)
        self.assertIn('No new sources or persons since last session.', md)

    def test_second_look_lists_parked_and_low_confidence_claims(self) -> None:
        # §1b (owner decision 2026-07-22): parked needs-review claims and
        # accepted low-confidence claims surface as revisit leads - counts
        # plus the oldest few, not the whole backlog.
        result = report.run_report(self.archive_root, {}, full=True, section='second-look')
        md = result['markdown']
        self.assertIn('## 1b. Worth a second look', md)
        # Fixture source one carries the needs-review birth claim (parked bucket).
        self.assertIn('**Parked (1):**', md)
        self.assertIn('Born 1900', md)

    def test_second_look_reports_nothing_when_all_settled(self) -> None:
        (self.archive_root / 'sources' / 'sourceone_S-1111111111.md').write_text(
            _SOURCE_ONE_ACCEPTED, encoding='utf-8'
        )
        result = report.run_report(self.archive_root, {}, full=True, section='second-look')
        self.assertIn('Nothing waiting on a second look.', result['markdown'])

    def test_section_filter_prints_only_that_section(self) -> None:
        result = report.run_report(self.archive_root, {}, full=True, section='review-queue')
        md = result['markdown']
        self.assertIn('## 1. Review queue', md)
        self.assertNotIn('## 0. Discoveries', md)
        self.assertNotIn('## 3. Vitals gaps', md)

    def test_unknown_section_raises(self) -> None:
        with self.assertRaises(ValueError):
            report.run_report(self.archive_root, {}, section='not-a-real-section')

    def test_possible_connections_narrates_legacy_dismissed_migration(self) -> None:
        # #48: `fha cooccur`'s one self-permitted write - carrying an older
        # archive's `.cache/cooccur_dismissed.json` forward to its durable
        # home - must never be silent. `fha report` (and the `today` skill
        # that reads it) promise the human sees every write; this is the
        # section that has to say so.
        legacy_path = self.archive_root / '.cache' / 'cooccur_dismissed.json'
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            json.dumps({'pairs': [['p-aaaaaaaaaa', 'p-bbbbbbbbbb']]}), encoding='utf-8')

        result = report.run_report(self.archive_root, {}, full=True,
                                    section='possible-connections')
        md = result['markdown']
        self.assertIn('.cache/cooccur_dismissed.json', md)
        self.assertIn('notes/cooccur_dismissed.json', md)
        new_path = self.archive_root / 'notes' / 'cooccur_dismissed.json'
        self.assertTrue(new_path.exists())
        self.assertFalse(legacy_path.exists())

    def test_possible_connections_silent_when_nothing_to_migrate(self) -> None:
        result = report.run_report(self.archive_root, {}, full=True,
                                    section='possible-connections')
        self.assertNotIn('housekeeping', result['markdown'])

    def test_place_candidates_section_uses_live_places_tool(self) -> None:
        # places.py now exists (BUILD.md M6.2), so the section calls
        # places.run_candidates() instead of printing the deferral stub.
        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        self.assertNotIn('BUILD.md M6.2', md)
        self.assertIn('No recurring unlinked place-text or GPS clusters found.', md)

    def test_place_candidates_section_names_the_confirm_command(self) -> None:
        # Issue #79 point 1: §6b must not just describe a cluster, it must
        # name the exact `fha confirm place` command that resolves it - every
        # claim id in the cluster, and the same majority-vote name
        # `fha places candidates` itself would print.
        (self.archive_root / 'sources' / 'sourcesix_S-6666666666.md').write_text(
            _SOURCE_PLACE_CLUSTER_MD, encoding='utf-8'
        )
        result = report.run_report(self.archive_root, {}, full=True, section='place-candidates')
        md = result['markdown']
        self.assertIn('Topeka, Kansas - 3 claim(s)', md)
        # Built via the same shell_quote() the production code calls, not a
        # hardcoded literal - shell_quote deliberately produces different
        # quoting per platform (single-quote POSIX shlex vs. double-quote
        # Windows list2cmdline), so a fixed string here would be right on
        # only one of them.
        self.assertIn(
            'fha confirm place C-6666666661 C-6666666662 C-6666666663 '
            f'--name={shell_quote("Topeka, Kansas")}',
            md,
        )

    def test_place_candidates_below_threshold_gets_no_command(self) -> None:
        # Two claims never clear run_candidates()'s default threshold (3) -
        # no cluster line, so no command should be invented for it either.
        below_threshold = _SOURCE_PLACE_CLUSTER_MD.replace(
            '- id: C-6666666663\n'
            '  type: residence\n'
            '  persons: [P-aaaaaaaaaa]\n'
            '  value: Lived in Topeka\n'
            '  place_text: "Topeka, Kansas"\n'
            '  status: needs-review\n',
            '',
        )
        (self.archive_root / 'sources' / 'sourcesix_S-6666666666.md').write_text(
            below_threshold, encoding='utf-8'
        )
        result = report.run_report(self.archive_root, {}, full=True, section='place-candidates')
        md = result['markdown']
        self.assertNotIn('fha confirm place', md)
        self.assertIn('No recurring unlinked place-text or GPS clusters found.', md)

    def test_place_candidates_command_is_shell_safe_for_quoted_names(self) -> None:
        # A place_text carrying a `"` (e.g. a building name quoted straight
        # off a deed) must not be spliced unescaped into `--name "..."` -
        # that breaks the printed command's own quoting and would corrupt or
        # misdirect it if pasted into a shell. The generated line must
        # round-trip through a real shell split back to the exact name.
        (self.archive_root / 'sources' / 'sourceseven_S-7777777777.md').write_text(
            _SOURCE_PLACE_CLUSTER_QUOTED_MD, encoding='utf-8'
        )
        result = report.run_report(self.archive_root, {}, full=True, section='place-candidates')
        md = result['markdown']

        line = next(l for l in md.splitlines() if 'fha confirm place' in l)
        command = line.split('`')[1]
        argv = shlex.split(command)
        self.assertEqual(
            argv[:5],
            ['fha', 'confirm', 'place', 'C-7777777771', 'C-7777777772'],
        )
        name_arg = next(a for a in argv if a.startswith('--name='))
        self.assertEqual(name_arg, '--name=The "Old Manse", Springfield')

    def test_photo_triage_section_reports_absent_index(self) -> None:
        result = report.run_report(self.archive_root, {}, full=True)
        self.assertIn('Photo index absent', result['markdown'])

    def test_unreadable_photos_explain_why_the_catalog_stays_out_of_date(self) -> None:
        # run_scan pulls the catalog's date back behind the oldest photo it
        # could not read, so `fha find` will keep calling the photo index out
        # of date. Unexplained, that looks like a second, permanent fault -
        # the report has to say why, and how to clear it.
        scan = report.Result(data={
            'root_found': True, 'total': 3, 'scraped': 2, 'unchanged': 0, 'removed': 0,
            'unreadable': 1, 'unreadable_sample': ['photos/1950/locked.jpg'],
            'unreadable_unindexed': 1, 'ignore_patterns': [],
            'groups': 0, 'dated_groups': 0, 'conflicts': 0, 'rebuilt_reason': None,
        })
        with unittest.mock.patch.object(report.photoindex, 'run_scan', return_value=scan):
            result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        self.assertIn('could not be read by exiftool', md)
        self.assertIn('stays marked out of date', md)
        self.assertIn('`fha photoindex` again', md)

    def test_unreadable_photos_that_are_already_cataloged_say_nothing_extra(self) -> None:
        # A photo that is unreadable but whose catalog row still matches the
        # file does NOT hold the catalog back (run_scan only counts the
        # unindexed ones), so the report must not claim it does.
        scan = report.Result(data={
            'root_found': True, 'total': 3, 'scraped': 2, 'unchanged': 0, 'removed': 0,
            'unreadable': 1, 'unreadable_sample': ['photos/1950/odd.jpg'],
            'unreadable_unindexed': 0, 'ignore_patterns': [],
            'groups': 0, 'dated_groups': 0, 'conflicts': 0, 'rebuilt_reason': None,
        })
        with unittest.mock.patch.object(report.photoindex, 'run_scan', return_value=scan):
            result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        self.assertIn('could not be read by exiftool', md)
        self.assertNotIn('stays marked out of date', md)

    def test_a_photo_folder_that_would_not_open_is_named_in_section_6(self) -> None:
        # Section 6 is where the human reliably looks, and it read only the
        # exiftool-unreadable-FILE keys. A whole folder the scan could not
        # open - the case where cached rows were held rather than swept -
        # went unmentioned, so the report looked clean about a subtree the
        # scan never saw.
        scan = report.Result(data={
            'root_found': True, 'total': 3, 'scraped': 0, 'unchanged': 3, 'removed': 0,
            'unreadable': 0, 'unreadable_sample': [], 'unreadable_unindexed': 0,
            'unreadable_dirs': ['photos/Attic'], 'held_unreadable': 2,
            'ignore_patterns': [],
            'groups': 0, 'dated_groups': 0, 'conflicts': 0, 'rebuilt_reason': None,
        })
        with unittest.mock.patch.object(report.photoindex, 'run_scan', return_value=scan):
            result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        self.assertIn('photos/Attic', md)
        self.assertIn('could not be opened', md)
        self.assertIn('2 photo(s) already catalogued from there were kept', md)
        self.assertIn('`fha photoindex` again', md)

    def test_a_source_folder_that_would_not_open_is_named_in_section_6(self) -> None:
        # The other half: the photos were all readable, but the source records
        # saying who is in them were not. The rows were held rather than
        # recomputed, and the human has to be told which folder to restore.
        scan = report.Result(data={
            'root_found': True, 'total': 3, 'scraped': 0, 'unchanged': 3, 'removed': 0,
            'unreadable': 0, 'unreadable_sample': [], 'unreadable_unindexed': 0,
            'unreadable_dirs': [], 'held_unreadable': 0,
            'unreadable_record_dirs': ['sources/photos'], 'ignore_patterns': [],
            'groups': 0, 'dated_groups': 0, 'conflicts': 0, 'rebuilt_reason': None,
        })
        with unittest.mock.patch.object(report.photoindex, 'run_scan', return_value=scan):
            result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        self.assertIn('sources/photos', md)
        self.assertIn('which people your sources say are in each photo', md)
        self.assertIn('left exactly as they were', md)

    def test_all_three_unseen_conditions_get_their_own_note(self) -> None:
        # Three different faults with three different fixes; folding them into
        # one line would send the human to the wrong one.
        scan = report.Result(data={
            'root_found': True, 'total': 3, 'scraped': 0, 'unchanged': 2, 'removed': 0,
            'unreadable': 1, 'unreadable_sample': ['photos/1950/locked.jpg'],
            'unreadable_unindexed': 1,
            'unreadable_dirs': ['photos/Attic'], 'held_unreadable': 0,
            'unreadable_record_dirs': ['sources/photos'], 'ignore_patterns': [],
            'groups': 0, 'dated_groups': 0, 'conflicts': 0, 'rebuilt_reason': None,
        })
        with unittest.mock.patch.object(report.photoindex, 'run_scan', return_value=scan):
            result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        self.assertEqual(md.count('Note: '), 3)
        self.assertIn('Nothing was removed from the catalog for them', md)
        self.assertIn('could not be read by exiftool', md)

    def test_a_scan_payload_missing_the_new_keys_still_reports(self) -> None:
        # An older cached payload (or a partially filled summary) must degrade
        # to fewer notes, never to a KeyError inside the session-start feed.
        self.assertEqual(report._photo_scan_notes({}), [])
        self.assertEqual(
            report._photo_scan_notes({'root_found': True, 'total': 1}), [])

    def test_triage_candidate_that_is_not_on_disk_names_the_real_fix(self) -> None:
        # `fha photoindex reconcile` re-keys a vanished photo 'MISSING:…' and
        # leaves it as its group's primary, so triage can name one. Telling
        # the human to run `fha process MISSING:photos/…` would be a dead end.
        triage = report.Result(data={
            'status': 'fresh',
            'candidates': [
                {'path': 'MISSING:photos/1950/reunion.jpg', 'score': 3,
                 'signals': ['caption']},
            ],
        })
        with unittest.mock.patch.object(report.photoindex, 'run_triage', return_value=triage):
            result = report.run_report(self.archive_root, {}, full=True, section='photo-triage')
        md = result['markdown']
        self.assertIn('photos/1950/reunion.jpg', md)
        self.assertNotIn('MISSING:', md)
        self.assertNotIn('fha process', md)
        self.assertIn('fha photoindex reconcile --with-exif', md)

    def test_triage_suggested_command_is_shell_safe_for_spaced_paths(self) -> None:
        # A real photo filename can hold a space ("Family Reunion 1962.jpg")
        # - unquoted, the shell splits `fha process` onto two arguments and
        # sends it a path that does not exist. Same defect class as §6b's
        # `fha confirm place --name` command.
        triage = report.Result(data={
            'status': 'fresh',
            'candidates': [
                {'path': 'photos/1962/Family Reunion 1962.jpg', 'score': 3,
                 'signals': ['caption']},
            ],
        })
        with unittest.mock.patch.object(report.photoindex, 'run_triage', return_value=triage):
            result = report.run_report(self.archive_root, {}, full=True, section='photo-triage')
        md = result['markdown']
        line = next(l for l in md.splitlines() if 'suggested: fha process' in l)
        command = line.split('suggested: ', 1)[1]
        argv = shlex.split(command)
        self.assertEqual(argv, ['fha', 'process', 'photos/1962/Family Reunion 1962.jpg'])

    def test_scan_error_still_shows_stale_triage_candidates(self) -> None:
        # Audit finding: a scan_error used to short-circuit
        # _section_photo_triage entirely, returning a bare "triage results
        # below may be stale" message and never actually calling
        # run_triage() - discarding the real (if session-stale) candidates
        # the PERSISTED .cache/photos.sqlite still has, even though the
        # message's own wording promised results would follow. run_triage()
        # reads that persisted catalog independently of whether this
        # session's live rescan (the source of scan_error) succeeded, so it
        # is always safe to call - the scan-error note should compose onto
        # the real results, not replace them.
        triage = report.Result(data={
            'status': 'fresh',
            'candidates': [
                {'path': 'photos/1950/reunion.jpg', 'score': 3, 'signals': ['caption']},
            ],
        })
        with unittest.mock.patch.object(
            report.photoindex, 'run_scan', side_effect=RuntimeError('exiftool not found')
        ), unittest.mock.patch.object(report.photoindex, 'run_triage', return_value=triage):
            result = report.run_report(self.archive_root, {}, full=True, section='photo-triage')
        md = result['markdown']
        self.assertIn('photo scan failed this session', md)
        self.assertIn('photos/1950/reunion.jpg', md)

    def test_answerable_questions_skips_marriage_for_no_known_marriages_person(self) -> None:
        # lint.py's W101 rule never requires a marriage claim for a person
        # with no_known_marriages: true; the answerable-questions proposal
        # logic must mirror that or it will never propose closure for a
        # person whose vitals are already complete by lint's own standard.
        (self.archive_root / 'people' / 'nomarriages_P-bbbbbbbbbb.md').write_text(
            _PERSON_NO_MARRIAGES_MD, encoding='utf-8'
        )
        (self.archive_root / 'sources' / 'sourcethree_S-3333333333.md').write_text(
            _SOURCE_VITALS_MD, encoding='utf-8'
        )
        (self.archive_root / 'notes' / 'questions.md').write_text(
            _QUESTIONS_NO_MARRIAGES_MD, encoding='utf-8'
        )

        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']

        self.assertIn('Is No Marriages Person fully documented?', md)
        self.assertIn('propose: review', md)
        self.assertIn('No Marriages Person', md)

    def test_answerable_questions_proposes_closure_for_partial_vital_match(self) -> None:
        # Partial Birth Person needs birth + marriage + death (no
        # no_known_marriages, not living) but only has an accepted birth
        # claim. The open question only asks "When was X born?" - it names
        # birth specifically, so a closure proposal must fire on birth alone
        # rather than waiting on the unrelated marriage/death gaps too.
        (self.archive_root / 'people' / 'partialbirth__P-cccccccccc.md').write_text(
            _PERSON_PARTIAL_VITALS_MD, encoding='utf-8'
        )
        (self.archive_root / 'sources' / 'sourcefour_S-4444444444.md').write_text(
            _SOURCE_BIRTH_ONLY_MD, encoding='utf-8'
        )
        (self.archive_root / 'notes' / 'questions.md').write_text(
            _QUESTIONS_MD + _QUESTIONS_PARTIAL_VITALS_MD, encoding='utf-8'
        )

        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']

        self.assertIn('When was Partial Birth Person born?', md)
        self.assertIn('propose: review', md)
        self.assertIn('Partial Birth Person', md)
        # The proposal must cite only the matched vital (birth), not the
        # full needed set (birth, death, marriage).
        self.assertIn('accepted birth claim(s)', md)

    def test_answerable_questions_does_not_propose_for_parent_role_only_claim(self) -> None:
        # Role Only Parent has no birth claim of their own. The only birth
        # claim naming them is their CHILD's (Role Only Child) - and there
        # roles: casts them as `parent`, not the claim's subject. Being
        # merely named in persons: on someone else's vital claim must not
        # read as Role Only Parent's own birth gap being closeable - a
        # proposal line only ever prints its heading text (see the
        # partial-vitals test above), so its ABSENCE here is what proves no
        # closure was proposed.
        (self.archive_root / 'people' / 'roleonly_P-dddddddddd.md').write_text(
            _PERSON_PARENT_ROLE_ONLY_MD, encoding='utf-8'
        )
        (self.archive_root / 'people' / 'roleonlychild_P-eeeeeeeeee.md').write_text(
            _PERSON_CHILD_OF_ROLE_ONLY_MD, encoding='utf-8'
        )
        (self.archive_root / 'sources' / 'sourcefive_S-5555555555.md').write_text(
            _SOURCE_CHILDS_BIRTH_NAMES_PARENT_MD, encoding='utf-8'
        )
        (self.archive_root / 'notes' / 'questions.md').write_text(
            _QUESTIONS_PARENT_ROLE_ONLY_MD, encoding='utf-8'
        )

        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']

        self.assertNotIn('When was Role Only Parent born?', md)
        self.assertNotIn('propose: review', md)
        self.assertIn('No open question currently has a closing proposal.', md)

    def test_search_log_only_marks_old_nil_searches_stale(self) -> None:
        report.run_report(self.archive_root, {}, full=True)

        db_path = self.archive_root / '.cache' / 'index.sqlite'
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                "INSERT INTO search_log(date, person_id, collection, result) VALUES (?,?,?,?)",
                ('2020-01-01', 'p-aaaaaaaaaa', 'County probate index', 'nil'),
            )
            conn.execute(
                "INSERT INTO search_log(date, person_id, collection, result) VALUES (?,?,?,?)",
                ('2020-01-01', 'p-aaaaaaaaaa', 'Newspaper archive', 'found S-1111111111'),
            )
            conn.commit()

            current = {'vitals_gap_person_ids': ['p-aaaaaaaaaa']}
            lines = report._section_search_log(conn, current)
        finally:
            conn.close()

        self.assertIn(
            '- Test Person [P-aaaaaaaaaa] - County probate index: worth re-running (stale nil search)',
            lines,
        )
        self.assertIn(
            '- Test Person [P-aaaaaaaaaa] - Newspaper archive: already searched 2020-01-01',
            lines,
        )

    def test_search_log_calls_out_recent_unreconciled_captures(self) -> None:
        report.run_report(self.archive_root, {}, full=True)

        db_path = self.archive_root / '.cache' / 'index.sqlite'
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            today = datetime.date.today().isoformat()
            conn.execute(
                "INSERT INTO search_log(date, person_id, question, repository, result) "
                "VALUES (?, NULL, ?, ?, ?)",
                (today, 'Captured page', 'site.test', 'staged inbox/page.notes.md'),
            )
            stale = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
            conn.execute(
                "INSERT INTO search_log(date, person_id, question, repository, result) "
                "VALUES (?, NULL, ?, ?, ?)",
                (stale, 'Old captured page', 'old.test', 'staged inbox/old.notes.md'),
            )
            conn.commit()

            lines = report._section_search_log(conn, {'vitals_gap_person_ids': []})
        finally:
            conn.close()

        self.assertIn('Recently captured (not yet linked to a person):', lines)
        self.assertTrue(any('Captured page' in line for line in lines))
        self.assertFalse(any('Old captured page' in line for line in lines))

    def test_search_log_excludes_general_research_log_entries(self) -> None:
        # notes/research-log.md (SPEC §16) also logs person_id IS NULL rows for
        # general/locality searches - those aren't `fha capture` rows and must
        # not be mislabeled as "Recently captured".
        report.run_report(self.archive_root, {}, full=True)

        db_path = self.archive_root / '.cache' / 'index.sqlite'
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            today = datetime.date.today().isoformat()
            conn.execute(
                "INSERT INTO search_log(date, person_id, question, repository, result, path) "
                "VALUES (?, NULL, ?, ?, ?, ?)",
                (today, 'County land records', 'site.test', 'nil', 'notes/research-log.md'),
            )
            conn.commit()

            lines = report._section_search_log(conn, {'vitals_gap_person_ids': []})
        finally:
            conn.close()

        self.assertFalse(any('County land records' in line for line in lines))


class ReportRootGuardTests(unittest.TestCase):
    """`fha report --root <non-archive>` must refuse (exit 3) and create
    NOTHING (round-2 finding 10). Empirically, before the shared
    resolve_root_arg guard: exit 0, a healthy-empty report printed, and a
    .cache minted inside whatever folder the typo named - a permanently
    "successful" empty archive anywhere on disk."""

    def test_non_archive_root_refused_and_creates_nothing(self) -> None:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from _lib import EXIT_FAILURE
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = report._standalone_main(['--root', tmp])
            self.assertEqual(rc, EXIT_FAILURE)
            # The empirical heart of the finding: zero files created.
            self.assertEqual(list(Path(tmp).iterdir()), [])
            self.assertIn('does not look like an archive', err.getvalue())
            self.assertIn('fha report', err.getvalue())

    def test_root_with_fha_yaml_still_reports(self) -> None:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                rc = report._standalone_main(['--root', tmp, '--full'])
            self.assertIn(rc, (0, 1, 2))     # lint-driven, never the refusal 3
            self.assertIn('# fha report', out.getvalue())


_PLACES_GOOD_COORDS = (
    '- id: L-1111111111\n  name: Millbrook\n  coords: [41.786, -73.694]\n'
)
_PLACES_BAD_COORDS = (
    '- id: L-1111111111\n  name: Millbrook\n  coords: "41.786, -73.694"\n'
)


class ReportArchiveNotesTests(unittest.TestCase):
    """Round-2 finding 16: report used to discard build_index's Result, so
    the coord warnings that ride ONLY on that Result (build collects them
    for the front door to render) were invisible on the session-start path.
    run_report now surfaces them as an archive-notes block near the top of
    the markdown and as result.messages - and, per report's documented
    exit-code contract, they stay lint-driven (printed, not exit-changing)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'people').mkdir()
        (self.archive_root / 'places').mkdir()
        (self.archive_root / 'people' / 'test__person_P-aaaaaaaaaa.md').write_text(
            _PERSON_MD, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, places_yaml: str):
        (self.archive_root / 'places' / 'places.yaml').write_text(
            places_yaml, encoding='utf-8')
        return report.run_report(self.archive_root, {}, full=True)

    def test_coord_warning_lands_in_markdown_and_messages(self) -> None:
        result = self._run(_PLACES_BAD_COORDS)
        md = result['markdown']
        self.assertIn('**Archive notes from this refresh:**', md)
        self.assertIn('Millbrook', md)
        self.assertIn('coordinate pair', md)
        # Near the top: before the first section heading, where the human
        # actually looks at session start.
        self.assertLess(md.index('Archive notes'), md.index('## 0.'))
        # Structured mirror for headless consumers.
        self.assertTrue(result.messages)
        self.assertIn('Millbrook', result.messages[0].text)

    def test_clean_coords_render_no_notes_block(self) -> None:
        result = self._run(_PLACES_GOOD_COORDS)
        self.assertNotIn('Archive notes', result['markdown'])
        self.assertEqual(result.messages, [])

    def test_warnings_do_not_change_the_lint_driven_exit_code(self) -> None:
        # Same archive, only the coords line differs: the exit code must not
        # move (report's contract is the lint verdict; the note is printed).
        clean_rc = self._run(_PLACES_GOOD_COORDS).exit_code
        noted_rc = self._run(_PLACES_BAD_COORDS).exit_code
        self.assertEqual(noted_rc, clean_rc)

    def test_section_filtered_run_still_shows_notes(self) -> None:
        # Narrowing the view must never hide that a line was skipped.
        (self.archive_root / 'places' / 'places.yaml').write_text(
            _PLACES_BAD_COORDS, encoding='utf-8')
        result = report.run_report(
            self.archive_root, {}, full=True, section='review-queue')
        self.assertIn('Archive notes', result['markdown'])


class PlaceTextEscalationTests(unittest.TestCase):
    """Issue #79 point 2: a place-text cluster large enough to be an
    oversight, not a candidate someone still needs to weigh, is promoted
    above every section (the `archive_notes` position) instead of sitting
    only at §6b, position 10 of 13 - where a 47%-unlinked, 13-month-old
    archive proved nobody was reliably reaching it (the issue's own
    motivating case)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'people').mkdir(parents=True)
        (self.archive_root / 'sources').mkdir(parents=True)
        (self.archive_root / 'people' / 'test__person_P-aaaaaaaaaa.md').write_text(
            _PERSON_MD, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_cluster_source(self, sid: str, place_text: str, count: int) -> None:
        # One source carrying `count` residence claims that all share
        # `place_text` and no `place_id` - exactly what
        # `places._place_text_candidates` clusters on. Claim ids are a
        # digit-only counter seeded from the source id's own trailing digits
        # (Crockford-valid; digits carry no ambiguous-letter exclusions) so
        # two clusters in the same fixture never mint the same claim id.
        seed = int(sid[-1]) * 100
        claims = ''.join(
            f'- id: C-9{seed + i:09d}\n  type: residence\n  persons: [P-aaaaaaaaaa]\n'
            f'  value: Lived there\n  place_text: "{place_text}"\n'
            '  status: accepted\n  reviewed: 2026-01-01\n'
            for i in range(count)
        )
        body = (
            f'---\nid: {sid}\ntitle: Cluster source\nsource_type: vital-record\n---\n\n'
            f'## Claims\n```yaml\n{claims}```\n'
        )
        fname = sid.lower().replace('-', '_') + '.md'
        (self.archive_root / 'sources' / fname).write_text(body, encoding='utf-8')

    def test_cluster_below_threshold_produces_no_escalation(self) -> None:
        # 19 claims: one short of the 20-claim escalation threshold. Still
        # an ordinary §6b candidate (well past run_candidates()'s own
        # default threshold of 3) - just not promoted above every section.
        self._write_cluster_source('S-9000000001', 'Warsaw, Poland', 19)
        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        self.assertNotIn('oversight to close', md)
        self.assertNotIn('place-text cluster(s) past the', md)
        self.assertIn('Warsaw, Poland - 19 claim(s)', md)   # still listed in §6b itself

    def test_cluster_at_threshold_escalates_above_every_section(self) -> None:
        self._write_cluster_source('S-9000000002', 'Warsaw, Poland', 20)
        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        self.assertIn(
            '1 place-text cluster(s) past the 20-claim oversight threshold, '
            'no place registered', md)
        self.assertIn('oversight to close', md)
        self.assertIn('Warsaw, Poland - 20 claim(s)', md)
        self.assertIn('fha confirm place', md)
        # Above every section, same position as archive_notes - not just
        # repeated at its usual spot in §6b.
        self.assertLess(
            md.index('place-text cluster(s) past the'), md.index('## 0.'))

    def test_only_the_oversized_cluster_escalates_the_smaller_one_stays_in_6b_only(self) -> None:
        self._write_cluster_source('S-9000000003', 'Warsaw, Poland', 20)
        self._write_cluster_source('S-9000000004', 'Berlin, Germany', 5)
        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        banner = md[:md.index('## 0.')]
        self.assertIn('Warsaw, Poland', banner)
        self.assertNotIn('Berlin, Germany', banner)
        # §6b itself still lists both clusters in full.
        self.assertIn('Warsaw, Poland - 20 claim(s)', md)
        self.assertIn('Berlin, Germany - 5 claim(s)', md)

    def test_section_filtered_run_still_shows_the_escalation(self) -> None:
        # Narrowing the view must never hide the one thing this feature
        # exists to make impossible to miss - same rule as archive_notes.
        self._write_cluster_source('S-9000000005', 'Warsaw, Poland', 20)
        result = report.run_report(
            self.archive_root, {}, full=True, section='review-queue')
        self.assertIn('place-text cluster(s) past the', result['markdown'])

    def test_escalation_recommends_linking_to_an_already_registered_place(self) -> None:
        # Codex review, PR #142 finding 1: a cluster naming a place that is
        # ALREADY in places/places.yaml (e.g. claims drafted before the
        # write-time resolver landed, issue #79 point 3, or any 'near' match
        # that resolver deliberately never auto-attaches) must not be told
        # to mint a brand-new place - copying the banner's own suggested
        # `--name` command would create a duplicate L-id for a place that
        # already has one. It must recommend `--into <existing L-id>` instead.
        (self.archive_root / 'places').mkdir(parents=True)
        (self.archive_root / 'places' / 'places.yaml').write_text(
            '- id: L-baba9801fa\n  name: Warsaw, Poland\n', encoding='utf-8')
        self._write_cluster_source('S-9000000006', 'Warsaw, Poland', 20)
        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        self.assertIn('--into=L-baba9801fa', md)
        self.assertNotIn('--name=', md)

    def test_escalation_banner_says_not_linked_not_unregistered_when_already_registered(
        self,
    ) -> None:
        # Codex review, PR #142 follow-up finding 1: the bold banner heading
        # above every section used to unconditionally say "no place
        # registered", even when the escalated cluster's own bullet right
        # below it (via _place_text_group_line, the fix immediately above)
        # is busy recommending `--into <existing L-id>` because the label
        # DOES match something already registered. That is self-contradictory
        # - it found a registered place but the heading says none exists -
        # and a human skimming only the bold heading would still walk away
        # minting a duplicate. The heading must describe an already-
        # registered-but-unlinked cluster accurately instead.
        (self.archive_root / 'places').mkdir(parents=True)
        (self.archive_root / 'places' / 'places.yaml').write_text(
            '- id: L-baba9801fa\n  name: Warsaw, Poland\n', encoding='utf-8')
        self._write_cluster_source('S-9000000011', 'Warsaw, Poland', 20)
        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        banner = md[:md.index('## 0.')]
        self.assertNotIn('no place registered', banner)
        self.assertIn('already registered but not yet linked', banner)

    def test_escalation_banner_mixed_registered_and_unregistered_avoids_overclaiming(
        self,
    ) -> None:
        # One escalated cluster already matches a registered place, another
        # genuinely matches none - the banner covers both clusters in one
        # heading, so it must not assert either extreme ("no place
        # registered" is false for the first; "already registered" is false
        # for the second). It falls back to wording that is true of both.
        (self.archive_root / 'places').mkdir(parents=True)
        (self.archive_root / 'places' / 'places.yaml').write_text(
            '- id: L-baba9801fa\n  name: Warsaw, Poland\n', encoding='utf-8')
        self._write_cluster_source('S-9000000012', 'Warsaw, Poland', 20)
        self._write_cluster_source('S-9000000013', 'Unregistered City', 20)
        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        banner = md[:md.index('## 0.')]
        self.assertNotIn('no place registered', banner)
        self.assertNotIn('already registered but not yet linked', banner)
        self.assertIn('not yet linked to a place', banner)

    def test_cluster_matching_multiple_registered_places_does_not_recommend_a_mint(
        self,
    ) -> None:
        # Codex review, PR #142 follow-up finding 2: `_lib.
        # match_place_text_to_registry` deliberately returns tier: None when
        # two different registered place_ids share this cluster's normalized
        # name (a PL002 duplicate-name registry problem) - it refuses to
        # guess which one is meant. Falling through to the `--name` mint
        # recommendation there would invite minting a THIRD, duplicate
        # place_id on top of an already-unresolved clash instead of
        # resolving it. The line must name the real tied ids and point at
        # `--into`/`fha places lint`, never `--name`.
        (self.archive_root / 'places').mkdir(parents=True)
        (self.archive_root / 'places' / 'places.yaml').write_text(
            '- id: L-aaaaaaaaaa\n  name: Springfield\n'
            '- id: L-bbbbbbbbbb\n  name: Springfield\n', encoding='utf-8')
        self._write_cluster_source('S-9000000014', 'Springfield', 20)
        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        self.assertIn('MULTIPLE registered places', md)
        self.assertIn('L-aaaaaaaaaa', md)
        self.assertIn('L-bbbbbbbbbb', md)
        self.assertIn('fha places lint', md)
        self.assertNotIn('--name=', md)
        # Issue #166 finding 1: the old text here spliced in the literal,
        # unsubstituted placeholder `--into=<one of the above>` - not a real
        # L-id, and `<`/`>` are shell redirection, so copying it (every
        # OTHER command in this report is meant to be pasted verbatim) broke
        # before `fha` even ran. Each real candidate id now gets its own
        # complete, ready-to-paste command instead.
        self.assertNotIn('<one of the above>', md)
        self.assertNotIn('<', md)
        self.assertNotIn('>', md)
        line = next(l for l in md.splitlines() if 'MULTIPLE registered places' in l)
        commands = re.findall(r'`([^`]+)`', line)
        into_commands = [c for c in commands if c.startswith('fha confirm place')]
        self.assertEqual(len(into_commands), 2)   # one complete command per candidate
        seen_targets = set()
        for command in into_commands:
            argv = shlex.split(command)
            self.assertEqual(argv[:3], ['fha', 'confirm', 'place'])
            self.assertTrue(argv[-1].startswith('--into=L-'))
            seen_targets.add(argv[-1].split('=', 1)[1])
            # Every claim in the 20-claim cluster is still named, exactly
            # like the exact/near-match and no-match branches already do.
            self.assertEqual(len(argv) - 4, 20)   # fha, confirm, place, --into= bracket the ids
        self.assertEqual(seen_targets, {'L-aaaaaaaaaa', 'L-bbbbbbbbbb'})

    def test_escalation_banner_flags_ambiguous_match_without_asserting_unregistered(
        self,
    ) -> None:
        # The banner heading for an escalated cluster whose name is
        # ambiguous (ties between two registered places) must not claim "no
        # place registered" either - a registered place (in fact two) does
        # exist, the problem is picking between them.
        (self.archive_root / 'places').mkdir(parents=True)
        (self.archive_root / 'places' / 'places.yaml').write_text(
            '- id: L-aaaaaaaaaa\n  name: Springfield\n'
            '- id: L-bbbbbbbbbb\n  name: Springfield\n', encoding='utf-8')
        self._write_cluster_source('S-9000000015', 'Springfield', 20)
        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        banner = md[:md.index('## 0.')]
        self.assertNotIn('no place registered', banner)
        self.assertIn('ambiguous', banner)

    def test_place_candidates_run_only_once_per_report(self) -> None:
        # Codex review, PR #142 finding 2: §6b's own listing and the
        # escalation banner above it used to each make their own
        # independent `places.run_candidates()` call - doubling the full
        # GPS photo-cluster pass (`_gps_clusters`' photo-index read and
        # greedy clustering) and any stale-photo-index warning on every
        # report run that had an escalation. One `fha report` run must
        # call it exactly once, whether or not an escalation fires.
        self._write_cluster_source('S-9000000007', 'Warsaw, Poland', 20)
        import places
        with unittest.mock.patch.object(
            places, 'run_candidates', wraps=places.run_candidates
        ) as spy:
            report.run_report(self.archive_root, {}, full=True)
        self.assertEqual(spy.call_count, 1)

    def test_section_filtered_run_points_to_the_place_candidates_command(self) -> None:
        # Codex review, PR #142 finding 3: `--section review-queue` (or any
        # filter other than place-candidates) omits §6b from what actually
        # prints, so telling the human to "see the Place candidates section
        # below" points at content that is not in this run's output. The
        # banner must name the exact follow-up command instead.
        self._write_cluster_source('S-9000000008', 'Warsaw, Poland', 20)
        result = report.run_report(
            self.archive_root, {}, full=True, section='review-queue')
        md = result['markdown']
        self.assertIn('fha report --section place-candidates', md)
        self.assertNotIn('Place candidates section below', md)

    def test_place_candidates_filtered_run_keeps_the_below_wording(self) -> None:
        # When §6b IS the section being shown (or on a full, unfiltered
        # report), the "see the Place candidates section below" wording is
        # still accurate and should not be replaced.
        self._write_cluster_source('S-9000000009', 'Warsaw, Poland', 20)
        result = report.run_report(
            self.archive_root, {}, full=True, section='place-candidates')
        md = result['markdown']
        self.assertIn('Place candidates section below', md)
        self.assertNotIn('fha report --section place-candidates', md)

    def test_escalation_exposed_in_structured_result(self) -> None:
        # Codex review, PR #142 finding 4: `run_report` used to thread the
        # escalation state only into the Markdown-rendering path, so a
        # workbench or other headless consumer reading the structured
        # Result (not reparsing Markdown) had no way to tell a 20-claim
        # oversight escalation apart from an ordinary §6b candidate.
        self._write_cluster_source('S-9000000010', 'Warsaw, Poland', 20)
        result = report.run_report(self.archive_root, {}, full=True)

        escalations = result.data['place_escalations']
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0]['label'], 'Warsaw, Poland')
        self.assertEqual(escalations[0]['claim_count'], 20)

        # Also marked in data['sections'], the same key -> list[str] shape
        # every other section already uses there.
        self.assertIn('place-escalations', result.data['sections'])
        self.assertTrue(any(
            'Warsaw, Poland' in line
            for line in result.data['sections']['place-escalations']
        ))

    def test_no_escalation_reports_empty_structured_result(self) -> None:
        # The structured field must exist (and be empty/say-so) even on a
        # run with no escalation, not only appear when one fires.
        result = report.run_report(self.archive_root, {}, full=True)
        self.assertEqual(result.data['place_escalations'], [])
        self.assertEqual(
            result.data['sections']['place-escalations'],
            ['No place-text clusters past the oversight threshold.'],
        )


class PlaceTextGroupLineAmbiguousMatchTests(unittest.TestCase):
    """Issue #166 finding 1, isolated at the unit level: `_place_text_group_
    line`'s "matches MULTIPLE registered places" branch used to splice in
    the literal, unsubstituted placeholder text `--into=<one of the above>`
    - not a real L-id, and shell-breaking if pasted verbatim (`<`/`>` are
    redirection). Every other branch in this function ends with a command
    that is genuinely ready to paste (issue #79 point 1's whole point); this
    pins that the ambiguous branch now does too, with one complete command
    per real candidate id `match['ambiguous_ids']` names."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _group(self) -> dict:
        return {
            'label': 'Springfield', 'claim_count': 2, 'date_min': None, 'date_max': None,
            'claim_ids': ['C-1111111111', 'C-2222222222'],
        }

    def test_no_unsubstituted_placeholder_survives(self) -> None:
        match = {
            'tier': None, 'place_id': None, 'name': None, 'registry_error': None,
            'ambiguous_ids': ['L-aaaaaaaaaa', 'L-bbbbbbbbbb'],
        }
        line = report._place_text_group_line(self.archive_root, self._group(), match=match)
        self.assertNotIn('one of the above', line)
        self.assertNotIn('<', line)
        self.assertNotIn('>', line)

    def test_one_complete_pasteable_command_per_candidate(self) -> None:
        match = {
            'tier': None, 'place_id': None, 'name': None, 'registry_error': None,
            'ambiguous_ids': ['L-aaaaaaaaaa', 'L-bbbbbbbbbb'],
        }
        line = report._place_text_group_line(self.archive_root, self._group(), match=match)
        for pid in ('L-aaaaaaaaaa', 'L-bbbbbbbbbb'):
            command = f'fha confirm place C-1111111111 C-2222222222 --into={pid}'
            self.assertIn(f'`{command}`', line)
            # Genuinely a complete, shell-splittable command naming every
            # claim id in the cluster - not a fragment or a sample.
            self.assertEqual(
                shlex.split(command),
                ['fha', 'confirm', 'place', 'C-1111111111', 'C-2222222222', f'--into={pid}'])
        self.assertIn('fha places lint', line)   # the registry-hygiene pointer stays too

    def test_three_way_tie_still_gives_one_command_each(self) -> None:
        # ambiguous_ids can carry more than two ids (three or more registered
        # places sharing one normalized name) - nothing in the fix assumes
        # exactly two.
        match = {
            'tier': None, 'place_id': None, 'name': None, 'registry_error': None,
            'ambiguous_ids': ['L-aaaaaaaaaa', 'L-bbbbbbbbbb', 'L-cccccccccc'],
        }
        line = report._place_text_group_line(self.archive_root, self._group(), match=match)
        for pid in ('L-aaaaaaaaaa', 'L-bbbbbbbbbb', 'L-cccccccccc'):
            self.assertIn(f'--into={pid}`', line)
        self.assertNotIn('<', line)
        self.assertNotIn('>', line)

    def test_wide_clash_caps_spelled_out_commands_at_five(self) -> None:
        # Adversarial review, round 4 audit: a cluster with many claims,
        # matched against a wide same-named-place clash (a plausible shape
        # in a large archive with recurring town names - "Springfield" isn't
        # rare), used to spell out one full, claim-list-repeating command
        # PER candidate with no limit at all - the line grew without bound
        # as either dimension grew. Past 5 candidates, the rest are still
        # named as a count (nothing hidden), just not spelled out as their
        # own pasteable command; `fha places lint` remains the pointer for
        # resolving the clash itself.
        ambiguous_ids = [f'L-{n:010d}' for n in range(8)]
        match = {
            'tier': None, 'place_id': None, 'name': None, 'registry_error': None,
            'ambiguous_ids': ambiguous_ids,
        }
        line = report._place_text_group_line(self.archive_root, self._group(), match=match)
        for pid in ambiguous_ids[:5]:
            self.assertIn(f'--into={pid}`', line)
        for pid in ambiguous_ids[5:]:
            self.assertNotIn(f'--into={pid}`', line)
        self.assertIn('3 more', line)
        self.assertIn('fha places lint', line)


class PlaceRegistryReadCountTests(unittest.TestCase):
    """Issue #166 finding 2: `_place_text_group_line`'s registry lookup used
    to call `_lib.read_places_registry` - a `places/places.yaml` file read
    plus a full scan against every registered name - fresh, once per
    place-text cluster. A report with N unlinked clusters therefore read and
    re-parsed the same never-changing file N times over, for no reason a
    human running `fha report` would ever see, only feel as a report that
    gets slower the more clusters (or the bigger the registry) an archive
    accumulates - a synthetic 200-cluster/200-place fixture measured ~6.9s
    in this section alone. One report run must read the registry exactly
    once no matter how many clusters it renders, the same "one fetch,
    shared" discipline issue #79/Codex review PR #142 finding 2 already
    established for `places.run_candidates()` itself, just above
    (`test_place_candidates_run_only_once_per_report`)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'people').mkdir(parents=True)
        (self.archive_root / 'sources').mkdir(parents=True)
        (self.archive_root / 'people' / 'test__person_P-aaaaaaaaaa.md').write_text(
            _PERSON_MD, encoding='utf-8')
        (self.archive_root / 'places').mkdir(parents=True)
        (self.archive_root / 'places' / 'places.yaml').write_text(
            '- id: L-baba9801fa\n  name: Topeka, Kansas\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_cluster_source(self, sid: str, place_text: str, count: int) -> None:
        # Mirrors PlaceTextEscalationTests._write_cluster_source above - one
        # source carrying `count` residence claims sharing `place_text` and
        # no `place_id`, the shape `places._place_text_candidates` clusters
        # on. run_candidates()'s own default clustering threshold is 3.
        seed = int(sid[-1]) * 100
        claims = ''.join(
            f'- id: C-9{seed + i:09d}\n  type: residence\n  persons: [P-aaaaaaaaaa]\n'
            f'  value: Lived there\n  place_text: "{place_text}"\n'
            '  status: accepted\n  reviewed: 2026-01-01\n'
            for i in range(count)
        )
        body = (
            f'---\nid: {sid}\ntitle: Cluster source\nsource_type: vital-record\n---\n\n'
            f'## Claims\n```yaml\n{claims}```\n'
        )
        fname = sid.lower().replace('-', '_') + '.md'
        (self.archive_root / 'sources' / fname).write_text(body, encoding='utf-8')

    def test_registry_read_once_per_report_regardless_of_cluster_count(self) -> None:
        cities = ['Warsaw, Poland', 'Berlin, Germany', 'Paris, France',
                  'Madrid, Spain', 'Rome, Italy']
        for i, city in enumerate(cities, start=1):
            self._write_cluster_source(f'S-900000000{i}', city, 3)

        # Patched in both homes: `report.read_places_registry` (report.py's
        # own imported name, used at the top of run_report/_section_place_
        # candidates) and `_lib.read_places_registry` (the name `_lib.py`'s
        # own `match_place_text_to_registry` would fall back to re-reading
        # from if a future change stopped threading `registry=` through) -
        # sharing one spy so either path is counted, whichever a regression
        # would actually go through.
        spy = unittest.mock.Mock(wraps=_lib.read_places_registry)
        with unittest.mock.patch.object(_lib, 'read_places_registry', spy), \
             unittest.mock.patch.object(report, 'read_places_registry', spy):
            result = report.run_report(self.archive_root, {}, full=True)

        md = result['markdown']
        for city in cities:
            self.assertIn(city, md)   # the fixture actually produced 5 clusters
        self.assertEqual(spy.call_count, 1)


_RESEARCH_SAME_HEADING_MD = '''# Research - Test Person

## Open Questions

## Q: When was Test Person born?
- origin: human
- status: answered [[S-1111111111]]
- refs: [P-aaaaaaaaaa]
'''


class QuestionNamespacingTests(unittest.TestCase):
    """
    The same `## Q:` heading in two files must not shadow.  parse_questions
    keys by '{file} :: {heading}' so a heading that recurs across
    notes/questions.md and a person research file (easy at hundreds of
    questions) keeps both entries; display and old-snapshot comparison use
    the plain heading.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'people').mkdir(parents=True)
        (self.archive_root / 'notes').mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_same_heading_in_two_files_keeps_both_questions(self) -> None:
        (self.archive_root / 'notes' / 'questions.md').write_text(
            _QUESTIONS_MD, encoding='utf-8'
        )
        (self.archive_root / 'people' / 'test__person_research_P-aaaaaaaaaa.md').write_text(
            _RESEARCH_SAME_HEADING_MD, encoding='utf-8'
        )
        questions = report.parse_questions(self.archive_root)
        same_heading = [
            info for info in questions.values()
            if info['heading'] == 'When was Test Person born?'
        ]
        self.assertEqual(len(same_heading), 2)
        statuses = {info['file']: info['status'] for info in same_heading}
        self.assertEqual(statuses['notes/questions.md'], 'open')
        self.assertTrue(
            statuses['people/test__person_research_P-aaaaaaaaaa.md'].startswith('answered')
        )

    def test_a_given_name_containing_the_kind_word_is_not_a_research_file(self) -> None:
        # SPEC §13 puts the companion kind immediately before the P-id, so
        # `smith__research_anne_P-…` is Research Anne Smith's own profile.
        # Merging a profile's body into the research-notes scan would let the
        # report propose closures for questions lint's E009 scope never saw -
        # the two are documented to cover the same set.
        (self.archive_root / 'people' / 'smith__research_anne_P-bbbbbbbbbb.md').write_text(
            _RESEARCH_SAME_HEADING_MD, encoding='utf-8'
        )
        (self.archive_root / 'people' / 'smith__anne_research_P-bbbbbbbbbb.md').write_text(
            _RESEARCH_SAME_HEADING_MD, encoding='utf-8'
        )
        questions = report.parse_questions(self.archive_root)
        self.assertEqual(
            sorted({info['file'] for info in questions.values()}),
            ['people/smith__anne_research_P-bbbbbbbbbb.md'],
        )

    def test_a_person_record_named_like_a_research_file_is_not_one(self) -> None:
        # The other half of the same ambiguity: SPEC §13's kind slot is also
        # the last given-name slot, so a file may be NAMED like a research
        # companion and BE Anne Research Smith's own record. Content settles
        # it, here and in lint's E009 scope alike - the two are documented to
        # see the same question set, and a profile's ## Open Questions block
        # is in neither.
        (self.archive_root / 'people' / 'smith__anne_research_P-cccccccccc.md').write_text(
            '---\nid: P-cccccccccc\nname: Anne Research Smith\nliving: false\n---\n\n'
            + _RESEARCH_SAME_HEADING_MD,
            encoding='utf-8',
        )
        questions = report.parse_questions(self.archive_root)
        self.assertEqual(questions, {})

    def test_discoveries_show_plain_heading_and_accept_old_snapshot_keys(self) -> None:
        heading = 'When was Test Person born?'
        key = f'notes/questions.md :: {heading}'
        base = {
            'claim_status_by_id': {},
            'claim_links': [],
            'vitals_gap_person_ids': [],
            'relationships': [],
            'e009_messages': [],
        }
        current = {**base, 'question_status_by_heading': {key: 'answered [[S-1111111111]]'}}

        # A snapshot written before the namespacing keyed this question by its
        # bare heading; it must still count as previously answered, so the
        # question is not re-announced once after a tools update.
        prev_old_format = {
            **base, 'question_status_by_heading': {heading: 'answered [[S-1111111111]]'},
        }
        lines = report._section_discoveries(None, prev_old_format, current)
        self.assertEqual(lines, ['No discoveries since last session.'])

        # With no prior answer it announces - displaying the plain heading,
        # never the internal '{file} :: ' prefix.
        lines = report._section_discoveries(
            None, {**base, 'question_status_by_heading': {}}, current
        )
        self.assertIn('**Questions newly answered:**', lines)
        self.assertIn(f'- {heading} - answered [[S-1111111111]]', lines)
        self.assertFalse(any('notes/questions.md ::' in line for line in lines))

    def test_discoveries_disambiguate_duplicate_headings_with_file(self) -> None:
        heading = 'When was Test Person born?'
        k1 = f'notes/questions.md :: {heading}'
        k2 = f'people/test__person_research_P-aaaaaaaaaa.md :: {heading}'
        base = {
            'claim_status_by_id': {},
            'claim_links': [],
            'vitals_gap_person_ids': [],
            'relationships': [],
            'e009_messages': [],
        }
        current = {
            **base,
            'question_status_by_heading': {k1: 'answered [[S-1111111111]]', k2: 'open'},
        }
        lines = report._section_discoveries(
            None, {**base, 'question_status_by_heading': {}}, current
        )
        # Same heading text lives in two files, so the announced one carries
        # its file to stay tellable-apart from its twin.
        self.assertIn(
            f'- {heading} (notes/questions.md) - answered [[S-1111111111]]', lines
        )


class PromotionCandidatesTests(unittest.TestCase):
    """Section 7b (promotion-candidates): direct-line stubs offer the promote
    verb; claim-heavy non-direct stubs are noted but stay stubs (the
    connections design fork); the threshold reads fha.yaml's
    `promotion.claims_threshold` (default 5); empty state is one plain line.
    Stateless - nothing here depends on the snapshot."""

    KID = 'P-4aaaaaaaaa'
    PA = 'P-4bbbbbbbbb'
    FRIEND = 'P-4ccccccccc'
    SID = 'S-4aaaaaaaaa'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'people' / '002 Kid Folder').mkdir(parents=True)
        (self.root / 'sources' / 'notes').mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _person(self, pid: str, name: str, sex: str = 'U', tier: str = 'stub') -> str:
        return (f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: false\n'
                f'tier: {tier}\n---\n\n# {name}\n\n## Biography\n\nx\n')

    def _build(self, *, threshold_line: str = '', friend_claims: int = 6) -> dict:
        (self.root / 'fha.yaml').write_text(
            f'root_person: {self.KID}\n{threshold_line}'
            'roots:\n  documents: documents\n', encoding='utf-8')
        (self.root / 'people' / '002 Kid Folder' / f'kid__k_{self.KID}.md').write_text(
            self._person(self.KID, 'Kid Person', 'F', 'curated'), encoding='utf-8')
        (self.root / 'people' / 'stubs' / f'pa__p_{self.PA}.md').write_text(
            self._person(self.PA, 'Pa Person', 'M'), encoding='utf-8')
        (self.root / 'people' / 'stubs' / f'woodbury__frank_{self.FRIEND}.md').write_text(
            self._person(self.FRIEND, 'Frank S. Woodbury', 'M'), encoding='utf-8')
        rel = (
            f'- value: "{self.KID} child of {self.PA}"\n'
            f'  id: C-4aaaaaaaaa\n  type: relationship\n  subtype: biological\n'
            f'  persons: [{self.KID}, {self.PA}]\n  roles:\n'
            f'    child: {self.KID}\n    parent: [{self.PA}]\n'
            f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
            f'  information: primary\n  evidence: direct\n  notes: x.\n'
        )
        occ = ''.join(
            f'- value: "occupation {i}"\n'
            f'  id: C-4bbbbbbb{i:02d}\n  type: occupation\n'
            f'  persons: [{self.FRIEND}]\n'
            f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
            f'  information: primary\n  evidence: direct\n  notes: x.\n'
            for i in range(friend_claims)
        )
        (self.root / 'sources' / 'notes' / f'rel_{self.SID.lower()}.md').write_text(
            f'---\nid: {self.SID}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{rel}{occ}```\n', encoding='utf-8')
        import _lib
        return _lib.load_fha_yaml(self.root)

    def _section(self, cfg: dict) -> list[str]:
        result = report.run_report(self.root, cfg, full=True)
        return result['sections']['promotion-candidates']

    def test_registry_carries_the_7b_row(self) -> None:
        self.assertIn(('promotion-candidates', '7b', 'Promotion candidates'),
                      report.SECTIONS)

    def test_lists_direct_line_stub_and_threshold_stub(self) -> None:
        cfg = self._build()
        body = '\n'.join(self._section(cfg))
        # Direct-line bucket offers the verb.
        self.assertIn('Direct-line ancestors still filed as stubs (1)', body)
        self.assertIn('Pa Person', body)
        self.assertIn(f'fha person promote {self.PA}', body)
        # #80: the claim-heavy non-direct stub is now offered the verb too.
        self.assertIn('Frank S. Woodbury', body)
        self.assertIn('6 accepted claims and no curated profile', body)
        self.assertIn(f'fha person promote {self.FRIEND} --into connections/', body)

    def test_threshold_reads_fha_yaml(self) -> None:
        cfg = self._build(
            threshold_line='promotion:\n  claims_threshold: 9\n', friend_claims=6)
        body = '\n'.join(self._section(cfg))
        # 6 accepted claims no longer crosses a threshold of 9.
        self.assertNotIn('Frank S. Woodbury', body)
        self.assertIn('Pa Person', body)   # the direct-line bucket is unaffected

    def test_bad_threshold_falls_back_with_a_note(self) -> None:
        cfg = self._build(
            threshold_line='promotion:\n  claims_threshold: lots\n', friend_claims=6)
        body = '\n'.join(self._section(cfg))
        self.assertIn('using the default 5', body)
        self.assertIn('Frank S. Woodbury', body)

    def test_empty_state_is_one_plain_line(self) -> None:
        cfg = self._build(friend_claims=1)
        # Curate Pa properly so nothing qualifies.
        src = self.root / 'people' / 'stubs' / f'pa__p_{self.PA}.md'
        (self.root / 'people' / '002 Kid Folder' / src.name).write_text(
            src.read_text(encoding='utf-8').replace('tier: stub', 'tier: curated'),
            encoding='utf-8')
        src.unlink()
        body = self._section(cfg)
        self.assertEqual(len(body), 1)
        self.assertIn('No promotion candidates', body[0])

    def test_full_report_renders_the_7b_heading(self) -> None:
        cfg = self._build()
        result = report.run_report(self.root, cfg, full=True)
        self.assertIn('## 7b. Promotion candidates', result['markdown'])


class ReportUndecodableFileVerdictTests(unittest.TestCase):
    """`fha report`'s exit code IS the lint verdict, so a lint finding it
    cannot see is an archive certified clean over a file nobody read (#68).

    `fha report` calls `lint._run_lint_core` directly - a third entry point
    beside `run_lint` and `run_lint_silent`. When W128 was raised only by the
    two named entry points, this one silently dropped it and came back exit 0
    on an archive holding an unreadable record, which is precisely the failure
    W123/W128 exist to prevent. The emitter now runs inside the core pass, so
    no caller can lose it by not knowing to ask.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'people').mkdir()
        (self.archive_root / 'people' / 'test__person_P-aaaaaaaaaa.md').write_text(
            _PERSON_MD, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_bad_note(self) -> None:
        (self.archive_root / 'notes').mkdir(exist_ok=True)
        (self.archive_root / 'notes' / 'bad.md').write_bytes(
            '# Kraków\n'.encode('cp1252'))

    def test_the_core_pass_report_consumes_carries_the_finding(self) -> None:
        # report reads `findings` straight out of `_run_lint_core`; this is
        # the seam where W128 used to be missing, and the exit code below is
        # derived from exactly this list.
        self._write_bad_note()
        findings, _registry = lint._run_lint_core(self.archive_root, {})
        self.assertIn('W128', [f.code for f in findings])

    def test_the_report_verdict_is_not_clean(self) -> None:
        self._write_bad_note()
        result = report.run_report(self.archive_root, {}, full=True)
        self.assertNotEqual(result.exit_code, 0,
                            'a file nobody could read must not come back as a clean archive')

    def test_the_refresh_still_completes(self) -> None:
        self._write_bad_note()
        result = report.run_report(self.archive_root, {}, full=True)
        self.assertIn('## 0.', result['markdown'])


if __name__ == '__main__':
    unittest.main()
