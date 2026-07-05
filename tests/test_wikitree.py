import os
import sqlite3
import sys
import tempfile
import time
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


def _freshen_index(archive_root: Path) -> None:
    """Stamp the index newer than every record so the strict freshness check
    passes after a test edits fixture files (same pattern as
    test_privacy_restricted's _Archive.fresh())."""
    future = time.time() + 5
    os.utime(archive_root / '.cache' / 'index.sqlite', (future, future))


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

    def test_restricted_name_wikilink_refused(self):
        # A restricted name variant (deadname) written as a name-style wikilink
        # renders verbatim and would publish the deadname even though the linked
        # person is not themselves restricted. The export must fail closed.
        marian = self.root / 'people' / 'p-0000000002.md'
        marian.write_text(
            '---\nid: P-0000000002\nname: Mary Jones\nliving: false\n'
            'name_variants:\n  - value: Marion Jones\n    restricted: true\n---\n',
            encoding='utf-8',
        )
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8')
            + '\nFormerly known as [[Marion Jones]].\n',
            encoding='utf-8',
        )
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.execute("UPDATE persons SET path='people/p-0000000002.md' WHERE id='p-0000000002'")
        conn.execute(
            "INSERT INTO aliases(alias, canonical_id, kind) VALUES (?,?,?)",
            ('marion jones', 'p-0000000002', 'variant'),
        )
        conn.commit()
        conn.close()

        r = wikitree.run_wikitree(self.root, 'p-0000000001')

        self.assertEqual(r['status'], 'restricted-names')
        self.assertIsNone(r['text'])
        self.assertIn('Marion Jones', r['messages'][0])

    def test_restricted_name_in_token_display_refused(self):
        # The same deadname written as an ID-token display, [[P-x|Marion
        # Jones]], would be re-emitted verbatim as the link text - it must be
        # refused exactly like the name-wikilink form.
        marian = self.root / 'people' / 'p-0000000002.md'
        marian.write_text(
            '---\nid: P-0000000002\nname: Mary Jones\nliving: false\n'
            'name_variants:\n  - value: Marion Jones\n    restricted: true\n---\n',
            encoding='utf-8',
        )
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8')
            + '\nFormerly known as [[P-0000000002|Marion Jones]].\n',
            encoding='utf-8',
        )
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.execute("UPDATE persons SET path='people/p-0000000002.md' WHERE id='p-0000000002'")
        conn.commit()
        conn.close()

        _freshen_index(self.root)
        r = wikitree.run_wikitree(self.root, 'p-0000000001')

        self.assertEqual(r['status'], 'restricted-names')
        self.assertIsNone(r['text'])
        self.assertIn('Marion Jones', r['messages'][0])

    def test_unrestricted_in_token_display_still_renders(self):
        # An in-token display that is NOT a restricted variant keeps rendering
        # as the link text - the deadname gate must not eat ordinary displays.
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8')
            + '\nAlso called [[P-0000000002|Molly]] by friends.\n',
            encoding='utf-8',
        )

        _freshen_index(self.root)
        r = wikitree.run_wikitree(self.root, 'p-0000000001')

        self.assertEqual(r['status'], 'ok')
        self.assertIn('[[Jones-99|Molly]]', r['text'])

    def test_living_id_token_redacted(self):
        # Pin the ID-token redaction: a living person cited by [[P-id]] (with
        # their name in the preceding prose) renders as [living person], and
        # the name does not survive anywhere in the output.
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        _add_person(conn, 'p-0000000003', 'Ken Smith', tier='connection',
                    living='true', surname='Smith')
        conn.commit()
        conn.close()
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8')
            + '\nHe worked with Ken Smith [P-0000000003] for years.\n',
            encoding='utf-8',
        )

        _freshen_index(self.root)
        r = wikitree.run_wikitree(self.root, 'p-0000000001')

        self.assertEqual(r['status'], 'ok')
        self.assertIn('[living person]', r['text'])
        self.assertNotIn('Ken Smith', r['text'])

    def test_living_name_wikilink_refused(self):
        # A living person referenced ONLY by a name-wikilink is not an ID
        # token, so the [living person] redaction never fires - the export
        # must fail closed and tell the human how to fix the reference.
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        _add_person(conn, 'p-0000000003', 'Ken Smith', tier='connection',
                    living='true', surname='Smith')
        conn.execute(
            "INSERT INTO aliases(alias, canonical_id, kind) VALUES (?,?,?)",
            ('ken smith', 'p-0000000003', 'name'),
        )
        conn.commit()
        conn.close()
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8')
            + '\nHe knew [[Ken Smith]] around town.\n',
            encoding='utf-8',
        )

        _freshen_index(self.root)
        r = wikitree.run_wikitree(self.root, 'p-0000000001')

        self.assertEqual(r['status'], 'living-people')
        self.assertIsNone(r['text'])
        self.assertIn('Ken Smith', r['messages'][0])
        self.assertIn('[living person]', r['messages'][0])   # the fix is named

    def test_living_unknown_name_wikilink_refused(self):
        # living: unknown IS living (SPEC §19) - the name-link gate must treat
        # it the same as an explicit true.
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        _add_person(conn, 'p-0000000003', 'Ken Smith', tier='connection',
                    living='unknown', surname='Smith')
        conn.execute(
            "INSERT INTO aliases(alias, canonical_id, kind) VALUES (?,?,?)",
            ('ken smith', 'p-0000000003', 'name'),
        )
        conn.commit()
        conn.close()
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8')
            + '\nHe knew [[Ken Smith]] around town.\n',
            encoding='utf-8',
        )

        _freshen_index(self.root)
        r = wikitree.run_wikitree(self.root, 'p-0000000001')

        self.assertEqual(r['status'], 'living-people')

    def test_deceased_name_wikilink_renders_verbatim(self):
        # A name-link that resolves to a deceased, unrestricted person keeps
        # today's behavior: it passes through untouched.
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.execute(
            "INSERT INTO aliases(alias, canonical_id, kind) VALUES (?,?,?)",
            ('mary jones', 'p-0000000002', 'name'),
        )
        conn.commit()
        conn.close()
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8')
            + '\nShe wrote often to [[Mary Jones]] after the war.\n',
            encoding='utf-8',
        )

        _freshen_index(self.root)
        r = wikitree.run_wikitree(self.root, 'p-0000000001')

        self.assertEqual(r['status'], 'ok')
        self.assertIn('[[Mary Jones]]', r['text'])

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

    def test_ambiguous_deadname_alias_names_every_restricting_person(self):
        # X4 regression (round-2 finding 19): an ambiguous alias resolves to
        # every candidate, and when several persons all restrict the variant
        # the refusal must name EACH of them - a break after the first match
        # silently dropped the rest of the cleanup list.
        for pid in ('P-0000000002', 'P-0000000003'):
            (self.root / 'people' / f'{pid.lower()}.md').write_text(
                f'---\nid: {pid}\nname: Someone Jones\nliving: false\n'
                'name_variants:\n  - value: Marion Jones\n    restricted: true\n---\n',
                encoding='utf-8',
            )
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8')
            + '\nFormerly known as [[Marion Jones]].\n',
            encoding='utf-8',
        )
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        # p-0000000002 exists from setUp; point it at the record written
        # above, and add a second person sharing the ambiguous alias.
        conn.execute(
            "UPDATE persons SET path='people/p-0000000002.md' WHERE id='p-0000000002'")
        _add_person(conn, 'p-0000000003', 'Other Jones', tier='stub',
                    path='people/p-0000000003.md', surname='Jones')
        for pid in ('p-0000000002', 'p-0000000003'):
            conn.execute(
                'INSERT INTO aliases(alias, canonical_id, kind) VALUES (?,?,?)',
                ('marion jones', pid, 'variant'),
            )
        conn.commit()
        conn.close()

        _freshen_index(self.root)
        r = wikitree.run_wikitree(self.root, 'p-0000000001')

        self.assertEqual(r['status'], 'restricted-names')
        msg = r['messages'][0]
        self.assertIn('P-0000000002', msg)
        self.assertIn('P-0000000003', msg)   # the person the old break dropped


