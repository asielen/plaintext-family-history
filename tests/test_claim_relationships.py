"""\ntest_claim_relationships.py - accepting a kin claim writes the person records'\n`relationships:` entries (SPEC §9), and `fha lint --fix-applied` writes the ones\nstill missing.\n\nThe rule for which entries an accepted claim owes, and on whom, is shared\n(`_lib.claim_owed_relationship_entries`): `fha lint` W115 reads it to report a\nblock that leaves a claim out, and `fha claim` / `--fix-applied` write exactly\nthose entries. So every write test ends by asking lint whether the records now\nreconcile.\n\nCovers:\n  * accept (single and batch) adds a citing entry on each person whose record\n    already has a `relationships:` list, and on nobody else;\n  * an entry for the same tie gains the claim in its `claims:` list;\n  * a guess (`status: hypothesis`) is never rewritten: the sourced entry goes\n    beside it;\n  * `--dry-run` writes nothing and says what would change;\n  * the `claims: ["[[c-...]]"]` list format round-trips exactly, CRLF too;\n  * a list the tools do not edit is reported with the hand fix, record untouched;\n  * disputing, rejecting or superseding takes the claim out of every list, and\n    drops an entry left with nothing behind it (never a guess, never a\n    `source:` entry);\n  * `fha lint --fix-applied` (and its dry run) for the W115 set.\n"""

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import claim  # noqa: E402
import lint  # noqa: E402
from _lib import EXIT_CLEAN, claim_owed_relationship_entries  # noqa: E402

THOMAS = 'p-aaaaaaaaaa'     # the child
MARGARET = 'p-bbbbbbbbbb'   # his mother
JOHN = 'p-cccccccccc'       # his father
REL = 'c-dddddddddd'        # Thomas is Margaret's son
MARRIAGE = 'c-eeeeeeeeee'   # John married Margaret
OLDER = 'c-ffffffffff'      # an earlier accepted claim for the same tie
SOURCE = 's-gggggggggg'


def _person(name: str, pid: str, block: str = '', *, newline: str = '\n') -> tuple[str, str]:
    surname = name.split()[-1].lower()
    given = name.split()[0].lower()
    text = (f'---\nid: {pid}\nname: {name}\nliving: false\ntier: stub\n'
            f'{block}---\n\n# {name}\n\n## Biography\n\nText.\n')
    return f'people/stubs/{surname}__{given}_{pid}.md', text.replace('\n', newline)


def _entry(to_pid: str, name: str, role: str, claims: list[str] | None = None,
           extra: str = '') -> str:
    lines = [f'  - to: "[[{to_pid}|{name}]]"', f'    type: {role}']
    if claims:
        lines.append('    claims: [' + ', '.join(f'"[[{c}]]"' for c in claims) + ']')
    return '\n'.join(lines) + '\n' + extra


def _claim(cid: str, ctype: str, roles: dict[str, str], status: str = 'suggested') -> str:
    persons = ', '.join(roles.values())
    role_lines = ''.join(f'    {k}: {v}\n' for k, v in roles.items())
    return (f'- value: "a {ctype}"\n  id: {cid}\n  type: {ctype}\n'
            f'  persons: [{persons}]\n  roles:\n{role_lines}'
            f'  status: {status}\n  reviewed: 2026-01-01\n  confidence: high\n'
            '  information: primary\n  evidence: direct\n  notes: A test claim.\n')


def _source(*claims: str) -> tuple[str, str]:
    text = (f'---\nid: {SOURCE}\ntitle: Family register\nsource_type: other\n---\n\n'
            '## Claims\n\n```yaml\n' + ''.join(claims) + '```\n')
    return f'sources/other/register_{SOURCE}.md', text


def _rel_claim(status: str = 'suggested', cid: str = REL) -> str:
    return _claim(cid, 'relationship', {'child': THOMAS, 'parent': MARGARET}, status)


def _build(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode('utf-8'))
    return root


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_bytes().decode('utf-8')


def _codes(root: Path, code: str) -> list:
    findings, _registry = lint._run_lint_core(root, {})
    return [f for f in findings if f.code == code]


