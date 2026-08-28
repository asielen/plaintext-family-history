import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom
import gedcom_import
from index import _DDL
from _lib import EXIT_FAILURE


def _make_index(archive_root: Path) -> sqlite3.Connection:
    cache = archive_root / '.cache'
    cache.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache / 'index.sqlite'))
    conn.executescript(_DDL)
    conn.row_factory = sqlite3.Row
    return conn


def _add_person(conn, pid, name, sex='', living='false', tier='curated', surname=None, status='active'):
    conn.execute(
        'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (pid, name, surname, sex, living, tier, status, f'people/{pid}.md'),
    )


def _rel(conn, a, rel, b, ds='', de=None):
    conn.execute(
        'INSERT INTO relationships(person_id, rel, other_id, claim_id, date_start, date_end) '
        'VALUES (?,?,?,?,?,?)',
        (a, rel, b, 'c-rel000000', ds, de),
    )


def _spouse(conn, a, b, ds='', de=None):
    _rel(conn, a, 'spouse', b, ds, de)
    _rel(conn, b, 'spouse', a, ds, de)


def _parent_child(conn, parent, child, ds='1900-01-01', de='1900-12-31'):
    _rel(conn, child, 'parent', parent, ds, de)
    _rel(conn, parent, 'child', child, ds, de)


def _add_claim(conn, cid, ctype, persons, date_edtf='', place_id=None, place_text=None,
               source_id='s-0000000001', status='accepted', value='x', negated=0,
               roles=None):
    """Seed one claim. `roles` is {person_id: role} - the `roles:` map that says
    which of the people a claim names plays which part (SPEC §8.3)."""
    mn = ''
    if date_edtf:
        from _lib import edtf_bounds
        mn = edtf_bounds(date_edtf)[0]
    conn.execute(
        'INSERT INTO claims(id, source_id, type, date_edtf, date_min, place_id, place_text, value, status, negated) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (cid, source_id, ctype, date_edtf, mn, place_id, place_text, value, status, negated),
    )
    for pos, p in enumerate(persons):
        conn.execute(
            'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
            (cid, p, pos, (roles or {}).get(p)),
        )


def _individuals(text: str) -> dict:
    """Every INDI record in an export, keyed by the person id in its REFN line."""
    out, current = {}, None
    for line in text.split('\r\n'):
        if line.startswith('0 '):
            if current:
                block = '\r\n'.join(current)
                for ln in current:
                    if ln.startswith('1 REFN '):
                        out[ln[len('1 REFN '):].strip().lower()] = block
            current = [line] if line.endswith(' INDI') else None
            continue
        if current is not None:
            current.append(line)
    if current:
        block = '\r\n'.join(current)
        for ln in current:
            if ln.startswith('1 REFN '):
                out[ln[len('1 REFN '):].strip().lower()] = block
    return out


def _add_source(conn, sid, title, *, source_type='vital-record', restricted=0):
    conn.execute(
        'INSERT INTO sources(id, title, source_type, restricted, path) VALUES (?,?,?,?,?)',
        (sid, title, source_type, restricted, f'sources/{sid}.md'),
    )


class GedcomDateTests(unittest.TestCase):
    def test_year(self):
        self.assertEqual(gedcom._edtf_to_gedcom('1850'), '1850')

    def test_approx_year(self):
        self.assertEqual(gedcom._edtf_to_gedcom('1850~'), 'ABT 1850')

    def test_year_month(self):
        self.assertEqual(gedcom._edtf_to_gedcom('1850-05'), 'MAY 1850')

    def test_full_date(self):
        self.assertEqual(gedcom._edtf_to_gedcom('1850-05-20'), '20 MAY 1850')

    def test_interval(self):
        self.assertEqual(gedcom._edtf_to_gedcom('1871-02/1871-03'), 'BET FEB 1871 AND MAR 1871')

    def test_decade(self):
        self.assertEqual(gedcom._edtf_to_gedcom('185X'), 'ABT 1855')

    def test_open_before(self):
        self.assertEqual(gedcom._edtf_to_gedcom('[..1920]'), 'BEF 1920')

    def test_empty(self):
        self.assertIsNone(gedcom._edtf_to_gedcom(''))


