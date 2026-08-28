import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib
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
    test_privacy_restricted's _Archive.fresh()).

    #48: also (re)writes `.cache/index_manifest.json` to match whatever real
    files exist right now. This index is hand-built via raw DDL, bypassing
    build_index/upsert_source - the only two places that write the #48 path
    manifest - so without this, open_index_db's additive manifest check
    finds no manifest at all and (correctly, per the bootstrapping rule)
    reads every real record file this fixture wrote as newly "added", i.e.
    stale, regardless of the mtime stamp above.
    """
    future = time.time() + 5
    os.utime(archive_root / '.cache' / 'index.sqlite', (future, future))
    _lib.write_path_manifest(
        _lib.index_manifest_path(archive_root), _lib.record_path_manifest(archive_root))


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
               source_id='s-0000000001', status='accepted', value='x', negated=0,
               roles=None):
    """Seed one claim. `roles` is {person_id: role} - which of the people the
    claim names plays which part on the record (SPEC §8.3)."""
    mn = ''
    if date_edtf:
        from _lib import edtf_bounds
        mn = edtf_bounds(date_edtf)[0]
    conn.execute(
        'INSERT INTO claims(id, source_id, type, date_edtf, date_min, place_text, '
        'value, status, negated) VALUES (?,?,?,?,?,?,?,?,?)',
        (cid, source_id, ctype, date_edtf, mn, place_text, value, status, negated),
    )
    for pos, p in enumerate(persons):
        conn.execute(
            'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
            (cid, p, pos, (roles or {}).get(p)),
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
        _freshen_index(self.root)

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

    def test_undecodable_cited_source_falls_back_to_title_not_crash(self):
        # `_source_reference`'s read is wrapped in a bare `except Exception`,
        # which already catches `UnicodeDecodeError` (a ValueError) - unlike
        # the subject-record site, this one never needed `on_decode_error` to
        # avoid crashing. Unit-level: going through `run_wikitree` instead
        # would only prove `_restricted_source_refs`' EARLIER fail-closed scan
        # (also already decode-safe) refuses the export first, never reaching
        # this fallback. Pin the fallback directly: a source saved in another
        # codepage degrades to its indexed title as the citation text, the
        # same graceful fallback an unreadable/missing source already gets.
        (self.root / 'sources' / 'birth.md').write_bytes(
            ('---\nid: S-0000000001\ntitle: Birth cert\nsource_type: vital-record\n'
             'citation: "Akt urodzenia, Kraków."\n---\n').encode('cp1252')
        )
        source_row = {'id': 'S-0000000001', 'title': 'Birth cert', 'path': 'sources/birth.md'}
        citation = wikitree._source_reference(self.root, source_row)
        self.assertEqual(citation, 'Birth cert')

    def _reopen(self):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        return conn

    def test_needs_review_excluded_from_spacetime_recast_as_research_note(self):
        # Owner decision 2026-07-22: a parked needs-review claim never stamps
        # a fact (no spacetime span), but goes out as an open question in
        # Research Notes so a collaborator can pick it up.
        conn = self._reopen()
        _add_claim(conn, 'c-0000000003', 'residence', ['p-0000000001'],
                   date_edtf='1910', place_text='Topeka', source_id='s-0000000002',
                   status='needs-review', value='Possibly moved to Topeka')
        conn.commit(); conn.close()
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        text = r['text']
        # The marriage source now carries TWO dated+placed claims, so its
        # spacetime entry disappears either way; the parked claim must not
        # resurrect it, and no span may carry the 1910 date.
        self.assertNotIn('data-date="1910-01-01"', text)
        self.assertIn('== Research Notes ==', text)
        self.assertIn('Unconfirmed: Possibly moved to Topeka (1910)', text)
        self.assertIn('noted in "Marriage record"', text)
        # Research Notes precedes the Sources render anchor.
        self.assertLess(text.index('== Research Notes =='), text.index('<references/>'))

    def test_research_notes_precede_an_authored_sources_section(self):
        # A profile that authors its own '## Sources' section exports it as
        # '== Sources ==' with <references/> appended inside it - the minted
        # Research Notes section must land BEFORE that heading, or the
        # footnote block renders detached under Research Notes.
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8')
            + '\n## Sources\nSee also the county records office card file.\n',
            encoding='utf-8',
        )
        _freshen_index(self.root)
        conn = self._reopen()
        _add_claim(conn, 'c-0000000005', 'residence', ['p-0000000001'],
                   source_id='s-0000000002', status='needs-review',
                   value='Possibly moved to Topeka')
        conn.commit(); conn.close()
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        text = r['text']
        self.assertIn('== Research Notes ==', text)
        self.assertLess(text.index('== Research Notes =='), text.index('== Sources =='))
        self.assertLess(text.index('== Sources =='), text.index('<references/>'))
        self.assertIn('county records office', text)   # authored section survives

    def test_research_note_naming_a_restricted_person_is_withheld(self):
        # A deceased person whose record carries `restricted: by-request` is a
        # no-override exclusion from public output (SPEC §19) - a parked claim
        # co-naming them must not export their wording, even though the
        # living-person guard passes (living: false) and the index has no
        # person-level restricted column (the record file is the truth).
        (self.root / 'people' / 'edith.md').write_text(
            '---\nid: P-0000000003\nname: Edith Kowalski\ntier: stub\n'
            'living: false\nrestricted: by-request\n---\n',
            encoding='utf-8',
        )
        _freshen_index(self.root)
        conn = self._reopen()
        _add_person(conn, 'p-0000000003', 'Edith Kowalski', tier='stub',
                    path='people/edith.md')
        _add_claim(conn, 'c-0000000006', 'marriage',
                   ['p-0000000001', 'p-0000000003'],
                   source_id='s-0000000002', status='needs-review',
                   value='Possibly married Edith Kowalski in Chicago')
        conn.commit(); conn.close()
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertNotIn('Edith Kowalski', r['text'])
        self.assertNotIn('== Research Notes ==', r['text'])

    def test_research_note_naming_a_participant_with_malformed_record_is_withheld(self):
        # P1 fail-closed: a claim participant whose record becomes MALFORMED
        # after indexing (read_record returns parse_errors + empty meta, it does
        # not raise) must be treated as restricted, not read as public because
        # the empty meta lacks a `restricted:` marker. The parked claim naming
        # them must NOT publish - otherwise a now-unreadable (possibly living or
        # restricted) person's details leak into the Research Notes block.
        (self.root / 'people' / 'edith.md').write_text(
            '---\nid: P-0000000003\nname: [broken\n---\n',   # unterminated flow seq
            encoding='utf-8',
        )
        _freshen_index(self.root)
        conn = self._reopen()
        _add_person(conn, 'p-0000000003', 'Edith Kowalski', tier='stub',
                    path='people/edith.md')
        _add_claim(conn, 'c-0000000006', 'marriage',
                   ['p-0000000001', 'p-0000000003'],
                   source_id='s-0000000002', status='needs-review',
                   value='Possibly married Edith Kowalski in Chicago')
        conn.commit(); conn.close()
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        # Export still succeeds for the subject - only the tainted bonus item is
        # dropped - but the participant's claim wording never reaches the wiki.
        self.assertEqual(r['status'], 'ok')
        self.assertNotIn('Edith Kowalski', r['text'])
        self.assertNotIn('Possibly married', r['text'])
        self.assertNotIn('== Research Notes ==', r['text'])

    def test_malformed_subject_record_refused(self):
        # Fail-closed on the SUBJECT: if their own record is malformed, its
        # `restricted:`/marker fields may be unreadable, so publishing from the
        # partial parse could leak a record meant to stay private. Refuse.
        (self.root / 'people' / 'subject.md').write_text(
            '---\nid: P-0000000001\nname: [broken\n---\n',
            encoding='utf-8',
        )
        _freshen_index(self.root)
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'unreadable-subject')
        self.assertIsNone(r['text'])

    def test_undecodable_subject_record_refused_not_crashed(self):
        # #68's residual gap: `read_record` raises `UnicodeDecodeError` unless
        # a caller supplies `on_decode_error`, and _wikitree_payload's subject
        # read (the export's own entry point) called it bare. A person record
        # saved in another codepage - cp1252 is a Windows editor's default,
        # and the accented names this archive is full of are exactly the
        # bytes that differ from UTF-8 - crashed the export outright instead
        # of refusing like the malformed-record case just above. Same
        # fail-closed posture, same 'unreadable-subject' status: the subject's
        # own restriction marker cannot be confirmed, so nothing publishes.
        (self.root / 'people' / 'subject.md').write_bytes(
            ('---\nid: P-0000000001\nname: John Smith\ntier: curated\n'
             'living: false\n---\n\n# John Smith\n\nBorn in Kraków.\n').encode('cp1252')
        )
        _freshen_index(self.root)
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'unreadable-subject')
        self.assertIsNone(r['text'])
        # The message must name the real cause (encoding), not a generic
        # parse error, and must not surface a raw traceback to the human.
        self.assertTrue(any('UTF-8' in m for m in r['messages']), r['messages'])

    def test_undecodable_subject_record_bytes_are_never_touched(self):
        path = self.root / 'people' / 'subject.md'
        path.write_bytes(
            ('---\nid: P-0000000001\nname: John Smith\ntier: curated\n'
             'living: false\n---\n\nBorn in Kraków.\n').encode('cp1252')
        )
        before = path.read_bytes()
        _freshen_index(self.root)
        wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(before, path.read_bytes(),
                         "the file is the human's and is not damaged - never touch it")

    def test_undecodable_cited_source_refusal_names_the_encoding(self):
        # The refusal was always right (fail closed) - the SENTENCE was not.
        # A cited source saved in another codepage carries no `restricted:`
        # marker at all, so "remove or rewrite citations to restricted/DNA
        # sources" named a cleanup the human cannot perform: they open the
        # record, find nothing restricted, and the export refuses forever.
        # Same refusal, cause named, and the re-save is the fix.
        (self.root / 'sources' / 'birth.md').write_bytes(
            ('---\nid: S-0000000001\ntitle: Birth cert\nsource_type: vital-record\n'
             'citation: "Akt urodzenia, Kraków."\n---\n').encode('cp1252')
        )
        _freshen_index(self.root)
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'restricted-sources')
        self.assertIsNone(r['text'])          # still fails closed
        joined = ' '.join(r['messages'])
        self.assertIn('not saved as UTF-8 text', joined)
        self.assertIn('sources/birth.md', joined)
        self.assertNotIn('remove or rewrite citations', joined)

    def test_undecodable_linked_person_refusal_names_the_encoding(self):
        # The person half of the same defect: a `[[P-id]]` link to a record
        # that will not decode was refused as "restricted people".
        (self.root / 'people' / 'p-0000000002.md').write_bytes(
            ('---\nid: P-0000000002\nname: Mary Jones\nliving: false\n---\n\n'
             'Born in Kraków.\n').encode('cp1252')
        )
        _freshen_index(self.root)
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'restricted-people')
        self.assertIsNone(r['text'])          # still fails closed
        joined = ' '.join(r['messages'])
        self.assertIn('not saved as UTF-8 text', joined)
        self.assertIn('people/p-0000000002.md', joined)
        self.assertNotIn('remove or rewrite links', joined)

    def test_restricted_and_undecodable_sources_each_get_their_own_sentence(self):
        # Both causes at once: one cited source really is restricted, the
        # other only will not decode. Neither sentence may swallow the other -
        # the human has two different jobs to do.
        (self.root / 'sources' / 'birth.md').write_bytes(
            ('---\nid: S-0000000001\ntitle: Birth cert\n'
             'source_type: vital-record\ncitation: "Kraków."\n---\n').encode('cp1252')
        )
        conn = self._reopen()
        conn.execute("UPDATE sources SET restricted = 1 WHERE id = 's-0000000002'")
        conn.commit()
        conn.close()
        _freshen_index(self.root)
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'restricted-sources')
        self.assertIsNone(r['text'])
        joined = ' '.join(r['messages'])
        self.assertIn('remove or rewrite citations', joined)   # the restricted one
        self.assertIn('S-0000000002', joined)
        self.assertIn('not saved as UTF-8 text', joined)       # the undecodable one
        self.assertIn('sources/birth.md', joined)
        # ...and the restricted source is not blamed for the encoding, nor the
        # undecodable one for a restriction it does not carry.
        removal, encoding = sorted(r['messages'], key=lambda m: 'UTF-8' in m)
        self.assertNotIn('S-0000000001', removal)
        self.assertNotIn('S-0000000002', encoding)

    def test_undecodable_source_is_still_flagged_when_the_decode_is_reported(self):
        # The trap `_lib.read_record`'s docstring names, pinned where it would
        # bite: with `on_decode_error` supplied the read no longer RAISES, so
        # the scan's `except Exception` arm never fires and the record comes
        # back EMPTY - which reads as "no restricted: marker, publishable".
        # The scan must flag it from `undecodable` itself, or wiring the
        # channel in for a better message would have opened a leak.
        (self.root / 'sources' / 'birth.md').write_bytes(
            ('---\nid: S-0000000001\ntitle: Birth cert\n'
             'source_type: vital-record\ncitation: "Kraków."\n---\n').encode('cp1252')
        )
        conn = self._reopen()
        recorded: list = []
        flagged = wikitree._restricted_source_refs(
            conn, self.root, 'He was born in 1875 [S-0000000001].',
            on_decode_error=_lib.undecodable_file_recorder(recorded))
        conn.close()
        self.assertEqual([r['id'] for r in flagged], ['s-0000000001'])
        self.assertEqual(recorded, [self.root / 'sources' / 'birth.md'])

    def test_restricted_source_research_note_withheld(self):
        conn = self._reopen()
        _add_source(conn, 's-0000000003', 'Sealed record', 'sources/sealed.md', restricted=1)
        _add_claim(conn, 'c-0000000004', 'residence', ['p-0000000001'],
                   source_id='s-0000000003', status='needs-review',
                   value='Secret whereabouts')
        conn.commit(); conn.close()
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertNotIn('Secret whereabouts', r['text'])
        self.assertNotIn('== Research Notes ==', r['text'])   # nothing eligible → no section

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

    def test_negated_claim_excluded_from_spacetime_span(self):
        # A confirmed ABSENCE - "not in Topeka in 1880" - is the only
        # dated+placed claim on its source, so before the fix _spacetime_index
        # would stamp data-loc/data-date onto a sentence citing it, machine-
        # asserting the very presence the claim denies. It must be excluded.
        src3 = self.root / 'sources' / 'residence.md'
        src3.write_text(
            '---\nid: S-0000000003\ntitle: State census\nsource_type: vital-record\n'
            'citation: "1880 state census, John absent from Topeka."\n---\n',
            encoding='utf-8',
        )
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8').replace(
                'He was born in 1875 [S-0000000001].',
                'He was born in 1875 [S-0000000001].\n'
                'He was recorded away from Topeka in 1880 [S-0000000003].'),
            encoding='utf-8',
        )
        _freshen_index(self.root)
        conn = self._reopen()
        _add_source(conn, 's-0000000003', 'State census', 'sources/residence.md')
        _add_claim(conn, 'c-0000000009', 'residence', ['p-0000000001'],
                   date_edtf='1880', place_text='Topeka', source_id='s-0000000003',
                   status='accepted', negated=1, value='not resident in Topeka')
        conn.commit(); conn.close()
        text = wikitree.run_wikitree(self.root, 'p-0000000001')['text']
        self.assertNotIn('data-loc="Topeka"', text)
        self.assertNotIn('data-date="1880-01-01"', text)

    def test_negated_claim_excluded_from_infobox_template(self):
        # Sibling of the spacetime fix: an infobox template ({{Residence|...}})
        # is a structured machine-fact, so a negated claim must not emit the
        # positive field it denies. A positive companion proves the template
        # DOES render when the claim is not negated.
        conn = self._reopen()
        _add_claim(conn, 'c-0000000011', 'residence', ['p-0000000001'],
                   place_text='Boston', source_id='s-0000000001',
                   status='accepted', negated=0, value='resident')
        _add_claim(conn, 'c-0000000012', 'residence', ['p-0000000001'],
                   place_text='Topeka', source_id='s-0000000002',
                   status='accepted', negated=1, value='not resident in Topeka')
        conn.commit()
        templates = {'residence': {'template': 'Residence', 'fields': {'location': 'place'}}}
        out = wikitree._render_templates(conn, self.root, 'p-0000000001', templates)
        conn.close()
        self.assertIn('{{Residence|location=Boston}}', out)
        self.assertNotIn('{{Residence|location=Topeka}}', out)

    def test_a_vital_of_a_relative_emits_no_infobox_for_this_person(self):
        # #126: an infobox {{Birth|place=…}} is a structured machine-fact about
        # the subject. A birth certificate names the baby AND both parents, so
        # rendering one for every accepted birth claim NAMING the person put
        # the son's birthplace in his mother's infobox. `roles:` says which of
        # them the record is OF (SPEC §8.3).
        conn = self._reopen()
        _add_person(conn, 'p-0000000004', 'Peter Smith', tier='stub', surname='Smith')
        _add_claim(conn, 'c-0000000013', 'birth', ['p-0000000004', 'p-0000000001'],
                   place_text='Riverton', source_id='s-0000000001',
                   status='accepted', value='born at Riverton',
                   roles={'p-0000000004': 'child', 'p-0000000001': 'parent'})
        conn.commit()
        templates = {'birth': {'template': 'Birth', 'fields': {'place': 'place'}}}
        parent = wikitree._render_templates(conn, self.root, 'p-0000000001', templates)
        child = wikitree._render_templates(conn, self.root, 'p-0000000004', templates)
        conn.close()
        self.assertNotIn('{{Birth|place=Riverton}}', parent,
                         "a mother named as `parent` on her son's birth record "
                         'must not get his birthplace as her own infobox field')
        self.assertIn('{{Birth|place=Riverton}}', child)

    def test_a_legacy_vital_naming_two_people_with_no_roles_emits_no_infobox(self):
        # Two people, no roles: map at all - the claim has not said which of
        # them was born. Emitting the infobox for BOTH (or, as here, for the
        # wrong one - the parent, not the child) is exactly this class's own
        # bug reached through the unroled case instead of the miscast one:
        # the old back-compatibility bargain used to guess "everyone" for a
        # claim with zero role signal at all, restating #126 rather than
        # fixing it (#126, reopened).
        conn = self._reopen()
        _add_person(conn, 'p-0000000004', 'Peter Smith', tier='stub', surname='Smith')
        _add_claim(conn, 'c-0000000014', 'birth', ['p-0000000004', 'p-0000000001'],
                   place_text='Riverton', source_id='s-0000000001',
                   status='accepted', value='born at Riverton')
        conn.commit()
        templates = {'birth': {'template': 'Birth', 'fields': {'place': 'place'}}}
        parent = wikitree._render_templates(conn, self.root, 'p-0000000001', templates)
        child = wikitree._render_templates(conn, self.root, 'p-0000000004', templates)
        conn.close()
        self.assertNotIn('{{Birth|place=Riverton}}', parent)
        self.assertNotIn('{{Birth|place=Riverton}}', child)

    def test_a_legacy_vital_naming_only_the_subject_still_emits_its_infobox(self):
        # One person named, no roles: map - nobody to be ambiguous about, so
        # the pre-#126 "a claim that never said keeps rendering as it did"
        # back-compatibility bargain is still exactly right here.
        conn = self._reopen()
        _add_claim(conn, 'c-0000000014', 'birth', ['p-0000000001'],
                   place_text='Riverton', source_id='s-0000000001',
                   status='accepted', value='born at Riverton')
        conn.commit()
        templates = {'birth': {'template': 'Birth', 'fields': {'place': 'place'}}}
        out = wikitree._render_templates(conn, self.root, 'p-0000000001', templates)
        conn.close()
        self.assertIn('{{Birth|place=Riverton}}', out)

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
        _freshen_index(self.root)

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

    def test_restricted_display_on_name_target_wikilink_refused(self):
        # The gap the ID-token test above doesn't cover: a NAME-target wikilink
        # whose display half is the deadname, [[Mary Jones|Marion Jones]]. The
        # target "Mary Jones" is a public name, so it passes the target scan -
        # but the display "Marion Jones" is a restricted variant and would
        # publish verbatim. Both halves must be checked.
        marian = self.root / 'people' / 'p-0000000002.md'
        marian.write_text(
            '---\nid: P-0000000002\nname: Mary Jones\nliving: false\n'
            'name_variants:\n  - value: Marion Jones\n    restricted: true\n---\n',
            encoding='utf-8',
        )
        profile = self.root / 'people' / 'subject.md'
        profile.write_text(
            profile.read_text(encoding='utf-8')
            + '\nFormerly [[Mary Jones|Marion Jones]].\n',
            encoding='utf-8',
        )
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.execute("UPDATE persons SET path='people/p-0000000002.md' WHERE id='p-0000000002'")
        conn.execute("INSERT INTO aliases(alias, canonical_id, kind) VALUES (?,?,?)",
                     ('mary jones', 'p-0000000002', 'name'))
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
        _freshen_index(self.root)

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
        _freshen_index(self.root)

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


class WikitreeUnfilledSectionTests(unittest.TestCase):
    """#125 on the public-publication path. A §16 section nobody has written
    yet still holds the record template's own authoring instructions, and
    `fha wikitree` used to convert them straight into the exported markup -
    so a public profile read "Write their story in plain sentences..." under
    `== Biography ==`, as if that were what the family had to say about this
    person. Worse than the site symptom the fix started from: a wiki page is
    published outward and edited by strangers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people').mkdir()
        conn = _make_index(self.root)
        _add_person(conn, 'p-0000000001', 'John Smith', path='people/subject.md',
                    surname='Smith')
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_profile(self, body):
        (self.root / 'people' / 'subject.md').write_text(
            '---\nid: P-0000000001\nname: John Smith\ntier: curated\nliving: false\n---\n\n'
            + body,
            encoding='utf-8',
        )
        _freshen_index(self.root)

    def test_freshly_scaffolded_person_exports_no_placeholder_text(self):
        # Built from the SAME renderer `fha person new`/`fha stubs` call, so
        # this cannot drift from what a brand-new record actually holds.
        self._write_profile(_lib.render_person_body_scaffold('John Smith'))
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'ok')
        text = r['text']
        self.assertNotIn('Write their story in plain sentences', text)
        self.assertNotIn('Open questions, hunches, and brick walls', text)
        self.assertNotIn("aren't blood relatives", text)
        self.assertNotIn('== Biography ==', text)
        self.assertNotIn('== Research Notes ==', text)
        self.assertNotIn('== Friends & Family ==', text)
        # `*(none yet)*` was already dropped line-by-line, which left a bare
        # `== Stories ==` heading standing over nothing - scaffolding too.
        self.assertNotIn('== Stories ==', text)

    def test_research_notes_placeholder_private_example_never_published(self):
        # The Research Notes placeholder embeds a `<!-- private -->` example
        # block whose TEXT sits between the markers, not inside a comment -
        # so it published verbatim. Dropping the whole unwritten section is
        # what keeps it off the wiki.
        self._write_profile(_lib.render_person_body_scaffold('John Smith'))
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'ok')
        self.assertNotIn("A hunch you're not ready to publish", r['text'])
        self.assertNotIn('possible tie to a living relative', r['text'])

    def test_written_sections_still_export(self):
        # The other half of the rule: a section a human actually wrote goes
        # out untouched, even when it reuses a few of the scaffold's words.
        self._write_profile(
            '# John Smith\n\n'
            '## Biography\n'
            'Write their story? He already lived one: born in 1875 in Boston.\n\n'
            '## Stories\n*(none yet)*\n\n'
            '## Research Notes\n'
            'Open questions remain about his first wife.\n')
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'ok')
        text = r['text']
        self.assertIn('== Biography ==', text)
        self.assertIn('born in 1875 in Boston', text)
        self.assertIn('== Research Notes ==', text)
        self.assertIn('Open questions remain about his first wife', text)
        self.assertNotIn('== Stories ==', text)


if __name__ == '__main__':
    unittest.main()
