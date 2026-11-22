"""
test_views.py - undecodable-profile guard for `fha views draft-queue` (#68).

`_generate_draft_queue` is the one `read_record` call in views.py (line
~1292). It sits right after the existing missing-profile guard
("WARNING: no profile found for ... - skipped."), and the fix extends that
same skip-and-warn idiom to a profile that IS indexed but will not decode as
UTF-8: a record that was fine when `fha index` last ran and has since been
re-saved in another encoding (cp1252, a Windows editor's default) - the
index still lists it, so `_profile_path_for` still resolves a path, but the
bytes on disk no longer decode.

Distinct from tests/test_views_index_sync.py (a different module, guards
index/views mtime freshness, currently flaky/unrelated to this fix) and
tests/test_views_stub_guard.py (guards the *missing*-profile / wrong-location
skip paths this fix sits beside).

Run: py -3.14 -m unittest tests.test_views -v   (from the repo root)
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

import views
import index as index_mod
from _lib import EXIT_WARNINGS, load_fha_yaml

PID = 'P-aaaaaaaaaa'


def _person(pid: str, name: str) -> str:
    return (f'---\nid: {pid}\nname: {name}\nliving: false\n'
            f'tier: curated\n---\n\n# {name}\n\n## Biography\n\nx\n')


class SourcesRegionGroupsTests(unittest.TestCase):
    """A profile's generated `## Sources` list groups sources by kind under a
    bold label. Each label must start its own paragraph: written straight
    under the previous group's last bullet, Markdown reads it as more text of
    that bullet, so the reader sees "newspaper" glued onto the census entry."""

    def test_each_group_label_follows_a_blank_line(self):
        rows = {
            'census': [{'id': 's-1111111111', 'title': 'Census', 'path': 'sources/census/a_s-1111111111.md'}],
            'newspaper': [{'id': 's-2222222222', 'title': 'Notice', 'path': 'sources/newspaper/b_s-2222222222.md'}],
        }
        body = views._sources_index_region_body(rows)
        self.assertIn('_s-1111111111.md\n\n**newspaper**', body)
        self.assertTrue(body.startswith('**census**\n\n- Census'))


class DraftQueueUndecodableProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'people' / '040 Test Couple').mkdir(parents=True)
        (self.root / 'sources' / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        self.profile = (
            self.root / 'people' / '040 Test Couple' / f'hartley__ann_{PID}.md')
        # Index while the file is still good UTF-8, so the index's
        # person_files row (what _profile_path_for reads) points at it -
        # `_profile_path_for` resolves through the index, not a live disk
        # scan, so an undecodable file must still be FOUND (and then skipped
        # for its own reason), not silently invisible.
        self.profile.write_text(_person(PID, 'Ann Hartley'), encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        # Simulate the file being re-saved in another encoding after that
        # index build - the realistic way this state arises (#68). The mtime
        # is restored to what it was right after the index build so the
        # freshness gate (index vs. newest_record_mtime) reads the archive as
        # still current - it is testing the SAME state a real archive could
        # be in moments after a Windows editor "Save As cp1252" overwrote a
        # profile without bumping the clock past the last `fha index` run.
        before_stat = self.profile.stat()
        self.profile.write_bytes(
            ('---\nid: {}\nname: Ann Müller Hartley\nliving: false\n'
             'tier: curated\n---\n\n## Biography\n\nBorn in Kraków.\n'
             .format(PID)).encode('cp1252'))
        os.utime(self.profile, (before_stat.st_atime, before_stat.st_mtime))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_draft_queue_does_not_crash(self) -> None:
        # Pre-fix: `rec = read_record(profile_p)` has no on_decode_error, so
        # this raises UnicodeDecodeError straight out of the generator and
        # `fha views draft-queue <P-id>` crashes instead of reporting.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            res = views.run_draft_queue(self.root, person_id=PID)
        self.assertNotIn('Traceback', err.getvalue())
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertFalse(res.changed)
        self.assertEqual(res.data.get('count'), 0)

    def test_warning_names_the_real_cause(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            views.run_draft_queue(self.root, person_id=PID)
        text = err.getvalue()
        self.assertIn(self.profile.name, text)
        self.assertIn('not saved as UTF-8', text)
        # Same idiom as the missing-profile case: a plain "WARNING: ... -
        # skipped." line, not a bare "malformed" claim (a decode failure is
        # not a YAML problem) and not the missing-profile wording (the
        # profile does exist).
        self.assertIn('skipped', text)
        self.assertNotIn('no profile found', text)

    def test_nothing_is_written(self) -> None:
        views.run_draft_queue(self.root, person_id=PID)
        companion = (
            self.root / 'people' / '040 Test Couple'
            / f'hartley__ann_view_draft-queue_{PID}.md')
        self.assertFalse(companion.exists())


class TreeNodeVitalScopingTests(unittest.TestCase):
    """A tree node's life dates are the person's OWN (#126).

    `_build_nodes_bulk` labelled every node with the first accepted birth/death
    claim NAMING that person, so a mother co-named on her son's birth
    certificate was drawn as `Iris Marr (1888-)` - his year, under her name.
    The chart label is the shortest, most quotable thing on the page, which is
    exactly why a wrong one travels. Same defect the site build carries in
    `_person_vitals`; the two renderers must answer identically.
    """

    MOM, SON = 'p-2000000001', 'p-2000000002'

    def setUp(self) -> None:
        import sqlite3
        from index import _DDL
        self.conn = sqlite3.connect(':memory:')
        self.conn.executescript(_DDL)
        self.conn.row_factory = sqlite3.Row
        for pid, name, sex in ((self.MOM, 'Iris Marr', 'F'),
                               (self.SON, 'Peter Marr', 'M')):
            self.conn.execute(
                'INSERT INTO persons(id, name, sex, living, tier, status, path) '
                "VALUES (?,?,?,'false','curated','active',?)",
                (pid, name, sex, f'people/{pid}.md'))

    def tearDown(self) -> None:
        self.conn.close()

    def _seed_birth(self, roles: dict | None) -> None:
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, date_edtf, date_min, value, status) '
            "VALUES ('c-0000000001','s-0000000001','birth','1888','1888-01-01',"
            "'born','accepted')")
        for pos, pid in enumerate((self.SON, self.MOM)):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) '
                "VALUES ('c-0000000001',?,?,?)", (pid, pos, (roles or {}).get(pid)))

    def _vitals(self) -> dict:
        nodes = views._build_nodes_bulk(self.conn, [self.MOM, self.SON])
        return {pid: nodes[pid]['vitals'] for pid in (self.MOM, self.SON)}

    def test_mother_named_as_parent_gets_no_birth_year_on_her_node(self) -> None:
        self._seed_birth({self.SON: 'child', self.MOM: 'parent'})
        self.assertIsNone(self._vitals()[self.MOM]['birth'])

    def test_the_child_the_claim_names_keeps_his_own_birth_year(self) -> None:
        self._seed_birth({self.SON: 'child', self.MOM: 'parent'})
        self.assertEqual(self._vitals()[self.SON]['birth'], '1888')

    def test_a_legacy_claim_naming_two_people_with_no_roles_labels_neither_node(
            self) -> None:
        # Two people, no roles: map at all - the claim has not said which of
        # them was born, which is exactly this class's own bug (a mother's
        # node drawn with her son's birth year) reached through the unroled
        # case instead of the miscast one. The old fallback used to guess
        # "everyone" for zero role signal at all, restating #126 rather than
        # fixing it (#126, reopened).
        self._seed_birth(None)
        vitals = self._vitals()
        self.assertIsNone(vitals[self.MOM]['birth'])
        self.assertIsNone(vitals[self.SON]['birth'])

    def test_a_legacy_claim_naming_only_the_child_keeps_its_old_behaviour(
            self) -> None:
        # One person named, no roles: map - nobody to be ambiguous about, so
        # the pre-#126 "the claim never said, so nothing is withheld"
        # back-compatibility bargain is still exactly right here.
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, date_edtf, date_min, value, status) '
            "VALUES ('c-0000000001','s-0000000001','birth','1888','1888-01-01',"
            "'born','accepted')")
        self.conn.execute(
            'INSERT INTO claim_persons(claim_id, person_id, position, role) '
            "VALUES ('c-0000000001',?,0,NULL)", (self.SON,))
        self.assertEqual(self._vitals()[self.SON]['birth'], '1888')

# ── The profile's summary region (SPEC §16, plan 19 D5) ───────────────────────

ANN, FATHER, MOTHER, HUSBAND, CHILD = (
    'p-aaaaaaaaaa', 'p-bbbbbbbbbb', 'p-cccccccccc', 'p-dddddddddd', 'p-eeeeeeeeee')
S_BIRTH, S_DEATH, S_MARR, S_KID = (
    's-1111111111', 's-2222222222', 's-3333333333', 's-4444444444')


def _summary_profile(pid: str, name: str, *, tier: str = 'curated', body: str = '') -> str:
    return (f'---\nid: {pid}\nname: {name}\nliving: false\ntier: {tier}\n---\n\n'
            f'# {name}\n\n'
            "> **This person's record - yours to write.** Purpose.\n\n"
            f'{body}'
            '## Sources\n*(none yet)*\n\n## Biography\n\nShe lived.\n')


def _summary_source(sid: str, claim: str) -> str:
    return (f'---\nid: {sid}\ntitle: Record {sid}\nsource_type: vital\n---\n\n'
            f'## Claims\n\n```yaml\n{claim}```\n')


class SummaryRegionTests(unittest.TestCase):
    """A grandchild opening a profile in a text editor sees who the person
    was in five plain lines under the name, written from accepted claims."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        folder = self.root / 'people' / '040 Ann Hartley + Dan Webb'
        folder.mkdir(parents=True)
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'sources' / 'vital').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        self.profile = folder / f'hartley__ann_{ANN}.md'
        self.profile.write_text(_summary_profile(ANN, 'Ann Hartley'), encoding='utf-8')
        for pid, name, sex in ((FATHER, 'Caleb Hartley', 'M'), (MOTHER, 'Chastina Reed', 'F'),
                               (HUSBAND, 'Dan Webb', 'M'), (CHILD, 'Ethel Webb', 'F')):
            (self.root / 'people' / 'stubs' / f'x__{pid}.md').write_text(
                f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: false\ntier: stub\n---\n\n# {name}\n',
                encoding='utf-8')
        claims = {
            S_BIRTH: (f'- value: Ann born\n  id: c-1111111111\n  type: birth\n'
                      f'  date: 1840-03-03\n  place_text: Easton, New York\n'
                      f'  persons: [{ANN}, {FATHER}, {MOTHER}]\n'
                      f'  roles:\n    child: {ANN}\n    parent: [{FATHER}, {MOTHER}]\n'
                      '  status: accepted\n  reviewed: 2026-01-01\n'),
            S_DEATH: (f'- value: Ann died\n  id: c-2222222222\n  type: death\n'
                      f'  date: 1909\n  persons: [{ANN}]\n'
                      '  status: accepted\n  reviewed: 2026-01-01\n'
                      f'- value: Ann died later\n  id: c-2222222223\n  type: death\n'
                      f'  date: 1950\n  persons: [{ANN}]\n  status: suggested\n'),
            S_MARR: (f'- value: married\n  id: c-3333333333\n  type: marriage\n'
                     f'  date: 1871-02\n  place_text: Fairview, Kansas\n'
                     f'  persons: [{ANN}, {HUSBAND}]\n'
                     f'  roles:\n    spouse: [{ANN}, {HUSBAND}]\n'
                     '  status: accepted\n  reviewed: 2026-01-01\n'),
            S_KID: (f'- value: Ethel born\n  id: c-4444444444\n  type: birth\n'
                    f'  date: 1872\n  persons: [{CHILD}, {ANN}, {HUSBAND}]\n'
                    f'  roles:\n    child: {CHILD}\n    parent: [{ANN}, {HUSBAND}]\n'
                    '  status: accepted\n  reviewed: 2026-01-01\n'),
        }
        for sid, claim in claims.items():
            (self.root / 'sources' / 'vital' / f'r_{sid}.md').write_text(
                _summary_source(sid, claim), encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return views.run_summary(self.root, **kw)

    def _region(self) -> str:
        text = self.profile.read_text(encoding='utf-8')
        start = text.index('<!-- GENERATED-BEGIN summary')
        end = text.index('<!-- GENERATED-END summary -->')
        return text[start:end]

    def test_writes_the_five_lines_under_the_title_and_purpose_block(self) -> None:
        res = self._run(person_id=ANN)
        self.assertEqual(res.exit_code, 0)
        text = self.profile.read_text(encoding='utf-8')
        self.assertLess(text.index('yours to write'), text.index('GENERATED-BEGIN summary'))
        self.assertLess(text.index('GENERATED-END summary'), text.index('## Sources'))
        region = self._region()
        self.assertIn(f'**Born:** 3 March 1840 - Easton, New York [[{S_BIRTH}]]', region)
        self.assertIn(f'**Died:** 1909 [[{S_DEATH}]]', region)
        self.assertNotIn('1950', region)   # a suggested claim never reaches the summary
        self.assertIn(f'**Married:** [[{HUSBAND}|Dan Webb]] - February 1871, '
                      f'Fairview, Kansas [[{S_MARR}]]', region)
        self.assertIn(f'**Parents:** [[{FATHER}|Caleb Hartley]] [[{S_BIRTH}]] · '
                      f'[[{MOTHER}|Chastina Reed]] [[{S_BIRTH}]]', region)
        self.assertIn(f'**Children:** [[{CHILD}|Ethel Webb]] [[{S_KID}]]', region)

    def _add_claims(self, sid: str, claims: str) -> None:
        (self.root / 'sources' / 'vital' / f'r_{sid}.md').write_text(
            _summary_source(sid, claims), encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def test_each_wedding_is_listed_with_the_divorce_between(self) -> None:
        # Beverly married Charles in 1962, divorced him in 1974 and married
        # him again in 1978: the summary must say all three, in order, not
        # fold the two weddings into one line for the one husband.
        both = f'  persons: [{ANN}, {HUSBAND}]\n  roles:\n    spouse: [{ANN}, {HUSBAND}]\n'
        self._add_claims('s-5555555555', (
            f'- value: same first wedding, another record\n  id: c-5555555551\n'
            f'  type: marriage\n  date: 1871\n{both}  status: accepted\n'
            f'- value: undated mention\n  id: c-5555555552\n  type: marriage\n'
            f'{both}  status: accepted\n'
            f'- value: divorced\n  id: c-5555555553\n  type: divorce\n'
            f'  date: 1874-10\n{both}  status: accepted\n'
            f'- value: married again\n  id: c-5555555554\n  type: marriage\n'
            f'  date: 1878-07-29\n  place_text: Los Angeles\n{both}  status: accepted\n'))
        self._run(person_id=ANN)
        self.assertIn(
            f'**Married:** [[{HUSBAND}|Dan Webb]] - February 1871, Fairview, Kansas '
            f'[[{S_MARR}]]; divorced October 1874 [[s-5555555555]] · '
            f'[[{HUSBAND}|Dan Webb]] - 29 July 1878, Los Angeles [[s-5555555555]]',
            self._region())

    def test_a_divorce_that_only_names_the_couple_as_parents_is_not_theirs(self) -> None:
        # Their daughter's divorce record names Ann and Dan as her parents:
        # that is not Ann and Dan divorcing, and Ann's line must not say so.
        self._add_claims('s-7777777777', (
            f'- value: Ethel divorced\n  id: c-7777777771\n  type: divorce\n'
            f'  date: 1900\n  persons: [{CHILD}, {FATHER}, {ANN}, {HUSBAND}]\n'
            f'  roles:\n    spouse: [{CHILD}, {FATHER}]\n    parent: [{ANN}, {HUSBAND}]\n'
            '  status: accepted\n'))
        self._run(person_id=ANN)
        self.assertNotIn('divorced', self._region())

    def test_a_couple_known_without_a_wedding_record_still_shows_the_divorce(self) -> None:
        # Ann and her father-in-law stand in for a couple known only from a
        # relationship record and a divorce record - no marriage claim at all.
        self._add_claims('s-8888888888', (
            f'- value: Ann and Caleb were a couple\n  id: c-8888888881\n'
            f'  type: relationship\n  persons: [{ANN}, {FATHER}]\n'
            f'  roles: {{spouse: [{ANN}, {FATHER}]}}\n  status: accepted\n'
            f'- value: Ann and Caleb divorced\n  id: c-8888888882\n  type: divorce\n'
            f'  date: 1880\n  persons: [{ANN}, {FATHER}]\n'
            f'  roles: {{spouse: [{ANN}, {FATHER}]}}\n  status: accepted\n'))
        self._run(person_id=ANN)
        self.assertIn(f'[[{FATHER}|Caleb Hartley]] [[s-8888888888]]; divorced 1880 '
                      f'[[s-8888888888]]', self._region())

    def test_a_loosely_dated_record_of_one_wedding_is_not_a_second_wedding(self) -> None:
        # "about 1875" beside the certificate's February 1871, with no divorce
        # between: the same wedding remembered loosely, listed once.
        self._add_claims('s-6666666666', (
            f'- value: married about 1875\n  id: c-6666666661\n  type: marriage\n'
            f'  date: 1875~\n  persons: [{ANN}, {HUSBAND}]\n'
            f'  roles:\n    spouse: [{ANN}, {HUSBAND}]\n  status: accepted\n'))
        self._run(person_id=ANN)
        line = next(l for l in self._region().split('\n') if l.startswith('**Married:**'))
        self.assertEqual(line, f'**Married:** [[{HUSBAND}|Dan Webb]] - February 1871, '
                               f'Fairview, Kansas [[{S_MARR}]]')

    def test_rerun_is_a_no_op(self) -> None:
        self._run(person_id=ANN)
        first = self.profile.read_bytes()
        self._run(person_id=ANN)
        self.assertEqual(self.profile.read_bytes(), first)

    def test_region_is_rewritten_in_place_and_nothing_else_moves(self) -> None:
        self._run(person_id=ANN)
        text = self.profile.read_text(encoding='utf-8')
        stale = text.replace('**Died:** 1909', '**Died:** 1999')
        self.profile.write_text(stale, encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        self._run(person_id=ANN)
        self.assertEqual(self.profile.read_text(encoding='utf-8'), text)

    def test_hand_written_lines_without_markers_are_left_alone(self) -> None:
        hand = '**Born:** about 1840 in New York, as Grandma told it\n\n'
        self.profile.write_text(_summary_profile(ANN, 'Ann Hartley', body=hand),
                                encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        before = self.profile.read_bytes()
        res = self._run(person_id=ANN)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(self.profile.read_bytes(), before)

    def test_refresh_writes_the_region_too(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            views.run_refresh(self.root)
        self.assertIn(f'[[{S_BIRTH}]]', self._region())

    def test_draft_queue_does_not_count_the_summary_as_written_up(self) -> None:
        self._run(person_id=ANN)
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            views.run_draft_queue(self.root, person_id=ANN)
        queue = next(self.profile.parent.glob('*draft-queue*')).read_text(encoding='utf-8')
        self.assertIn(S_DEATH, queue.lower())

    def test_strip_generated_region_cuts_only_the_region(self) -> None:
        from _lib import strip_generated_region
        self._run(person_id=ANN)
        stripped = strip_generated_region(self.profile.read_text(encoding='utf-8'), 'summary')
        self.assertNotIn('GENERATED-BEGIN summary', stripped)
        self.assertNotIn('**Born:**', stripped)
        self.assertIn('## Sources', stripped)


class SummaryRecordsLineTests(SummaryRegionTests):
    """With `views: completeness_line: true` in fha.yaml, the summary lines end
    with one plain line saying how well documented the person is - the same
    words `fha metrics` prints - so a grandchild opening the profile sees what
    is still missing without running a command. Off unless asked for."""

    # Ann's birth, marriage and child records sit on `source_type: vital`
    # sources, not vital-record ones, and none of her places is registered,
    # so three checks are short; both parents and her death are on file.
    LINE = ('**Records:** 2 of 5 - birth from later records only; census country '
            'unknown (register the place); no marriage record')

    def _setting(self, value: str | None) -> None:
        text = 'roots:\n  documents: documents\n'
        if value is not None:
            text += f'views:\n  completeness_line: {value}\n'
        (self.root / 'fha.yaml').write_text(text, encoding='utf-8')
        # The index records the fha.yaml it was built from: rebuild, or the
        # views refuse it as stale.
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def _records_lines(self) -> list[str]:
        return [ln for ln in self._region().split('\n') if ln.startswith('**Records:**')]

    def test_off_by_default(self) -> None:
        self._run(person_id=ANN)
        self.assertEqual(self._records_lines(), [])

    def test_on_writes_one_records_line_last(self) -> None:
        self._setting('true')
        self._run(person_id=ANN)
        self.assertEqual(self._records_lines(), [self.LINE])
        lines = [ln for ln in self._region().split('\n') if ln.startswith('**')]
        self.assertEqual(lines[-1], self.LINE)

    def test_the_line_says_what_fha_metrics_says(self) -> None:
        import metrics
        self._setting('true')
        self._run(person_id=ANN)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            res = metrics.run_metrics(self.root, load_fha_yaml(self.root), person=ANN)
        score, rest = res.data['person']['summary'].split(': ', 1)
        self.assertEqual(self.LINE, f'**Records:** {score} - {rest}')

    def test_rerun_with_the_line_on_is_a_no_op(self) -> None:
        self._setting('true')
        self._run(person_id=ANN)
        first = self.profile.read_bytes()
        self._run(person_id=ANN)
        self.assertEqual(self.profile.read_bytes(), first)

    def test_turning_it_off_removes_the_line(self) -> None:
        self._setting('true')
        self._run(person_id=ANN)
        self.assertEqual(len(self._records_lines()), 1)
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        self._setting('false')
        self._run(person_id=ANN)
        self.assertEqual(self._records_lines(), [])
        self.assertIn('**Born:** 3 March 1840', self._region())

    def test_a_value_that_is_not_true_or_false_leaves_the_line_out(self) -> None:
        self._setting('sometimes')
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            views.run_summary(self.root, person_id=ANN)
        self.assertEqual(self._records_lines(), [])
        self.assertIn('completeness_line', err.getvalue())

    def test_refresh_writes_the_line_but_starts_no_region_for_it(self) -> None:
        # A stub with no accepted facts gets no region just to list what is
        # missing; a person with facts gets the line.
        bare = self.root / 'people' / 'stubs' / 'x__p-ffffffffff.md'
        bare.write_text('---\nid: p-ffffffffff\nname: Nobody Yet\nliving: false\n'
                        'tier: stub\n---\n\n# Nobody Yet\n', encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        self._setting('true')
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            views.run_refresh(self.root)
        self.assertEqual(self._records_lines(), [self.LINE])
        self.assertNotIn('GENERATED-BEGIN summary', bare.read_text(encoding='utf-8'))

    def test_living_and_unknown_people_get_the_line_too(self) -> None:
        # The region never leaves the archive (the export test below), so
        # the line is written for a living or unknown person like anyone's,
        # in the engine's own words for them.
        (self.root / 'sources' / 'vital' / f'r_{S_DEATH}.md').unlink()
        original = self.profile.read_text(encoding='utf-8')
        for living, tail in (
                ('true', 'marked living but born over 130 years ago'),
                ('unknown', 'no death or burial')):
            with self.subTest(living=living):
                self.profile.write_text(
                    original.replace('living: false', f'living: {living}'), encoding='utf-8')
                self._setting('true')
                self._run(person_id=ANN)
                [line] = self._records_lines()
                self.assertTrue(line.startswith('**Records:** 1 of 5 - '), line)
                self.assertTrue(line.endswith(tail), line)

    def test_no_export_carries_the_line(self) -> None:
        # Site pages, packets and WikiTree text never carry the summary
        # region (SPEC §21b), so the line cannot reach a shared copy.
        import importlib.util
        import packet
        import wikitree
        spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
        site_mod = sys.modules.get('fha_site') or importlib.util.module_from_spec(spec)
        if 'fha_site' not in sys.modules:
            sys.modules['fha_site'] = site_mod
            spec.loader.exec_module(site_mod)
        self._setting('true')
        self._run(person_id=ANN)
        self.assertEqual(self._records_lines(), [self.LINE])
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            packed = packet.run_packet(self.root, ANN, out / 'packet', no_photos=True)
            wiki = wikitree._wikitree_payload(self.root, ANN)
            built = site_mod.run_site(self.root, out / 'site')
        self.assertEqual(packed['status'], 'ok', packed.get('messages'))
        copied = '\n'.join(f.read_text(encoding='utf-8')
                           for f in (out / 'packet').rglob('*.md'))
        self.assertIn('She lived.', copied)
        self.assertNotIn('Records:', copied)
        self.assertEqual(wiki['status'], 'ok', wiki.get('messages'))
        self.assertIn('She lived.', wiki['text'])
        self.assertNotIn('Records:', wiki['text'])
        self.assertEqual(built['status'], 'ok', built.get('messages'))
        pages = '\n'.join(f.read_text(encoding='utf-8')
                          for f in (out / 'site').rglob('*.html'))
        self.assertIn('She lived.', pages)
        self.assertNotIn('Records:', pages)
        self.assertNotIn('census country unknown', pages)


class TreeCaseTests(unittest.TestCase):
    """`fha views tree` on the hard family shapes (tests/chart_cases.py, C1 to
    C18): a man with five wives, a niece who married her uncle, a person who is
    their own ancestor. Whoever asks for the tree of any of these families gets
    an answer in both formats, never a hang or a crash. The later chart
    changes (confidence on edges 08-6, union nodes in DOT 08-11) assert their
    own output against the same cases."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _index_case(self, name: str) -> dict:
        import sqlite3
        import time
        import chart_cases
        from _lib import (
            index_manifest_path, record_path_manifest, write_path_manifest)
        cache = self.root / '.cache'
        cache.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(cache / 'index.sqlite'))
        conn.executescript(index_mod._DDL)
        case = chart_cases.build_case(conn, self.root, name)
        conn.commit()
        conn.close()
        future = time.time() + 5
        os.utime(cache / 'index.sqlite', (future, future))
        write_path_manifest(index_manifest_path(self.root), record_path_manifest(self.root))
        return case

    def _tree(self, seed: str, mode: str, fmt: str) -> str:
        with contextlib.redirect_stdout(io.StringIO()):
            res = views.run_tree(self.root, seed, mode, None, fmt, None)
        self.assertEqual(res.exit_code, 0, (mode, fmt))
        return res['output']

    def test_every_case_gives_a_tree_in_both_modes_and_both_formats(self) -> None:
        import json
        import chart_cases
        for name in chart_cases.CASE_NAMES:
            with self.subTest(case=name):
                self._tmp.cleanup()
                self.setUp()
                case = self._index_case(name)
                for mode in ('ancestors', 'descendants'):
                    tree = json.loads(self._tree(case['seed'], mode, 'json'))
                    ids = [n['p_id'] for n in tree['nodes']]
                    self.assertEqual(len(ids), len(set(ids)))   # a person is one node
                    self.assertIn(case['seed'], ids)
                    dot = self._tree(case['seed'], mode, 'dot')
                    self.assertTrue(dot.startswith('digraph'))

    def test_a_person_who_is_their_own_ancestor_ends_the_walk_in_c12(self) -> None:
        import json
        case = self._index_case('C12')
        tree = json.loads(self._tree(case['seed'], 'ancestors', 'json'))
        self.assertEqual({n['p_id'] for n in tree['nodes']}, set(case['people'].values()))

    def test_the_private_tool_shows_the_living_in_c13(self) -> None:
        # views applies no redaction (the shared site does, see
        # ChartCaseTests); a family researcher's own view names everyone.
        import json
        case = self._index_case('C13')
        tree = json.loads(self._tree(case['seed'], 'descendants', 'json'))
        ids = {n['p_id'] for n in tree['nodes']}
        for pid in case['living']:
            self.assertIn(pid, ids)
        self.assertIn(case['people']['grandchild'], ids)


if __name__ == '__main__':
    unittest.main()