class WikitreeDraftExclusionTests(unittest.TestCase):
    """Unaccepted `<!-- AI-DRAFT ... -->` prose is not-yet-content (AGENTS.md:
    it stays inside its markers until `fha confirm draft` accepts it): the
    export silently excludes it - no draft text, no marker, no ref for a
    citation that lives only inside a draft, and no privacy refusal triggered
    by draft-only material."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people').mkdir()
        (self.root / 'sources').mkdir()
        (self.root / 'sources' / 'birth.md').write_text(
            '---\nid: S-0000000001\ntitle: Birth cert\nsource_type: vital-record\n'
            'citation: "Birth certificate, 1875."\n---\n',
            encoding='utf-8',
        )
        (self.root / 'sources' / 'marr.md').write_text(
            '---\nid: S-0000000002\ntitle: Marriage record\nsource_type: vital-record\n'
            'citation: "Marriage record, 1900."\n---\n',
            encoding='utf-8',
        )
        conn = _make_index(self.root)
        _add_person(conn, 'p-0000000001', 'John Smith', path='people/subject.md',
                    surname='Smith')
        _add_source(conn, 's-0000000001', 'Birth cert', 'sources/birth.md')
        _add_source(conn, 's-0000000002', 'Marriage record', 'sources/marr.md')
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_profile(self, body):
        (self.root / 'people' / 'subject.md').write_text(
            '---\nid: P-0000000001\nname: John Smith\ntier: curated\nliving: false\n---\n\n'
            '# John Smith\n\n' + body,
            encoding='utf-8',
        )
        _freshen_index(self.root)

    def test_draft_excluded_accepted_and_human_kept(self):
        self._write_profile(
            '## Biography\n'
            'He was born in 1875 [S-0000000001].\n\n'
            '<!-- AI-ACCEPTED 2026-06-01 claude-x - v1 (accepted 2026-06-20) -->\n\n'
            'A drafted marriage paragraph [S-0000000002].\n\n'
            '<!-- AI-DRAFT 2026-07-01 claude-x - v2 -->\n')
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'ok')
        text = r['text']
        self.assertIn('He was born in 1875', text)
        self.assertIn('<ref name="S-0000000001"/>', text)
        self.assertNotIn('drafted marriage', text)
        self.assertNotIn('S-0000000002', text)    # draft-only citation: no use, no definition
        self.assertNotIn('AI-DRAFT', text)
        self.assertNotIn('AI-ACCEPTED', text)

    def test_draft_citing_restricted_source_does_not_refuse(self):
        # The restricted-source gate fails closed on CONTENT; a draft is not
        # yet content, so a restricted citation living only inside the draft
        # must neither refuse the export nor leak into it.
        (self.root / 'sources' / 'private.md').write_text(
            '---\nid: S-0000000003\ntitle: Private\nrestricted: true\n---\n',
            encoding='utf-8',
        )
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        _add_source(conn, 's-0000000003', 'Private', 'sources/private.md', restricted=1)
        conn.commit()
        conn.close()
        self._write_profile(
            '## Biography\n'
            'He was born in 1875 [S-0000000001].\n\n'
            '<!-- AI-ACCEPTED 2026-06-01 claude-x - v1 (accepted 2026-06-20) -->\n\n'
            'A drafted private fact [S-0000000003].\n\n'
            '<!-- AI-DRAFT 2026-07-01 claude-x - v2 -->\n')
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'ok')
        self.assertNotIn('S-0000000003', r['text'])

    def test_all_draft_biography_no_stray_heading(self):
        self._write_profile(
            '## Biography\n'
            'Entirely drafted paragraph [S-0000000001].\n\n'
            '<!-- AI-DRAFT 2026-07-01 claude-x - v1 -->\n\n'
            '## Stories\n'
            'A human-written tale.\n')
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'ok')
        text = r['text']
        self.assertNotIn('Entirely drafted', text)
        self.assertNotIn('== Biography ==', text)   # emptied section: heading dropped
        self.assertIn('== Stories ==', text)
        self.assertIn('A human-written tale.', text)

    def test_damaged_draft_marker_refuses_export(self):
        # X1 fail-closed (round-2 finding 18): a marker missing its `-->`
        # used to publish the whole draft plus the dangling marker into the
        # export - and `fha confirm draft` cannot flip a broken marker, so
        # the state was sticky. The export now refuses, naming the file and
        # the fix, exit/refusal family same as the privacy scans.
        self._write_profile(
            '## Biography\n'
            'He was born in 1875 [S-0000000001].\n\n'
            'A drafted paragraph.\n\n'
            '<!-- AI-DRAFT 2026-07-01 claude-x - v2 missing its arrow\n')
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'broken-draft-marker')
        self.assertIsNone(r['text'])
        self.assertEqual(r.exit_code, wikitree.EXIT_FAILURE)
        msg = r['messages'][0]
        self.assertIn('people/subject.md', msg)      # names the file
        self.assertIn('-->', msg)                    # names the fix
        self.assertNotIn('Traceback', msg)

    def test_wrap_style_marker_refuses_not_leaks(self):
        # Wrap-style authoring (marker above + /AI-DRAFT below) used to cut
        # the HUMAN text above and export the draft below it. Fail closed.
        self._write_profile(
            '## Biography\n'
            'Human paragraph above.\n\n'
            '<!-- AI-DRAFT 2026-07-01 claude-x - wrap -->\n'
            'A wrapped draft paragraph [S-0000000001].\n'
            '<!-- /AI-DRAFT -->\n')
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'broken-draft-marker')
        self.assertIsNone(r['text'])


if __name__ == '__main__':
    unittest.main()