class GedcomNameTests(unittest.TestCase):
    """`_gedcom_name` returns `(name_field, suffix)` (issue #78): a
    generational suffix must never become - or double as - the /Surname/
    field, whether the index carries a surname or not."""

    def test_surname_suffix(self):
        self.assertEqual(gedcom._gedcom_name('John Smith', 'Smith'), ('John /Smith/', None))

    def test_no_surname_uses_last_token(self):
        self.assertEqual(gedcom._gedcom_name('John Smith', None), ('John /Smith/', None))

    def test_single_name(self):
        self.assertEqual(gedcom._gedcom_name('Madonna', None), ('Madonna //', None))

    # ── issue #78: generational suffix must not reach the /Surname/ field ──

    _SUFFIXES = ['Jr', 'Sr', 'II', 'III', 'IV', 'V']

    def test_suffix_without_indexed_surname_is_not_taken_as_surname(self):
        # GUARD (issue #78 case 1): no indexed surname, the `parts[-1]`
        # fallback used to make the suffix itself the surname -
        # 'Roy Eugene Dodson Jr' -> 'Roy Eugene Dodson /Jr/'.
        for suffix in self._SUFFIXES:
            with self.subTest(suffix=suffix):
                field, returned_suffix = gedcom._gedcom_name(f'Roy Eugene Dodson {suffix}', None)
                self.assertEqual(field, f'Roy Eugene /Dodson/ {suffix}')
                self.assertEqual(returned_suffix, suffix)

    def test_suffix_with_indexed_surname_does_not_double_the_surname(self):
        # GUARD (issue #78 case 1, the #53-created regression): once the
        # index correctly holds 'Dodson', the old `endswith` guard failed
        # (the raw name ends with the suffix, not the surname) and fell
        # through to the append-both-copies branch -
        # 'Roy Eugene Dodson Jr' -> 'Roy Eugene Dodson Jr /Dodson/'.
        for suffix in self._SUFFIXES:
            with self.subTest(suffix=suffix):
                field, returned_suffix = gedcom._gedcom_name(f'Roy Eugene Dodson {suffix}', 'Dodson')
                self.assertEqual(field, f'Roy Eugene /Dodson/ {suffix}')
                self.assertEqual(returned_suffix, suffix)

    def test_mononym_unchanged(self):
        self.assertEqual(gedcom._gedcom_name('Cher', None), ('Cher //', None))

    def test_surname_genuinely_at_the_end_unchanged(self):
        self.assertEqual(
            gedcom._gedcom_name('Roy Eugene Dodson', 'Dodson'), ('Roy Eugene /Dodson/', None),
        )
        self.assertEqual(
            gedcom._gedcom_name('Roy Eugene Dodson', None), ('Roy Eugene /Dodson/', None),
        )

    # ── the token-match rule the suffix fix introduced, in its own right ──
    # #85 changed the surname guard from `endswith` to token equality and
    # claimed two further fixes in its docstring without testing either.
    # These pin them, plus the `/` and capitalisation defects the rewrite
    # left standing. Comments marked GUARD name behaviour that was broken
    # when the test was written; the rest is regression coverage.

    def test_an_incidental_substring_is_not_a_surname_match(self):
        # GUARD: the old `name.lower().endswith(sn.lower())` guard read
        # "Vandodson" as a hit for an indexed "Dodson" and sliced the field
        # mid-token - 'Roy Van /Dodson/', inventing a given name "Van" and
        # a surname the name never carried. Token equality says no match,
        # so the indexed surname is appended as the override it is.
        self.assertEqual(
            gedcom._gedcom_name('Roy Vandodson', 'Dodson'),
            ('Roy Vandodson /Dodson/', None),
        )

    def test_a_multi_word_indexed_surname_matches_as_a_token_run(self):
        self.assertEqual(
            gedcom._gedcom_name('Roy van der Berg', 'van der Berg'),
            ('Roy /van der Berg/', None),
        )
        self.assertEqual(
            gedcom._gedcom_name('Roy van der Berg III', 'van der Berg'),
            ('Roy /van der Berg/ III', 'III'),
        )

    def test_an_indexed_surname_the_name_does_not_carry_is_appended(self):
        # A married woman filed under her birth surname: the index says
        # Jones, the record says Smith. Neither is dropped.
        self.assertEqual(
            gedcom._gedcom_name('Mary Smith', 'Jones'), ('Mary Smith /Jones/', None),
        )

    def test_the_surname_keeps_the_records_own_capitalisation(self):
        # GUARD: `persons.surname` is the lowercase filename slug run
        # through `str.title()` (index.py), so `mcdonald__john_P-….md`
        # indexes as "Mcdonald" and the export published the machine's
        # spelling of a name the record spells "McDonald". The match is
        # case-insensitive, so the name's own tokens are free to win.
        self.assertEqual(
            gedcom._gedcom_name('John McDonald', 'Mcdonald'), ('John /McDonald/', None),
        )
        self.assertEqual(
            gedcom._gedcom_name('Roy van Dodson', 'Van Dodson'),
            ('Roy /van Dodson/', None),
        )

    # ── `/` is the NAME line's grammar, never name text ──

    def test_a_slash_in_the_name_never_redraws_the_surname_field(self):
        # GUARD: `1 NAME` delimits the surname with slashes, so a slash in
        # the name text moved the field. 'Roy A/B Dodson' exported as
        # '1 NAME Roy A/B /Dodson/', which reads back - through this
        # repo's own `fha gedcom import` - with the surname "B".
        for surname in ('Dodson', None):
            with self.subTest(surname=surname):
                self.assertEqual(
                    gedcom._gedcom_name('Roy A/B Dodson', surname),
                    ('Roy A B /Dodson/', None),
                )

    def test_a_slash_in_the_indexed_surname_never_redraws_the_field(self):
        # The same defect from the other side: a slash inside the slash
        # field closes it early.
        self.assertEqual(
            gedcom._gedcom_name('Roy Dodson', 'Dod/son'),
            ('Roy Dodson /Dod son/', None),
        )

    def test_a_name_of_nothing_but_slashes_falls_back_to_unknown(self):
        # Removing the slashes can empty the name; the `or 'Unknown'`
        # fallback has to survive that, not just an empty input.
        self.assertEqual(gedcom._gedcom_name('/', None), ('Unknown //', None))
        self.assertEqual(gedcom._gedcom_name(' // ', 'Dodson'), ('Unknown /Dodson/', None))

    # ── a placeholder is not somebody's name ──

    def test_the_unknown_placeholder_is_never_exported_as_a_surname(self):
        # GUARD: `fha index` stores a record with no `name:` as the string
        # "unknown" and reads "Unknown" out of the `unknown__unknown_…`
        # filename slug, so a person nobody has named yet exported as
        # '1 NAME /Unknown/' - a surname asserted in material that leaves
        # the archive. Both slots drop it; the given-name placeholder the
        # exporter already had is the one place the word may appear. The
        # guard is the shared `_lib.is_placeholder_name`, so the whole set
        # is covered, not just "unknown" - a bare `name:` key reaches the
        # index as the string "None".
        for name, indexed in (('unknown', 'Unknown'), ('unknown', None),
                              ('Unknown', 'Unknown'), ('None', 'Unknown'),
                              ('?', None), ('unnamed', 'None')):
            with self.subTest(name=name, indexed=indexed):
                self.assertEqual(gedcom._gedcom_name(name, indexed), ('Unknown //', None))

    def test_a_named_person_under_a_placeholder_slug_keeps_their_own_name(self):
        # GUARD: the same slug outlives the placeholder - a human types a
        # name into an `unknown__unknown_…` file and `fha lint --fix-ids`
        # has not renamed it yet. That exported as 'Roy Dodson /Unknown/'.
        self.assertEqual(gedcom._gedcom_name('Roy Dodson', 'Unknown'),
                         ('Roy /Dodson/', None))

    def test_the_exported_name_survives_a_round_trip_through_the_importer(self):
        # Two-sided rule, two-sided test: whatever `_gedcom_name` writes,
        # `gedcom_import._parse_gedcom_name` must read back with the same
        # surname AND the same display name. This is the assertion the
        # slash defect failed. The expected field is spelled out rather
        # than derived from the output, so a wrong field cannot satisfy
        # its own round trip.
        for name, indexed, expected_field, expected_display in (
            ('Roy A/B Dodson', 'Dodson', 'Roy A B /Dodson/', 'Roy A B Dodson'),
            ('Roy Eugene Dodson Jr', 'Dodson',
             'Roy Eugene /Dodson/ Jr', 'Roy Eugene Dodson Jr'),
            ('Roy Eugene Dodson Jr', None,
             'Roy Eugene /Dodson/ Jr', 'Roy Eugene Dodson Jr'),
            ('John McDonald', 'Mcdonald', 'John /McDonald/', 'John McDonald'),
            ('Cher', None, 'Cher //', 'Cher'),
            ('unknown', 'Unknown', 'Unknown //', 'Unknown'),
        ):
            with self.subTest(name=name):
                field, suffix = gedcom._gedcom_name(name, indexed)
                self.assertEqual(field, expected_field)
                # The exporter writes the suffix twice (NAME text + NSFX);
                # the importer is handed both, exactly as the file has it.
                display, _given, surname_slot = gedcom_import._parse_gedcom_name(
                    field, suffix or '')
                self.assertEqual(display, expected_display)
                self.assertEqual(surname_slot, '' if '//' in field else field.split('/')[1])


class GedcomExportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        conn = _make_index(self.root)
        # John + Mary -> Sam; Sam + Liz(living) -> Kid(living)
        _add_person(conn, 'p-0000000001', 'John Smith', 'M', surname='Smith')
        _add_person(conn, 'p-0000000002', 'Mary Jones', 'F', surname='Jones')
        _add_person(conn, 'p-0000000003', 'Sam Smith', 'M', surname='Smith')
        _add_person(conn, 'p-0000000004', 'Liz Doe', 'F', living='unknown', surname='Doe')
        _add_person(conn, 'p-0000000005', 'Kid Smith', 'M', living='unknown', surname='Smith')
        _spouse(conn, 'p-0000000001', 'p-0000000002', ds='1900-06-01')
        _parent_child(conn, 'p-0000000001', 'p-0000000003')
        _parent_child(conn, 'p-0000000002', 'p-0000000003')
        _spouse(conn, 'p-0000000003', 'p-0000000004')
        _parent_child(conn, 'p-0000000003', 'p-0000000005')
        _parent_child(conn, 'p-0000000004', 'p-0000000005')
        _add_source(conn, 's-0000000001', 'Birth cert')
        _add_source(conn, 's-0000000002', 'Marriage record')
        _add_claim(conn, 'c-0000000001', 'birth', ['p-0000000001'],
                   date_edtf='1875-03-02', place_text='Boston', source_id='s-0000000001')
        _add_claim(conn, 'c-0000000002', 'marriage', ['p-0000000001', 'p-0000000002'],
                   date_edtf='1900-06-01', place_text='Boston', source_id='s-0000000002')
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_descendants_selects_whole_tree(self):
        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='descendants')
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['person_count'], 5)
        self.assertIn('0 TRLR', r['text'])
        self.assertTrue(r['text'].startswith('0 HEAD'))

    def test_seed_and_all_conflict_rejected(self):
        # A seed P-id together with --all is ambiguous; reject it rather than
        # silently letting --all win and dropping the seed.
        r = gedcom.run_gedcom(self.root, 'p-0000000001', all_persons=True)
        self.assertEqual(r['status'], 'bad-args')
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertIsNone(r['text'])
        self.assertIn('conflict', ' '.join(r['messages']).lower())

    def test_generations_cap(self):
        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='descendants', generations=1)
        # John, Mary (spouse), Sam (child), Liz (Sam's spouse) - not Kid (depth 2)
        self.assertEqual(r['person_count'], 4)

    def test_living_redacted_by_default(self):
        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='descendants')
        self.assertIn('1 NAME /Living/', r['text'])
        self.assertNotIn('Kid /Smith/', r['text'])
        self.assertNotIn('Liz /Doe/', r['text'])

    def test_include_living(self):
        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='descendants', include_living=True)
        self.assertIn('Liz /Doe/', r['text'])
        self.assertIn('Kid /Smith/', r['text'])
        self.assertNotIn('/Living/', r['text'])

    def test_a_suffix_reaches_the_file_as_a_name_line_and_an_nsfx_subtag(self):
        # GUARD (issue #78, end to end): `_gedcom_name` returning the
        # suffix is only half the contract - the exported FILE has to
        # carry it, on the `1 NAME` line and under the dedicated `2 NSFX`
        # sub-tag beneath it. Before the fix this person exported as
        # '1 NAME Roy Eugene Dodson Jr /Dodson/' with no NSFX at all.
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        _add_person(conn, 'p-0000000006', 'Roy Eugene Dodson Jr', 'M', surname='Dodson')
        _parent_child(conn, 'p-0000000001', 'p-0000000006')
        conn.commit()
        conn.close()

        text = gedcom.run_gedcom(self.root, 'p-0000000001', mode='descendants')['text']
        lines = text.replace('\r\n', '\n').split('\n')
        self.assertIn('1 NAME Roy Eugene /Dodson/ Jr', lines)
        # The NSFX must sit directly under that NAME line - a level-2
        # sub-tag belongs to whatever level-1 line precedes it, so a
        # stray one would attach the suffix to the wrong person.
        i = lines.index('1 NAME Roy Eugene /Dodson/ Jr')
        self.assertEqual(lines[i + 1], '2 NSFX Jr')
        self.assertEqual(text.count('2 NSFX'), 1)
        self.assertNotIn('Dodson Jr /Dodson/', text)

    def test_a_redacted_person_emits_no_nsfx(self):
        # A withheld name gives nothing away, suffix included: `2 NSFX Jr`
        # under '1 NAME /Living/' would leak that this is a junior.
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        _add_person(conn, 'p-0000000007', 'Roy Eugene Dodson Jr', 'M',
                    living='true', surname='Dodson')
        _parent_child(conn, 'p-0000000001', 'p-0000000007')
        conn.commit()
        conn.close()

        text = gedcom.run_gedcom(self.root, 'p-0000000001', mode='descendants')['text']
        self.assertIn('1 NAME /Living/', text)
        self.assertNotIn('NSFX', text)

    def test_vitals_and_sources_emitted(self):
        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='ancestors')
        self.assertIn('1 BIRT', r['text'])
        self.assertIn('2 DATE 2 MAR 1875', r['text'])
        self.assertIn('1 MARR', r['text'])
        self.assertIn('0 @S1@ SOUR', r['text'])
        self.assertIn('Birth cert', r['text'])

    def test_negated_vitals_and_marriage_not_exported(self):
        # A --negated claim records a confirmed ABSENCE, never a positive fact.
        # It must not surface as a BIRT/DEAT/MARR event, which another genealogy
        # app would read as a real event; and an earlier-dated negated birth must
        # not evict the real one. (Same negated-exclusion sweep as index/site/lint.)
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        _add_claim(conn, 'c-0000000010', 'birth', ['p-0000000001'],
                   date_edtf='1800', place_text='Nowhere',
                   source_id='s-0000000001', negated=1)
        _add_claim(conn, 'c-0000000011', 'death', ['p-0000000001'],
                   date_edtf='1850', place_text='Nowhere',
                   source_id='s-0000000001', negated=1)
        _add_claim(conn, 'c-0000000012', 'marriage', ['p-0000000001', 'p-0000000002'],
                   date_edtf='1899', place_text='Nowhere',
                   source_id='s-0000000002', negated=1)
        conn.commit()
        conn.close()

        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='ancestors')
        self.assertEqual(r['status'], 'ok')
        self.assertIn('2 DATE 2 MAR 1875', r['text'])   # real birth still wins
        self.assertNotIn('1800', r['text'])             # negated birth excluded
        self.assertNotIn('1 DEAT', r['text'])           # negated death not emitted
        self.assertNotIn('Nowhere', r['text'])          # no negated place leaks
        self.assertEqual(r['text'].count('1 MARR'), 1)  # only the real marriage

    def test_restricted_vital_fact_not_exported(self):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        _add_source(conn, 's-0000000003', 'Restricted death cert', restricted=1)
        _add_claim(conn, 'c-0000000003', 'death', ['p-0000000001'],
                   date_edtf='1950', place_text='Hidden Town', source_id='s-0000000003')
        conn.commit()
        conn.close()

        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='ancestors')

        self.assertNotIn('1 DEAT', r['text'])
        self.assertNotIn('Hidden Town', r['text'])
        self.assertNotIn('Restricted death cert', r['text'])

    def test_dna_vital_fact_not_exported(self):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        _add_source(conn, 's-0000000003', 'DNA birth estimate', source_type='dna', restricted=1)
        _add_claim(conn, 'c-0000000003', 'death', ['p-0000000001'],
                   date_edtf='1950', place_text='DNA Lab', source_id='s-0000000003')
        conn.commit()
        conn.close()

        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='ancestors')

        self.assertNotIn('DNA Lab', r['text'])
        self.assertNotIn('DNA birth estimate', r['text'])

    def test_marriage_with_witness_uses_spouse_roles(self):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        _add_person(conn, 'p-0000000006', 'Anna Role', 'F', surname='Role')
        _add_person(conn, 'p-0000000007', 'Ben Role', 'M', surname='Role')
        _add_person(conn, 'p-0000000008', 'Witness Person', 'M', surname='Person')
        _spouse(conn, 'p-0000000006', 'p-0000000007')
        _add_source(conn, 's-0000000003', 'Role marriage record')
        conn.execute(
            'INSERT INTO claims(id, source_id, type, date_edtf, date_min, place_text, value, status) '
            'VALUES (?,?,?,?,?,?,?,?)',
            ('c-0000000003', 's-0000000003', 'marriage', '1901', '1901-01-01',
             'Role Town', 'married with witness', 'accepted'),
        )
        for pos, (pid, role) in enumerate([
            ('p-0000000006', 'spouse'),
            ('p-0000000007', 'spouse'),
            ('p-0000000008', 'witness'),
        ]):
            conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
                ('c-0000000003', pid, pos, role),
            )
        conn.commit()
        conn.close()

        r = gedcom.run_gedcom(self.root, 'p-0000000006', mode='connected')

        self.assertIn('Role Town', r['text'])
        self.assertIn('Role marriage record', r['text'])

    def test_marriage_redacted_when_spouse_living(self):
        # Sam + Liz family: Liz is living, so MARR detail (none here) and the
        # couple is not given marriage details; ensure no Liz name leaks.
        r = gedcom.run_gedcom(self.root, 'p-0000000003', mode='descendants')
        self.assertIn('/Living/', r['text'])

    def test_all_persons(self):
        r = gedcom.run_gedcom(self.root, None, all_persons=True)
        self.assertEqual(r['person_count'], 5)

    def test_not_found(self):
        r = gedcom.run_gedcom(self.root, 'p-9999999999', mode='descendants')
        self.assertEqual(r['status'], 'not-found')

    def test_bad_id(self):
        r = gedcom.run_gedcom(self.root, 'not-an-id', mode='descendants')
        self.assertEqual(r['status'], 'bad-args')

    def test_no_index(self):
        with tempfile.TemporaryDirectory() as empty:
            r = gedcom.run_gedcom(Path(empty), 'p-0000000001')
            self.assertEqual(r['status'], 'no-index')