class AcceptWritesEntriesTests(unittest.TestCase):

    def setUp(self) -> None:
        self.thomas, t_text = _person('Thomas Hartley', THOMAS,
                                      'relationships:\n' + _entry(JOHN, 'John Hartley', 'parent'))
        self.margaret, m_text = _person('Margaret Cole', MARGARET,
                                        'relationships:\n' + _entry(JOHN, 'John Hartley', 'spouse'))
        self.john, j_text = _person('John Hartley', JOHN)
        self.src, s_text = _source(_rel_claim())
        self.root = _build({self.thomas: t_text, self.margaret: m_text,
                            self.john: j_text, self.src: s_text})

    def test_accept_adds_a_citing_entry_on_both_people(self) -> None:
        result = claim.run_claim(self.root, claim_id=REL, status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        thomas = _read(self.root, self.thomas)
        margaret = _read(self.root, self.margaret)
        self.assertIn(f'  - to: "[[{MARGARET}|Margaret Cole]]"\n    type: parent\n'
                      f'    claims: ["[[{REL}]]"]\n', thomas)
        self.assertIn(f'  - to: "[[{THOMAS}|Thomas Hartley]]"\n    type: child\n'
                      f'    claims: ["[[{REL}]]"]\n', margaret)
        texts = ' '.join(m.text for m in result.messages)
        self.assertIn("Thomas Hartley's record now lists Margaret Cole as a parent", texts)
        self.assertIn("Margaret Cole's record now lists Thomas Hartley as a child", texts)
        self.assertEqual(sorted(result.data['relationships_updated']), [THOMAS, MARGARET])
        for path in (self.thomas, self.margaret):
            self.assertIn(str(self.root / path), result.changed)
        # The shared rule: what was written is what lint asks for.
        self.assertEqual([f for f in _codes(self.root, 'W115') if REL in f.message], [])
        self.assertEqual([f for f in _codes(self.root, 'W116') if REL in f.message], [])

    def test_a_record_with_no_relationships_list_is_left_alone(self) -> None:
        before = _read(self.root, self.john)
        src, s_text = _source(_claim(REL, 'relationship', {'child': THOMAS, 'parent': JOHN}))
        (self.root / src).write_bytes((s_text).encode('utf-8'))
        result = claim.run_claim(self.root, claim_id=REL, status='accepted')
        self.assertEqual(_read(self.root, self.john), before)
        self.assertIn(f'claims: ["[[{REL}]]"]', _read(self.root, self.thomas))
        texts = ' '.join(m.text for m in result.messages)
        self.assertIn("John Hartley's record has no relationships: list, so it was left alone",
                      texts)

    def test_dry_run_writes_nothing_and_says_what_would_change(self) -> None:
        before = {p: _read(self.root, p) for p in (self.thomas, self.margaret, self.src)}
        result = claim.run_claim(self.root, claim_id=REL, status='accepted', dry_run=True)
        self.assertEqual({p: _read(self.root, p) for p in before}, before)
        self.assertEqual(result.changed, [])
        texts = ' '.join(m.text for m in result.messages)
        self.assertIn("[dry-run] Thomas Hartley's record would list Margaret Cole as a parent",
                      texts)

    def test_an_entry_for_the_same_tie_gains_the_claim(self) -> None:
        older = _rel_claim('accepted', cid=OLDER)
        (self.root / self.src).write_bytes((_source(older, _rel_claim())[1]).encode('utf-8'))
        block = ('relationships:\n' + _entry(JOHN, 'John Hartley', 'spouse')
                 + _entry(THOMAS, 'Thomas Hartley', 'child', [OLDER]))
        (self.root / self.margaret).write_bytes((
            _person('Margaret Cole', MARGARET, block)[1]).encode('utf-8'))
        claim.run_claim(self.root, claim_id=REL, status='accepted')
        margaret = _read(self.root, self.margaret)
        self.assertEqual(margaret.count('Thomas Hartley'), 1, margaret)
        self.assertIn(f'    claims: ["[[{OLDER}]]", "[[{REL}]]"]\n', margaret)

    def test_a_guess_is_never_rewritten(self) -> None:
        guess = _entry(MARGARET, 'Margaret Cole', 'parent', extra='    status: hypothesis\n')
        block = 'relationships:\n' + guess
        (self.root / self.thomas).write_bytes((
            _person('Thomas Hartley', THOMAS, block)[1]).encode('utf-8'))
        claim.run_claim(self.root, claim_id=REL, status='accepted')
        thomas = _read(self.root, self.thomas)
        self.assertIn(guess, thomas)
        self.assertIn(f'    claims: ["[[{REL}]]"]\n', thomas)
        self.assertEqual(thomas.count('type: parent'), 2)

    def test_a_list_the_tools_do_not_edit_is_reported_and_left_alone(self) -> None:
        inline = f'relationships: [{{to: "[[{JOHN}]]", type: parent}}]\n'
        path, text = _person('Thomas Hartley', THOMAS, inline)
        (self.root / path).write_bytes((text).encode('utf-8'))
        result = claim.run_claim(self.root, claim_id=REL, status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(_read(self.root, path), text)
        warnings = [m.text for m in result.messages if m.level == 'warning']
        self.assertTrue(any("Thomas Hartley's record" in w and 'by hand' in w
                            for w in warnings), warnings)
        # The claim itself was still accepted, and Margaret's side written.
        self.assertIn('status: accepted', _read(self.root, self.src))
        self.assertIn(f'claims: ["[[{REL}]]"]', _read(self.root, self.margaret))

    def test_crlf_record_keeps_its_line_endings(self) -> None:
        path, text = _person('Thomas Hartley', THOMAS,
                             'relationships:\n' + _entry(JOHN, 'John Hartley', 'parent'),
                             newline='\r\n')
        (self.root / path).write_bytes(text.encode('utf-8'))
        claim.run_claim(self.root, claim_id=REL, status='accepted')
        after = (self.root / path).read_bytes().decode('utf-8')
        self.assertNotIn('\n', after.replace('\r\n', ''))
        self.assertIn(f'    claims: ["[[{REL}]]"]\r\n', after)
        self.assertIn(_entry(JOHN, 'John Hartley', 'parent').replace('\n', '\r\n'), after)

    def test_a_failed_person_write_leaves_that_record_whole_and_says_so(self) -> None:
        margaret_before = _read(self.root, self.margaret)
        real_write = claim.write_text_exact_atomic

        def flaky(path, text):
            if 'cole__' in str(path):
                raise OSError('disk full')
            return real_write(path, text)

        with mock.patch.object(claim, 'write_text_exact_atomic', side_effect=flaky):
            result = claim.run_claim(self.root, claim_id=REL, status='accepted')
        self.assertEqual(_read(self.root, self.margaret), margaret_before)
        self.assertIn(f'claims: ["[[{REL}]]"]', _read(self.root, self.thomas))
        self.assertIn('status: accepted', _read(self.root, self.src))
        self.assertNotIn(str(self.root / self.margaret), result.changed)
        warnings = ' '.join(m.text for m in result.messages if m.level == 'warning')
        self.assertIn('fha lint --fix-applied', warnings)
        texts = ' '.join(m.text for m in result.messages)
        self.assertNotIn("Margaret Cole's record now lists", texts)
        # The fix the warning names finishes the job.
        lint.run_lint(self.root, {}, fix_applied=True)
        self.assertIn(f'claims: ["[[{REL}]]"]', _read(self.root, self.margaret))

    def test_rerunning_the_accept_adds_nothing_twice(self) -> None:
        claim.run_claim(self.root, claim_id=REL, status='accepted')
        once = _read(self.root, self.thomas)
        result = claim.run_claim(self.root, claim_id=REL, status='accepted')
        self.assertEqual(result.data['status'], 'already-done')
        self.assertEqual(_read(self.root, self.thomas), once)


class BatchAcceptTests(unittest.TestCase):

    def test_batch_writes_every_claims_entries_once_per_record(self) -> None:
        thomas, t_text = _person('Thomas Hartley', THOMAS,
                                 'relationships:\n' + _entry(JOHN, 'John Hartley', 'parent'))
        margaret, m_text = _person('Margaret Cole', MARGARET,
                                   'relationships:\n' + _entry(THOMAS, 'Thomas Hartley', 'child'))
        john, j_text = _person('John Hartley', JOHN, 'relationships:\n'
                               + _entry(THOMAS, 'Thomas Hartley', 'child'))
        src, s_text = _source(
            _rel_claim(),
            _claim(MARRIAGE, 'marriage', {'spouse': f'[{JOHN}, {MARGARET}]'}))
        s_text = s_text.replace(f'persons: [[{JOHN}, {MARGARET}]]', f'persons: [{JOHN}, {MARGARET}]')
        root = _build({thomas: t_text, margaret: m_text, john: j_text, src: s_text})
        result = claim.run_claim_batch(root, claim_ids=[REL, MARRIAGE], status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        m_after = _read(root, margaret)
        self.assertIn(f'  - to: "[[{JOHN}|John Hartley]]"\n    type: spouse\n'
                      f'    claims: ["[[{MARRIAGE}]]"]\n', m_after)
        self.assertIn(f'    claims: ["[[{REL}]]"]\n', m_after)
        self.assertIn(f'  - to: "[[{MARGARET}|Margaret Cole]]"\n    type: spouse\n'
                      f'    claims: ["[[{MARRIAGE}]]"]\n', _read(root, john))
        self.assertEqual(result.changed.count(str(root / margaret)), 1)
        self.assertEqual([f for f in _codes(root, 'W115')
                          if REL in f.message or MARRIAGE in f.message], [])

    def test_batch_dry_run_writes_nothing(self) -> None:
        thomas, t_text = _person('Thomas Hartley', THOMAS,
                                 'relationships:\n' + _entry(JOHN, 'John Hartley', 'parent'))
        margaret, m_text = _person('Margaret Cole', MARGARET)
        src, s_text = _source(_rel_claim(), _rel_claim(cid=OLDER))
        root = _build({thomas: t_text, margaret: m_text, src: s_text})
        result = claim.run_claim_batch(root, claim_ids=[REL, OLDER], status='accepted',
                                       dry_run=True)
        self.assertEqual(_read(root, thomas), t_text)
        self.assertEqual(result.changed, [])
        self.assertTrue(any('would list Margaret Cole' in m.text for m in result.messages))


class UncitingTests(unittest.TestCase):
    """Disputing, rejecting or superseding takes the claim out of the lists."""

    def _setup(self, margaret_block: str, thomas_block: str) -> tuple[Path, str, str]:
        thomas, t_text = _person('Thomas Hartley', THOMAS, thomas_block)
        margaret, m_text = _person('Margaret Cole', MARGARET, margaret_block)
        src, s_text = _source(_rel_claim('accepted'), _rel_claim('accepted', cid=OLDER))
        root = _build({thomas: t_text, margaret: m_text, src: s_text})
        return root, thomas, margaret

    def test_reject_never_deletes_an_entry_and_names_one_left_bare(self) -> None:
        root, thomas, margaret = self._setup(
            'relationships:\n' + _entry(JOHN, 'John Hartley', 'spouse')
            + _entry(THOMAS, 'Thomas Hartley', 'child', [REL]),
            'relationships:\n' + _entry(MARGARET, 'Margaret Cole', 'parent', [OLDER, REL]))
        result = claim.run_claim(root, claim_id=REL, status='rejected')
        m_after = _read(root, margaret)
        self.assertIn(_entry(JOHN, 'John Hartley', 'spouse')
                      + _entry(THOMAS, 'Thomas Hartley', 'child') + '---', m_after)
        t_after = _read(root, thomas)
        self.assertIn(f'    claims: ["[[{OLDER}]]"]\n', t_after)
        self.assertNotIn(REL, t_after + m_after)
        texts = ' '.join(m.text for m in result.messages)
        self.assertIn(f"Margaret Cole's record no longer cites claim {REL}", texts)
        self.assertIn("Margaret Cole's record still lists Thomas Hartley as a child, with no "
                      "evidence now; remove that entry by hand if it is wrong.", texts)
        self.assertIn(f"Thomas Hartley's record no longer cites claim {REL}", texts)
        self.assertNotIn("Thomas Hartley's record still lists", texts)

    def test_a_plain_hand_entry_survives_accept_then_reject(self) -> None:
        block = 'relationships:\n' + _entry(THOMAS, 'Thomas Hartley', 'child')
        root, _thomas, margaret = self._setup(block, '')
        before = _read(root, margaret)
        src = next((root / 'sources').rglob('*.md'))
        src.write_bytes(_source(_rel_claim())[1].encode('utf-8'))
        claim.run_claim(root, claim_id=REL, status='accepted')
        self.assertIn(f'claims: ["[[{REL}]]"]', _read(root, margaret))
        claim.run_claim(root, claim_id=REL, status='rejected')
        self.assertEqual(_read(root, margaret), before)

    def test_a_guess_or_a_source_entry_keeps_its_entry(self) -> None:
        guess = _entry(THOMAS, 'Thomas Hartley', 'child', [REL], extra='    status: hypothesis\n')
        sourced = _entry(MARGARET, 'Margaret Cole', 'parent', [REL],
                         extra=f'    source: "[[{SOURCE}]]"\n')
        root, thomas, margaret = self._setup('relationships:\n' + guess,
                                             'relationships:\n' + sourced)
        claim.run_claim(root, claim_id=REL, status='disputed')
        self.assertIn(f'  - to: "[[{THOMAS}|Thomas Hartley]]"\n    type: child\n'
                      '    status: hypothesis\n', _read(root, margaret))
        self.assertIn(f'  - to: "[[{MARGARET}|Margaret Cole]]"\n    type: parent\n'
                      f'    source: "[[{SOURCE}]]"\n', _read(root, thomas))
        self.assertNotIn(REL, _read(root, thomas) + _read(root, margaret))

    def test_superseded_in_a_batch_and_dry_run(self) -> None:
        root, thomas, margaret = self._setup(
            'relationships:\n' + _entry(THOMAS, 'Thomas Hartley', 'child', [REL, OLDER]),
            'relationships:\n' + _entry(MARGARET, 'Margaret Cole', 'parent', [REL]))
        before = _read(root, thomas)
        preview = claim.run_claim_batch(root, claim_ids=[REL, OLDER], status='superseded',
                                        dry_run=True)
        self.assertEqual(_read(root, thomas), before)
        self.assertTrue(any('would no longer' in m.text for m in preview.messages))
        claim.run_claim_batch(root, claim_ids=[REL, OLDER], status='superseded')
        self.assertIn(_entry(MARGARET, 'Margaret Cole', 'parent') + '---', _read(root, thomas))
        self.assertIn(_entry(THOMAS, 'Thomas Hartley', 'child') + '---', _read(root, margaret))

    def test_parking_an_accepted_claim_takes_it_out_of_the_lists(self) -> None:
        block = 'relationships:\n' + _entry(MARGARET, 'Margaret Cole', 'parent', [OLDER, REL])
        root, thomas, _margaret = self._setup('', block)
        claim.run_claim(root, claim_id=REL, status='suggested')
        self.assertIn(f'    claims: ["[[{OLDER}]]"]\n', _read(root, thomas))
        self.assertNotIn(REL, _read(root, thomas))

    def test_parking_in_a_batch_takes_them_out_too(self) -> None:
        block = 'relationships:\n' + _entry(MARGARET, 'Margaret Cole', 'parent', [OLDER, REL])
        root, thomas, _margaret = self._setup('', block)
        claim.run_claim_batch(root, claim_ids=[REL, OLDER], status='suggested')
        self.assertIn(_entry(MARGARET, 'Margaret Cole', 'parent') + '---', _read(root, thomas))

    def test_an_entry_the_owner_wrote_more_into_keeps_its_lines(self) -> None:
        note = '    note: "Mother per the family Bible"  # my own line\n'
        hyp = '    hypothesis: h-abcdefghjk   # why we believed it\n'
        root, thomas, margaret = self._setup(
            'relationships:\n' + _entry(THOMAS, 'Thomas Hartley', 'child', [REL], extra=hyp),
            'relationships:\n' + _entry(MARGARET, 'Margaret Cole', 'parent', [REL], extra=note))
        claim.run_claim(root, claim_id=REL, status='disputed')
        t_after, m_after = _read(root, thomas), _read(root, margaret)
        self.assertIn(f'  - to: "[[{MARGARET}|Margaret Cole]]"\n    type: parent\n' + note, t_after)
        self.assertIn(f'  - to: "[[{THOMAS}|Thomas Hartley]]"\n    type: child\n' + hyp, m_after)
        self.assertNotIn(REL, t_after + m_after)

    def test_a_comment_keeps_an_entry(self) -> None:
        block = ('relationships:\n  - to: "[[%s|Margaret Cole]]"  # Aunt Ruth says so\n'
                 '    type: parent\n    claims: ["[[%s]]"]\n' % (MARGARET, REL))
        root, thomas, _margaret = self._setup('', block)
        claim.run_claim(root, claim_id=REL, status='rejected')
        self.assertIn('# Aunt Ruth says so\n    type: parent\n---', _read(root, thomas))

    def test_an_empty_list_is_still_a_kept_list(self) -> None:
        root, thomas, _margaret = self._setup('', 'relationships: []\n')
        # Lint still asks for the accepted ties, and the writers open it up.
        self.assertTrue([f for f in _codes(root, 'W115') if REL in f.message])
        lint.run_lint(root, {}, fix_applied=True)
        self.assertIn(f'relationships:\n  - to: "[[{MARGARET}|Margaret Cole]]"\n'
                      f'    type: parent\n    claims: ["[[{REL}]]", "[[{OLDER}]]"]\n',
                      _read(root, thomas))

    def test_person_relate_writes_into_an_empty_list(self) -> None:
        import person
        root, thomas, _margaret = self._setup('', 'relationships: []\n')
        result = person.run_relate(root, THOMAS, 'parent', MARGARET)
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        after = _read(root, thomas)
        self.assertNotIn('[]', after)
        self.assertIn('status: hypothesis', after)


class AcceptJudgesEachOwedEntryTests(unittest.TestCase):
    """What counts as already applied, and which entry a claim joins."""

    def test_an_entry_citing_the_claims_source_already_applies_it(self) -> None:
        block = ('relationships:\n  - to: "[[%s|Margaret Cole]]"\n    type: parent\n'
                 '    source: "[[%s]]"\n' % (MARGARET, SOURCE))
        thomas, t_text = _person('Thomas Hartley', THOMAS, block)
        margaret, m_text = _person('Margaret Cole', MARGARET)
        src, s_text = _source(_rel_claim())
        root = _build({thomas: t_text, margaret: m_text, src: s_text})
        claim.run_claim(root, claim_id=REL, status='accepted')
        self.assertEqual(_read(root, thomas), t_text)
        self.assertEqual([f for f in _codes(root, 'W115') if REL in f.message], [])

    def test_a_marriage_with_a_subtype_joins_the_spouse_entry(self) -> None:
        margaret, m_text = _person('Margaret Cole', MARGARET,
                                   'relationships:\n' + _entry(JOHN, 'John Hartley', 'spouse'))
        john, j_text = _person('John Hartley', JOHN)
        marriage = (f'- value: "a marriage"\n  id: {MARRIAGE}\n  type: marriage\n'
                    '  subtype: civil\n'
                    f'  persons: [{JOHN}, {MARGARET}]\n  roles:\n    spouse: [{JOHN}, {MARGARET}]\n'
                    '  status: suggested\n  reviewed: 2026-01-01\n  confidence: high\n'
                    '  information: primary\n  evidence: direct\n  notes: A test claim.\n')
        src, s_text = _source(marriage)
        root = _build({margaret: m_text, john: j_text, src: s_text})
        claim.run_claim(root, claim_id=MARRIAGE, status='accepted')
        m_after = _read(root, margaret)
        self.assertEqual(m_after.count('John Hartley'), 1, m_after)
        self.assertIn(f'    type: spouse\n    subtype: civil\n    claims: ["[[{MARRIAGE}]]"]\n',
                      m_after)
        self.assertEqual([f for f in _codes(root, 'W115') if MARRIAGE in f.message], [])

    def test_a_claim_cited_on_one_entry_still_writes_the_others(self) -> None:
        birth_id = 'c-hhhhhhhhhh'
        birth = _claim(birth_id, 'birth', {'child': THOMAS, 'mother': MARGARET, 'father': JOHN})
        thomas, t_text = _person('Thomas Hartley', THOMAS, 'relationships:\n'
                                 + _entry(JOHN, 'John Hartley', 'parent', [birth_id]))
        margaret, m_text = _person('Margaret Cole', MARGARET)
        john, j_text = _person('John Hartley', JOHN)
        src, s_text = _source(birth)
        root = _build({thomas: t_text, margaret: m_text, john: j_text, src: s_text})
        claim.run_claim(root, claim_id=birth_id, status='accepted')
        self.assertIn(f'"[[{MARGARET}|Margaret Cole]]"\n    type: parent\n'
                      f'    claims: ["[[{birth_id}]]"]', _read(root, thomas))
        self.assertEqual([f for f in _codes(root, 'W115') if birth_id in f.message], [])

    def test_lint_asks_for_each_owed_entry(self) -> None:
        birth_id = 'c-hhhhhhhhhh'
        birth = _claim(birth_id, 'birth', {'child': THOMAS, 'mother': MARGARET, 'father': JOHN},
                       'accepted')
        thomas, t_text = _person('Thomas Hartley', THOMAS, 'relationships:\n'
                                 + _entry(JOHN, 'John Hartley', 'parent', [birth_id]))
        src, s_text = _source(birth)
        root = _build({thomas: t_text, src: s_text})
        self.assertTrue([f for f in _codes(root, 'W115') if birth_id in f.message])

    def test_a_biological_claim_never_joins_an_adoptive_entry(self) -> None:
        adopt_id, bio_id = 'c-2222222222', 'c-3333333333'
        adoptive = _entry(MARGARET, 'Margaret Cole', 'parent', [adopt_id],
                          extra='')
        adoptive = adoptive.replace('    type: parent\n', '    type: parent\n    subtype: adoptive\n')
        thomas, t_text = _person('Thomas Hartley', THOMAS, 'relationships:\n' + adoptive)
        margaret, m_text = _person('Margaret Cole', MARGARET)
        adopt = _claim(adopt_id, 'relationship', {'child': THOMAS, 'parent': MARGARET},
                       'accepted').replace('  type: relationship\n',
                                           '  type: relationship\n  subtype: adoptive\n')
        src, s_text = _source(adopt, _claim(bio_id, 'relationship',
                                            {'child': THOMAS, 'parent': MARGARET}))
        root = _build({thomas: t_text, margaret: m_text, src: s_text})
        claim.run_claim(root, claim_id=bio_id, status='accepted')
        t_after = _read(root, thomas)
        self.assertIn(adoptive, t_after)
        self.assertIn(f'    type: parent\n    claims: ["[[{bio_id}]]"]\n', t_after)
        self.assertEqual(_codes(root, 'W115'), [])

    def test_a_source_entry_with_a_name_no_record_answers_to_applies_it(self) -> None:
        block = ('relationships:\n  - to: "[[Maggie]]"   # what the family called her\n'
                 f'    type: parent\n    source: "[[{SOURCE}]]"\n')
        thomas, t_text = _person('Thomas Hartley', THOMAS, block)
        margaret, m_text = _person('Margaret Cole', MARGARET)
        src, s_text = _source(_rel_claim())
        root = _build({thomas: t_text, margaret: m_text, src: s_text})
        claim.run_claim(root, claim_id=REL, status='accepted')
        self.assertEqual(_read(root, thomas), t_text)
        self.assertEqual(_codes(root, 'W115'), [])

    def test_a_bare_number_alias_does_not_stop_the_accept(self) -> None:
        thomas, t_text = _person('Thomas Hartley', THOMAS,
                                 'relationships:\n' + _entry(JOHN, 'John Hartley', 'parent'))
        margaret, m_text = _person('Margaret Cole', MARGARET,
                                   'aliases: 1912\nname_variants: 1913\n')
        src, s_text = _source(_rel_claim())
        root = _build({thomas: t_text, margaret: m_text, src: s_text})
        result = claim.run_claim(root, claim_id=REL, status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertIn(f'claims: ["[[{REL}]]"]', _read(root, thomas))
        lint._run_lint_core(root, {})   # and lint reads the record too


class LintFixAppliedTests(unittest.TestCase):

    def setUp(self) -> None:
        self.thomas, t_text = _person('Thomas Hartley', THOMAS,
                                      'relationships:\n' + _entry(JOHN, 'John Hartley', 'parent'))
        self.margaret, m_text = _person('Margaret Cole', MARGARET)
        src, s_text = _source(_rel_claim('accepted'))
        self.t_text = t_text
        self.root = _build({self.thomas: t_text, self.margaret: m_text, src: s_text})

    def test_w115_names_the_fix(self) -> None:
        w115 = [f for f in _codes(self.root, 'W115') if REL in f.message]
        self.assertEqual(len(w115), 1)
        self.assertIn('fha lint --fix-applied', w115[0].message)

    def test_dry_run_writes_nothing(self) -> None:
        result = lint.run_lint(self.root, {}, fix_applied=True, dry_run=True)
        self.assertEqual(_read(self.root, self.thomas), self.t_text)
        self.assertEqual(result.changed, [])
        self.assertTrue(any('--fix-applied dry-run' in line and 'would list Margaret Cole' in line
                            for line in result.data['progress']), result.data['progress'])

    def test_fix_applied_writes_the_entry_then_lint_is_quiet(self) -> None:
        result = lint.run_lint(self.root, {}, fix_applied=True)
        self.assertEqual(result.changed, [str(self.root / self.thomas)])
        self.assertIn(f'    claims: ["[[{REL}]]"]\n', _read(self.root, self.thomas))
        self.assertEqual([f for f in _codes(self.root, 'W115') if REL in f.message], [])
        # Margaret has no list: the fix never gives her one.
        self.assertNotIn('relationships', _read(self.root, self.margaret))


class OwedEntriesRuleTests(unittest.TestCase):

    def test_a_birth_with_two_parents_owes_four_entries(self) -> None:
        birth = {'id': 'c-hhhhhhhhhh', 'type': 'birth', 'status': 'accepted',
                 'persons': [THOMAS, MARGARET, JOHN],
                 'roles': {'child': THOMAS, 'mother': MARGARET, 'father': JOHN}}
        self.assertEqual(sorted(claim_owed_relationship_entries(birth)), sorted([
            (THOMAS, 'parent', MARGARET, 'biological'),
            (THOMAS, 'parent', JOHN, 'biological'),
            (MARGARET, 'child', THOMAS, 'biological'),
            (JOHN, 'child', THOMAS, 'biological'),
        ]))

    def test_suggested_or_negated_owes_nothing(self) -> None:
        base = {'id': REL, 'type': 'relationship', 'persons': [THOMAS, MARGARET],
                'roles': {'child': THOMAS, 'parent': MARGARET}}
        self.assertEqual(claim_owed_relationship_entries(dict(base, status='suggested')), [])
        self.assertEqual(claim_owed_relationship_entries(
            dict(base, status='accepted', negated=True)), [])

    def test_parents_on_a_marriage_certificate_owe_nothing(self) -> None:
        marriage = {'id': MARRIAGE, 'type': 'marriage', 'status': 'accepted',
                    'persons': [JOHN, MARGARET, THOMAS],
                    'roles': {'spouse': [JOHN, MARGARET], 'parent': THOMAS}}
        self.assertEqual(sorted(claim_owed_relationship_entries(marriage)), sorted([
            (JOHN, 'spouse', MARGARET, ''), (MARGARET, 'spouse', JOHN, '')]))


if __name__ == '__main__':
    unittest.main()
