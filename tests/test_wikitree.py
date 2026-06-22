import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import wikitree
from index import _DDL


def _make_index(archive_root: Path) -> sqlite3.Connection:
    cache = archive_root / '.cache'
    cache.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache / 'index.sqlite'))
    conn.executescript(_DDL)
    conn.row_factory = sqlite3.Row
    return conn


def _add_person(conn, pid, name, tier='curated', living='false', path=None, surname=None):
    conn.execute(
        'INSERT INTO persons(id, name, surname, living, tier, path) VALUES (?,?,?,?,?,?)',
        (pid, name, surname, living, tier, path or f'people/{pid}.md'),
    )


def _add_source(conn, sid, title, path, *, source_type='vital-record', restricted=0):
    conn.execute(
        'INSERT INTO sources(id, title, source_type, restricted, path) VALUES (?,?,?,?,?)',
        (sid, title, source_type, restricted, path),
    )


def _add_claim(conn, cid, ctype, persons, date_edtf='', place_text=None,
               source_id='s-0000000001', status='accepted', value='x'):
    mn = ''
    if date_edtf:
        from _lib import edtf_bounds
        mn = edtf_bounds(date_edtf)[0]
    conn.execute(
        'INSERT INTO claims(id, source_id, type, date_edtf, date_min, place_text, value, status) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (cid, source_id, ctype, date_edtf, mn, place_text, value, status),
    )
    for pos, p in enumerate(persons):
        conn.execute(
            'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
            (cid, p, pos, None),
        )


class WikitreeUnitTests(unittest.TestCase):
    def test_ancestry_dbid_h(self):
        url = 'https://search.ancestry.com/cgi-bin/sse.dll?dbid=6224&h=12345'
        self.assertEqual(wikitree._ancestry_image_template(url), '{{Ancestry Image|6224|12345}}')

    def test_ancestry_view(self):
        url = 'https://www.ancestry.com/discoveryui-content/view/98765:6224'
        self.assertEqual(wikitree._ancestry_image_template(url), '{{Ancestry Image|6224|98765}}')

    def test_non_ancestry(self):
        self.assertIsNone(wikitree._ancestry_image_template('https://findagrave.com/123'))

    def test_heading_conversion(self):
        self.assertEqual(wikitree._convert_heading('## Biography'), '== Biography ==')
        self.assertEqual(wikitree._convert_heading('### Notes'), '=== Notes ===')
        self.assertIsNone(wikitree._convert_heading('# Title'))

    def test_sentence_split_keeps_initials(self):
        s = wikitree._split_sentences('Margaret A. Cole married him. She lived in Boston.')
        self.assertEqual(len(s), 2)


class WikitreeRenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people').mkdir()
        (self.root / 'sources').mkdir()

        profile = (self.root / 'people' / 'subject.md')
        profile.write_text(
            '---\n'
            'id: P-0000000001\n'
            'name: John Smith\n'
            'tier: curated\n'
            'living: false\n'
            '---\n\n'
            '# John Smith\n\n'
            '## Biography\n'
            'John married Mary Jones [P-0000000002] in 1900 [S-0000000002].\n'
            'He was born in 1875 [S-0000000001].\n\n'
            '## Stories\n'
            '*(none yet)*\n',
            encoding='utf-8',
        )
        src1 = (self.root / 'sources' / 'birth.md')
        src1.write_text(
            '---\nid: S-0000000001\ntitle: Birth cert\nsource_type: vital-record\n'
            'citation: "Birth certificate of John Smith, 1875."\n'
            'external_links: ["https://search.ancestry.com/x?dbid=6224&h=99"]\n---\n',
            encoding='utf-8',
        )
        src2 = (self.root / 'sources' / 'marr.md')
        src2.write_text(
            '---\nid: S-0000000002\ntitle: Marriage record\nsource_type: vital-record\n'
            'citation: "Marriage record, John & Mary, 1900."\n---\n',
            encoding='utf-8',
        )

        conn = _make_index(self.root)
        _add_person(conn, 'p-0000000001', 'John Smith', path='people/subject.md', surname='Smith')
        _add_person(conn, 'p-0000000002', 'Mary Jones', tier='stub', surname='Jones')
        conn.execute(
            "INSERT INTO person_external(person_id, system, ext_id) VALUES (?,?,?)",
            ('p-0000000002', 'wikitree', 'Jones-99'),
        )
        _add_source(conn, 's-0000000001', 'Birth cert', 'sources/birth.md')
        _add_source(conn, 's-0000000002', 'Marriage record', 'sources/marr.md')
        # Marriage source has exactly one dated+placed claim about John -> spacetime.
        _add_claim(conn, 'c-0000000002', 'marriage', ['p-0000000001'],
                   date_edtf='1900', place_text='Boston', source_id='s-0000000002')
        # Birth source's single dated+placed claim (year 1875).
        _add_claim(conn, 'c-0000000001', 'birth', ['p-0000000001'],
                   date_edtf='1875', place_text='Boston', source_id='s-0000000001')
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_refs_definitions_once_each(self):
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'ok')
        text = r['text']
        self.assertEqual(text.count('<ref name="S-0000000001">'), 1)
        self.assertEqual(text.count('<ref name="S-0000000002">'), 1)
        # self-closing at use site
        self.assertIn('<ref name="S-0000000001"/>', text)
        self.assertIn('<div name="references" style="display: none">', text)
        self.assertTrue(text.rstrip().endswith('<references/>'))
        self.assertIn('== Sources ==', text)

    def test_person_link_with_wikitree_id(self):
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        # "Mary Jones [P-...]" folds into a single WikiTree link, not doubled.
        self.assertIn('[[Jones-99|Mary Jones]]', r['text'])
        self.assertNotIn('Mary Jones Mary Jones', r['text'])

    def test_spacetime_span_on_matching_year(self):
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        text = r['text']
        self.assertIn('class="spacetime" data-loc="Boston" data-date="1900-01-01"', text)
        # The birth sentence (year 1875) must not carry the 1900 marriage date.
        self.assertNotIn('data-date="1900-01-01">He was born', text)

    def test_ancestry_template_in_reference(self):
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertIn('{{Ancestry Image|6224|99}}', r['text'])

    def test_placeholder_removed(self):
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertNotIn('(none yet)', r['text'])

    def test_living_subject_refused(self):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.execute("UPDATE persons SET living='unknown' WHERE id='p-0000000001'")
        conn.commit()
        conn.close()
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'living-subject')

    def test_not_curated(self):
        r = wikitree.run_wikitree(self.root, 'p-0000000002')
        self.assertEqual(r['status'], 'not-curated')

    def test_not_found(self):
        r = wikitree.run_wikitree(self.root, 'p-9999999999')
        self.assertEqual(r['status'], 'not-found')

    def test_bad_id(self):
        r = wikitree.run_wikitree(self.root, 'nope')
        self.assertEqual(r['status'], 'bad-args')

    def test_restricted_source_citation_refused(self):
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8')
            + '\nA private fact appears here [S-0000000003].\n',
            encoding='utf-8',
        )
        src3 = self.root / 'sources' / 'private.md'
        src3.write_text(
            '---\nid: S-0000000003\ntitle: Private source\nrestricted: true\n---\n',
            encoding='utf-8',
        )
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        _add_source(conn, 's-0000000003', 'Private source', 'sources/private.md', restricted=1)
        conn.commit()
        conn.close()

        r = wikitree.run_wikitree(self.root, 'p-0000000001')

        self.assertEqual(r['status'], 'restricted-sources')
        self.assertIsNone(r['text'])
        self.assertIn('S-0000000003', r['messages'][0])

    def test_dna_source_citation_refused(self):
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8')
            + '\nA DNA conclusion appears here [S-0000000003].\n',
            encoding='utf-8',
        )
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        _add_source(
            conn, 's-0000000003', 'DNA source', 'sources/dna.md',
            source_type='dna', restricted=1,
        )
        conn.commit()
        conn.close()

        r = wikitree.run_wikitree(self.root, 'p-0000000001')

        self.assertEqual(r['status'], 'restricted-sources')


if __name__ == '__main__':
    unittest.main()