class MarriageRoleScopingTests(unittest.TestCase):
    """The export reads `roles:` for who married whom, exactly as the index does.

    A marriage certificate names the couple AND both sets of parents, and
    listing all six in `persons:` is correct (`persons:` is who the claim is
    about, SPEC §8.3). The GEDCOM writer keys each marriage claim to a FAM by
    its spouse pair; keying it by the first two people on the certificate
    instead hangs the son's wedding date and place on his parents' family
    record - a fact about the wrong marriage, exported as truth into whatever
    program reads the file. Index and export must answer "who married whom" the
    same way or the archive contradicts its own export (TOOLING §197).
    """

    HUS, WIF = 'p-1000000001', 'p-1000000002'
    HFA, HMO = 'p-1000000003', 'p-1000000004'
    WFA, WMO = 'p-1000000005', 'p-1000000006'
    KID = 'p-1000000007'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        conn = _make_index(self.root)
        for pid, name, sex, surname in (
            (self.HUS, 'Amos Prentice', 'M', 'Prentice'),
            (self.WIF, 'Clara Denby', 'F', 'Denby'),
            (self.HFA, 'Reuben Prentice', 'M', 'Prentice'),
            (self.HMO, 'Hannah Prentice', 'F', 'Prentice'),
            (self.WFA, 'Silas Denby', 'M', 'Denby'),
            (self.WMO, 'Martha Denby', 'F', 'Denby'),
            (self.KID, 'Ida Prentice', 'F', 'Prentice'),
        ):
            _add_person(conn, pid, name, sex, surname=surname)
        # Both sets of parents are genuinely married; each raised one of the
        # couple; the couple has a child, so their own family record exists
        # whether or not the marriage claim could be scoped.
        _spouse(conn, self.HFA, self.HMO)
        _spouse(conn, self.WFA, self.WMO)
        for parent in (self.HFA, self.HMO):
            _parent_child(conn, parent, self.HUS)
        for parent in (self.WFA, self.WMO):
            _parent_child(conn, parent, self.WIF)
        for parent in (self.HUS, self.WIF):
            _parent_child(conn, parent, self.KID)
        _add_source(conn, 's-0000000009', 'Prentice-Denby marriage certificate')
        conn.commit()
        self.conn = conn

    def tearDown(self):
        try:
            self.conn.close()
        except sqlite3.ProgrammingError:
            pass
        self._tmp.cleanup()

    def _certificate(self, roles: dict | None) -> None:
        """The six-person certificate, parents transcribed first (the ordinary
        order on a printed form), optionally carrying a `roles: spouse:` map."""
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, date_edtf, date_min, place_text, '
            'value, status) VALUES (?,?,?,?,?,?,?,?)',
            ('c-0000000009', 's-0000000009', 'marriage', '1890', '1890-01-01',
             'Wedding Town', 'married', 'accepted'),
        )
        order = [self.HFA, self.HMO, self.WFA, self.WMO, self.HUS, self.WIF]
        for pos, pid in enumerate(order):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) '
                'VALUES (?,?,?,?)',
                ('c-0000000009', pid, pos, (roles or {}).get(pid)),
            )
        self.conn.commit()
        self.conn.close()

    def _families(self, text: str) -> dict[frozenset, str]:
        """Every FAM record in the export, keyed by its HUSB/WIFE person ids."""
        xref_to_pid = {}
        blocks, current = [], None
        for line in text.split('\r\n'):
            if line.startswith('0 '):
                if current is not None:
                    blocks.append(current)
                current = [line] if ' INDI' in line or ' FAM' in line else None
                continue
            if current is not None:
                current.append(line)
        if current is not None:
            blocks.append(current)
        for block in blocks:
            if block[0].endswith(' INDI'):
                xref = block[0].split('@')[1]
                for line in block:
                    if line.startswith('1 REFN '):
                        xref_to_pid[xref] = line[len('1 REFN '):].strip().lower()
        out = {}
        for block in blocks:
            if not block[0].endswith(' FAM'):
                continue
            members = frozenset(
                xref_to_pid.get(line.split('@')[1], line)
                for line in block
                if line.startswith('1 HUSB ') or line.startswith('1 WIFE ')
            )
            out[members] = '\r\n'.join(block)
        return out

    def test_six_person_certificate_without_roles_never_marries_the_parents(self):
        # Pre-fix the writer fell back to the first two people on the claim -
        # here the groom's parents - and hung the son's 1890 wedding on THEIR
        # family record. The index (correctly) derives no spouse edge from this
        # claim at all, so the export must record no marriage from it either.
        self._certificate(roles=None)
        r = gedcom.run_gedcom(self.root, self.HUS, mode='connected')
        self.assertEqual(r['status'], 'ok')
        self.assertNotIn(
            'Wedding Town', r['text'],
            "a certificate that never says who the couple were must not put "
            "its date and place on somebody else's family record")
        fams = self._families(r['text'])
        parents_fam = fams.get(frozenset({self.HFA, self.HMO}))
        self.assertIsNotNone(parents_fam, 'the parents keep their own family record')
        self.assertNotIn('MARR', parents_fam)

    def test_six_person_certificate_with_roles_marries_the_couple(self):
        # With the roles: map present the event belongs to the couple - and to
        # nobody else on the certificate.
        self._certificate(roles={self.HUS: 'spouse', self.WIF: 'spouse'})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        _spouse(conn, self.HUS, self.WIF)     # the edge the fixed index derives
        conn.commit()
        conn.close()
        r = gedcom.run_gedcom(self.root, self.HUS, mode='connected')
        fams = self._families(r['text'])
        couple_fam = fams.get(frozenset({self.HUS, self.WIF}))
        self.assertIsNotNone(couple_fam)
        self.assertIn('MARR', couple_fam)
        self.assertIn('Wedding Town', couple_fam)
        self.assertNotIn('MARR', fams[frozenset({self.HFA, self.HMO})])
        self.assertNotIn('MARR', fams[frozenset({self.WFA, self.WMO})])

    def test_partial_roles_map_falls_back_to_the_two_named_people(self):
        # A roles: map naming one resolvable spouse has not answered the
        # question, so a two-person claim still marries the two it names - the
        # same fallback the index applies, so the two never disagree.
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, date_edtf, date_min, place_text, '
            'value, status) VALUES (?,?,?,?,?,?,?,?)',
            ('c-0000000009', 's-0000000009', 'marriage', '1890', '1890-01-01',
             'Wedding Town', 'married', 'accepted'),
        )
        for pos, (pid, role) in enumerate(
                [(self.HUS, 'spouse'), (self.WIF, None)]):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) '
                'VALUES (?,?,?,?)', ('c-0000000009', pid, pos, role))
        _spouse(self.conn, self.HUS, self.WIF)
        self.conn.commit()
        self.conn.close()
        r = gedcom.run_gedcom(self.root, self.HUS, mode='connected')
        couple_fam = self._families(r['text'])[frozenset({self.HUS, self.WIF})]
        self.assertIn('Wedding Town', couple_fam)

    def test_serial_marriage_event_that_finds_no_family_is_reported(self):
        # A `roles: spouse:` naming three people (successive marriages recorded
        # on one claim) is a party set no two-person FAM key can match, so the
        # MARR date and place simply have nowhere to go. Refusing to guess
        # which two of the three to hang it on is right; dropping the event
        # without a word is not - the export would carry the families and quietly
        # lose the marriage facts, and nothing on the human's screen would say so.
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, date_edtf, date_min, place_text, '
            'value, status) VALUES (?,?,?,?,?,?,?,?)',
            ('c-0000000009', 's-0000000009', 'marriage', '1890', '1890-01-01',
             'Wedding Town', 'married', 'accepted'),
        )
        for pos, pid in enumerate([self.HUS, self.WIF, self.HMO]):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) '
                'VALUES (?,?,?,?)', ('c-0000000009', pid, pos, 'spouse'))
        # The three pairings the index derives from that claim.
        for a, b in ((self.HUS, self.WIF), (self.HUS, self.HMO), (self.WIF, self.HMO)):
            _spouse(self.conn, a, b)
        self.conn.commit()
        self.conn.close()
        r = gedcom.run_gedcom(self.root, self.HUS, mode='connected')
        self.assertEqual(r['status'], 'ok')
        # The event really is absent - that part is the deliberate refusal.
        self.assertNotIn('Wedding Town', r['text'])
        # And the export says so, in the same place it reports redactions.
        said = ' '.join(r['messages'])
        self.assertIn('marriage', said.lower())
        self.assertIn('c-0000000009', said.lower())
        self.assertIn('3 people', said)
        self.assertIn('roles:', said)

    def test_an_ordinary_marriage_reports_nothing(self):
        # The false positive the warning must not have: a two-person marriage
        # whose family the export builds is placed, and nothing is said.
        self._certificate(roles={self.HUS: 'spouse', self.WIF: 'spouse'})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        _spouse(conn, self.HUS, self.WIF)
        conn.commit()
        conn.close()
        r = gedcom.run_gedcom(self.root, self.HUS, mode='connected')
        self.assertEqual(
            [m for m in r['messages'] if 'marriage' in m.lower()], [])

    def test_a_marriage_outside_the_exported_set_reports_nothing(self):
        # The other false positive: a depth-capped or seeded export leaves
        # people out on purpose, and a marriage whose couple is not in the file
        # is scoping, not a lost fact. Only an event whose people ARE all here
        # and still has no home is worth a word.
        self._certificate(roles={self.HUS: 'spouse', self.WIF: 'spouse'})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        _spouse(conn, self.HUS, self.WIF)
        conn.commit()
        conn.close()
        r = gedcom.run_gedcom(self.root, self.WFA, mode='ancestors')
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(
            [m for m in r['messages'] if 'marriage' in m.lower()], [])


class BirthDeathRoleScopingTests(unittest.TestCase):
    """A BIRT/DEAT event belongs to the person the record is OF, not to every
    person the record names.

    The scoping rule `MarriageRoleScopingTests` covers for MARR was never
    carried to the vitals: `_load_vitals` picked the first accepted birth or
    death claim NAMING each person, so a mother co-named on her son's birth
    certificate exported his 1888 birth as her own BIRT. That reaches whatever
    program reads the file as a plain, unqualified fact about her (#126).
    """

    MOM, SON = 'p-2000000001', 'p-2000000002'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        conn = _make_index(self.root)
        _add_person(conn, self.MOM, 'Iris Marr', 'F', surname='Marr')
        _add_person(conn, self.SON, 'Peter Marr', 'M', surname='Marr')
        _parent_child(conn, self.MOM, self.SON)
        _add_source(conn, 's-0000000001', 'Birth certificate')
        self.conn = conn

    def tearDown(self):
        try:
            self.conn.close()
        except sqlite3.ProgrammingError:
            pass
        self._tmp.cleanup()

    def _export(self, roles):
        _add_claim(self.conn, 'c-0000000001', 'birth', [self.SON, self.MOM],
                   date_edtf='1888', place_text='Riverton', roles=roles)
        self.conn.commit()
        self.conn.close()
        r = gedcom.run_gedcom(self.root, self.SON, mode='connected')
        self.assertEqual(r['status'], 'ok')
        return _individuals(r['text'])

    def test_mother_named_as_parent_exports_no_birth_of_her_own(self):
        indi = self._export({self.SON: 'child', self.MOM: 'parent'})
        self.assertNotIn('BIRT', indi[self.MOM],
                         "the mother's INDI record must carry no birth event "
                         "taken from her son's birth certificate")

    def test_the_child_the_claim_names_still_exports_his_birth(self):
        # The other half: scoping must not cost the subject his own event.
        indi = self._export({self.SON: 'child', self.MOM: 'parent'})
        self.assertIn('BIRT', indi[self.SON])
        self.assertIn('Riverton', indi[self.SON])

    def test_a_legacy_claim_naming_two_people_with_no_roles_exports_neither_birth(self):
        # Two people, no roles: map at all - the claim has not said which of
        # them was born. This is the exact class-docstring bug (a mother's
        # own BIRT event copied off her son's birth certificate) reached
        # through the unroled case instead of the miscast one: the old
        # back-compatibility bargain used to guess "everyone" here too, which
        # is #126 restated with the roles: map simply absent rather than
        # wrong (#126, reopened). Withholding both is the same "missing beats
        # false" call `test_mother_named_as_parent_exports_no_birth_of_her_own`
        # already makes for the roled case.
        indi = self._export(None)
        self.assertNotIn('BIRT', indi[self.MOM])
        self.assertNotIn('BIRT', indi[self.SON])

    def test_a_legacy_claim_naming_only_the_child_keeps_its_old_behaviour(self):
        # One person named, no roles: map - nobody to be ambiguous about, so
        # the pre-#126 "the claim never said, so nothing is withheld"
        # back-compatibility bargain is still exactly right here.
        _add_claim(self.conn, 'c-0000000001', 'birth', [self.SON],
                   date_edtf='1888', place_text='Riverton', roles=None)
        self.conn.commit()
        self.conn.close()
        r = gedcom.run_gedcom(self.root, self.SON, mode='connected')
        self.assertEqual(r['status'], 'ok')
        indi = _individuals(r['text'])
        self.assertIn('BIRT', indi[self.SON])

    def test_a_relative_on_a_death_record_exports_no_death_of_her_own(self):
        # SPEC §8.3 names no role for the deceased, so the claim can only say
        # who the OTHERS were; the unroled person is the one who died.
        _add_source(self.conn, 's-0000000002', 'Death certificate')
        _add_claim(self.conn, 'c-0000000002', 'death', [self.MOM, self.SON],
                   date_edtf='1920', place_text='Riverton',
                   source_id='s-0000000002', roles={self.SON: 'child'})
        self.conn.commit()
        self.conn.close()
        r = gedcom.run_gedcom(self.root, self.SON, mode='connected')
        indi = _individuals(r['text'])
        self.assertNotIn('DEAT', indi[self.SON])
        self.assertIn('DEAT', indi[self.MOM])


if __name__ == '__main__':
    unittest.main()
