"""
test_site.py - fha site (M8.1 source page + M8.2 person page).

Builds a synthetic .cache/index.sqlite (and, where needed, .cache/photos.sqlite)
directly from index.py's / photoindex.py's DDL - the same pattern as
tests/test_packet.py - so the publication generator can be exercised without a
full archive fixture, exiftool, or a network. The prose/citation that `fha site`
reads from the record .md files is written to disk alongside the index rows.

`site.py`'s module stem collides with Python's stdlib `site`, so it is loaded by
path under the private name `fha_site` (the same trick fha.py uses).
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from index import _DDL as INDEX_DDL
from photoindex import _DDL as PHOTOS_DDL
from _lib import (
    index_manifest_path, path_to_alias, photoindex_manifest_path,
    photoindex_record_manifest, record_path_manifest, render_person_body_scaffold,
    write_path_manifest,
)

_spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site'] = site
_spec.loader.exec_module(site)


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


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        cache = self.archive_root / '.cache'
        cache.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(cache / 'index.sqlite'))
        self.conn.executescript(INDEX_DDL)
        self.conn.row_factory = sqlite3.Row
        self.out_dir = self.archive_root / '.cache' / 'site'

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    # - seeding -

    def _seed_person(self, pid, name='Test Person', *, living='false', tier='curated',
                     surname='Person', body='# Test Person\n', frontmatter_extra='', sex='M'):
        rel = f'people/{surname.lower()}__test_{pid}.md'
        path = self.archive_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        extra = f'{frontmatter_extra}\n' if frontmatter_extra else ''
        # newline='' - write the string's bytes exactly as given. Without it,
        # Python's own universal-newline translation on Windows would
        # re-translate a body that ALREADY contains literal '\r\n' (as the
        # Windows-editor placeholder test below constructs) into '\r\r\n' -
        # a corrupted sequence no real editor ever writes, and not what
        # these fixtures mean to test.
        path.write_text(f'---\nid: {pid}\nname: {name}\n{extra}---\n{body}',
                         encoding='utf-8', newline='')
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, name, surname, sex, living, tier, 'active', rel),
        )

    def _seed_source(self, sid, title='A Source', *, source_type='census', restricted=0,
                     publication_ok=None, citation='A citation.', people=(), frontmatter=None):
        rel = f'sources/{source_type}/src_{sid}.md'
        path = self.archive_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if frontmatter is None:
            frontmatter = f'---\nid: {sid}\ntitle: {title}\nsource_type: {source_type}\ncitation: "{citation}"\n---\n\n## Claims\n'
        path.write_text(frontmatter, encoding='utf-8')
        self.conn.execute(
            'INSERT INTO sources(id, title, source_type, restricted, publication_ok, status, path) '
            'VALUES (?,?,?,?,?,?,?)',
            (sid, title, source_type, restricted, publication_ok, 'active', rel),
        )
        for pid in people:
            self.conn.execute(
                'INSERT INTO source_people(source_id, person_id) VALUES (?,?)', (sid, pid))

    def _seed_claim(self, cid, sid, ctype, value, *, status='accepted', date_edtf=None,
                    place_text=None, persons=(), confidence=None, reviewed=None, negated=0,
                    roles=None, date_min=None, subtype=None):
        """Seed one claim. `roles` is {person_id: role}, the `roles:` map as
        `fha index` stores it - which of the people a claim names plays which
        part (SPEC §8.3). Omit it for the legacy/unroled claim.

        date_min defaults to the naive January-1 widening of date_edtf (matches
        what real claims carry for a plain year), but a test can override it to
        construct the #128 shape - an uncertain/ranged date_edtf whose widened
        date_min lands in a different decade than the display date reads as.

        `subtype` is the relationship claim's nature (SPEC §8.2/§12.2 -
        `adoptive`/`step`/`foster`/`guardian`/... for a non-genetic parent-
        child edge, `biological` or unset for a genetic one)."""
        if date_min is None:
            date_min = (date_edtf or '')[:4] + '-01-01' if date_edtf else None
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, subtype, value, status, date_edtf, date_min, '
            'place_text, confidence, reviewed, negated) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (cid, sid, ctype, subtype, value, status, date_edtf, date_min, place_text,
             confidence, reviewed, negated),
        )
        for pos, pid in enumerate(persons):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
                (cid, pid, pos, (roles or {}).get(pid)))

    def _seed_rel(self, pid, rel, other, *, claim_id='c-rrrrrrrrrr'):
        self.conn.execute(
            'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
            (pid, rel, other, claim_id))

    def _run(self, *, linked=False, dry_run=False, workbench=False):
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'index.sqlite', (future, future))
        # #48: this synthetic index.sqlite is hand-built via raw DDL/INSERTs,
        # bypassing build_index/upsert_source - the only two places that
        # write the #48 path manifest - so without this, open_index_db's
        # additive manifest check finds no manifest at all and (correctly,
        # per the bootstrapping rule) reads every real file _seed_person/
        # _seed_source already wrote as newly "added", i.e. stale. Called
        # here, right before every call to run_site, so the manifest always
        # matches whatever real files that test actually created.
        write_path_manifest(
            index_manifest_path(self.archive_root), record_path_manifest(self.archive_root))
        return site.run_site(self.archive_root, self.out_dir, linked=linked, dry_run=dry_run,
                             workbench=workbench)

    def _read(self, relpath):
        return (self.out_dir / relpath).read_text(encoding='utf-8')


class SourcePageTests(_Base):
    def test_source_page_has_citation_claims_and_status(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', '1880 Census', citation='1880 U.S. Census, Kansas.',
                          people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence', 'Lived in Kansas',
                         status='accepted', date_edtf='1880', place_text='Kansas',
                         persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-2222222222', 's-1111111111', 'occupation', 'Bookkeeper',
                         status='suggested', persons=('p-aaaaaaaaaa',))
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')
        html = self._read('sources/s-1111111111.html')
        self.assertIn('1880 U.S. Census, Kansas.', html)         # citation from .md
        self.assertIn('Lived in Kansas', html)
        self.assertIn('status-accepted', html)
        self.assertIn('status-suggested', html)                  # all statuses shown w/ badge
        self.assertIn('../persons/p-aaaaaaaaaa.html', html)      # person link in People column

    def test_standalone_shows_accepted_only_linked_shows_everything(self):
        # Owner decision 2026-07-22: the public snapshot publishes settled
        # facts only - accepted claims. needs-review (the parked verdict,
        # SPEC §8.1) is research state, withheld along with suggested and
        # rejected. --linked (developer preview / workbench) shows every
        # status with its badge.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', 'Mixed Source', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence', 'Accepted fact',
                         status='accepted', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-2222222222', 's-1111111111', 'occupation', 'Under review',
                         status='needs-review', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-3333333333', 's-1111111111', 'occupation', 'AI draft guess',
                         status='suggested', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-4444444444', 's-1111111111', 'occupation', 'Known wrong',
                         status='rejected', persons=('p-aaaaaaaaaa',))
        self._run(linked=False)
        public = self._read('sources/s-1111111111.html')
        self.assertIn('Accepted fact', public)
        self.assertNotIn('Under review', public)           # needs-review withheld (parked, not fact)
        self.assertNotIn('AI draft guess', public)         # suggested withheld
        self.assertNotIn('Known wrong', public)            # rejected withheld
        self._run(linked=True)
        dev = self._read('sources/s-1111111111.html')
        self.assertIn('Under review', dev)                 # linked shows everything
        self.assertIn('AI draft guess', dev)
        self.assertIn('Known wrong', dev)

    def test_accepted_low_confidence_claim_flagged_on_source_page(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', 'Thin Source', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'occupation', 'Maybe a miller',
                         status='accepted', confidence='low', persons=('p-aaaaaaaaaa',))
        self._run(linked=False)
        public = self._read('sources/s-1111111111.html')
        self.assertIn('Maybe a miller', public)             # accepted-low still publishes
        self.assertIn('flag-low', public)                   # ...flagged, not silent
        self.assertIn('low confidence', public)

    def test_workbench_marks_needs_review_rows(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', 'Mixed Source', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-2222222222', 's-1111111111', 'occupation', 'Under review',
                         status='needs-review', reviewed='2026-03-01',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=True, workbench=True)
        wb = self._read('sources/s-1111111111.html')
        self.assertIn('wb-needs-review', wb)                # tinted row
        self.assertIn('status-needs-review', wb)            # status word badge

    def test_missing_asset_listed_not_linked(self):
        self._seed_source('s-1111111111', 'Has Asset')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'documents/census/ghost.txt', 'page-1'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('file not available', html)

    def test_file_entry_note_adds_copy_letter_when_present(self):
        # #123: one continuous document with several same-role entries (a
        # household ledger, say) reads as indistinguishable when every
        # entry's note is the bare 'role: entry'. Once the indexer stops
        # dropping `copy:` on the floor (the source_files.copy fix, same
        # issue), the site's file-list note names the variant too - 'role:
        # entry · copy: b' - so same-role entries of one item are told apart
        # without needing per-file dates.
        self._seed_source('s-1111111111', 'Household Ledger', source_type='other')
        ledger_dir = self.archive_root / 'documents' / 'ledger'
        ledger_dir.mkdir(parents=True, exist_ok=True)
        (ledger_dir / 'entry-a.pdf').write_bytes(b'not a real pdf')
        (ledger_dir / 'entry-b.pdf').write_bytes(b'not a real pdf')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role, copy) VALUES (?,?,?,?)',
            ('s-1111111111', 'documents/ledger/entry-a.pdf', 'entry', None))
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role, copy) VALUES (?,?,?,?)',
            ('s-1111111111', 'documents/ledger/entry-b.pdf', 'entry', 'b'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('role: entry · copy: b', html)
        # The un-lettered sibling keeps the original, shorter note - no
        # 'copy: None' leaking through, and the two entries stay distinct.
        self.assertIn('role: entry</span>', html)

    def test_file_entry_note_includes_human_date_when_present(self):
        # #123, the schema's own worked example: a source whose files are
        # still facets of one piece of evidence (SPEC §7) but were not all
        # written on the same day - a household ledger whose entries span
        # months - can now give each `files:` entry its own `date:`
        # (SPEC §14), distinct from the source's own `source_date:`. Once
        # that value round-trips into source_files.date_edtf (tools/index.py),
        # the site's file note renders it human-readable and FIRST - '26
        # February 1916 · role: entry · copy: b' - so two same-role entries
        # of one ledger read as distinct by DATE, not just by an opaque copy
        # letter.
        self._seed_source('s-1111111111', 'Household Ledger', source_type='other')
        ledger_dir = self.archive_root / 'documents' / 'ledger'
        ledger_dir.mkdir(parents=True, exist_ok=True)
        (ledger_dir / 'entry-a.pdf').write_bytes(b'not a real pdf')
        (ledger_dir / 'entry-b.pdf').write_bytes(b'not a real pdf')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role, copy, date_edtf) VALUES (?,?,?,?,?)',
            ('s-1111111111', 'documents/ledger/entry-a.pdf', 'entry', None, '1916-02-26'))
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role, copy, date_edtf) VALUES (?,?,?,?,?)',
            ('s-1111111111', 'documents/ledger/entry-b.pdf', 'entry', 'b', '1916-06-03'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        # Undated-copy sibling: date first, no copy letter.
        self.assertIn('26 February 1916 · role: entry</span>', html)
        # Dated + copy-lettered sibling: date, then role, then copy - the two
        # files read as distinct on both axes at once.
        self.assertIn('3 June 1916 · role: entry · copy: b</span>', html)

    def test_file_entry_note_distinguishes_uncertain_date_from_approximate(self):
        # Codex review on PR #149 (P2): a `date: 1916?` (uncertain - "not sure
        # this is the right year") used to render through the same "about
        # {year}" prefix as `date: 1916~` (approximate - "this is a rough
        # guess"), misstating what the archive actually recorded on the
        # source page. The two markers must read as the two different things
        # they record - matching base.html's own date-notation legend, which
        # already explains `~` as "about this year" and `?` as "the year is
        # probably right but has not been confirmed".
        self._seed_source('s-1111111111', 'Uncertain Date Source', source_type='newspaper')
        clip_dir = self.archive_root / 'documents' / 'newspaper'
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / 'clipping-a.pdf').write_bytes(b'not a real pdf')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role, date_edtf) VALUES (?,?,?,?)',
            ('s-1111111111', 'documents/newspaper/clipping-a.pdf', 'clipping', '1916?'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('1916 (unconfirmed) · role: clipping</span>', html)
        self.assertNotIn('about 1916', html)

    def test_missing_asset_note_keeps_date_and_role(self):
        # Codex review on PR #149 (P2): a dated file missing on disk used to
        # have its ENTIRE role_note (date/role/copy) replaced by the fixed
        # 'file not available in this build' message, discarding facts the
        # index still has even though only the FILE's presentability is
        # degraded, not the record of what it was.
        self._seed_source('s-1111111111', 'Has Asset', source_type='newspaper')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role, copy, date_edtf) VALUES (?,?,?,?,?)',
            ('s-1111111111', 'documents/newspaper/ghost.pdf', 'clipping', 'b', '1916-02-26'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        self.assertIn(
            '26 February 1916 · role: clipping · copy: b · file not available in this build',
            html)

    def test_no_pillow_note_keeps_date_and_role(self):
        # Codex review on PR #149 (P2): a standalone build with Pillow
        # unavailable used to replace a dated photo's note with the fixed
        # 'image omitted (Pillow not installed)' message alone - a common,
        # supported configuration, not an edge case - dropping the date/role
        # the index still carries for it.
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role, date_edtf) VALUES (?,?,?,?)',
            ('s-1111111111', 'photos/1880/pic.jpg', 'front', '1916-02-26'))
        original = site._PIL_AVAILABLE
        site._PIL_AVAILABLE = False
        try:
            self._run(linked=False)
        finally:
            site._PIL_AVAILABLE = original
        html = self._read('sources/s-1111111111.html')
        self.assertIn(
            '26 February 1916 · role: front · image omitted (Pillow not installed)',
            html)

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_failed_derivative_note_keeps_date_and_role(self):
        # Codex review on PR #149 (P2): the other standalone-image fallback -
        # Pillow present but the derivative fails (corrupt image, unsupported
        # format, locked file) - had the same bug: 'image could not be
        # processed' replaced the whole note instead of joining it.
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')   # not a decodable image
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role, date_edtf) VALUES (?,?,?,?)',
            ('s-1111111111', 'photos/1880/pic.jpg', 'front', '1916-02-26'))
        res = self._run(linked=False)
        self.assertTrue(
            any('could not build a web image' in m for m in res['messages']), res['messages'])
        html = self._read('sources/s-1111111111.html')
        self.assertIn(
            '26 February 1916 · role: front · image could not be processed',
            html)

    def test_claims_table_value_scrubbed_but_workbench_prefill_keeps_raw(self):
        # #144 review finding 4: build_source_page passed the raw claim
        # value straight into the claims table - a source page could
        # publish a bare (C-xxxxxxxxxx) parenthetical the prose/timeline
        # scrub already keeps off every other page. The workbench "edit &
        # accept" prefill still needs the UNscrubbed original so a human
        # editing the claim sees exactly what is stored, not a lossy
        # display copy.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence',
                         'Lived in Kansas (C-4kx9m2p7qr) per the deed',
                         status='suggested', persons=('p-aaaaaaaaaa',))
        self._run(linked=True, workbench=True)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('Lived in Kansas per the deed', html)          # reader-facing cell: scrubbed
        self.assertIn('Lived in Kansas (C-4kx9m2p7qr) per the deed', html)  # wb prefill: raw


class SourceRedactionTests(_Base):
    def _setup_redactable(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe',
                           body='# Jane\n## Biography\nSee [S-2222222222].\n')
        self._seed_source('s-1111111111', 'Public Source', people=('p-aaaaaaaaaa',))
        self._seed_source('s-2222222222', 'Restricted Source', restricted=1, people=('p-aaaaaaaaaa',))

    def test_restricted_source_no_page_standalone(self):
        self._setup_redactable()
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'sources' / 's-2222222222.html').exists())
        self.assertTrue((self.out_dir / 'sources' / 's-1111111111.html').exists())
        bio = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn(site._RESTRICTED_LABEL, bio)               # reference redacted, not linked
        self.assertNotIn('sources/s-2222222222.html', bio)

    def test_restricted_source_page_in_linked(self):
        self._setup_redactable()
        self._run(linked=True)
        self.assertTrue((self.out_dir / 'sources' / 's-2222222222.html').exists())
        bio = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('sources/s-2222222222.html', bio)          # linked, not redacted

    def test_publication_ok_false_redacted_standalone(self):
        self._seed_source('s-3333333333', 'No-Pub Source', publication_ok=0)
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'sources' / 's-3333333333.html').exists())

    def test_dna_source_redacted_standalone(self):
        self._seed_source('s-4444444444', 'DNA Source', source_type='dna')
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'sources' / 's-4444444444.html').exists())


class PersonPageTests(_Base):
    def _setup_thomas(self):
        bio = ('# Thomas\n## Biography\n'
               'Thomas worked as a bookkeeper [S-1111111111] and married [P-bbbbbbbbbb].\n'
               'See also [S-9999999999].\n\n'
               '## Stories\nA tale worth keeping.\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=bio)
        self._seed_person('p-bbbbbbbbbb', 'Margaret Cole', tier='stub')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._seed_source('s-2222222222', 'Marriage Record', source_type='vital-record',
                          people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'birth', 'Born about 1840',
                         status='accepted', date_edtf='1840', place_text='New York',
                         persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-2222222222', 's-2222222222', 'marriage', 'Married Margaret',
                         status='accepted', date_edtf='1871', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-3333333333', 's-1111111111', 'residence', 'Lived in Fairview',
                         status='needs-review', date_edtf='1880', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-4444444444', 's-1111111111', 'occupation', 'Bookkeeper (unreviewed)',
                         status='suggested', date_edtf='1880', persons=('p-aaaaaaaaaa',))
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-bbbbbbbbbb')

    def test_person_page_all_sections(self):
        self._setup_thomas()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('<h2>Biography</h2>', html)
        self.assertIn('<h2>Timeline</h2>', html)
        self.assertIn('Friends', html)            # Friends & Family
        self.assertIn('<h2>Sources</h2>', html)
        # Summary block from accepted vitals
        self.assertIn('Born', html)
        self.assertIn('Married', html)
        # Stories section rendered
        self.assertIn('A tale worth keeping.', html)

    def test_negated_vital_not_rendered_as_positive_summary(self):
        # A --negated birth ("not born 1900") is a confirmed absence, not a
        # settled headline fact: `_person_summary` must not render it as
        # `Born 1900`, which would assert the very thing the claim denies.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'birth', 'not born 1900',
                         status='accepted', date_edtf='1900', persons=('p-aaaaaaaaaa',),
                         negated=1)
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('<dt>Born</dt>', html)

    def test_biography_token_swap(self):
        self._setup_thomas()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('../sources/s-1111111111.html', html)      # cited source appears (footnote list)
        self.assertIn('class="fn-ref"', html)                    # [S-id] in prose -> superscript footnote
        self.assertIn('Margaret Cole', html)                     # [P-id] -> name (stub, no link)
        self.assertNotIn('9999999999', html)                     # unresolved source id hidden, never shown raw

    def test_biography_strips_bare_claim_id_parenthetical(self):
        # #140: a bare `(C-xxxxxxxxxx)` parenthetical is an internal claim id
        # - meaningless and unlinked for a reader, and not the sanctioned
        # `[[C-xxxx]]` citation form - so it must never reach rendered prose.
        bio = ('# Thomas\n## Biography\n'
               'This session corroborates her birth on 9 September 1899 '
               '(C-4kx9m2p7qr); the family, then in Krakow, later emigrated.\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=bio)
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('corroborates her birth on 9 September 1899', html)
        self.assertNotIn('C-4kx9m2p7qr', html)

    def test_biography_embed_caption_claim_id_is_scrubbed(self):
        # P2 (PR #158 follow-up): `_prose_to_html` routes a prose embed
        # (`![[S-id|Caption]]`) through `render_embed` BEFORE `_inline_html`
        # (and its per-span scrub) ever runs, so `_render_embed`'s caption
        # handling only HTML-escaped the caption - a claim id embedded in
        # it leaked into both the visible <figcaption> text and the image's
        # alt attribute. The embed TARGET (the S-id) is unaffected.
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not a real image - linked mode never decodes it')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/pic.jpg', 'front'))
        bio = ('# Thomas\n## Biography\n'
               '![[S-1111111111|Old photo (C-4kx9m2p7qr)]]\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=bio)
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('C-4kx9m2p7qr', html)
        self.assertIn('<figcaption>Old photo</figcaption>', html)
        self.assertIn('alt="Old photo"', html)

    def test_biography_wikilink_display_label_claim_id_is_scrubbed(self):
        # P2 (PR #158 follow-up): a labeled wikilink's display text
        # (`[[S-id|label]]`) reaches `render_token` via `_inline_html`'s
        # `wdisp` group, which the per-span scrub fix (#144 finding 3)
        # never covered - `render_token`'s source/person/place renderers
        # only HTML-escape the display text, so a claim id embedded in the
        # label leaked straight onto the reader-facing page. The link
        # TARGET (the S-id) must stay untouched so the citation still
        # resolves.
        bio = ('# Thomas\n## Biography\n'
               'See [[S-1111111111|the record (C-4kx9m2p7qr)]] for details.\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=bio)
        self._seed_source('s-1111111111', 'Census')
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('C-4kx9m2p7qr', html)
        self.assertIn('>the record</a>', html)
        self.assertIn('../sources/s-1111111111.html', html)    # citation still resolves

    def test_biography_translates_before_date_bracket_in_prose(self):
        # #140: a raw `[..YYYY]` "before" bracket (SPEC §11) embedded mid-
        # sentence in a claim/biography's free text must render as the same
        # plain phrase the footer legend already uses for it (#131), not the
        # bracket/dot encoding verbatim.
        bio = ('# Thomas\n## Biography\n'
               'The family had emigrated [..1905], settling near Fairview.\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=bio)
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('before 1905', html)
        self.assertNotIn('[..1905]', html)

    def test_timeline_excludes_suggested_includes_needs_review(self):
        # Linked/workbench timelines keep needs-review claims - clearly marked
        # as unconfirmed (owner decision 2026-07-22).
        self._setup_thomas()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Lived in Fairview', html)                 # needs-review present
        self.assertIn('flag-unconfirmed', html)                  # ...and marked
        self.assertNotIn('Bookkeeper (unreviewed)', html)        # suggested excluded from timeline
        self.assertIn('1840s', html)                             # decade grouping
        self.assertIn('1880s', html)

    def test_public_timeline_is_accepted_only_with_low_confidence_flag(self):
        # The published standalone timeline shows settled facts only; an
        # accepted low-confidence fact publishes with its flag (owner decision
        # 2026-07-22: "sometimes that's the best we ever get").
        self._setup_thomas()
        self._seed_claim('c-5555555555', 's-1111111111', 'occupation', 'Perhaps a miller',
                         status='accepted', confidence='low', date_edtf='1885',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=False)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Lived in Fairview', html)              # needs-review withheld publicly
        self.assertNotIn('flag-unconfirmed', html)
        self.assertIn('Perhaps a miller', html)                  # accepted-low publishes
        self.assertIn('flag-low', html)                          # ...flagged

    def _badge_legend_present(self, html):
        return 'class="note badge-legend-note"' in html

    def test_badge_legend_absent_when_no_badges_on_page(self):
        # #130: a family member reading a page with neither mark present must
        # not be handed an explanation for vocabulary that page never uses -
        # the legend is gated on has_qa_badges (site.py), not printed
        # unconditionally the way base.html's date-notation legend is.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence', 'Lived quietly',
                         status='accepted', date_edtf='1900', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('flag-low', html)
        self.assertNotIn('flag-unconfirmed', html)
        self.assertFalse(self._badge_legend_present(html))

    def test_badge_legend_appears_with_low_confidence_only(self):
        # An accepted low-confidence vital carries its flag in the Born/Died/
        # Married SUMMARY block too (#130's summary surface), which renders
        # ABOVE the legend (the legend sits by the Timeline heading, further
        # down the page) - so the legend's wording must not assume the flag
        # it explains is always below it on the page. This also confirms the
        # legend fires from a summary-only page even in a standalone build.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'birth', 'Born about 1900',
                         status='accepted', confidence='low', date_edtf='1900',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=False)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('flag-unconfirmed', html)
        self.assertTrue(self._badge_legend_present(html))
        summary_idx = html.index('class="summary"')
        flag_idx = html.index('flag-low')
        legend_idx = html.index('class="note badge-legend-note"')
        self.assertTrue(summary_idx < flag_idx < legend_idx,
                        'the low-confidence flag must sit in the summary block, ABOVE the legend')
        legend = html[legend_idx:]
        self.assertIn('low confidence', legend)
        self.assertIn('unconfirmed', legend)   # both marks glossed together (see template comment)
        self.assertNotIn('below', legend)      # the wording must not assume the flag sits below it

    def test_badge_legend_appears_with_unconfirmed_only(self):
        # needs-review/"unconfirmed" only ever reaches linked or workbench
        # pages (never standalone), so this exercises that surface.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence', 'Lived in Fairview',
                         status='needs-review', date_edtf='1900', reviewed='2026-08-20',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('flag-unconfirmed', html)
        self.assertIn('parked 2026-08-20', html)
        self.assertNotIn('flag-low', html)
        self.assertTrue(self._badge_legend_present(html))

    def test_badge_legend_appears_with_both_badges(self):
        self._setup_thomas()   # already carries a needs-review claim
        self._seed_claim('c-5555555555', 's-1111111111', 'occupation', 'Perhaps a miller',
                         status='accepted', confidence='low', date_edtf='1885',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('flag-unconfirmed', html)
        self.assertIn('flag-low', html)
        self.assertTrue(self._badge_legend_present(html))
        # Exactly one legend, even though both marks appear (matches the
        # date-notation legend's own one-per-page posture, #131).
        self.assertEqual(html.count('class="note badge-legend-note"'), 1)

    def test_timeline_place_tag_omitted_when_sentence_already_states_it(self):
        # #127: the timeline was appending a bare "@ Place" tag even when the
        # claim's own sentence already named that place naturally moments
        # earlier ("moved to Millbrook to farm @ Millbrook"). Only append the
        # tag when the sentence does NOT already contain the place text.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence',
                         'Moved to Millbrook to farm', status='accepted',
                         date_edtf='1900', place_text='Millbrook',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Moved to Millbrook to farm', html)
        self.assertNotIn('@ Millbrook', html)
        # Not duplicated as a second, separate mention either.
        self.assertEqual(html.count('Millbrook'), 1)

    def test_timeline_place_tag_rendered_as_prose_when_not_already_stated(self):
        # A place that genuinely differs from what the sentence already says
        # still needs to render (it may be a real field-selection bug worth
        # its own follow-up, but the timeline should still show it) - just as
        # natural prose ("at Placename"), not a bare "@ Placename" suffix.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'death',
                         'Died at Fairview, Illinois, on 5 July 1891',
                         status='accepted', date_edtf='1891',
                         place_text='Lexington, Missouri', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Died at Fairview, Illinois, on 5 July 1891', html)
        self.assertIn('at Lexington, Missouri', html)
        self.assertNotIn('@ Lexington, Missouri', html)

    def test_timeline_place_tag_omitted_when_only_punctuation_differs(self):
        # #127 again, the shape the archive actually produces: the registry
        # writes "Millbrook, NY" and the sentence writes "Millbrook NY". The
        # place is the same one said twice, so the trailing mention still has
        # to go - the comma is not a second fact.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence',
                         'Moved to Millbrook NY to farm', status='accepted',
                         date_edtf='1900', place_text='Millbrook, NY',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Moved to Millbrook NY to farm', html)
        self.assertNotIn('at Millbrook, NY', html)

    def test_timeline_place_tag_omitted_when_sentence_states_only_the_leading_name(self):
        # #127 reopened (reopening comment example 1, paraphrased/genericized
        # per this project's convention of never using real family data in
        # test fixtures): PR #134's fix only changed the connector from "@"
        # to "at" - it never touched the underlying rule that the WHOLE
        # place label had to appear before the trailing tag was suppressed.
        # A sentence naming just a place's leading/common name (here, the
        # city) while the registry's fuller label adds a state qualifier
        # still read as "...in Millbrook at Millbrook, New York" - the city
        # named twice.
        #
        # Finding 1 follow-up: this claim carries no place_id (the common
        # case - most one-off claims never get a place registered), so
        # fully suppressing the trailing tag would delete "New York" from
        # the page with no link anywhere to recover it. The fix instead
        # prints the label's own remainder - everything from its first
        # comma onward - as a plain continuation of the sentence, not a
        # second "at Millbrook, New York" repeat.
        # ctype is deliberately NOT one of the vital types (_person_summary's
        # 'birth'/'death'/'marriage'/'baptism'/'burial') - those get their own
        # separate, structured summary row that also prints the place, which
        # would double-count "Millbrook" for a reason unrelated to this bug.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'event',
                         'The birth certificate was delivered at the Grange Hall '
                         'in Millbrook', status='accepted', date_edtf='1900',
                         place_text='Millbrook, New York', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('The birth certificate was delivered at the Grange Hall '
                      'in Millbrook, New York', html)     # remainder appended, not dropped
        self.assertNotIn('at Millbrook, New York', html)  # never a second, separate "at" tag
        self.assertEqual(html.count('Millbrook'), 1)      # named once, not doubled

    def test_timeline_place_tag_shows_remainder_for_unlinked_hierarchy_place(self):
        # Finding 1 (P1, #127 reopened round-3 review): a genealogist writes
        # "Moved to Millbrook to farm" for a claim whose actual place is
        # "Millbrook, Dutchess County, New York" - but most one-off
        # residence/travel claims never get a place formally registered
        # (SPEC.md), so this claim carries `place_text` only, no
        # `place_id`. Before this fix, the prior commit's suppression
        # logic dropped the trailing tag entirely once it recognized
        # "Millbrook" in the sentence - Dutchess County and New York
        # vanished from the page with no link anywhere to recover them,
        # a real loss of information the record actually contains. The
        # fix prints the label's own remainder as a plain continuation of
        # the sentence instead.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence',
                         'Moved to Millbrook to farm', status='accepted',
                         date_edtf='1900', place_text='Millbrook, Dutchess County, New York',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Moved to Millbrook to farm, Dutchess County, New York', html)
        self.assertNotIn('at Millbrook, Dutchess County, New York', html)  # not a second "at" repeat
        self.assertEqual(html.count('Millbrook'), 1)      # named once, not doubled

    def test_timeline_place_tag_omitted_when_sentence_states_the_home_name(self):
        # #127 reopened (reopening comment example 2, paraphrased): a
        # sentence already naming "the family home" still grew a trailing
        # "at the family home, Cook County, Illinois" - the home named
        # twice, once naturally and once as the leading words of the
        # "new" fact being appended.
        #
        # Finding 1 follow-up: no place_id here either, so the county/state
        # qualifier is not simply dropped - it prints as a continuation
        # tacked onto the end of the sentence (where the old trailing tag
        # used to go), not re-inserted next to "the family home" itself.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence',
                         'Resided at the family home as of the 1920 census, at age 79',
                         status='accepted', date_edtf='1920',
                         place_text='the family home, Cook County, Illinois',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Resided at the family home as of the 1920 census, at age 79, '
                      'Cook County, Illinois', html)             # remainder continues the sentence
        self.assertNotIn('at the family home, Cook County, Illinois', html)  # not a second "at" repeat
        self.assertEqual(html.count('the family home'), 1)       # named once, not doubled

    def test_timeline_place_tag_omitted_for_reordered_compound_place_list(self):
        # #127 reopened (reopening comment example 3, paraphrased): a
        # "traveled through" sentence already spells out both countries and
        # all their cities, but the trailing tag repeated the exact same
        # place list verbatim - "...and France (Paris, Lyon) in 1920 ... at
        # Italy and France (Rome, Milan, Paris, Lyon)". This is not the
        # punctuation-only gap #127's original fix handled (a real word,
        # "and", separates the two countries in the sentence, and the two
        # sides are in different positions relative to their city lists), so
        # the old whole-label contiguous match could never bridge it. Once
        # the sentence already states the label's leading name ("Italy"),
        # the whole tag - reordering and all - is redundant. (Finding 1
        # follow-up: an "and"-coordinated compound has no natural
        # "remainder" the way a hierarchy does, so this shape keeps its
        # full-suppress-when-matched behavior even with no place_id here.)
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'event',
                         'Traveled through Italy (Rome, Milan) and France (Paris, Lyon) '
                         'in 1920', status='accepted', date_edtf='1920',
                         place_text='Italy and France (Rome, Milan, Paris, Lyon)',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Traveled through Italy (Rome, Milan) and France (Paris, Lyon) in 1920', html)
        self.assertNotIn('at Italy and France (Rome, Milan, Paris, Lyon)', html)
        self.assertEqual(html.count('Italy'), 1)   # named once, not doubled

    def test_timeline_place_tag_not_omitted_for_and_conjoined_place_name(self):
        # Adversarial-review follow-up to #127 reopened: the leading-component
        # fallback above treats a comma and the word "and" as the same kind of
        # split, both marking a droppable trailing qualifier. That is right for
        # a hierarchy (a county, a state - broader, so dropping it only loses
        # precision) but wrong for a coordinate pair like "Trinidad and
        # Tobago", where the two names TOGETHER are one country's name -
        # "Trinidad" alone is a different, ambiguous place (there is also a
        # Trinidad, Colorado and a Trinidad, Cuba). A sentence that only says
        # "Born in Trinidad" does not already name "Trinidad and Tobago", so
        # the trailing tag must still print in full rather than silently
        # narrowing the record's own country of origin down to one island.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'immigration',
                         'Born in Trinidad', status='accepted', date_edtf='1900',
                         place_text='Trinidad and Tobago', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Born in Trinidad', html)
        self.assertIn('at Trinidad and Tobago', html)   # full tag still prints
        self.assertIn('Tobago', html)                   # never silently dropped

    def test_timeline_place_tag_not_omitted_when_and_conjoined_part_unmentioned(self):
        # Same root cause as the Trinidad/Tobago case above, on the exact
        # shape #127 reopened's own compound-list fix
        # (test_timeline_place_tag_omitted_for_reordered_compound_place_list,
        # above) was designed for: that test's sentence names BOTH countries
        # ("...Italy (Rome, Milan) and France (Paris, Lyon)..."), which was
        # the only shape it ever exercised. A sentence naming only ONE side
        # of an "and"-joined label ("Traveled to Italy", nothing about
        # France) must not have "France" suppressed along with the
        # already-stated "Italy" - only when EVERY coordinate part is
        # actually found in the sentence is the whole tag safe to drop.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'event',
                         'Traveled to Italy in 1920', status='accepted', date_edtf='1920',
                         place_text='Italy and France', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Traveled to Italy in 1920', html)
        self.assertIn('at Italy and France', html)   # full tag still prints
        self.assertIn('France', html)                # never silently dropped

    def test_timeline_place_tag_not_omitted_for_scattered_and_conjoined_mentions(self):
        # Codex's adversarial follow-up to #127 reopened (finding 2): the
        # coordinate-part check above only required each part of an
        # "and"-joined label to be found SOMEWHERE in the sentence
        # independently, with no requirement that they name the SAME event's
        # place together. "Born in Trinidad; a witness later traveled to
        # Tobago" satisfies two independent searches for "Trinidad" and
        # "Tobago" even though the sentence never once identifies the
        # compound country "Trinidad and Tobago" as the birthplace - it
        # names one island as the birthplace and a wholly different,
        # unrelated island in an unrelated later clause. The parts must
        # form one coordinated "part1 ... and ... part2" mention, not two
        # scattered, unrelated ones, before the trailing tag is safe to
        # suppress.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'immigration',
                         'Born in Trinidad; a witness later traveled to Tobago',
                         status='accepted', date_edtf='1900',
                         place_text='Trinidad and Tobago', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Born in Trinidad; a witness later traveled to Tobago', html)
        self.assertIn('at Trinidad and Tobago', html)   # full tag still prints, not suppressed

    def test_timeline_place_tag_omitted_for_oxford_comma_and_conjoined_mention(self):
        # Adversarial review, round 4 audit: an ordinary Oxford-style comma
        # before "and" ("Trinidad, and Tobago") already matched fine against
        # a BARE compound label with no trailing qualifier - the whole-label
        # matcher joins words with a loose "any punctuation" gap. But the
        # identical phrasing, against a label carrying a trailing qualifier
        # ("Trinidad and Tobago, Caribbean" - falling through to the
        # coordinate-parts matcher since the whole label is never fully
        # stated), used to be silently rejected: the connective regex
        # allowed an elaborating parenthetical before "and" but not a plain
        # comma, so the tag never suppressed and the compound name printed
        # twice for no reason the sentence's own wording explains.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'immigration',
                         'Born in Trinidad, and Tobago', status='accepted', date_edtf='1900',
                         place_text='Trinidad and Tobago, Caribbean', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Born in Trinidad, and Tobago', html)
        self.assertNotIn('at Trinidad and Tobago, Caribbean', html)
        self.assertEqual(html.count('Trinidad'), 1)   # named once, not doubled

    def test_timeline_place_text_internal_encoding_is_scrubbed_from_the_trailing_remainder(self):
        # Adversarial review, round 4 audit (#140's own concern, extended):
        # every OTHER reader-facing free-text field on this page already
        # runs through `_scrub_internal_encoding` before it can reach a
        # rendered page - a claim's `value` text does, a source's prose
        # does - but `place_text` reached the trailing-remainder text (the
        # unlinkable-hierarchy continuation this branch's own earlier round
        # introduced) completely unscrubbed. A bare citation-id
        # parenthetical or an unedited `[..YYYY]` date bracket accidentally
        # left in place_text used to leak straight onto the page.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence',
                         'Moved to Millbrook to farm', status='accepted',
                         date_edtf='1900',
                         place_text='Millbrook (C-4kx9m2p7qr), Dutchess County, [..1900]',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Dutchess County, before 1900', html)
        self.assertNotIn('C-4kx9m2p7qr', html)
        self.assertNotIn('[..1900]', html)

    def test_place_cell_internal_encoding_is_scrubbed(self):
        # Same leak, the OTHER reader-facing surface `_place_html` feeds
        # (the claims-table "place" cell, distinct from the timeline's
        # remainder text above) - a claim whose place is linkable (so the
        # timeline tag suppresses instead of printing a remainder) still
        # shows its place in the source's own claims table.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence',
                         'Moved somewhere to farm', status='accepted', date_edtf='1900',
                         place_text='Somewhere (C-4kx9m2p7qr)', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        self.assertNotIn('C-4kx9m2p7qr', html)
        self.assertIn('Somewhere', html)

    def test_timeline_strips_bare_claim_id_parenthetical(self):
        # #140: same rendering bug as the Biography case, on the Timeline's
        # separate render path (_timeline_value_html) - a bare `(C-xxxxxxxxxx)`
        # parenthetical in a claim's value text must not reach the page.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'birth',
                         'Disputes her birth on 9 September 1899 (C-4kx9m2p7qr); '
                         'the family then in Krakow.', status='accepted',
                         date_edtf='1899', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Disputes her birth on 9 September 1899', html)
        self.assertNotIn('C-4kx9m2p7qr', html)

    def test_timeline_translates_before_date_bracket_in_prose(self):
        # #140: same bracket-translation bug as the Biography case, on the
        # Timeline's separate render path - a raw `[..YYYY]` "before" bracket
        # in a claim's value text must render as plain English, matching the
        # footer legend's own wording (#131), not the bracket/dot encoding.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'immigration',
                         'The family had emigrated [..1905], settling near Fairview.',
                         status='accepted', date_edtf='1900', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('before 1905', html)
        self.assertNotIn('[..1905]', html)

    def test_timeline_decades_stay_contiguous_despite_date_min_divergence(self):
        # #128: decade grouping reads the DISPLAY date (date_edtf, via
        # _decade_header), but sort order reads date_min - a different,
        # widened field (site.py:559's documented reason: an approximate date
        # would otherwise sort into the wrong decade). An uncertain date_edtf
        # ('193X') can widen to a date_min that sorts far from its own
        # decade's other entries, which used to split one decade into two
        # non-contiguous groups with another decade's heading between them.
        # Here date_min order is 1930, 1945, 1950 but decade order is
        # 1930s, 1940s, 1930s - exactly that shape.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence', 'Lived in Millbrook',
                         status='accepted', date_edtf='1930', date_min='1930-01-01',
                         persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-2222222222', 's-1111111111', 'occupation', 'Worked as a clerk',
                         status='accepted', date_edtf='1945', date_min='1945-01-01',
                         persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-3333333333', 's-1111111111', 'residence', 'Lived in Fairview',
                         status='accepted', date_edtf='193X', date_min='1950-01-01',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        start = html.index('<div class="timeline">')
        end = html.index('</div>', start)
        timeline_html = html[start:end]
        headings = re.findall(r'<h3>([^<]*)</h3>', timeline_html)
        self.assertEqual(headings, ['1930s', '1940s'])   # each decade heading exactly once
        # Both 1930s entries land in the one 1930s block, ahead of the 1940s block.
        self.assertLess(timeline_html.index('Lived in Millbrook'), timeline_html.index('Lived in Fairview'))
        self.assertLess(timeline_html.index('Lived in Fairview'), timeline_html.index('<h3>1940s</h3>'))
        self.assertLess(timeline_html.index('<h3>1940s</h3>'), timeline_html.index('Worked as a clerk'))

    def test_timeline_undated_entries_get_their_own_heading(self):
        # #129: undated entries rendered directly beneath the most recent
        # decade heading with no heading of their own, so a reader scanning
        # by decade could mistake one for belonging to that decade. The
        # Python grouping already tags an undated group `decade: None`
        # (verified separately) - the gap is template-side: give that group
        # its own explicit "Undated" heading.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence', 'Lived in Millbrook',
                         status='accepted', date_edtf='1930', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-2222222222', 's-1111111111', 'occupation', 'Worked as a farmer',
                         status='accepted', date_edtf=None, persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        start = html.index('<div class="timeline">')
        end = html.index('</div>', start)
        timeline_html = html[start:end]
        self.assertIn('<h3>1930s</h3>', timeline_html)
        self.assertIn('<h3>Undated</h3>', timeline_html)
        self.assertLess(timeline_html.index('<h3>1930s</h3>'), timeline_html.index('<h3>Undated</h3>'))
        self.assertLess(timeline_html.index('<h3>Undated</h3>'), timeline_html.index('Worked as a farmer'))

    def test_timeline_collects_every_undated_entry_under_one_heading(self):
        # The other half of #128, and the one #129's heading made visible: a
        # claim whose date the archive cannot read ('circa 1870' - the loose
        # hand-edit AGENTS.md says the tools must tolerate) has no decade but
        # DOES have a date_min, so it sorted with the 1870s while a genuinely
        # undated claim sorted last. The linear pass then opened an undated
        # group, closed it for the 1930s, and opened a second one - two
        # "Undated" headings on one page. Every dateless entry belongs to one
        # group, at the end.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'occupation', 'Unreadable date entry',
                         status='accepted', date_edtf='circa 1870', date_min='1870-01-01',
                         persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-2222222222', 's-1111111111', 'residence', 'Dated entry',
                         status='accepted', date_edtf='1930', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-3333333333', 's-1111111111', 'note', 'No date at all',
                         status='accepted', date_edtf=None, persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        start = html.index('<div class="timeline">')
        timeline_html = html[start:html.index('</div>', start)]
        self.assertEqual(re.findall(r'<h3>([^<]*)</h3>', timeline_html), ['1930s', 'Undated'])
        self.assertLess(timeline_html.index('<h3>Undated</h3>'),
                        timeline_html.index('Unreadable date entry'))
        self.assertLess(timeline_html.index('<h3>Undated</h3>'),
                        timeline_html.index('No date at all'))

    def test_timeline_keeps_date_order_within_a_decade(self):
        # #128 sorts a copy of the rows by decade before grouping; the sort
        # must be STABLE or it would trade a heading bug for an ordering bug.
        # Within one decade the rows keep the SQL's date_min order (January
        # before June), and an interval date groups by the decade it starts
        # in.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'note', 'June entry',
                         status='accepted', date_edtf='1923-06', date_min='1923-06-01',
                         persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-2222222222', 's-1111111111', 'note', 'January entry',
                         status='accepted', date_edtf='1923-01', date_min='1923-01-01',
                         persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-3333333333', 's-1111111111', 'note', 'Interval entry',
                         status='accepted', date_edtf='1852/1883', date_min='1852-01-01',
                         persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        start = html.index('<div class="timeline">')
        timeline_html = html[start:html.index('</div>', start)]
        self.assertEqual(re.findall(r'<h3>([^<]*)</h3>', timeline_html), ['1850s', '1920s'])
        self.assertLess(timeline_html.index('Interval entry'), timeline_html.index('<h3>1920s</h3>'))
        self.assertLess(timeline_html.index('January entry'), timeline_html.index('June entry'))

    def test_family_and_source_footnotes(self):
        self._setup_thomas()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Spouses', html)
        self.assertIn('Margaret Cole', html)
        # Sources are a numbered footnote list of human names, not raw [S-id] chips,
        # and inline citations are superscript refs into it.
        self.assertIn('<ol class="footnotes">', html)
        self.assertIn('id="fn-1"', html)
        self.assertIn('>Census</a>', html)                       # source shown by name
        self.assertIn('class="fn-ref"', html)                    # inline superscript ref
        self.assertNotIn('[S-1111111111]', html)                 # backend id never shown inline
        self.assertNotIn('<h3>census</h3>', html)                # no longer grouped by type
        self.assertNotIn('class="ids"', html)                    # person id line removed

    def test_summary_vitals_are_separate_dt_dd_pairs(self):
        # Win 3: Born / Married / Died must each read on its own line. The
        # `.summary` block has no dt/dd display override in design/styles.css
        # (dl/dt/dd default to block), so this is a markup check - one dt/dd
        # PAIR per vital, never two labels or two values sharing an element -
        # which is what actually makes each line separate on the page.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'birth', 'Born',
                         status='accepted', date_edtf='1840', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-2222222222', 's-1111111111', 'marriage', 'Married',
                         status='accepted', date_edtf='1871', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-3333333333', 's-1111111111', 'death', 'Died',
                         status='accepted', date_edtf='1910', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('<dt>Born</dt>', html)
        self.assertIn('<dt>Married</dt>', html)
        self.assertIn('<dt>Died</dt>', html)
        born_idx = html.index('<dt>Born</dt>')
        married_idx = html.index('<dt>Married</dt>')
        died_idx = html.index('<dt>Died</dt>')
        self.assertTrue(born_idx < married_idx < died_idx)
        # Each <dd> holds exactly its own vital's value - the next label never
        # bleeds into the previous value, which would read as one run-on line.
        self.assertIn('1840', html[born_idx:married_idx])
        self.assertNotIn('1871', html[born_idx:married_idx])

    def test_summary_vital_without_date_scrubs_claim_id_paren_in_free_text_value(self):
        # #144 review finding 4: a vital claim with no date_edtf falls back
        # to its free-text value for the Born/Died/Married summary line -
        # the one place a bare (C-xxxxxxxxxx) parenthetical could reach the
        # person summary unscrubbed, since every OTHER vital display uses
        # the structured date_edtf instead (which must stay untranslated -
        # base.html's own legend explains its notation).
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'birth',
                         'Born about harvest time (C-4kx9m2p7qr)',
                         status='accepted', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Born about harvest time', html)
        self.assertNotIn('C-4kx9m2p7qr', html)

    def _legend(self, html):
        """The date-notation legend paragraph on a built page, tag to tag.

        Sliced exactly, rather than by a fixed character window, so a longer
        legend cannot quietly push a notation out of what the assertions can
        see - and so an assertion can never pass on text that belongs to some
        other part of the footer.
        """
        self.assertIn('<p class="date-notation-note">', html)
        start = html.index('<p class="date-notation-note">')
        # Exactly one legend per page: it lives in base.html's shared footer.
        self.assertNotIn('<p class="date-notation-note">', html[start + 1:])
        return html[start:html.index('</p>', start)]

    def test_date_notation_legend_reachable_from_person_page(self):
        # #131: a person page that actually shows the shorthand (~ / X / /)
        # must sit on a page that also explains it - the legend lives in the
        # shared footer (base.html), so every page that extends it carries
        # the explanation regardless of which notation that particular page
        # happens to use.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'death', 'Died',
                         status='accepted', date_edtf='1891~', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('1891~', html)          # the notation really is on this page, as written
        legend = self._legend(html)
        self.assertIn('~', legend)
        self.assertIn('193X', legend)         # the decade form, shown by example
        self.assertIn('decade', legend)
        self.assertIn('range', legend)

    def test_date_notation_legend_covers_every_spec_11_notation(self):
        # SPEC.md 11 is the list of date forms a record may hold, and `fha site`
        # prints date_edtf exactly as stored - so every mark in that table is a
        # mark a visitor can meet on a page. Derived from the table, not from
        # the legend's current wording: a notation the archive can store and the
        # legend cannot explain is the bug #131 reported, one form later.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_source('s-1111111111', 'Record', people=('p-aaaaaaaaaa',))
        # One claim per SPEC 11 row that carries a mark a reader must decode.
        for n, (cid, edtf) in enumerate((
                ('c-1111111111', '1850~'),            # Circa
                ('c-2222222222', '1850?'),            # Uncertain
                ('c-3333333333', '185X'),             # Decade
                ('c-4444444444', '[..1920]'),         # Before
                ('c-5555555555', '1871-02/1871-03'),  # Interval
        )):
            self._seed_claim(cid, 's-1111111111', 'event', f'Event {n}',
                             status='accepted', date_edtf=edtf, persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        legend = self._legend(html)
        for edtf, mark in (('1850~', '~'), ('1850?', '?'), ('185X', 'X'),
                           ('[..1920]', '[..'), ('1871-02/1871-03', '/')):
            # The page shows the stored form as written ...
            self.assertIn(edtf, html, f'{edtf} is not rendered on the page')
            # ... so the legend has to account for its mark.
            self.assertIn(mark, legend, f'the legend never explains {mark!r} ({edtf})')
        # The two hedges mean different things (SPEC 11 lists Circa and
        # Uncertain as separate rows); the legend must not collapse them.
        self.assertIn('confirmed', legend)
        self.assertIn('before', legend)

    def test_date_notation_legend_also_reaches_source_and_home_pages(self):
        # The legend is shared footer markup (base.html), not a person-page
        # special case - it travels to every page that extends base.html.
        self._seed_source('s-1111111111', 'A Source')
        self._run(linked=True)
        self._legend(self._read('sources/s-1111111111.html'))
        self._legend(self._read('index.html'))

    def test_alt_names_and_tags_in_header(self):
        self._seed_person(
            'p-aaaaaaaaaa', 'Margaret Hartley', surname='Hartley',
            frontmatter_extra='name_at_birth: Margaret Cole\nalso_known_as: [Peggy]\ntags: [brick-wall, priority]')
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('class="alt-names"', html)
        self.assertIn('Margaret Cole', html)                     # birth name (né/née)
        self.assertIn('Peggy', html)                             # also_known_as
        self.assertIn('class="tag-pill"', html)
        self.assertIn('brick-wall', html)
        self.assertIn('priority', html)

    def test_research_notes_private_fence(self):
        body = ('# P\n## Research Notes\nPublic research note.\n\n'
                '<!-- private -->\nSecret hunch.\n<!-- /private -->\n')
        self._seed_person('p-aaaaaaaaaa', 'P', body=body)
        self._run(linked=True)
        linked = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Research Notes', linked)
        self.assertIn('Public research note', linked)
        self.assertIn('Secret hunch', linked)                    # kept in the preview
        self._run(linked=False)
        standalone = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Public research note', standalone)
        self.assertNotIn('Secret hunch', standalone)             # dropped from the shared build
        self.assertNotIn('private -->', standalone)              # no raw marker leak

    def test_cp1252_person_record_in_linked_mode_degrades_and_names_the_cause(self):
        # Linked mode skips `prepare()`'s privacy pre-pass (standalone-only),
        # so this is the one mode where the page-building reads themselves -
        # `_person_prose` and `_person_header_meta` - are what meet a person
        # record saved in another codepage. Both must degrade (no crash, no
        # traceback) and both must say WHY in plain language, not raise
        # `UnicodeDecodeError` up through the page build.
        self._seed_person('p-aaaaaaaaaa', 'Margaret Hartley', surname='Hartley')
        broken = self.archive_root / 'people' / 'hartley__test_p-aaaaaaaaaa.md'
        broken.write_bytes(
            ('---\nid: p-aaaaaaaaaa\nname: Margaret Hartley\n'
             'name_at_birth: Margaret Cole\n---\n\n## Biography\n\nBorn in Kraków.\n')
            .encode('cp1252'))
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')
        self.assertTrue((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Born in', html)                 # prose withheld, not garbled
        self.assertNotIn('Margaret Cole', html)            # alt name withheld too
        messages = res['messages']
        self.assertTrue(
            any('hartley__test_p-aaaaaaaaaa.md' in m and "isn't saved as UTF-8 text" in m
                and 'skipping its prose' in m for m in messages), messages)
        self.assertTrue(
            any('hartley__test_p-aaaaaaaaaa.md' in m and "isn't saved as UTF-8 text" in m
                and 'alternate names and editorial tags' in m for m in messages), messages)
        self.assertFalse(any('codec' in m for m in messages), messages)


class PersonRedactionTests(_Base):
    def test_living_person_no_page_standalone(self):
        self._seed_person('p-aaaaaaaaaa', 'Living Larry', living='true')
        self._seed_person('p-bbbbbbbbbb', 'Dead Dan', living='false',
                          body='# Dan\n## Biography\nKnew [P-aaaaaaaaaa] well.\n')
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())
        self.assertTrue((self.out_dir / 'persons' / 'p-bbbbbbbbbb.html').exists())
        dan = self._read('persons/p-bbbbbbbbbb.html')
        self.assertIn(site._LIVING_LABEL, dan)
        self.assertNotIn('persons/p-aaaaaaaaaa.html', dan)

    def test_unknown_living_treated_as_living(self):
        self._seed_person('p-aaaaaaaaaa', 'Unknown Ursula', living='unknown')
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())

    def test_living_person_has_page_in_linked(self):
        self._seed_person('p-aaaaaaaaaa', 'Living Larry', living='true')
        self._run(linked=True)
        self.assertTrue((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())

    def test_stub_person_never_gets_page(self):
        self._seed_person('p-aaaaaaaaaa', 'Stubby', tier='stub')
        self._run(linked=True)
        self.assertFalse((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())

    def test_linked_shows_living_person_data(self):
        # Inverse of redaction: in --linked, a living person keeps their real
        # name and a working link (developer preview is unredacted).
        self._seed_person('p-aaaaaaaaaa', 'Living Larry', living='true')
        self._seed_person('p-bbbbbbbbbb', 'Dead Dan', living='false',
                          body='# Dan\n## Biography\nKnew [P-aaaaaaaaaa] well.\n')
        self._run(linked=True)
        dan = self._read('persons/p-bbbbbbbbbb.html')
        self.assertIn('Living Larry', dan)                       # real name, not redacted
        self.assertIn('href="p-aaaaaaaaaa.html"', dan)           # real link (sibling page)
        self.assertNotIn(site._LIVING_LABEL, dan)


class FamilyStripTests(_Base):
    """The compact parents/spouses/siblings/children nav at the top of a person
    page (`_person_family_strip`). Redaction here must mirror what the other
    strip groups (and `_build_family_wings`'s pedigree columns) already do -
    this is the fix-1 regression: a `spouse` edge from `relationships` was
    never surfaced into the strip's `spouses` key at all."""

    def test_family_strip_shows_spouse_linked_and_standalone(self):
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', surname='Hartley', living='false')
        self._seed_person('p-bbbbbbbbbb', 'Margaret Cole', surname='Cole', living='false')
        self._seed_source('s-1111111111', 'Marriage Record', source_type='vital-record',
                          people=('p-aaaaaaaaaa', 'p-bbbbbbbbbb'))
        self._seed_claim('c-1111111111', 's-1111111111', 'marriage', 'Married Margaret Cole',
                         status='accepted', date_edtf='1871',
                         persons=('p-aaaaaaaaaa', 'p-bbbbbbbbbb'))
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-bbbbbbbbbb')

        self._run(linked=True)
        linked = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('class="family-strip"', linked)
        self.assertIn('<span class="fs-label">Spouse</span>', linked)
        strip = linked[linked.index('class="family-strip"'):]
        self.assertIn('Margaret Cole', strip[:strip.index('</nav>')])

        self._run(linked=False)
        standalone = self._read('persons/p-aaaaaaaaaa.html')
        strip = standalone[standalone.index('class="family-strip"'):]
        self.assertIn('Margaret Cole', strip[:strip.index('</nav>')])

    def test_family_strip_redacts_living_spouse_same_as_living_child(self):
        # The non-negotiable case (mirrors FamilyChartTests' pedigree-column
        # version of this same rule): a living spouse must be redacted from
        # the standalone strip exactly as a living child already is - both
        # omitted outright, both restored in --linked.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', living='false')
        self._seed_person('p-bbbbbbbbbb', 'Living Spouse', living='true')
        self._seed_person('p-cccccccccc', 'Living Child', living='true')
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-bbbbbbbbbb')
        self._seed_rel('p-aaaaaaaaaa', 'child', 'p-cccccccccc')

        self._run(linked=False)
        standalone = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Living Spouse', standalone)
        self.assertNotIn('Living Child', standalone)
        # No parent/spouse/sibling/child survives redaction, so the strip
        # itself is correctly absent (not shown empty) - same as the pedigree
        # chart's all-redacted case.
        self.assertNotIn('class="family-strip"', standalone)

        self._run(linked=True)
        linked = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Living Spouse', linked)
        self.assertIn('Living Child', linked)


class UnreadableRecordPrivacyTests(_Base):
    """A record this build could not read is treated as restricted.

    The `restricted:` marker lives ONLY in the record file - the index carries
    `restricted` for sources and nothing at all for persons - so the exclusion
    set is built by reading files. That makes a failed read fail OPEN: no
    marker read exactly like no marker written, and a person carrying
    `restricted: by-request` whose file would not open got a page on the
    PUBLIC snapshot, with their name on it. The only sign was a warning about
    missing prose, which reads as less content, not as a privacy marker lost.

    The docstring's stated mitigation - "the standalone audit catches any
    leak" - could not: what it named is the page-set design, which promises
    only that no link points at a page that was not built. The missing marker
    corrupts the page set itself.
    """

    def _all_output_text(self) -> str:
        return '\n'.join(
            p.read_text(encoding='utf-8', errors='replace')
            for p in sorted(self.out_dir.rglob('*'))
            if p.is_file() and p.suffix in ('.html', '.json')
        )

    def _seed_trio(self):
        # One restricted person whose file reads (the control - already
        # withheld today), one restricted person whose file will not, and one
        # ordinary person whose biography links to both.
        self._seed_person(
            'p-aaaaaaaaaa', 'Public Pat', surname='Pat',
            body='# Public Pat\n\n## Biography\n\n'
                 'Worked with [[p-bbbbbbbbbb]] and [[p-cccccccccc]].\n')
        self._seed_person('p-bbbbbbbbbb', 'Quiet Quinn', surname='Quinn',
                          frontmatter_extra='restricted: by-request')
        self._seed_person('p-cccccccccc', 'Hidden Hollis', surname='Hollis',
                          frontmatter_extra='restricted: by-request')

    def test_a_person_record_that_is_gone_is_withheld_not_published(self):
        self._seed_trio()
        gone = self.archive_root / 'people' / 'hollis__test_p-cccccccccc.md'
        gone.unlink()
        res = self._run(linked=False)

        # The control: restricted and readable, correctly no page.
        self.assertFalse((self.out_dir / 'persons' / 'p-bbbbbbbbbb.html').exists())
        # The fix: restricted and unreadable, also no page.
        self.assertFalse((self.out_dir / 'persons' / 'p-cccccccccc.html').exists())
        self.assertTrue((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())

        text = self._all_output_text()
        self.assertNotIn('Hidden Hollis', text)
        self.assertNotIn('Hollis', text)
        self.assertNotIn('Quiet Quinn', text)
        self.assertIn('Living Person', self._read('persons/p-aaaaaaaaaa.html'))

        # And the message says what actually happened, naming the file.
        self.assertTrue(
            any('hollis__test_p-cccccccccc.md' in m and 'withheld' in m
                for m in res['messages']), res['messages'])
        self.assertTrue(
            any('left out of public output' in m for m in res['messages']),
            res['messages'])

    def test_a_person_record_whose_read_raises_is_withheld_too(self):
        # `read_record` normally reports a bad file through `parse_errors`
        # rather than raising, so both routes have to land in the same place.
        self._seed_trio()
        real = site.read_record
        target = self.archive_root / 'people' / 'hollis__test_p-cccccccccc.md'

        def boom(path, *a, **kw):
            if Path(path) == target:
                raise RuntimeError('injected read failure')
            return real(path, *a, **kw)

        with unittest.mock.patch.object(site, 'read_record', new=boom):
            res = self._run(linked=False)
        self.assertFalse((self.out_dir / 'persons' / 'p-cccccccccc.html').exists())
        self.assertNotIn('Hidden Hollis', self._all_output_text())
        self.assertTrue(
            any('injected read failure' in m for m in res['messages']),
            res['messages'])

    def test_a_person_record_saved_as_cp1252_is_withheld_with_a_plain_cause(self):
        # #68's actual failure mode, not a mocked exception: a file saved in
        # another codepage (cp1252, a Windows editor's default) raises
        # UnicodeDecodeError out of `read_record` unless a caller opts in to
        # `on_decode_error`. Before this fix the broad `except Exception`
        # already caught it (so the person was already withheld, correctly -
        # this is a wording fix, not a privacy fix), but the message showed
        # the raw exception text (byte offsets, codec names). It must now
        # name the real, fixable cause instead.
        self._seed_trio()
        broken = self.archive_root / 'people' / 'hollis__test_p-cccccccccc.md'
        broken.write_bytes(
            ('---\nid: p-cccccccccc\nname: Hidden Hollis\nrestricted: by-request\n'
             '---\n\n## Biography\n\nBorn in Kraków.\n').encode('cp1252'))
        res = self._run(linked=False)
        self.assertFalse((self.out_dir / 'persons' / 'p-cccccccccc.html').exists())
        self.assertNotIn('Hidden Hollis', self._all_output_text())
        messages = res['messages']
        self.assertTrue(
            any('hollis__test_p-cccccccccc.md' in m and "isn't saved as UTF-8 text" in m
                for m in messages), messages)
        # The old wording (a raw UnicodeDecodeError's `str()`) must be gone.
        self.assertFalse(any('codec' in m for m in messages), messages)

    def test_a_source_record_saved_as_cp1252_is_withheld_with_a_plain_cause(self):
        self._seed_person('p-aaaaaaaaaa', 'Public Pat', surname='Pat')
        self._seed_source('s-1111111111', 'Sealed Adoption File',
                          people=('p-aaaaaaaaaa',))
        broken = self.archive_root / 'sources' / 'census' / 'src_s-1111111111.md'
        broken.write_bytes(
            ('---\nid: s-1111111111\ntitle: Sealed Adoption File\n'
             'source_type: census\n---\n\n## Claims\n\nFound in Kraków.\n').encode('cp1252'))
        res = self._run(linked=False)
        self.assertFalse((self.out_dir / 'sources' / 's-1111111111.html').exists())
        messages = res['messages']
        self.assertTrue(
            any('src_s-1111111111.md' in m and "isn't saved as UTF-8 text" in m
                for m in messages), messages)
        self.assertFalse(any('codec' in m for m in messages), messages)

    def test_malformed_person_frontmatter_is_withheld(self):
        # A hand-edit that breaks the YAML empties `meta` without raising,
        # which loses the marker just as completely as a deleted file.
        self._seed_trio()
        broken = self.archive_root / 'people' / 'hollis__test_p-cccccccccc.md'
        broken.write_text(
            '---\nid: p-cccccccccc\nname: "unterminated\n  : : :\n---\n# H\n',
            encoding='utf-8')
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'persons' / 'p-cccccccccc.html').exists())
        self.assertNotIn('Hidden Hollis', self._all_output_text())

    def test_linked_mode_is_unchanged(self):
        # The developer preview applies no redaction at all and never reads
        # these markers; withholding there would be a behaviour change for a
        # mode that has no privacy contract to keep.
        self._seed_trio()
        (self.archive_root / 'people' / 'hollis__test_p-cccccccccc.md').unlink()
        self._run(linked=True)
        self.assertTrue((self.out_dir / 'persons' / 'p-bbbbbbbbbb.html').exists())
        self.assertTrue((self.out_dir / 'persons' / 'p-cccccccccc.html').exists())

    def test_an_unreadable_source_withholds_its_page_and_its_facts(self):
        # The per-claim `restricted:` markers live in the source file too, so
        # an unreadable source record means an unknown number of withheld
        # facts. The whole source is withheld, and its claims with it.
        self._seed_person('p-aaaaaaaaaa', 'Public Pat', surname='Pat')
        self._seed_source('s-1111111111', 'Sealed Adoption File',
                          people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'birth',
                         'A private detail', status='accepted', date_edtf='1901',
                         persons=('p-aaaaaaaaaa',))
        (self.archive_root / 'sources' / 'census' / 'src_s-1111111111.md').unlink()
        res = self._run(linked=False)
        self.assertFalse((self.out_dir / 'sources' / 's-1111111111.html').exists())
        text = self._all_output_text()
        self.assertNotIn('A private detail', text)
        self.assertNotIn('Sealed Adoption File', text)
        self.assertTrue(
            any('src_s-1111111111.md' in m and 'withheld' in m
                for m in res['messages']), res['messages'])

    def test_a_readable_person_record_still_publishes(self):
        # The guard on the guard: fail-closed must not become closed-always.
        self._seed_person('p-aaaaaaaaaa', 'Public Pat', surname='Pat')
        self._run(linked=False)
        self.assertTrue((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())
        self.assertIn('Public Pat', self._read('persons/p-aaaaaaaaaa.html'))


class ResilienceTests(_Base):
    def test_malformed_source_yaml_warns_and_continues(self):
        # Broken frontmatter YAML in one source; another source is fine.
        self._seed_source('s-1111111111', 'Broken Source',
                          frontmatter='---\nid: s-1111111111\ntitle: "unterminated\n  : : :\n---\n\n## Claims\n')
        self._seed_source('s-2222222222', 'Good Source')
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')
        self.assertTrue(any('formatting problem' in m or 'could not read' in m for m in res['messages']))
        # Both pages still built; broken one falls back to its index title.
        self.assertTrue((self.out_dir / 'sources' / 's-1111111111.html').exists())
        self.assertIn('Broken Source', self._read('sources/s-1111111111.html'))
        self.assertTrue((self.out_dir / 'sources' / 's-2222222222.html').exists())

    def test_cp1252_source_record_falls_back_to_title_with_a_plain_cause(self):
        # Linked mode skips the privacy pre-pass entirely (`_load_restriction_
        # markers` is standalone-only), so this is the one mode where
        # `build_source_page`'s own read - not `prepare()`'s - is what meets
        # the bad decode. Before the fix the fallback still worked (a bare
        # `except Exception` already caught `UnicodeDecodeError`), but named
        # the raw codec exception instead of a fixable cause.
        self._seed_source('s-1111111111', 'Broken Source')
        broken = self.archive_root / 'sources' / 'census' / 'src_s-1111111111.md'
        broken.write_bytes(
            ('---\nid: s-1111111111\ntitle: Broken Source\nsource_type: census\n'
             'citation: "Found in Kraków"\n---\n\n## Claims\n').encode('cp1252'))
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')
        self.assertTrue((self.out_dir / 'sources' / 's-1111111111.html').exists())
        html = self._read('sources/s-1111111111.html')
        self.assertIn('Broken Source', html)              # index title still shown
        self.assertNotIn('Found in Kraków', html)          # citation could not be read
        messages = res['messages']
        self.assertTrue(
            any('src_s-1111111111.md' in m and "isn't saved as UTF-8 text" in m
                and 'showing the title only' in m for m in messages), messages)
        self.assertFalse(any('codec' in m for m in messages), messages)

    def test_dry_run_writes_nothing(self):
        self._seed_source('s-1111111111', 'A Source')
        res = self._run(dry_run=True)
        self.assertEqual(res['status'], 'dry-run')
        self.assertFalse((self.out_dir / 'sources').exists())
        self.assertFalse((self.out_dir / 'index.html').exists())

    def test_rebuild_drops_now_redacted_page(self):
        self._seed_person('p-aaaaaaaaaa', 'Was Dead', living='false')
        self._run(linked=False)
        self.assertTrue((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())
        # Person becomes living; a rebuild must remove the stale page.
        self.conn.execute("UPDATE persons SET living='true' WHERE id='p-aaaaaaaaaa'")
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())

    def test_refuses_archive_root_as_output(self):
        # The site clears its sources/ subtree on rebuild; the archive's own
        # sources/ must never be the target.
        self._seed_source('s-1111111111', 'A Source')
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'index.sqlite', (future, future))
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        res = site.run_site(self.archive_root, self.archive_root, linked=True)
        self.assertEqual(res['status'], 'bad-output')
        self.assertTrue((self.archive_root / 'sources').exists())   # records untouched

    def test_refuses_output_inside_record_tree(self):
        # Building into the archive's own sources/ would scatter pages among the
        # record .md files; refuse before any write.
        self._seed_source('s-1111111111', 'A Source')
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'index.sqlite', (future, future))
        res = site.run_site(self.archive_root, self.archive_root / 'sources', linked=True)
        self.assertEqual(res['status'], 'bad-output')

    def test_no_index_status(self):
        # Remove the index file entirely.
        self.conn.close()
        (self.archive_root / '.cache' / 'index.sqlite').unlink()
        res = site.run_site(self.archive_root, self.out_dir, linked=True)
        self.assertEqual(res['status'], 'no-index')
        # reopen so tearDown's close() doesn't error
        self.conn = sqlite3.connect(':memory:')

    def test_old_schema_index_rejected(self):
        # P2-4: an index built before the publication_ok three-state fix (older
        # schema version) must be refused, not trusted, so a rebuild applies the
        # corrected redaction. Overwrite with a v1-shaped index.
        self.conn.close()
        db = self.archive_root / '.cache' / 'index.sqlite'
        db.unlink()
        conn = sqlite3.connect(str(db))
        conn.executescript(
            "PRAGMA user_version=1;"
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta(key, value) VALUES ('schema_version', '1');"
            "CREATE TABLE persons(id TEXT, name TEXT, surname TEXT, sex TEXT, living TEXT,"
            " tier TEXT, status TEXT, merged_into TEXT, path TEXT);"
            "CREATE TABLE sources(id TEXT, title TEXT, source_type TEXT, date_edtf TEXT,"
            " repository TEXT, source_class TEXT, restricted INTEGER, publication_ok INTEGER,"
            " status TEXT, path TEXT);"
        )
        conn.commit()
        conn.close()
        future = time.time() + 5
        os.utime(db, (future, future))
        res = site.run_site(self.archive_root, self.out_dir, linked=True)
        self.assertEqual(res['status'], 'no-index')   # old schema → refused, prompt to rebuild
        self.conn = sqlite3.connect(':memory:')


class AssetTests(_Base):
    def _make_photos_db(self):
        conn = sqlite3.connect(str(self.archive_root / '.cache' / 'photos.sqlite'))
        conn.executescript(PHOTOS_DDL)
        conn.row_factory = sqlite3.Row
        return conn

    def _make_photos_fresh(self):
        far_future = time.time() + 10_000
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (far_future, far_future))
        # #48: this synthetic photos.sqlite is hand-built via raw DDL,
        # bypassing run_scan - the only place that writes the #48 photo
        # manifest - so without this, photoindex_status's additive manifest
        # check finds no manifest at all and (correctly, per the
        # bootstrapping rule) reads any real photo file already written to
        # `photos/`, plus any real person/sources-photos file _seed_person
        # already wrote, as newly "added", i.e. stale.
        photos_dir = self.archive_root / 'photos'
        manifest = {
            path_to_alias(p, 'photos', {}, self.archive_root): p.stat().st_mtime
            for p in (photos_dir.rglob('*') if photos_dir.is_dir() else []) if p.is_file()
        }
        manifest.update(photoindex_record_manifest(self.archive_root))
        write_path_manifest(photoindex_manifest_path(self.archive_root), manifest)

    def test_linked_photo_strip(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        # A real (dummy) file on disk; linked mode only checks existence + links it.
        img = self.archive_root / 'photos' / '1880' / 'jane.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')
        pconn = self._make_photos_db()
        pconn.execute(
            'INSERT INTO photos(path, group_id, is_primary, caption) VALUES (?,?,?,?)',
            ('photos/1880/jane.jpg', 'g1', 1, 'Jane in 1880'))
        pconn.execute(
            'INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
            ('photos/1880/jane.jpg', 'p-aaaaaaaaaa', 'pid-keyword'))
        pconn.commit()
        pconn.close()
        self._make_photos_fresh()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Photographs', html)
        self.assertIn('Jane in 1880', html)
        self.assertIn('jane.jpg', html)

    def test_photo_strip_link_opens_in_new_tab(self):
        # #122: the Photographs strip opens the real photo file, not another
        # page on this site - it must not replace the person page a visitor
        # was reading.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        img = self.archive_root / 'photos' / '1880' / 'jane.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')
        pconn = self._make_photos_db()
        pconn.execute(
            'INSERT INTO photos(path, group_id, is_primary, caption) VALUES (?,?,?,?)',
            ('photos/1880/jane.jpg', 'g1', 1, 'Jane in 1880'))
        pconn.execute(
            'INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
            ('photos/1880/jane.jpg', 'p-aaaaaaaaaa', 'pid-keyword'))
        pconn.commit()
        pconn.close()
        self._make_photos_fresh()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        # Just the strip: slicing to the end of the document would let any
        # target="_blank" further down the page satisfy the assertion.
        strip_start = html.index('class="photo-strip"')
        strip = html[strip_start:html.index('</figure>', strip_start)]
        self.assertIn('jane.jpg', strip)
        self.assertIn('target="_blank"', strip)
        self.assertIn('rel="noopener"', strip)

    def test_missing_photo_row_does_not_hide_its_live_variant(self):
        # `fha photoindex reconcile` re-keys a vanished photo 'MISSING:…' and
        # leaves its is_primary flag alone, so the photo strip's
        # one-entry-per-group pick would choose the row that cannot be
        # rendered and drop the whole physical photo - back scan included -
        # off the person page.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        back = self.archive_root / 'photos' / '1880' / 'jane-back.jpg'
        back.parent.mkdir(parents=True, exist_ok=True)
        back.write_bytes(b'not-a-real-image-but-exists')
        pconn = self._make_photos_db()
        pconn.execute(
            'INSERT INTO photos(path, group_id, is_primary, caption) VALUES (?,?,?,?)',
            ('MISSING:photos/1880/jane.jpg', 'g1', 1, 'Jane in 1880'))
        pconn.execute(
            'INSERT INTO photos(path, group_id, is_primary, caption) VALUES (?,?,?,?)',
            ('photos/1880/jane-back.jpg', 'g1', 0, 'Reverse of the 1880 portrait'))
        pconn.execute(
            'INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
            ('MISSING:photos/1880/jane.jpg', 'p-aaaaaaaaaa', 'pid-keyword'))
        pconn.execute(
            'INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
            ('photos/1880/jane-back.jpg', 'p-aaaaaaaaaa', 'pid-keyword'))
        pconn.commit()
        pconn.close()
        self._make_photos_fresh()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('jane-back.jpg', html)
        self.assertNotIn('MISSING:', html)

    def test_living_tagged_gate_reads_missing_keyed_tags(self):
        # The file came back (a reconnected drive) but the catalog has not
        # been rescanned, so the living person's tag still sits on the
        # 'MISSING:' key while source_files names the plain path. The gate
        # must still fire - a stale catalog may not publish a living person.
        self._seed_person('p-aaaaaaaaaa', 'Living Larry', living='true')
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/pic.jpg', 'front'))
        pconn = self._make_photos_db()
        pconn.execute(
            'INSERT INTO photos(path, group_id, is_primary, caption) VALUES (?,?,?,?)',
            ('MISSING:photos/1880/pic.jpg', 'g1', 1, ''))
        pconn.execute(
            'INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
            ('MISSING:photos/1880/pic.jpg', 'p-aaaaaaaaaa', 'pid-keyword'))
        pconn.commit()
        pconn.close()
        self._make_photos_fresh()
        self._run(linked=False)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('image omitted - tagged to a living person', html)

    def test_a_photo_folder_that_will_not_open_stops_the_bare_name_guess(self):
        # The bare-filename guess is allowed to publish a photo ONLY when
        # exactly one file in the library answers to that name - one match is
        # the whole guard. A folder that will not list hides the second match,
        # so the site would publish, on a page it may hand to the family, a
        # photo picked by which folder happened to open. Under-seeing here
        # fails the guard OPEN, and a published photo cannot be unpublished.
        (self.archive_root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n', encoding='utf-8')
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe',
                          frontmatter_extra='profile_photo: mystery.jpg')
        # The second copy sits one level below the folder that will not
        # open. (`rglob('mystery.jpg')` stats a literal name rather than
        # listing the folder holding it, so a file directly inside the shut
        # folder is still found - it is the subtree below it that vanishes.)
        for rel in ('1890/mystery.jpg', '1975/summer/mystery.jpg'):
            stray = self.archive_root / 'photos' / rel
            stray.parent.mkdir(parents=True, exist_ok=True)
            stray.write_bytes(b'not-a-real-image-but-exists')
        shut = self.archive_root / 'photos' / '1975'
        with unittest.mock.patch('os.scandir', new=_scandir_denying(shut)):
            res = self._run(linked=True)
        self.assertNotIn('mystery.jpg', self._read('persons/p-aaaaaaaaaa.html'))
        self.assertTrue(
            any('could not be opened' in m and 'mystery.jpg' in m
                for m in res['messages']), res['messages'])
        self.assertTrue(
            any('<year>/mystery.jpg' in m for m in res['messages']),
            res['messages'])

    def test_a_shut_folder_inside_an_ignored_subtree_does_not_stop_the_guess(self):
        # The guard is "publish a bare filename only when exactly ONE file in
        # the library answers to it". A folder under `photos_ignore:` holds no
        # candidate the guess would ever have accepted, so it can hide no
        # second match and the count is not in doubt. Refusing over it is a
        # refusal the guard has not earned - and photos_ignore: is there to
        # name bulk exports, which are exactly the folders that live on drives
        # that come and go.
        (self.archive_root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\nphotos_ignore:\n  - "Flickr Export"\n',
            encoding='utf-8')
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe',
                          frontmatter_extra='profile_photo: mystery.jpg')
        real = self.archive_root / 'photos' / '1890' / 'mystery.jpg'
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(b'not-a-real-image-but-exists')
        shut = self.archive_root / 'photos' / 'Flickr Export' / '2019'
        shut.mkdir(parents=True, exist_ok=True)
        (shut / 'filler.jpg').write_bytes(b'x')
        with unittest.mock.patch('os.scandir', new=_scandir_denying(shut)):
            res = self._run(linked=True)
        self.assertIn('mystery.jpg', self._read('persons/p-aaaaaaaaaa.html'))
        self.assertFalse(
            [m for m in res['messages'] if 'could not be opened' in m],
            res['messages'])

    def test_a_shut_folder_outside_the_ignored_subtree_still_stops_the_guess(self):
        # The other half of the same rule: a folder the setting says nothing
        # about can still be hiding the second copy, so the refusal stands.
        (self.archive_root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\nphotos_ignore:\n  - "Flickr Export"\n',
            encoding='utf-8')
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe',
                          frontmatter_extra='profile_photo: mystery.jpg')
        for rel in ('1890/mystery.jpg', '1975/summer/mystery.jpg'):
            stray = self.archive_root / 'photos' / rel
            stray.parent.mkdir(parents=True, exist_ok=True)
            stray.write_bytes(b'not-a-real-image-but-exists')
        shut = self.archive_root / 'photos' / '1975'
        with unittest.mock.patch('os.scandir', new=_scandir_denying(shut)):
            res = self._run(linked=True)
        self.assertNotIn('mystery.jpg', self._read('persons/p-aaaaaaaaaa.html'))
        self.assertTrue(
            any('could not be opened' in m for m in res['messages']),
            res['messages'])

    def test_photos_ignore_excludes_bare_filename_guess(self):
        # A bare `profile_photo: mystery.jpg` is answered by scanning the
        # photos root. photos_ignore: marks a subtree as not part of the
        # family library, so the scan must not answer out of it (#35).
        (self.archive_root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\nphotos_ignore:\n  - "Flickr Export"\n',
            encoding='utf-8')
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe',
                          frontmatter_extra='profile_photo: mystery.jpg')
        stray = self.archive_root / 'photos' / 'Flickr Export' / 'mystery.jpg'
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b'not-a-real-image-but-exists')
        res = self._run(linked=True)
        self.assertTrue(any('matched no photo' in m for m in res['messages']))
        self.assertNotIn('mystery.jpg', self._read('persons/p-aaaaaaaaaa.html'))

        # Same file, same reference, with the setting removed: it resolves.
        (self.archive_root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n', encoding='utf-8')
        res = self._run(linked=True)
        self.assertIn('mystery.jpg', self._read('persons/p-aaaaaaaaaa.html'))

    def test_cp1252_person_record_skips_profile_photo_with_a_warning(self):
        # `_resolve_profile_photo` used to read `profile_photo:` through a
        # bare `except Exception: return None` - safe (no crash) but
        # perfectly silent, unlike every other miss this method already
        # warns about ("matched no photo", "could not build a web image").
        # Linked mode is needed so the read is reached at all - standalone's
        # privacy pre-pass would withhold this person's page outright.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe',
                          frontmatter_extra='profile_photo: mystery.jpg')
        broken = self.archive_root / 'people' / 'person__test_p-aaaaaaaaaa.md'
        broken.write_bytes(
            ('---\nid: p-aaaaaaaaaa\nname: Jane Doe\n'
             'profile_photo: mystery.jpg\n---\n\n## Biography\n\nBorn in Kraków.\n')
            .encode('cp1252'))
        real = self.archive_root / 'photos' / '1890' / 'mystery.jpg'
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(b'not-a-real-image-but-exists')
        res = self._run(linked=True)
        self.assertNotIn('mystery.jpg', self._read('persons/p-aaaaaaaaaa.html'))
        messages = res['messages']
        self.assertTrue(
            any('person__test_p-aaaaaaaaaa.md' in m and "isn't saved as UTF-8 text" in m
                and 'profile photo' in m for m in messages), messages)
        self.assertFalse(any('codec' in m for m in messages), messages)

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_standalone_image_derivative(self):
        from PIL import Image
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.png'
        img.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (2000, 1500), (120, 90, 60)).save(img)
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/pic.png', 'front'))
        self._run(linked=False)
        # An EXIF-stripped, resized derivative is created under media/ and linked.
        derivs = list((self.out_dir / 'media').rglob('*.jpg'))
        self.assertTrue(derivs, 'expected a media derivative to be written')
        with Image.open(derivs[0]) as im:
            self.assertLessEqual(max(im.size), site._DERIVATIVE_MAX_PX)
            self.assertEqual(im.info.get('exif'), None)
        self.assertIn('media/', self._read('sources/s-1111111111.html'))

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_same_stem_photos_get_distinct_derivatives(self):
        # Two photos sharing a filename stem in different folders must not
        # overwrite each other's derivative (P2-1).
        from PIL import Image
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        pconn = self._make_photos_db()
        for i, (group, sub) in enumerate(((1, '1880'), (2, '1890'))):
            img = self.archive_root / 'photos' / sub / 'scan.jpg'
            img.parent.mkdir(parents=True, exist_ok=True)
            Image.new('RGB', (300, 200), (10 * i, 20, 30)).save(img)
            pconn.execute('INSERT INTO photos(path, group_id, is_primary, caption) VALUES (?,?,?,?)',
                          (f'photos/{sub}/scan.jpg', f'g{group}', 1, f'Scan {sub}'))
            pconn.execute('INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
                          (f'photos/{sub}/scan.jpg', 'p-aaaaaaaaaa', 'pid-keyword'))
        pconn.commit()
        pconn.close()
        self._make_photos_fresh()
        self._run(linked=False)
        derivs = list((self.out_dir / 'media' / 'people').glob('scan_*.jpg'))
        self.assertEqual(len(derivs), 2, 'both same-stem photos should get distinct derivatives')

    def test_standalone_non_image_kept_in_archive(self):
        self._seed_source('s-1111111111', 'Doc Source', source_type='letter')
        doc = self.archive_root / 'documents' / 'letters' / 'note_s-1111111111.txt'
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text('a letter', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'documents/letters/note_s-1111111111.txt', 'transcript'))
        self._run(linked=False)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('original kept in the archive', html)      # not copied out of the archive

    def test_source_portrait_honors_living_tagged_gate(self):
        # Win 2's record-head thumbnail reuses the Files section's own file
        # entry, so it must inherit the same living-tagged-photo gate rather
        # than re-resolving (and re-publishing) the image on its own.
        self._seed_person('p-aaaaaaaaaa', 'Living Larry', living='true')
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/pic.jpg', 'front'))
        pconn = self._make_photos_db()
        pconn.execute(
            'INSERT INTO photos(path, group_id, is_primary, caption) VALUES (?,?,?,?)',
            ('photos/1880/pic.jpg', 'g1', 1, ''))
        pconn.execute(
            'INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
            ('photos/1880/pic.jpg', 'p-aaaaaaaaaa', 'pid-keyword'))
        pconn.commit()
        pconn.close()
        self._make_photos_fresh()
        # The living-tagged gate is a standalone-only redaction (linked is an
        # unredacted developer preview, like every other asset rule here).
        self._run(linked=False)
        html = self._read('sources/s-1111111111.html')
        self.assertNotIn('class="source-portrait"', html)
        self.assertIn('image omitted - tagged to a living person', html)

    def test_living_tagged_note_keeps_date_and_role(self):
        # Same bug shape as PR #149 finding 4 (see _with_role_note), but in a
        # fallback branch that fix never touched: the living-tagged-photo gate
        # in _source_file_entries builds its 'note' straight from a fixed
        # string, even though the very source_files row it is iterating
        # (role/copy/date_edtf) is right there. Only the FILE's presentability
        # is degraded (it names a living person) - the indexed date/role/copy
        # facts should still show, exactly as they do on the other degraded
        # paths this same function calls into (_file_entry/
        # _standalone_image_entry).
        self._seed_person('p-aaaaaaaaaa', 'Living Larry', living='true')
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role, copy, date_edtf) VALUES (?,?,?,?,?)',
            ('s-1111111111', 'photos/1880/pic.jpg', 'front', 'b', '1916-02-26'))
        pconn = self._make_photos_db()
        pconn.execute(
            'INSERT INTO photos(path, group_id, is_primary, caption) VALUES (?,?,?,?)',
            ('photos/1880/pic.jpg', 'g1', 1, ''))
        pconn.execute(
            'INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
            ('photos/1880/pic.jpg', 'p-aaaaaaaaaa', 'pid-keyword'))
        pconn.commit()
        pconn.close()
        self._make_photos_fresh()
        self._run(linked=False)
        html = self._read('sources/s-1111111111.html')
        self.assertIn(
            '26 February 1916 · role: front · copy: b · image omitted - tagged to a living person',
            html)


class LivingPhotoCheckUnavailableTests(_Base):
    """The living-person photo gate fails OPEN, and the build says so.

    Owner decision, 2026-08-16: `fha site` must build without
    `.cache/photos.sqlite`, so a missing or unreadable catalog publishes the
    source images rather than refusing them - "fine if living photo is
    included. that should be on the researcher to monitor." Monitoring is only
    possible if someone is told, so the one thing that is NOT optional is the
    warning: which check did not run, why, and what to do about it.
    """

    def _seed_photo_source(self, sid='s-1111111111', name='pic.png'):
        """A public source with one real image attached, and nothing else."""
        from PIL import Image
        self._seed_source(sid, 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / name
        img.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (60, 40), (10, 20, 30)).save(img)
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            (sid, f'photos/1880/{name}', 'front'))

    def _warnings(self, res):
        return [m for m in res['messages'] if 'tags naming living people' in m]

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_no_catalog_publishes_the_image_and_warns_once(self):
        self._seed_photo_source()
        res = self._run(linked=False)
        # Behaviour is unchanged: the image is published, the build is clean of
        # refusals, and the page shows the picture.
        self.assertIn('media/', self._read('sources/s-1111111111.html'))
        self.assertEqual(res['status'], 'ok')
        warnings = self._warnings(res)
        self.assertEqual(len(warnings), 1, res['messages'])
        # Cause, and the state it is in.
        self.assertIn('the photo catalog has not been built yet', warnings[0])
        # What did not happen.
        self.assertIn('NOT', warnings[0])
        self.assertIn('someone still living', warnings[0])
        # The next step, and the fallback for a reader who cannot run it now.
        self.assertIn('fha photoindex', warnings[0])
        self.assertIn('fha site', warnings[0])

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_the_warning_is_not_repeated_per_photo(self):
        # A site with a wall of scans must not bury the sentence under copies
        # of itself - one build, one warning.
        for i, sid in enumerate(('s-1111111111', 's-2222222222')):
            self._seed_photo_source(sid, name=f'pic{i}.png')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/pic1.png', 'back'))
        res = self._run(linked=False)
        self.assertEqual(len(self._warnings(res)), 1, res['messages'])

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_a_fresh_catalog_raises_no_alarm(self):
        # The other half: a warning that fires when the check DID run is a
        # warning the researcher learns to ignore.
        self._seed_photo_source()
        pconn = sqlite3.connect(str(self.archive_root / '.cache' / 'photos.sqlite'))
        pconn.executescript(PHOTOS_DDL)
        pconn.execute(
            'INSERT INTO photos(path, group_id, is_primary, caption) VALUES (?,?,?,?)',
            ('photos/1880/pic.png', 'g1', 1, ''))
        pconn.commit()
        pconn.close()
        far_future = time.time() + 10_000
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (far_future, far_future))
        # #48: hand-built via raw DDL, bypassing run_scan - see AssetTests.
        # _make_photos_fresh's matching comment. The real photo file this
        # test wrote via _seed_photo_source needs a manifest entry too, or
        # the "no alarm" this test is about never gets exercised.
        photo = self.archive_root / 'photos' / '1880' / 'pic.png'
        write_path_manifest(
            photoindex_manifest_path(self.archive_root),
            {path_to_alias(photo, 'photos', {}, self.archive_root): photo.stat().st_mtime,
             **photoindex_record_manifest(self.archive_root)})
        res = self._run(linked=False)
        self.assertEqual(self._warnings(res), [], res['messages'])

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_an_unreadable_catalog_names_itself(self):
        # A file that is not a database at all reads as unreadable, and the
        # warning must say that rather than "not built yet" - the two have
        # different fixes.
        self._seed_photo_source()
        (self.archive_root / '.cache' / 'photos.sqlite').write_bytes(b'not a database')
        far_future = time.time() + 10_000
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (far_future, far_future))
        res = self._run(linked=False)
        warnings = self._warnings(res)
        self.assertEqual(len(warnings), 1, res['messages'])
        self.assertIn('could not be read', warnings[0])

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_a_stale_catalog_names_itself(self):
        # Stale is the state a real archive sits in most often: the catalog
        # exists, so "not built yet" would send the reader looking for a file
        # that is right there.
        self._seed_photo_source()
        pconn = sqlite3.connect(str(self.archive_root / '.cache' / 'photos.sqlite'))
        pconn.executescript(PHOTOS_DDL)
        pconn.commit()
        pconn.close()
        long_ago = time.time() - 10_000
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (long_ago, long_ago))
        res = self._run(linked=False)
        warnings = self._warnings(res)
        self.assertEqual(len(warnings), 1, res['messages'])
        self.assertIn('out of date', warnings[0])

    def test_a_source_with_no_images_says_nothing(self):
        # Nothing was published that the gate would have looked at, so there is
        # nothing to warn about - the transcript stays in the archive either way.
        self._seed_source('s-1111111111', 'Doc Source', source_type='letter')
        doc = self.archive_root / 'documents' / 'letters' / 'note_s-1111111111.txt'
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text('a letter', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'documents/letters/note_s-1111111111.txt', 'transcript'))
        res = self._run(linked=False)
        self.assertEqual(self._warnings(res), [], res['messages'])

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_the_linked_preview_says_nothing(self):
        # --linked is the unredacted local preview: the gate does not run there
        # by design, so there is no failed check to report and nothing is
        # shared out of it.
        self._seed_photo_source()
        res = self._run(linked=True)
        self.assertEqual(self._warnings(res), [], res['messages'])

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_the_warning_reaches_the_person_who_ran_the_command(self):
        # A warning only in the Result is a warning nobody sees: `fha site`
        # prints these to stderr and finishes with exit 1 (warnings), which is
        # what the human at the terminal actually reads.
        self._seed_photo_source()
        (self.archive_root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n', encoding='utf-8')
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'index.sqlite', (future, future))
        # #48: hand-built index, no manifest written yet - see _Base._run's
        # matching comment. This test calls site._cmd_site directly instead
        # of _run, so it needs its own copy of the same resync (fha.yaml,
        # just written above, is a real file the manifest must know about).
        write_path_manifest(
            index_manifest_path(self.archive_root), record_path_manifest(self.archive_root))
        args = argparse.Namespace(root=str(self.archive_root), out=str(self.out_dir),
                                  linked=False, dry_run=False)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = site._cmd_site(args)
        self.assertEqual(code, 1)
        self.assertIn('tags naming living people', err.getvalue())
        self.assertIn('fha photoindex', err.getvalue())


class SourcePortraitTests(_Base):
    """Win 2 (plan 17): a right-floated scan thumbnail at the head of a source
    record, linking to the full-size image (private/wireframes/source.html's
    `.person-portrait` pattern, reused as `.source-portrait` so the image can
    sit inside one floated <figure> with its caption instead of two competing
    right floats). The Files section gets a matching 'full size' text link on
    every image it already lists."""

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_standalone_portrait_uses_media_derivative(self):
        from PIL import Image
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (2000, 1500), (120, 90, 60)).save(img)
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/pic.jpg', 'front'))
        self._run(linked=False)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('class="source-portrait"', html)
        self.assertIn('Open the scan full size', html)
        self.assertIn('media/', html)                    # a derivative, not the archive original
        self.assertIn('full-size-link', html)             # the Files entry also links "full size"

    def test_linked_portrait_uses_real_path(self):
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/pic.jpg', 'front'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('class="source-portrait"', html)
        self.assertIn('pic.jpg', html)                    # the real archive path, no derivative

    def test_portrait_prefers_front_role_over_first(self):
        self._seed_source('s-1111111111', 'Multi Image', source_type='photo')
        for name in ('first.jpg', 'second.jpg'):
            img = self.archive_root / 'photos' / '1880' / name
            img.parent.mkdir(parents=True, exist_ok=True)
            img.write_bytes(b'not-a-real-image-but-exists')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/first.jpg', 'page-1'))
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/second.jpg', 'front'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        start = html.index('class="source-portrait"')
        block = html[start:start + 400]
        self.assertIn('second.jpg', block)
        self.assertNotIn('first.jpg', block)

    def test_portrait_falls_back_to_first_image_without_front_role(self):
        self._seed_source('s-1111111111', 'Multi Image', source_type='photo')
        for name in ('first.jpg', 'second.jpg'):
            img = self.archive_root / 'photos' / '1880' / name
            img.parent.mkdir(parents=True, exist_ok=True)
            img.write_bytes(b'not-a-real-image-but-exists')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/first.jpg', 'page-1'))
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/second.jpg', 'page-2'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        start = html.index('class="source-portrait"')
        block = html[start:start + 400]
        self.assertIn('first.jpg', block)
        self.assertNotIn('second.jpg', block)

    def test_portrait_absent_without_image_asset(self):
        self._seed_source('s-1111111111', 'Doc Source', source_type='letter')
        doc = self.archive_root / 'documents' / 'letters' / 'note_s-1111111111.txt'
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text('a letter', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'documents/letters/note_s-1111111111.txt', 'transcript'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        self.assertNotIn('class="source-portrait"', html)

    def test_portrait_absent_when_no_files_at_all(self):
        self._seed_source('s-1111111111', 'No Files Source')
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        self.assertNotIn('class="source-portrait"', html)

    def test_no_pillow_degrades_gracefully_no_portrait(self):
        # Standalone with Pillow unavailable: the page still builds, the
        # image is omitted (never the original, which would leak EXIF), and
        # the head thumbnail simply does not appear rather than breaking.
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/pic.jpg', 'front'))
        original = site._PIL_AVAILABLE
        site._PIL_AVAILABLE = False
        try:
            res = self._run(linked=False)
        finally:
            site._PIL_AVAILABLE = original
        self.assertEqual(res['status'], 'ok')
        self.assertTrue((self.out_dir / 'sources' / 's-1111111111.html').exists())
        html = self._read('sources/s-1111111111.html')
        self.assertNotIn('class="source-portrait"', html)
        self.assertIn('Pillow not installed', html)


class FileOpeningLinkTargetTests(_Base):
    """#122: links that open an actual scan/photo/document file must carry
    target="_blank" rel="noopener" (open in a new tab, no window.opener leak
    back to this page) - same-site navigation links must NOT, so a visitor
    reading a person or source page never loses their place to a file open.
    Two-sided by design (AGENTS_TOOLING.md - "two-sided rules get two-sided
    tests"): every assertion below is paired with a negative one proving the
    attribute was not sprayed everywhere."""

    def test_source_portrait_links_open_in_new_tab(self):
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/pic.jpg', 'front'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('class="source-portrait"', html)
        # Both the image wrapper and the caption link the same full_href.
        portrait_block = html[html.index('class="source-portrait"'):]
        figure_end = portrait_block.index('</figure>')
        figure_html = portrait_block[:figure_end]
        self.assertEqual(figure_html.count('target="_blank"'), 2)
        self.assertEqual(figure_html.count('rel="noopener"'), 2)

    def test_source_files_image_links_open_in_new_tab(self):
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/pic.jpg', 'front'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        files_block = html[html.index('<h2>Files</h2>'):html.index('</ul>', html.index('<h2>Files</h2>'))]
        self.assertIn('pic.jpg', files_block)
        # thumbnail link + "full size" link, both new-tab.
        self.assertEqual(files_block.count('target="_blank"'), 2)
        self.assertEqual(files_block.count('rel="noopener"'), 2)
        self.assertIn('full-size-link', files_block)

    def test_source_files_nonimage_link_opens_in_new_tab(self):
        self._seed_source('s-1111111111', 'Doc Source', source_type='letter')
        doc = self.archive_root / 'documents' / 'letters' / 'note_s-1111111111.txt'
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text('a letter', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'documents/letters/note_s-1111111111.txt', 'transcript'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        files_block = html[html.index('<h2>Files</h2>'):html.index('</ul>', html.index('<h2>Files</h2>'))]
        self.assertIn('note_s-1111111111.txt', files_block)
        self.assertIn('target="_blank"', files_block)
        self.assertIn('rel="noopener"', files_block)

    def test_internal_navigation_links_stay_in_same_tab(self):
        # Negative half of the pair above: same-site links (the header nav,
        # a citation's person cross-link, the source's own record-open link)
        # must never pick up target="_blank" - only file-opening anchors do.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', '1880 Census', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence', 'Lived in Kansas',
                         status='accepted', persons=('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        # The header nav (from base.html) never opens a new tab.
        self.assertIn('<a href="../index.html">Home</a>', html)
        nav_block = html[html.index('site-nav'):html.index('</nav>')]
        self.assertNotIn('target="_blank"', nav_block)
        # The claim's person cross-link is same-site navigation, not a file.
        self.assertIn('../persons/p-aaaaaaaaaa.html', html)
        person_link_idx = html.index('../persons/p-aaaaaaaaaa.html')
        # Look at just that one anchor tag (up to its closing '>').
        tag_end = html.index('>', person_link_idx)
        self.assertNotIn('target="_blank"', html[person_link_idx:tag_end])

    # - the whole-site invariant -

    def _seed_a_page_of_every_kind(self):
        """Seed enough archive that a build emits all four link shapes at once:
        site navigation, person/source/place cross-links, an image file and a
        non-image file. One seeding used by both halves of the sweep below."""
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo',
                          people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence', 'Lived in Kansas',
                         status='accepted', date_edtf='1880', persons=('p-aaaaaaaaaa',))
        img = self.archive_root / 'photos' / '1880' / 'pic.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')
        doc = self.archive_root / 'documents' / 'letters' / 'note_s-1111111111.txt'
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text('a letter', encoding='utf-8')
        for rel, role in (('photos/1880/pic.jpg', 'front'),
                          ('documents/letters/note_s-1111111111.txt', 'transcript')):
            self.conn.execute(
                'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
                ('s-1111111111', rel, role))

    def _site_anchors(self):
        """Every anchor in every built page as (page, href, tag).

        A per-template assertion only ever guards the templates someone
        remembered to write one for - and #122 was one template's attribute
        missing from four places at once. This sweeps the built output instead,
        so a new template, or a new link in an old one, is covered the day it
        lands rather than the day someone notices."""
        anchors = []
        for page in sorted(self.out_dir.rglob('*.html')):
            page_html = page.read_text(encoding='utf-8')
            for tag in re.findall(r'<a\b[^>]*>', page_html, re.I):
                href = re.search(r'href="([^"]*)"', tag)
                anchors.append((page.relative_to(self.out_dir).as_posix(),
                                href.group(1) if href else '', tag))
        return anchors

    @staticmethod
    def _link_kind(href):
        """'page', 'external' or 'file' for one href.

        'page' covers same-site navigation and in-page fragments (including the
        workbench's `href="#"` stubs, which JavaScript intercepts). 'external'
        is an absolute http/https/mailto URL - the place page's "View on
        OpenStreetMap" link is the only one the site emits today, and #122
        explicitly scoped itself to file links, so this sweep records its shape
        without ruling on it. Everything else is a path to a real file on disk.
        """
        target = href.split('#')[0]
        if target.lower().startswith(('http://', 'https://', 'mailto:')):
            return 'external'
        if target == '' or target.endswith('.html'):
            return 'page'
        return 'file'

    def test_no_site_page_link_opens_in_a_new_tab(self):
        # Spraying target="_blank" across the templates would "fix" #122 and
        # break the reading experience it protects: navigating the tree must
        # never litter a visitor's browser with tabs.
        self._seed_a_page_of_every_kind()
        self._run(linked=True)
        anchors = self._site_anchors()
        self.assertTrue(anchors, 'the build emitted no anchors to check')
        for page, href, tag in anchors:
            if self._link_kind(href) == 'page':
                self.assertNotIn('target="_blank"', tag,
                                 f'{page}: same-site link {href!r} opens a new tab')

    def test_every_file_link_in_the_built_site_opens_in_a_new_tab(self):
        # Positive half, swept over the whole build rather than named template
        # by named template - the shape of the miss #122 reported.
        self._seed_a_page_of_every_kind()
        self._run(linked=True)
        file_links = [(p, h, t) for p, h, t in self._site_anchors()
                      if self._link_kind(h) == 'file']
        self.assertTrue(file_links, 'the build emitted no file links to check')
        for page, href, tag in file_links:
            self.assertIn('target="_blank"', tag,
                          f'{page}: file link {href!r} replaces the page instead of opening a tab')
            self.assertIn('rel="noopener"', tag,
                          f'{page}: file link {href!r} opens a new tab without rel="noopener"')

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_standalone_build_keeps_the_same_two_sided_rule(self):
        # --standalone links at EXIF-stripped derivatives under media/ instead
        # of the originals: different hrefs, same two-sided rule. The published
        # snapshot is the build a visitor actually receives, so it gets its own
        # pass rather than inheriting --linked's.
        from PIL import Image
        self._seed_a_page_of_every_kind()
        # A real image this time: standalone omits anything Pillow cannot open,
        # and an omitted image emits no link at all - nothing to assert on.
        Image.new('RGB', (60, 40), (10, 20, 30)).save(
            self.archive_root / 'photos' / '1880' / 'pic.jpg')
        self._run(linked=False)
        checked = 0
        for page, href, tag in self._site_anchors():
            kind = self._link_kind(href)
            if kind == 'page':
                self.assertNotIn('target="_blank"', tag,
                                 f'{page}: same-site link {href!r} opens a new tab')
            elif kind == 'file':
                checked += 1
                self.assertIn('target="_blank"', tag, f'{page}: file link {href!r}')
                self.assertIn('rel="noopener"', tag, f'{page}: file link {href!r}')
        self.assertTrue(checked, 'the standalone build emitted no file links to check')


class PlacePageTests(_Base):
    def _seed_place(self, lid, name, *, hierarchy=None, within=None, lat=None, lon=None,
                    alt_names=(), history=()):
        self.conn.execute(
            'INSERT INTO places(id, name, hierarchy, within, lat, lon) VALUES (?,?,?,?,?,?)',
            (lid, name, hierarchy, within, lat, lon))
        for a in alt_names:
            self.conn.execute('INSERT INTO place_names(place_id, alt_name) VALUES (?,?)', (lid, a))
        for period, hier in history:
            self.conn.execute(
                'INSERT INTO place_history(place_id, period_edtf, date_min, hierarchy) VALUES (?,?,?,?)',
                (lid, period, (period or '')[:4], hier))

    def _seed_claim_at_place(self, cid, sid, lid, value, persons):
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, value, status, place_id, date_edtf, date_min) '
            "VALUES (?,?,?,?,?,?,?,?)",
            (cid, sid, 'residence', value, 'accepted', lid, '1880', '1880'))
        for pos, pid in enumerate(persons):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position) VALUES (?,?,?)', (cid, pid, pos))

    def test_place_page_sections(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', 'Census')
        self._seed_place('l-1111111111', 'Fairview', hierarchy='Fairview, Kansas',
                         lat=39.8, lon=-95.6, alt_names=('Fairview City',),
                         history=(('1858/1861', 'Fairview, Kansas Territory'),))
        self._seed_place('l-2222222222', 'Fairview Cemetery', within='l-1111111111')
        self._seed_claim_at_place('c-1111111111', 's-1111111111', 'l-1111111111',
                                  'Lived in Fairview', ('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('places/l-1111111111.html')
        self.assertIn('Fairview', html)
        self.assertIn('openstreetmap.org', html)                 # coords → map URL, no embed
        self.assertIn('Fairview City', html)                     # alt name
        self.assertIn('Kansas Territory', html)                  # dated history
        self.assertIn('Lived in Fairview', html)                 # claim naming the place
        self.assertIn('../persons/p-aaaaaaaaaa.html', html)      # associated person linked
        self.assertIn('"l-2222222222.html"', html)               # micro-place (within:) linked (same places/ dir)

    def test_l_token_links_to_place_page(self):
        self._seed_place('l-1111111111', 'Fairview')
        self._seed_person('p-aaaaaaaaaa', 'Jane',
                          body='# Jane\n## Biography\nBorn in [L-1111111111].\n')
        self._run(linked=True)
        self.assertIn('../places/l-1111111111.html', self._read('persons/p-aaaaaaaaaa.html'))

    def test_claim_place_column_links_to_place_page(self):
        # Symmetry fix: a claim's place cell links to the place page when the
        # claim carries a registered place_id (not just prose [L-id] tokens).
        # #127 note: the value deliberately does NOT already name "Fairview",
        # so this pins the plain case - a trailing place mention, linked. When
        # the sentence does name the place the timeline moves that link into
        # the sentence instead (see
        # test_timeline_keeps_place_link_on_the_words_the_sentence_uses); the
        # source page's claims table is a table cell, not prose, so it is
        # unaffected either way.
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._seed_place('l-1111111111', 'Fairview')
        self._seed_claim_at_place('c-1111111111', 's-1111111111', 'l-1111111111',
                                  'Lived with her family', ('p-aaaaaaaaaa',))
        self._run(linked=True)
        # Source page claims table and the person timeline both link the place.
        self.assertIn('../places/l-1111111111.html', self._read('sources/s-1111111111.html'))
        self.assertIn('../places/l-1111111111.html', self._read('persons/p-aaaaaaaaaa.html'))

    def test_timeline_keeps_place_link_on_the_words_the_sentence_uses(self):
        # #127 must not cost the reader the place-page link. When the claim's
        # own sentence already states the place, the timeline prints that
        # place once - and the words already in the sentence carry the link,
        # so the place page is still one click from the person page (the
        # symmetry _place_html was added for). Dropping the trailing tag AND
        # its link would have left this person page with no route to the
        # place at all.
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._seed_place('l-1111111111', 'Fairview')
        self._seed_claim_at_place('c-1111111111', 's-1111111111', 'l-1111111111',
                                  'Lived in Fairview', ('p-aaaaaaaaaa',))
        self._run(linked=True)
        person_html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Lived in <a href="../places/l-1111111111.html">Fairview</a>', person_html)
        self.assertEqual(person_html.count('Fairview'), 1)          # named once, not doubled
        self.assertNotIn(' at <a href="../places/l-1111111111.html">', person_html)

    def test_timeline_value_is_escaped_around_the_place_link(self):
        # Linking the place inside the sentence moved the timeline value's
        # escaping out of Jinja's autoescape and into site.py, which is worth
        # a guard of its own: the value is split into three pieces around the
        # place name and every piece has to be escaped, or a claim someone
        # typed with angle brackets in it becomes markup.
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._seed_place('l-1111111111', 'Fairview')
        self._seed_claim_at_place('c-1111111111', 's-1111111111', 'l-1111111111',
                                  '<b>Lived</b> in Fairview & <i>farmed</i>',
                                  ('p-aaaaaaaaaa',))
        self._run(linked=True)
        person_html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('&lt;b&gt;Lived&lt;/b&gt; in '
                      '<a href="../places/l-1111111111.html">Fairview</a> '
                      '&amp; &lt;i&gt;farmed&lt;/i&gt;', person_html)

    def test_timeline_place_name_inside_a_longer_word_still_renders(self):
        # #127's suppression is whole-word: "Hampton" sits inside
        # "Southampton", and a plain substring test read the sentence as
        # already naming the place - silently dropping a real, different
        # place (and its link) off the page. Losing a fact is worse than
        # repeating one, so this claim keeps its trailing place.
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._seed_place('l-1111111111', 'Hampton')
        self._seed_claim_at_place('c-1111111111', 's-1111111111', 'l-1111111111',
                                  'Married at Southampton', ('p-aaaaaaaaaa',))
        self._run(linked=True)
        person_html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Married at Southampton at '
                      '<a href="../places/l-1111111111.html">Hampton</a>', person_html)

    def test_timeline_place_partly_named_by_the_sentence_is_still_redundant(self):
        # #127 reopened: this used to be the OLD, too-strict policy's pinned
        # case - a sentence naming only the town ("Moved to Millbrook") did
        # NOT make the registry's fuller "Millbrook, Dutchess County, New
        # York" redundant, so the timeline printed "Moved to Millbrook at
        # Millbrook, Dutchess County, New York" - the reader sees "Millbrook"
        # twice, which is exactly the reopened bug report's shape (a city or
        # a home named once naturally, then repeated verbatim as the leading
        # word of the trailing tag). The county/state qualifier is real new
        # information, but forcing it onto the sentence is worse than the
        # near-duplicate it produces; the fuller name is still one click away
        # via the place-page link, which the caller moves onto "Millbrook" in
        # the sentence instead of losing it.
        #
        # Finding 1 follow-up (#127 reopened round-3 review): this is the
        # REGISTERED, linkable counterpart to
        # test_timeline_place_tag_shows_remainder_for_unlinked_hierarchy_place
        # above, which seeds the identical label/sentence with no place_id.
        # Full suppression stays correct here specifically because the
        # place page exists to send the reader to - see
        # `_place_trailing_remainder`'s docstring for why that link is what
        # makes suppression safe only in this (place_id-present) case.
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._seed_place('l-1111111111', 'Millbrook, Dutchess County, New York')
        self._seed_claim_at_place('c-1111111111', 's-1111111111', 'l-1111111111',
                                  'Moved to Millbrook', ('p-aaaaaaaaaa',))
        self._run(linked=True)
        person_html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Moved to <a href="../places/l-1111111111.html">Millbrook</a>',
                      person_html)
        self.assertNotIn('Dutchess County', person_html)   # qualifier not force-printed
        self.assertEqual(person_html.count('Millbrook'), 1)   # named once, not doubled

    def test_freetext_place_without_id_is_not_linked(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        # place_text but no place_id → plain text, no link, no crash.
        self.conn.execute(
            "INSERT INTO claims(id, source_id, type, value, status, place_text) VALUES (?,?,?,?,?,?)",
            ('c-1111111111', 's-1111111111', 'residence', 'Somewhere', 'accepted', 'Old Country'))
        self.conn.execute('INSERT INTO claim_persons(claim_id, person_id, position) VALUES (?,?,?)',
                          ('c-1111111111', 'p-aaaaaaaaaa', 0))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('Old Country', html)
        self.assertNotIn('places/', html.split('Old Country')[0][-200:])  # no place link around it

    def test_events_table_value_scrubs_claim_id_paren(self):
        # #144 review finding 4: build_place_page passed the raw claim value
        # straight into the "Events here" table - the same unscrubbed-
        # internal-encoding gap as the source page's claims table, on the
        # place page's own claims render.
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._seed_place('l-1111111111', 'Fairview')
        self._seed_claim_at_place('c-1111111111', 's-1111111111', 'l-1111111111',
                                  'Lived in Fairview (C-4kx9m2p7qr) per the deed',
                                  ('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('places/l-1111111111.html')
        self.assertIn('Lived in Fairview per the deed', html)
        self.assertNotIn('C-4kx9m2p7qr', html)


class DiscoveriesTests(_Base):
    def _write_discoveries(self, text):
        path = self.archive_root / 'notes' / 'discoveries.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def test_discoveries_links_and_redacts(self):
        self._seed_person('p-aaaaaaaaaa', 'Dead Dan', living='false')
        self._seed_person('p-bbbbbbbbbb', 'Living Larry', living='true')
        self._seed_source('s-1111111111', 'A Source')
        self._write_discoveries(
            '# Discoveries Log\n\n'
            '## 2026-06-01\nConfirmed [P-aaaaaaaaaa] via [S-1111111111].\n\n'
            '## 2026-06-02\nNew lead on [P-bbbbbbbbbb].\n')
        self._run(linked=False)
        html = self._read('discoveries.html')
        self.assertIn('persons/p-aaaaaaaaaa.html', html)                    # dead person linked
        self.assertIn('sources/s-1111111111.html', html)                    # source linked
        self.assertIn(site._LIVING_LABEL, html)                              # living person redacted
        self.assertNotIn('persons/p-bbbbbbbbbb.html', html)                  # ...and not linked

    def test_discoveries_teaser_on_home(self):
        self._seed_person('p-aaaaaaaaaa', 'Dan')
        self._write_discoveries(
            '# Discoveries Log\n\n## 2026-06-01\nFirst win.\n\n## 2026-06-02\nSecond win.\n')
        self._run(linked=True)
        home = self._read('index.html')
        self.assertIn('Recent discoveries', home)
        self.assertIn('Second win.', home)
        self.assertIn('discoveries.html', home)                  # link to full page

    def test_missing_discoveries_file_is_fine(self):
        self._seed_person('p-aaaaaaaaaa', 'Dan')
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')
        self.assertIn('No discoveries', self._read('discoveries.html'))
        self.assertNotIn('Recent discoveries', self._read('index.html'))

    def test_discoveries_nav_link_present_with_zero_entries(self):
        # Issue #121: build_discoveries_page() always builds discoveries.html,
        # but with zero entries the home teaser above (its only other inbound
        # link) renders nothing at all - so the persistent site nav is the
        # page's sole route in on a fresh or quiet archive. Check it from both
        # a top-level page (home) and a nested one (a person page), since the
        # two use different root_prefix values.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._run(linked=True)
        self.assertTrue((self.out_dir / 'discoveries.html').is_file())
        for relpath, prefix in (('index.html', '.'), ('persons/p-aaaaaaaaaa.html', '..')):
            html = self._read(relpath)
            nav_block = html[html.index('site-nav'):html.index('</nav>')]
            self.assertIn(f'href="{prefix}/discoveries.html"', nav_block, relpath)
            self.assertIn('Discoveries', nav_block, relpath)

    def test_cp1252_discoveries_file_reports_instead_of_crashing(self):
        # #68 in the one place in site.py that never went through
        # `read_record`: this file was read with a plain `read_text` guarded by
        # `except OSError`, and `UnicodeDecodeError` is a ValueError - so a
        # discoveries.md saved in another codepage raised straight out of
        # `run_site`, after every page had already been written. Not a wording
        # fix like the record reads: `run_site`'s contract is to RETURN a
        # Result, so the CLI turned it into `fha.py`'s catch-all ("something
        # went wrong: 'utf-8' codec can't decode byte 0xf3...", exit 3 - raw
        # codec text and a dead end) and serve's workbench snapshot rebuild,
        # which calls `run_site` directly and unguarded, got the exception.
        self._seed_person('p-aaaaaaaaaa', 'Dan')
        path = self.archive_root / 'notes' / 'discoveries.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            '# Discoveries Log\n\n## 2026-06-01\nFound in Krak\u00f3w.\n'.encode('cp1252'))
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')
        # Fails closed: nothing from the file reaches either surface.
        self.assertIn('No discoveries', self._read('discoveries.html'))
        self.assertNotIn('Recent discoveries', self._read('index.html'))
        messages = res['messages']
        hits = [m for m in messages if 'notes/discoveries.md' in m]
        self.assertTrue(
            any("isn't saved as UTF-8 text" in m for m in hits), messages)
        self.assertFalse(any('codec' in m for m in messages), messages)
        # Both callers (the discoveries page and the home teaser) go through
        # the same memoized read, so one broken file earns exactly one line.
        self.assertEqual(len(hits), 1, messages)

    def test_missing_discoveries_file_stays_silent(self):
        # The other half of the split `read_text_or_report` makes: an absent
        # (or unopenable) discoveries.md is ordinary and must NOT warn - only
        # a file that opens and will not decode does.
        self._seed_person('p-aaaaaaaaaa', 'Dan')
        res = self._run(linked=True)
        self.assertFalse(
            [m for m in res['messages'] if 'discoveries' in m], res['messages'])

    def test_ambiguous_name_link_to_living_is_redacted(self):
        # Two people share a name; one is living. The clash drops the name from
        # the single-id alias_map, so `[[John Smith]]` fails to resolve - it must
        # fail closed (redact), not publish the living person's name verbatim.
        self._seed_person('p-aaaaaaaaaa', 'John Smith', living='false', surname='Smith')
        self._seed_person('p-bbbbbbbbbb', 'John Smith', living='true', surname='Smith')
        for pid in ('p-aaaaaaaaaa', 'p-bbbbbbbbbb'):
            self.conn.execute("INSERT INTO aliases(alias, canonical_id, kind) VALUES (?,?,?)",
                              ('john smith', pid, 'name'))
        self._write_discoveries('# Discoveries Log\n\n## 2026-06-01\nA lead on [[John Smith]].\n')
        self._run(linked=False)
        html = self._read('discoveries.html')
        self.assertIn(site._LIVING_LABEL, html)          # redacted, not leaked
        self.assertNotIn('John Smith', html)             # the name never appears

    def test_unaccepted_draft_excluded_from_discoveries(self):
        # The standalone site is external output, so an AI-DRAFT block in
        # discoveries.md must be stripped just like person prose is.
        self._seed_person('p-aaaaaaaaaa', 'Dan')
        self._write_discoveries(
            '# Discoveries Log\n\n'
            '## 2026-06-01\nA published finding.\n\n'
            '## 2026-06-02\nAn unreviewed draft lead.\n\n'
            '<!-- AI-DRAFT 2026-07-01 claude-x - drafted -->\n')
        self._run(linked=False)
        html = self._read('discoveries.html')
        self.assertIn('A published finding.', html)
        self.assertNotIn('An unreviewed draft lead.', html)
        self.assertNotIn('AI-DRAFT', html)

    def test_damaged_draft_marker_withholds_discoveries(self):
        # Fail closed: an unterminated marker withholds the whole page rather
        # than leaking half-parsed draft text or a raw marker.
        self._seed_person('p-aaaaaaaaaa', 'Dan')
        self._write_discoveries(
            '# Discoveries Log\n\n## 2026-06-01\nA finding.\n\n<!-- AI-DRAFT missing its close\n')
        self._run(linked=False)
        html = self._read('discoveries.html')
        self.assertNotIn('A finding.', html)
        self.assertNotIn('AI-DRAFT', html)


class HomePageTests(_Base):
    def test_surname_az_index(self):
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', surname='Hartley')
        self._seed_person('p-bbbbbbbbbb', 'James Bradford', surname='Bradford')
        self._run(linked=True)
        home = self._read('index.html')
        self.assertIn('<h3>B</h3>', home)
        self.assertIn('<h3>H</h3>', home)
        self.assertLess(home.index('<h3>B</h3>'), home.index('<h3>H</h3>'))   # A-Z order
        self.assertIn('James Bradford', home)
        self.assertIn('Thomas Hartley', home)

    def test_home_omits_living_under_standalone(self):
        self._seed_person('p-aaaaaaaaaa', 'Dead Dan', living='false', surname='Dan')
        self._seed_person('p-bbbbbbbbbb', 'Living Larry', living='true', surname='Larry')
        self._run(linked=False)
        home = self._read('index.html')
        self.assertIn('Dead Dan', home)
        self.assertNotIn('Living Larry', home)                   # living person omitted from index
        self.assertNotIn('persons/p-bbbbbbbbbb.html', home)

    def test_cp1252_home_md_falls_back_to_the_default_intro(self):
        # notes/home.md is read unconditionally (not gated by the privacy
        # pre-pass), so a real decode failure here always reaches its own
        # try/except - the guard `read_record(home_md)` had before this fix
        # (no `on_decode_error`, `except Exception:` not even binding `e`),
        # which fell back correctly but said nothing about why.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', surname='Hartley')
        home_md = self.archive_root / 'notes' / 'home.md'
        home_md.parent.mkdir(parents=True, exist_ok=True)
        home_md.write_bytes('Welcome to the Kraków family archive.\n'.encode('cp1252'))
        res = self._run(linked=False)
        home = self._read('index.html')
        self.assertIn('safe-to-share snapshot', home)      # default intro, not garbled text
        self.assertNotIn('Kraków', home)
        messages = res['messages']
        self.assertTrue(
            any('notes/home.md' in m and "isn't saved as UTF-8 text" in m
                for m in messages), messages)
        self.assertFalse(any('codec' in m for m in messages), messages)


class StandaloneRedactionAuditTests(_Base):
    """M8.4: no standalone page may link to a person/source page that was not
    generated. Build a mixed archive, then crawl every emitted page for hrefs
    into persons/ and sources/ and assert each target exists on disk."""

    def test_no_dangling_links_to_redacted_pages(self):
        import re as _re
        self._seed_person('p-aaaaaaaaaa', 'Dead Dan', living='false',
                          body='# Dan\n## Biography\nKnew [P-bbbbbbbbbb]; see [S-2222222222] and [L-1111111111].\n')
        self._seed_person('p-bbbbbbbbbb', 'Living Larry', living='true')
        self._seed_source('s-1111111111', 'Public Source', people=('p-aaaaaaaaaa',))
        self._seed_source('s-2222222222', 'Restricted Source', restricted=1, people=('p-aaaaaaaaaa',))
        self.conn.execute(
            'INSERT INTO places(id, name) VALUES (?,?)', ('l-1111111111', 'Fairview'))
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, value, status, place_id) VALUES (?,?,?,?,?,?)',
            ('c-1111111111', 's-1111111111', 'residence', 'Lived here', 'accepted', 'l-1111111111'))
        self.conn.execute(
            'INSERT INTO claim_persons(claim_id, person_id, position) VALUES (?,?,?)',
            ('c-1111111111', 'p-bbbbbbbbbb', 0))      # a living person on a claim
        self._run(linked=False)
        href_re = _re.compile(r'href="((?:\.\./)?(?:persons|sources)/[a-z0-9-]+\.html)"')
        checked = 0
        for page in self.out_dir.rglob('*.html'):
            text = page.read_text(encoding='utf-8')
            for m in href_re.finditer(text):
                target = (page.parent / m.group(1)).resolve()
                self.assertTrue(target.exists(),
                                f'{page.name} links to missing page {m.group(1)}')
                checked += 1
        self.assertGreater(checked, 0)                # the crawl actually found links
        # The redacted source/person pages must not exist at all.
        self.assertFalse((self.out_dir / 'sources' / 's-2222222222.html').exists())
        self.assertFalse((self.out_dir / 'persons' / 'p-bbbbbbbbbb.html').exists())
        # The tree JSON artifacts are another surface: no node url may point at a
        # person page that wasn't generated, and no living person may be named.
        for data in self.out_dir.glob('data/*.json'):
            tree = json.loads(data.read_text(encoding='utf-8'))
            for n in tree['nodes']:
                if n['url']:
                    self.assertTrue((self.out_dir / 'persons' / Path(n['url']).name).exists(),
                                    f'{data.name} node url -> missing page {n["url"]}')


class TreeTests(_Base):
    """M8.5, re-seeded by #115: interactive trees - vendored renderer +
    adapter, build-time neutral tree JSON. The home page no longer carries
    this renderer at all (see HomePedigreeTests for its #115 replacement,
    the static ancestor pedigree); the descendant explorer moved to a
    per-person opt-in link, seeded on that person rather than once on the
    root's apex. Ancestor pedigree per curated person is unchanged."""

    def _seed_rels_chain(self):
        # Grandparent -> parent -> child (root_person). Edges both directions,
        # matching index.py's derivation (X parent Y = Y is X's parent;
        # X child Y = Y is X's child).
        self._seed_person('p-aaaaaaaaaa', 'Child Carl', surname='Carl')
        self._seed_person('p-bbbbbbbbbb', 'Parent Pat', surname='Pat')
        self._seed_person('p-cccccccccc', 'Grandparent Gus', surname='Gus')
        for child, parent in (('p-aaaaaaaaaa', 'p-bbbbbbbbbb'), ('p-bbbbbbbbbb', 'p-cccccccccc')):
            self.conn.execute(
                'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
                (child, 'parent', parent, 'c-1111111111'))
            self.conn.execute(
                'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
                (parent, 'child', child, 'c-1111111111'))
        (self.archive_root / 'fha.yaml').write_text(
            'roots: {}\nroot_person: P-aaaaaaaaaa\n', encoding='utf-8')

    def test_vendor_copied_and_offline(self):
        self._seed_person('p-aaaaaaaaaa', 'Solo')
        self._run(linked=True)
        self.assertTrue((self.out_dir / 'vendor' / 'fha-tree.js').exists())
        self.assertTrue((self.out_dir / 'vendor' / 'tree-adapter.js').exists())
        # No CDN / remote-loading references in the vendored bundle. The SVG
        # namespace URI (http://www.w3.org/2000/svg) is a required constant, not
        # a network fetch, so it is excluded before the check.
        for js in (self.out_dir / 'vendor').glob('*.js'):
            text = js.read_text(encoding='utf-8').replace('http://www.w3.org/2000/svg', '')
            self.assertNotIn('http://', text)
            self.assertNotIn('https://', text)

    def test_person_page_descendant_tree_opt_in_link(self):
        # #115: the descendant explorer is demoted from the home page to a
        # per-person opt-in link, seeded on THAT person (not the root's
        # apex) - Grandparent Gus has descendants (Pat, then Carl), so
        # Gus's OWN page carries the link/section; the data artifact is now
        # named for Gus, not for whatever the old apex-walk picked.
        self._seed_rels_chain()
        self._run(linked=True)
        data = self.out_dir / 'data' / 'tree_p-cccccccccc_descendants.json'
        self.assertTrue(data.exists())
        tree = json.loads(data.read_text(encoding='utf-8'))
        self.assertEqual(tree['seed'], 'P-cccccccccc')
        self.assertEqual(tree['mode'], 'descendants')
        ids = {n['p_id'] for n in tree['nodes']}
        self.assertEqual(ids, {'P-aaaaaaaaaa', 'P-bbbbbbbbbb', 'P-cccccccccc'})  # whole line
        # Gus's own page embeds the tree data + includes both vendor scripts,
        # behind the opt-in disclosure - the home page carries neither.
        gus_page = self._read('persons/p-cccccccccc.html')
        self.assertIn('fha-tree-data', gus_page)
        self.assertIn('vendor/fha-tree.js', gus_page)
        self.assertIn('vendor/tree-adapter.js', gus_page)
        self.assertIn('See descendants of Grandparent Gus', gus_page)
        home = self._read('index.html')
        self.assertNotIn('fha-tree-data', home)
        self.assertNotIn('vendor/tree-adapter.js', home)   # only the descendant pipeline needs the adapter

    def test_person_page_no_descendant_link_for_a_leaf(self):
        # Child Carl has no children in this fixture - no descendant edges at
        # all, so `_make_tree_ctx` returns None and the section/link simply
        # does not render (matches the pre-#115 "no edges -> no tree" rule).
        self._seed_rels_chain()
        self._run(linked=True)
        carl_page = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('fha-tree-data', carl_page)
        self.assertNotIn('See descendants of', carl_page)

    def test_person_ancestor_pedigree(self):
        self._seed_rels_chain()
        self._run(linked=True)
        page = self._read('persons/p-aaaaaaaaaa.html')
        # The person page now carries a static horizontal pedigree SVG (subject +
        # parents + grandparents), not the interactive descendant renderer.
        self.assertIn('class="pedigree"', page)
        for name in ('Child Carl', 'Parent Pat', 'Grandparent Gus'):   # 3 generations
            self.assertIn(name, page)
        self.assertNotIn('fha-tree-data', page)                        # no interactive tree here
        self.assertFalse((self.out_dir / 'data' / 'tree_p-aaaaaaaaaa_ancestors.json').exists())

    def test_tree_drops_a_living_node_and_everyone_below_it(self):
        # #115: the descendants-mode tree pipeline now lives on the seed
        # person's OWN page, and `build_person_page` only ever runs for a
        # page-owning (so, in standalone, already non-redacted) person - a
        # living SEED's tree is therefore never built at all now (no page,
        # no call). A living node reached mid-tree, by contrast, is dropped
        # OUTRIGHT per the existing `_build_tree_data` rule (never a 'Living
        # Person' placeholder - only a tree's own seed bypasses that gate),
        # and since the BFS only continues through nodes it enqueues, the
        # whole line below a dropped node goes with it. A branching fixture
        # (Gus has TWO children) keeps at least one edge alive so the tree
        # still gets built at all (`_make_tree_ctx` returns None on zero
        # edges) while proving the living branch (Pat, then Carl behind her)
        # is severed and the other branch (Uncle Umberto) is untouched.
        self._seed_person('p-cccccccccc', 'Grandparent Gus', surname='Gus')
        self._seed_person('p-bbbbbbbbbb', 'Parent Pat', surname='Pat', living='true')
        self._seed_person('p-aaaaaaaaaa', 'Child Carl', surname='Carl')
        self._seed_person('p-dddddddddd', 'Uncle Umberto', surname='Umberto')
        for parent, child in (('p-cccccccccc', 'p-bbbbbbbbbb'), ('p-bbbbbbbbbb', 'p-aaaaaaaaaa'),
                              ('p-cccccccccc', 'p-dddddddddd')):
            self._seed_rel(parent, 'child', child)
            self._seed_rel(child, 'parent', parent)
        (self.archive_root / 'fha.yaml').write_text(
            'roots: {}\nroot_person: P-cccccccccc\n', encoding='utf-8')
        self._run(linked=False)
        tree = json.loads(
            (self.out_dir / 'data' / 'tree_p-cccccccccc_descendants.json').read_text(encoding='utf-8'))
        ids = {n['p_id'] for n in tree['nodes']}
        # Gus and Umberto survive; Pat (living) and Carl (behind her) do not.
        self.assertEqual(ids, {'P-cccccccccc', 'P-dddddddddd'})
        # Every node url that is set must point to a generated person page.
        for n in tree['nodes']:
            if n['url']:
                self.assertTrue((self.out_dir / 'persons' / Path(n['url']).name).exists())

    def test_no_tree_without_root_person(self):
        self._seed_person('p-aaaaaaaaaa', 'Solo')   # no fha.yaml root_person, no edges
        self._run(linked=True)
        home = self._read('index.html')
        self.assertNotIn('fha-tree-data', home)
        self.assertNotIn('id="fha-pedigree"', home)   # #115: no home pedigree either

    def test_home_tree_bounds_initial_paint(self):
        # #115: the bounded initialDepth belongs to the descendant explorer,
        # which now lives on a person's own page (Gus has descendants), not
        # on the home page (a static SVG pedigree - no interactive-renderer
        # option of any kind, including initialDepth).
        self._seed_rels_chain()
        self._run(linked=True)
        home = self._read('index.html')
        self.assertNotIn('initialDepth', home)
        gus = self._read('persons/p-cccccccccc.html')
        self.assertIn('initialDepth: 4', gus)
        carl = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('initialDepth', carl)          # a leaf: no descendants, no tree at all
        self.assertIn('class="pedigree"', carl)          # the static ancestor chart is unaffected

    def test_relationship_cycle_terminates(self):
        # A cousin-marriage style cycle must not loop forever; the BFS visited
        # set bounds it and the node set is deduplicated. Exercised via a
        # curated person's own descendant tree (#115: the home page no
        # longer builds one at all).
        self._seed_person('p-aaaaaaaaaa', 'A')
        self._seed_person('p-bbbbbbbbbb', 'B')
        for a, b in (('p-aaaaaaaaaa', 'p-bbbbbbbbbb'), ('p-bbbbbbbbbb', 'p-aaaaaaaaaa')):
            self.conn.execute(
                'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
                (a, 'parent', b, 'c-1111111111'))
            self.conn.execute(
                'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
                (a, 'child', b, 'c-1111111111'))
        (self.archive_root / 'fha.yaml').write_text(
            'roots: {}\nroot_person: P-aaaaaaaaaa\n', encoding='utf-8')
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')                          # terminates
        artifacts = list((self.out_dir / 'data').glob('tree_*_descendants.json'))
        self.assertTrue(artifacts)
        ids = [n['p_id'] for n in json.loads(artifacts[0].read_text(encoding='utf-8'))['nodes']]
        self.assertEqual(sorted(set(ids)), ['P-aaaaaaaaaa', 'P-bbbbbbbbbb'])
        self.assertEqual(len(ids), len(set(ids)))                      # each node once

    def test_descendant_tree_data_stays_complete_beyond_old_bound(self):
        # #152 follow-up review fix (P2, finding 1): the first cut of the
        # #152 performance fix used `_DESCENDANT_TREE_MAX_HOPS` (then 12) as
        # a hard TRUNCATION bound on the server-side BFS, silently dropping
        # every descendant past 12 hops from both the embedded tree and the
        # reusable `data/tree_*.json` artifact - contradicting
        # tools/README.md's promise that "only the initial paint is bounded
        # while the data stays complete and the reader expands forward."
        # Build a chain well past that old 12-hop bound (but nowhere near
        # today's much larger safety-net value) and confirm every generation
        # survives in the artifact - this must fail against the pre-fix hard
        # truncation.
        depth = 20
        ids = []
        prev = None
        for g in range(depth):
            # Fixed-width numeric suffix (zero-padded to 2 digits) - a
            # variable-width `f'gen{g}'` + zero-fill collides once g reaches
            # double digits (e.g. 'gen1' and 'gen10' both zero-pad to the
            # same 10-char string), which this loop's depth (20) reaches.
            pid = f'p-g{g:02d}' + '0' * 7
            self._seed_person(pid, f'Descendant Gen{g}', surname=f'Gen{g}')
            if prev is not None:
                self._seed_rel(prev, 'child', pid)
                self._seed_rel(pid, 'parent', prev)
            ids.append(pid)
            prev = pid
        (self.archive_root / 'fha.yaml').write_text(
            f'roots: {{}}\nroot_person: P-{ids[0][2:]}\n', encoding='utf-8')
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')
        data = json.loads(
            (self.out_dir / 'data' / f'tree_{ids[0]}_descendants.json').read_text(encoding='utf-8'))
        got_ids = {n['p_id'] for n in data['nodes']}
        expected_ids = {f'P-{pid[2:]}' for pid in ids}
        self.assertEqual(got_ids, expected_ids)          # every generation present, none dropped
        self.assertFalse(any('truncated' in m.lower() for m in res['messages']))

    def test_descendant_tree_bfs_safety_net_still_truncates_and_warns(self):
        # #152 follow-up review fix (P2, finding 1): `_DESCENDANT_TREE_MAX_
        # HOPS` is now a generous safety net (real archives should never hit
        # it), not a display bound - but it must still actually stop a
        # pathological walk, and must say so loudly rather than silently
        # dropping generations. Patch it down to a small value to exercise
        # that safety-net path without building hundreds of fixture people.
        ids = []
        prev = None
        for g in range(5):
            pid = f'p-g{g:02d}' + '0' * 7
            self._seed_person(pid, f'Descendant Gen{g}', surname=f'Gen{g}')
            if prev is not None:
                self._seed_rel(prev, 'child', pid)
                self._seed_rel(pid, 'parent', prev)
            ids.append(pid)
            prev = pid
        (self.archive_root / 'fha.yaml').write_text(
            f'roots: {{}}\nroot_person: P-{ids[0][2:]}\n', encoding='utf-8')
        with unittest.mock.patch.object(site, '_DESCENDANT_TREE_MAX_HOPS', 2):
            res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')                          # terminates, not an error
        data = json.loads(
            (self.out_dir / 'data' / f'tree_{ids[0]}_descendants.json').read_text(encoding='utf-8'))
        got_ids = {n['p_id'] for n in data['nodes']}
        within_bound = f'P-{ids[2][2:]}'      # hop == 2: still walked
        beyond_bound = f'P-{ids[3][2:]}'      # hop == 3: never reached, safety net stopped it
        self.assertIn(within_bound, got_ids)
        self.assertNotIn(beyond_bound, got_ids)
        self.assertTrue(any('truncated' in m.lower() and 'descendants' in m.lower()
                            for m in res['messages']))                # warned, not silent

    def test_descendant_tree_render_deferred_until_details_opens(self):
        # #152 review fix (P2): the descendants <details> is closed by
        # default (person.html), so calling FhaTree.render() unconditionally
        # at page load - the pre-fix behavior - fed it a zero-width
        # container (a collapsed <details> is not laid out at all), which
        # left the chart badly fit until the reader pressed Fit by hand.
        # The emitted script must now wait for the enclosing <details>'s own
        # `toggle` event, which only fires once it is genuinely open and has
        # real layout dimensions.
        self._seed_rels_chain()
        self._run(linked=True)
        gus_page = self._read('persons/p-cccccccccc.html')
        self.assertIn('function renderTree()', gus_page)
        self.assertIn("addEventListener('toggle'", gus_page)

    def test_mistyped_root_person_warns(self):
        self._seed_person('p-aaaaaaaaaa', 'Real Person')
        (self.archive_root / 'fha.yaml').write_text(
            'roots: {}\nroot_person: P-zzzzzzzzzz\n', encoding='utf-8')   # not in index
        res = self._run(linked=True)
        self.assertTrue(any('root_person' in m and 'not in the index' in m for m in res['messages']))
        home = self._read('index.html')
        self.assertNotIn('fha-tree-data', home)
        self.assertNotIn('id="fha-pedigree"', home)   # #115: no home pedigree either


class PedigreeGeometryGuardTests(unittest.TestCase):
    """#115: `row_index()`'s generalization from a hardcoded 2-generation
    formula to an arbitrary-depth closed form is the single highest-risk
    piece of this whole feature - this project's chart-geometry code has
    shipped real off-by-one/collision defects before (#119/#120, in these
    same functions). This class is the regression guard the issue asked
    for: it pins the EXACT row positions the pre-#115 hardcoded row_index()
    produced at the default 2-generation depth (subject 1.5, parents
    0.5/2.5, grandparents 0/1/2/3, in ROW=72 units off base=PAD+CH/2=39 -
    verified against the shipped pre-#115 source before this change
    landed), then checks the SAME formula's binary-tree centering property
    (a slot's row is the average of its two children's rows) holds at
    3/4/5 generations too - proving depth 5 is trustworthy because it is
    literally the same formula as depth 2, not new math that happens to
    agree at one point."""

    def _full_labels(self, max_slot: int) -> dict:
        return {n: {'name': f'A{n}', 'url': None, 'redacted': False, 'dates': {}}
                for n in range(1, max_slot + 1)}

    def _positions(self, svg: str) -> dict[str, tuple[int, float]]:
        """{name: (x, y_center)} read back from the rendered foreignObjects -
        y_center undoes the `yc - CH/2` the renderer stores as `y`, so this
        recovers the same `y_center(row_index(slot))` value site.py computed."""
        out = {}
        for x, y, w, h, inner in re.findall(
                r'<foreignObject x="(-?\d+)" y="(-?\d+)" width="(\d+)" height="(\d+)">(.*?)</foreignObject>',
                svg, re.S):
            m = re.search(r'ped-name[^>]*>([^<]*)<', inner)
            if m and m.group(1):
                out[m.group(1)] = (int(x), int(y) + int(h) / 2)
        return out

    def test_two_generation_positions_match_the_pre_115_hardcoded_shape(self):
        labels = {
            1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}},
            2: {'name': 'Father', 'url': None, 'redacted': False, 'dates': {}},
            3: {'name': 'Mother', 'url': None, 'redacted': False, 'dates': {}},
            4: {'name': 'PatGF', 'url': None, 'redacted': False, 'dates': {}},
            5: {'name': 'PatGM', 'url': None, 'redacted': False, 'dates': {}},
            6: {'name': 'MatGF', 'url': None, 'redacted': False, 'dates': {}},
            7: {'name': 'MatGM', 'url': None, 'redacted': False, 'dates': {}},
        }
        svg = site._render_pedigree_svg(labels)   # ancestor_generations defaults to 2, unchanged
        pos = self._positions(svg)
        # Pre-#115: base = PAD + CH/2 - min_row*ROW = 8 + 31 - 0 = 39 (ancestors-only
        # sizing: CH=62, ROW=72, PAD=8); y_center(row) = 39 + row*72.
        expected_y = {'Subject': 39 + 1.5 * 72, 'Father': 39 + 0.5 * 72, 'Mother': 39 + 2.5 * 72,
                     'PatGF': 39 + 0.0 * 72, 'PatGM': 39 + 1.0 * 72,
                     'MatGF': 39 + 2.0 * 72, 'MatGM': 39 + 3.0 * 72}
        for name, y in expected_y.items():
            self.assertEqual(pos[name][1], y, name)
        # Same x per generation (col_x is generation-only), stepping right.
        self.assertEqual(pos['Father'][0], pos['Mother'][0])
        self.assertEqual(pos['PatGF'][0], pos['MatGM'][0])
        self.assertLess(pos['Subject'][0], pos['Father'][0])
        self.assertLess(pos['Father'][0], pos['PatGF'][0])
        # The exact legacy viewBox for this shape (proves W/H, not just rows,
        # are unchanged): 624x294 - see the ancestors-only sizing constants.
        self.assertIn('viewBox="0 0 624 294.0"', svg)

    def test_centering_holds_at_three_four_and_five_generations(self):
        # The defining property row_index() must have at ANY depth: every
        # slot's row is the average of its two children's rows (this is what
        # "centred over its ancestors" means). Checking it generically at
        # several depths - rather than hardcoding more magic numbers per
        # depth - is what actually proves the generalization is safe, not
        # just that it happens to reduce to the old constants at D=2.
        for depth in (2, 3, 4, 5):
            with self.subTest(depth=depth):
                max_slot = (1 << (depth + 1)) - 1
                labels = self._full_labels(max_slot)
                svg = site._render_pedigree_svg(labels, ancestor_generations=depth)
                pos = self._positions(svg)
                for slot in range(1, (1 << depth)):   # every non-leaf slot
                    parent_y = pos[f'A{slot}'][1]
                    left_y = pos[f'A{2 * slot}'][1]
                    right_y = pos[f'A{2 * slot + 1}'][1]
                    self.assertAlmostEqual(parent_y, (left_y + right_y) / 2, places=6,
                                           msg=f'depth={depth} slot={slot}')
                # Leaf generation rows are evenly spaced by exactly ROW (72,
                # the ancestors-only row height) and strictly increasing in
                # slot order - a real ladder, not a collapsed or overlapping one.
                leaf_lo = 1 << depth
                leaf_ys = [pos[f'A{n}'][1] for n in range(leaf_lo, max_slot + 1)]
                diffs = [b - a for a, b in zip(leaf_ys, leaf_ys[1:])]
                for d in diffs:
                    self.assertAlmostEqual(d, 72.0, places=6, msg=f'depth={depth}')

    def test_hub_only_fallback_depth_zero_draws_only_the_subject(self):
        # #115: the redaction-safe hub fallback with no eligible ancestor at
        # all calls with ancestor_generations=0 - no ancestor columns, no
        # 'Unknown' placeholders, just the subject (blank, since this is the
        # redacted-hub case) and whatever family wings are passed.
        svg = site._render_pedigree_svg(
            {1: {'name': '', 'url': None, 'redacted': True, 'dates': {}}},
            spouses=[{'name': 'Spouse', 'id': 'p-bbbbbbbbbb', 'url': None, 'dates': {}}],
            ancestor_generations=0, axis_label='ancestors →')
        self.assertNotIn('ped-empty', svg)         # no Unknown placeholders at all
        self.assertNotIn('ped-axis-label', svg)    # nothing to caption - label withheld
        self.assertIn('Spouse', svg)               # family wing still draws

    def test_shallow_known_tree_at_deep_configured_depth_stays_compact(self):
        """Regression guard for the home-pedigree geometry bug: `ancestor_band`
        (and, before this fix, `row_index`'s own row spacing) keyed height off
        `max_ancestor_gen` - the full CONFIGURED depth a caller asked for
        (`site.home_pedigree_generations`, default 5) - rather than `max_gen`,
        the depth the walk actually PLACED cards at. A person with only two
        known parents (grandparents unresearched, so slots 4-7 render as
        'Unknown' but slots 8+ are never even placed - see the render loop)
        reaches real data only 2 generations deep no matter how deep the
        caller is willing to look, so asking for the default D=5 must produce
        the exact SAME geometry as asking for D=2 with the same shape (proven
        below against `test_two_generation_positions_match_the_pre_115_hardcoded_shape`'s
        pinned 624x294 viewBox) - not a chart reserved for 2**5=32 leaf rows,
        which is what the un-fixed code produced (viewBox 624x2310 - the
        review's own synthetic repro number) and which, via the JS pan/zoom
        viewport's fit-to-height calculation, shrank the whole chart below
        legible text size on first paint."""
        labels = {
            1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}},
            2: {'name': 'Father', 'url': None, 'redacted': False, 'dates': {}},
            3: {'name': 'Mother', 'url': None, 'redacted': False, 'dates': {}},
        }
        svg = site._render_pedigree_svg(labels, ancestor_generations=5)
        # Same exact viewBox as the fully-known D=2 shape - reserving no more
        # (and no less) than the 2 generations this tree actually reached,
        # regardless of the D=5 configured depth asked for.
        self.assertIn('viewBox="0 0 624 294.0"', svg)
        # Exactly the 4 grandparent 'Unknown' placeholders slots 4-7 need -
        # not the dozens a D=5-deep walk would place if it kept going past
        # the point where research actually stopped.
        self.assertEqual(svg.count('ped-empty'), 4)
        pos = self._positions(svg)
        # Same row positions the D=2 pinned test expects (base=39, ROW=72).
        self.assertEqual(pos['Subject'][1], 39 + 1.5 * 72)
        self.assertEqual(pos['Father'][1], 39 + 0.5 * 72)
        self.assertEqual(pos['Mother'][1], 39 + 2.5 * 72)


class AncestorCoupleBracketTests(unittest.TestCase):
    """#115, REOPENED after #152 merged: the issue's own "Marriages" design
    called for two mechanisms - the hub's own spouse(s) via the existing
    couples-first bracket (shipped in #152), AND every ancestor-generation
    couple (Ahnentafel slots 2N/2N+1, always a couple by the numbering
    itself) getting a shared-vertex bracket "the same way the per-person
    chart already does". Only the first mechanism actually shipped: the
    ancestor-grid loop drew two independent elbows from a child to each of
    its two parent slots with nothing connecting the parents to EACH OTHER,
    so a hub with no spouse of their own showed zero visual marriage
    indicators anywhere - confirmed by reading the pre-fix renderer, not
    just by the reopening comment's say-so (the two elbows DO happen to
    share their starting vertex at the CHILD's own edge, which is not the
    same claim as "the parents are bracketed to each other" the issue asked
    for). This class pins the exact bracket geometry the fix adds, using
    the same real-coordinate-parsing rigor as `PedigreeGeometryGuardTests`
    rather than eyeballing markup."""

    def _full_labels(self, max_slot: int) -> dict:
        return {n: {'name': f'A{n}', 'url': None, 'redacted': False, 'dates': {}}
                for n in range(1, max_slot + 1)}

    def test_subject_own_parents_get_a_shared_vertex_bracket(self):
        # Pinned against the actual pre-fix two-elbow output for this exact
        # shape (M184,147 H204 V75 H224 / M184,147 H204 V219 H224 - verified
        # by direct inspection before this fix landed): the new bracket
        # covers the identical pixels (touches the parents' own shared
        # column x2=224 at both y=75 and y=219, vertical spine at the same
        # midx=204 the old elbows used), plus a single stub from that spine
        # to the child's own row (yc=147) - which is where BOTH old elbows'
        # first "H204" leg used to land, redundantly drawn twice.
        labels = {
            1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}},
            2: {'name': 'Father', 'url': None, 'redacted': False, 'dates': {}},
            3: {'name': 'Mother', 'url': None, 'redacted': False, 'dates': {}},
        }
        svg = site._render_pedigree_svg(labels)
        self.assertIn('<path class="ped-link ped-link-couple" d="M224,75 H204 V219 H224"/>', svg)
        self.assertIn('<path class="ped-link" d="M204,147 H184"/>', svg)
        # The old two-independent-elbow shape must be GONE for this pair -
        # replaced, not merely supplemented (a leftover old-style elbow
        # alongside the new bracket would double-draw the line).
        self.assertNotIn('<path class="ped-link" d="M184,147 H204 V75 H224"/>', svg)
        self.assertNotIn('<path class="ped-link" d="M184,147 H204 V219 H224"/>', svg)
        # Card/row geometry is provably untouched by this link-only change -
        # the exact legacy viewBox this shape has always produced.
        self.assertIn('viewBox="0 0 624 294.0"', svg)

    def test_bracket_drawn_at_every_internal_generation_not_just_the_subjects_own_parents(self):
        # A fully-known 3-generation tree (15 slots, 7 internal/non-leaf
        # slots: 1 subject-parents pair + 2 grandparent pairs + 4
        # great-grandparent pairs) must get exactly 7 brackets - one per
        # internal slot - proving the mechanism generalizes to every
        # generation the #115 depth default (5) will actually draw, not
        # just the shallow 2-generation case above.
        labels = self._full_labels(15)
        svg = site._render_pedigree_svg(labels, ancestor_generations=3)
        self.assertEqual(svg.count('ped-link-couple'), 7)
        # Every bracket must have exactly one matching stub (a bracket with
        # no stub, or a stub with no bracket, would mean the couple's own
        # child never actually connects to the pairing it belongs to).
        # The stub is the only OTHER 'ped-link'-classed path with a plain
        # 'M... H...' two-point shape (no V segment) - count those and
        # confirm it matches the bracket count.
        stubs = re.findall(r'<path class="ped-link" d="M\d+,\d+ H\d+"/>', svg)
        self.assertEqual(len(stubs), 7)

    def test_lone_known_parent_keeps_the_plain_elbow_no_bracket_asserted(self):
        # Only Father is known; Mother's slot (3) is still a faint 'Unknown'
        # placeholder with a real child (the subject) - the pedigree draws
        # it, but there is no marriage to bracket since only one half of the
        # pair is actually on the chart. Same for Father's own parents
        # (slots 4/5), both fully unresearched.
        labels = {
            1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}},
            2: {'name': 'Father', 'url': None, 'redacted': False, 'dates': {}},
        }
        svg = site._render_pedigree_svg(labels)
        self.assertNotIn('ped-link-couple', svg)
        # The old-style plain elbow to Father (real) and to Mother's slot
        # (Unknown) both still draw, unchanged.
        self.assertIn('<path class="ped-link" d="M184,147 H204 V75 H224"/>', svg)
        self.assertIn('<path class="ped-link" d="M184,147 H204 V219 H224"/>', svg)

    def test_redacted_parent_beside_a_known_parent_never_gets_a_bracket(self):
        # Adversarial self-review: a redacted (living/restricted) ancestor
        # already renders as an ordinary 'Unknown' placeholder, never its
        # real name (`_chart_entry` blanks the name, which makes its slot's
        # `kind` 'empty' - the exact same code path an unresearched slot
        # takes). A bracket drawn beside that blank card would still visibly
        # assert "this real ancestor is married to whoever is behind this
        # card" even with the name withheld - so this must fall through to
        # the plain single-parent elbow, the same as any other slot with
        # only one known occupant, never a couple bracket.
        labels = {
            1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}},
            2: {'name': 'Father', 'url': None, 'redacted': False, 'dates': {}},
            3: {'name': '', 'url': None, 'redacted': True, 'dates': {}},
        }
        svg = site._render_pedigree_svg(labels)
        self.assertNotIn('ped-link-couple', svg)
        self.assertIn('Father', svg)
        # 3 Unknowns: slot 3 itself (redacted -> blanked to 'empty', its own
        # children never placed since it is not a real drawn person) plus
        # Father's own two unresearched parent slots (4/5).
        self.assertEqual(svg.count('ped-empty'), 3)

    def test_two_unresearched_parent_slots_never_get_a_bracket(self):
        # A subject with NO known parents at all: both ancestor slots are
        # 'Unknown' placeholders - no couple to assert either.
        labels = {1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}}}
        svg = site._render_pedigree_svg(labels)
        self.assertNotIn('ped-link-couple', svg)
        self.assertEqual(svg.count('ped-empty'), 2)

    def test_workbench_hypothesis_parent_dashes_the_whole_pairing(self):
        # A workbench-only unsourced hypothesis tie makes the WHOLE pairing
        # exactly as unconfirmed as its weaker half - both the bracket AND
        # its stub render with ped-link-later's own dashed vocabulary,
        # mirroring how a later marriage's dashing already covers its
        # bracket and its children stubs together, not the bracket alone.
        labels = {
            1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}},
            2: {'name': 'Father', 'url': None, 'redacted': False, 'dates': {}, 'hypothesis': True},
            3: {'name': 'Mother', 'url': None, 'redacted': False, 'dates': {}},
        }
        svg = site._render_pedigree_svg(labels, workbench=True)
        self.assertIn(
            '<path class="ped-link ped-link-hypothesis ped-link-couple" d="M224,75 H204 V219 H224"/>', svg)
        self.assertIn('<path class="ped-link ped-link-hypothesis" d="M204,147 H184"/>', svg)

    def test_deep_fully_known_tree_has_no_new_link_collisions(self):
        # Adversarial self-review: the bracket+stub restructuring must not
        # reintroduce a #120-shaped collinear overlap at the DEFAULT home
        # pedigree depth (5) once every generation is actually drawing
        # brackets, not just the shallow 2-generation case the pinned tests
        # above check by hand. Reuses `_ped_link_row_collisions` (built for
        # #120's family-wing collisions) against a fully-populated depth-5
        # ancestor grid - the densest case this renderer can produce.
        max_slot = (1 << 6) - 1   # depth 5: slots 1..63
        labels = self._full_labels(max_slot)
        svg = site._render_pedigree_svg(labels, ancestor_generations=5)
        self.assertEqual(svg.count('ped-link-couple'), (1 << 5) - 1)   # 31 internal slots
        self.assertEqual(_ped_link_row_collisions(svg), [])

    def test_four_and_six_generation_depths_also_bracket_every_internal_slot(self):
        # The adversarial question this guards against: does the bracket
        # mechanism only happen to work at the default depth (5), or does
        # it generalize the same way `row_index` itself was proven to at
        # 3/4/5 generations? A fully-known tree at each depth must produce
        # exactly one bracket per internal slot (2**D - 1) and zero
        # collisions, for a depth both above and below the default.
        for depth in (4, 6):
            with self.subTest(depth=depth):
                max_slot = (1 << (depth + 1)) - 1
                labels = self._full_labels(max_slot)
                svg = site._render_pedigree_svg(labels, ancestor_generations=depth)
                self.assertEqual(svg.count('ped-link-couple'), (1 << depth) - 1)
                self.assertEqual(_ped_link_row_collisions(svg), [])


class PedigreeAxisLabelCenteringTests(unittest.TestCase):
    """#152 review fix (minor, cosmetic): the axis-label caption used to sit
    at `col_x(1)` - the left edge of just the FIRST ancestor generation's
    column - regardless of how many were actually drawn. Fine by coincidence
    at the person-page chart's fixed 2 generations; visibly off at the home
    page's deeper default, where it labeled only the nearest column instead
    of the whole ancestor block beneath it. `_full_labels`/`_positions` are
    copied from `PedigreeGeometryGuardTests` rather than shared by
    inheritance, so this class's own tests don't also re-run that class's."""

    def _full_labels(self, max_slot: int) -> dict:
        return {n: {'name': f'A{n}', 'url': None, 'redacted': False, 'dates': {}}
                for n in range(1, max_slot + 1)}

    def _positions(self, svg: str) -> dict[str, tuple[int, float]]:
        out = {}
        for x, y, w, h, inner in re.findall(
                r'<foreignObject x="(-?\d+)" y="(-?\d+)" width="(\d+)" height="(\d+)">(.*?)</foreignObject>',
                svg, re.S):
            m = re.search(r'ped-name[^>]*>([^<]*)<', inner)
            if m and m.group(1):
                out[m.group(1)] = (int(x), int(y) + int(h) / 2)
        return out

    def test_axis_label_centered_across_all_drawn_ancestor_columns(self):
        labels = self._full_labels(15)   # 3 fully-known ancestor generations
        svg = site._render_pedigree_svg(labels, ancestor_generations=3, axis_label='ancestors →')
        label_m = re.search(r'<text class="ped-axis-label" x="(-?\d+)"', svg)
        self.assertIsNotNone(label_m)
        label_x = int(label_m.group(1))
        positions = self._positions(svg)
        col1_left = positions['A2'][0]        # any slot-2 card sits at col_x(1)
        last_col_left = positions['A8'][0]    # a slot-8 card sits at col_x(3), the deepest column
        last_col_right = last_col_left + 176  # CW
        self.assertEqual(label_x, round((col1_left + last_col_right) / 2))
        # The specific defect this guards against: no longer pinned to
        # column 1's own left edge once more than one column is drawn.
        self.assertNotEqual(label_x, col1_left)

    def test_axis_label_still_centers_on_the_single_column_at_two_generations(self):
        # The formula must collapse correctly to "center of the one column"
        # when only one ancestor generation is drawn (the person-page
        # chart's own shape) - not just look right at deeper depths.
        labels = {
            1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}},
            2: {'name': 'Father', 'url': None, 'redacted': False, 'dates': {}},
            3: {'name': 'Mother', 'url': None, 'redacted': False, 'dates': {}},
        }
        svg = site._render_pedigree_svg(labels, ancestor_generations=1, axis_label='ancestors →')
        label_m = re.search(r'<text class="ped-axis-label" x="(-?\d+)"', svg)
        self.assertIsNotNone(label_m)
        positions = self._positions(svg)
        col1_left = positions['Father'][0]
        self.assertEqual(int(label_m.group(1)), round(col1_left + 176 / 2))


class HomePedigreeTests(_Base):
    """#115: the home page's marriage-aware ancestor pedigree - seeding
    (site.home_person, falling back to root_person), configurable depth
    (site.home_pedigree_generations), siblings on the hub row, branch
    coloring, the orientation caption/axis label, the pan/zoom enhancement
    script, and the redaction-safe hub fallback for a living seed."""

    def _seed_linear_ancestors(self, seed_pid: str, generations: int) -> list[str]:
        """Seed a single father-slot ancestor chain above `seed_pid`:
        gen1 is seed_pid's own parent, gen2 is gen1's parent, and so on.
        Every seeded person is 'M' (site._seed_person's fixed default), so
        each always fills the father slot (2N) - a clean single line with no
        branching, easy to reason about depth against. Returns the pids in
        depth order (index 0 = gen1)."""
        pids = []
        prev = seed_pid
        for g in range(1, generations + 1):
            pid = f'p-anc{g}' + '0' * (10 - len(f'anc{g}'))
            self._seed_person(pid, f'Ancestor Gen{g}', surname=f'Gen{g}')
            self._seed_rel(prev, 'parent', pid)
            self._seed_rel(pid, 'child', prev)
            pids.append(pid)
            prev = pid
        return pids

    def _seed_home(self, *, home_person: str | None = None, extra_yaml: str = '') -> None:
        home_line = f'  home_person: {home_person}\n' if home_person else ''
        (self.archive_root / 'fha.yaml').write_text(
            f'roots: {{}}\nroot_person: P-aaaaaaaaaa\nsite:\n{home_line}{extra_yaml}',
            encoding='utf-8')

    def _pedigree_section(self, page: str) -> str:
        """The home pedigree's own markup, cut out of the full page - the
        surname index below it legitimately lists every curated person
        regardless of how deep the chart itself drew, so a check like 'this
        generation is absent' means 'absent from the CHART', not 'absent
        anywhere on the page' (which the site-wide People index would give a
        false positive/negative on)."""
        m = re.search(r'<h2 id="pedigree">.*?(?=<h2 )', page, re.S)
        self.assertIsNotNone(m, 'no home pedigree section found on the page')
        return m.group(0)

    def test_default_depth_is_five_generations(self):
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_linear_ancestors('p-aaaaaaaaaa', 6)
        self._seed_home()
        self._run(linked=True)
        ped = self._pedigree_section(self._read('index.html'))
        self.assertIn('Ancestor Gen5', ped)       # 5 generations back: drawn
        self.assertNotIn('Ancestor Gen6', ped)     # a 6th generation: never queried, absent

    def test_configured_generations_respected(self):
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_linear_ancestors('p-aaaaaaaaaa', 5)
        self._seed_home(extra_yaml='  home_pedigree_generations: 3\n')
        self._run(linked=True)
        ped = self._pedigree_section(self._read('index.html'))
        self.assertIn('Ancestor Gen3', ped)
        self.assertNotIn('Ancestor Gen4', ped)

    def test_generations_above_maximum_are_clamped_with_a_warning(self):
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_home(extra_yaml='  home_pedigree_generations: 50\n')
        res = self._run(linked=True)
        self.assertTrue(any('home_pedigree_generations' in m and 'maximum' in m
                            for m in res['messages']))

    def test_non_numeric_generations_warns_and_uses_default(self):
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_home(extra_yaml='  home_pedigree_generations: not-a-number\n')
        res = self._run(linked=True)
        self.assertTrue(any('home_pedigree_generations' in m and 'whole number' in m
                            for m in res['messages']))

    def test_fractional_generations_warns_and_uses_default(self):
        # #152 review fix (P2): `int(3.9)` truncates to 3 with no error -
        # `int()` succeeding is not the same as "this was genuinely a whole
        # number." A fractional YAML value must be rejected the same way a
        # non-numeric string already is, not silently narrowed to a
        # shallower chart than configured with no warning at all.
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_linear_ancestors('p-aaaaaaaaaa', 6)
        self._seed_home(extra_yaml='  home_pedigree_generations: 3.9\n')
        res = self._run(linked=True)
        self.assertTrue(any('home_pedigree_generations' in m and 'whole number' in m
                            for m in res['messages']))
        ped = self._pedigree_section(self._read('index.html'))
        self.assertIn('Ancestor Gen5', ped)      # the documented default (5) - not int(3.9)=3
        self.assertNotIn('Ancestor Gen6', ped)

    def test_integral_float_generations_is_accepted(self):
        # #152 follow-up review fix (P2, finding 6): PyYAML parses a hand-
        # edited `home_pedigree_generations: 3.0` as a Python `float`, not an
        # `int` - a harmless slip, and mathematically a genuine whole number.
        # The fractional-rejection fix above must not catch this case too:
        # `3.0` should be honored as depth 3 (not silently overridden to the
        # default of 5), and with no "not a whole number" warning, since
        # nothing was actually wrong with what the human wrote.
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_linear_ancestors('p-aaaaaaaaaa', 5)
        self._seed_home(extra_yaml='  home_pedigree_generations: 3.0\n')
        res = self._run(linked=True)
        self.assertFalse(any('home_pedigree_generations' in m for m in res['messages']))
        ped = self._pedigree_section(self._read('index.html'))
        self.assertIn('Ancestor Gen3', ped)       # the REQUESTED depth (3) - not the default (5)
        self.assertNotIn('Ancestor Gen4', ped)

    def test_boolean_generations_warns_and_uses_default(self):
        # Same bug, different door: `bool` is an `int` subclass in Python, so
        # `int(True) == 1` used to be silently accepted as a valid depth.
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_home(extra_yaml='  home_pedigree_generations: true\n')
        res = self._run(linked=True)
        self.assertTrue(any('home_pedigree_generations' in m and 'whole number' in m
                            for m in res['messages']))

    def test_seeds_on_home_person_not_root_person(self):
        self._seed_person('p-aaaaaaaaaa', 'Root Person')
        self._seed_person('p-bbbbbbbbbb', 'Home Person')
        self._seed_home(home_person='P-bbbbbbbbbb')
        self._run(linked=True)
        ped = self._pedigree_section(self._read('index.html'))
        self.assertIn('Home Person', ped)
        self.assertNotIn('Root Person', ped)

    def test_falls_back_to_root_person_when_home_person_unset(self):
        self._seed_person('p-aaaaaaaaaa', 'Root Person')
        self._seed_home()
        self._run(linked=True)
        self.assertIn('Root Person', self._read('index.html'))

    def test_misconfigured_home_person_falls_back_to_root_person_with_warning(self):
        self._seed_person('p-aaaaaaaaaa', 'Root Person')
        self._seed_home(home_person='P-zzzzzzzzzz')   # not in the index
        res = self._run(linked=True)
        self.assertTrue(any('site.home_person' in m and 'not in' in m for m in res['messages']))
        self.assertIn('Root Person', self._read('index.html'))

    def test_misconfigured_root_person_does_not_warn_when_home_person_saves_the_page(self):
        # A broken root_person behind a WORKING home_person never actually
        # costs the reader anything - the pedigree still builds from
        # home_person - so warning "the home pedigree was skipped" here
        # would name the wrong cause for a page that built just fine.
        self._seed_person('p-bbbbbbbbbb', 'Home Person')
        (self.archive_root / 'fha.yaml').write_text(
            'roots: {}\nroot_person: P-zzzzzzzzzz\nsite:\n  home_person: P-bbbbbbbbbb\n',
            encoding='utf-8')
        res = self._run(linked=True)
        self.assertFalse(any('root_person' in m and 'not in the index' in m for m in res['messages']))
        self.assertIn('Home Person', self._read('index.html'))

    def test_siblings_shown_and_a_living_sibling_dropped(self):
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_person('p-bbbbbbbbbb', 'Shared Parent')
        self._seed_person('p-cccccccccc', 'Deceased Sibling')
        self._seed_person('p-dddddddddd', 'Living Sibling', living='true')
        for kid in ('p-aaaaaaaaaa', 'p-cccccccccc', 'p-dddddddddd'):
            self._seed_rel('p-bbbbbbbbbb', 'child', kid)
            self._seed_rel(kid, 'parent', 'p-bbbbbbbbbb')
        self._seed_home()
        self._run(linked=False)
        home = self._read('index.html')
        ped = self._pedigree_section(home)
        self.assertIn('Deceased Sibling', ped)
        self.assertNotIn('Living Sibling', home)   # nowhere on the page at all - never gets a page
        self.assertIn('ped-link-sibling', ped)

    def test_full_sibling_included_via_a_later_shared_parents_public_tie(self):
        # #152 review fix (minor, completeness): `_hub_siblings` used to mark
        # a candidate 'seen' the moment it was first reached, even when that
        # first-visited parent's tie failed the public-claim gate - so a full
        # sibling (sharing BOTH of the hub's parents) whose tie to the FIRST
        # shared parent (sorted by id) was non-public got silently excluded,
        # even though their tie to the SECOND shared parent was perfectly
        # public. A candidate must now be gated on its BEST tie, not its
        # first-visited one.
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_person('p-1111111111', 'Parent A')   # sorts before Parent B
        self._seed_person('p-2222222222', 'Parent B')
        self._seed_person('p-cccccccccc', 'Full Sibling')
        for parent in ('p-1111111111', 'p-2222222222'):
            for kid in ('p-aaaaaaaaaa', 'p-cccccccccc'):
                self._seed_rel(parent, 'child', kid)
                self._seed_rel(kid, 'parent', parent)
        # Full Sibling's tie to Parent A (visited FIRST) is backed only by a
        # claim from a hard-restricted source - not public.
        self._seed_source('s-1111111111', 'Restricted Source', restricted=1,
                          people=('p-1111111111', 'p-cccccccccc'))
        self._seed_claim('c-1111111111', 's-1111111111', 'relationship',
                         'Full Sibling is the child of Parent A', status='accepted',
                         persons=('p-1111111111', 'p-cccccccccc'),
                         roles={'p-1111111111': 'parent', 'p-cccccccccc': 'child'})
        # Full Sibling's tie to Parent B carries no claim at all - a bare
        # relationships-table row, which _has_public_claim treats as public
        # (no claims to withhold behind).
        self._seed_home()
        self._run(linked=False)
        home = self._read('index.html')
        self.assertIn('Full Sibling', self._pedigree_section(home))

    def test_restricted_hub_parent_tie_contributes_no_sibling_candidates(self):
        # #152 review fix (P1, privacy-adjacent): `_hub_siblings` used to
        # check ONLY the CANDIDATE's own tie to a shared parent
        # (`_has_public_claim(parent, candidate)`), never the HUB's OWN tie
        # to that same parent. So a parent the hub is tied to ONLY through a
        # restricted/sealed claim still contributed sibling candidates - via
        # those candidates' own PUBLIC ties to that same parent - which
        # tells a reader "the hub is tied to this parent" just as surely as
        # printing the parent's name on the hub's own card would, defeating
        # the whole point of restricting the hub's own tie. A parent the hub
        # is not itself publicly tied to must now contribute NO candidates
        # at all, regardless of how public a candidate's own tie to that
        # parent is.
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_person('p-bbbbbbbbbb', 'Shared Parent')
        self._seed_person('p-cccccccccc', 'Other Child')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-aaaaaaaaaa', claim_id='c-1111111111')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb', claim_id='c-1111111111')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-cccccccccc')
        self._seed_rel('p-cccccccccc', 'parent', 'p-bbbbbbbbbb')
        # The HUB's own tie to Shared Parent is backed only by a claim from
        # a hard-restricted source - not public. The relationships row above
        # carries this SAME claim_id (rather than the generic placeholder
        # `_seed_rel` otherwise defaults to) so the edge-specific check
        # (`_has_public_parent_edge`, the #152-round P1 fix) can actually see
        # which claim backs it - the same way `fha index` always ties a
        # derived relationships row to the real claim that produced it.
        self._seed_source('s-1111111111', 'Restricted Source', restricted=1,
                          people=('p-bbbbbbbbbb', 'p-aaaaaaaaaa'))
        self._seed_claim('c-1111111111', 's-1111111111', 'relationship',
                         'Hub is the child of Shared Parent', status='accepted',
                         persons=('p-bbbbbbbbbb', 'p-aaaaaaaaaa'),
                         roles={'p-bbbbbbbbbb': 'parent', 'p-aaaaaaaaaa': 'child'})
        # Other Child's own tie to Shared Parent carries no claim at all - a
        # bare relationships-table row, publicly tied per `_has_public_claim`.
        self._seed_home()
        self._run(linked=False)
        home = self._read('index.html')
        # Other Child is an ordinary curated person and legitimately appears
        # in the home page's surname A-Z index regardless - the assertion
        # must be scoped to the pedigree chart itself, not the whole page.
        self.assertNotIn('Other Child', self._pedigree_section(home))

    def test_unrelated_public_claim_does_not_fake_a_public_parent_tie(self):
        # Codex review (P1, PR #152 round): `_has_public_claim(p, pid)` asks
        # "is there ANY publishable claim connecting these two people, about
        # anything" - a completely UNRELATED public claim (a shared census
        # entry, here) satisfies that even though the parent-child tie
        # itself is backed only by a restricted claim, letting the hub's
        # sibling-candidate gate wrongly treat the hub as publicly tied to
        # this parent. `_has_public_parent_edge` asks the narrower, correct
        # question - is the parent-child RELATIONSHIP itself public - so it
        # must not be fooled by the census claim's mere presence.
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_person('p-bbbbbbbbbb', 'Shared Parent')
        self._seed_person('p-cccccccccc', 'Other Child')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-aaaaaaaaaa', claim_id='c-1111111111')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb', claim_id='c-1111111111')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-cccccccccc')
        self._seed_rel('p-cccccccccc', 'parent', 'p-bbbbbbbbbb')
        # The parent-child claim itself is restricted...
        self._seed_source('s-1111111111', 'Restricted Source', restricted=1,
                          people=('p-bbbbbbbbbb', 'p-aaaaaaaaaa'))
        self._seed_claim('c-1111111111', 's-1111111111', 'relationship',
                         'Hub is the child of Shared Parent', status='accepted',
                         persons=('p-bbbbbbbbbb', 'p-aaaaaaaaaa'),
                         roles={'p-bbbbbbbbbb': 'parent', 'p-aaaaaaaaaa': 'child'})
        # ...but a completely unrelated, perfectly public census claim ALSO
        # names both people (as household members, not as parent/child) -
        # the exact shape a pair-wide "any claim at all" check cannot tell
        # apart from genuine evidence of the parent-child tie itself.
        self._seed_source('s-2222222222', 'Public Census')
        self._seed_claim('c-2222222222', 's-2222222222', 'census',
                         'Shared Parent and Hub Person both listed in the 1900 census',
                         status='accepted', persons=('p-bbbbbbbbbb', 'p-aaaaaaaaaa'))
        self._seed_home()
        self._run(linked=False)
        home = self._read('index.html')
        self.assertNotIn('Other Child', self._pedigree_section(home))

    def test_branch_coloring_on_home_page_not_on_person_page(self):
        # #115: branch coloring is opt-in (the `home=True`/`branch_color=True`
        # pair `_build_home_pedigree` passes), so an ordinary person's own
        # chart stays byte-for-byte what it was before this feature.
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_person('p-bbbbbbbbbb', 'Father Person')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-aaaaaaaaaa')
        self._seed_home()
        self._run(linked=True)
        self.assertIn('ped-branch-1', self._read('index.html'))
        self.assertNotIn('ped-branch-1', self._read('persons/p-aaaaaaaaaa.html'))
        self.assertNotIn('ped-branch-2', self._read('persons/p-aaaaaaaaaa.html'))

    def test_home_page_shows_a_couple_bracket_for_the_hub_own_parents(self):
        # #115 REOPENED: the whole point of replacing the old descendant fan
        # was showing marriages the fan could never show at all - a married-
        # in ancestor who is not themself a blood descendant of the apex
        # never appeared. Confirm the fix reaches the actual generated home
        # page (AncestorCoupleBracketTests already pins the exact geometry
        # in isolation) - the hub's own two parents are both known and
        # public, so the built page must carry a shared-vertex bracket
        # (`ped-link-couple`), the same idiom `_build_family_wings`'s own
        # spouse bracket already uses one column over.
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_person('p-bbbbbbbbbb', 'Father Person', sex='M')
        self._seed_person('p-cccccccccc', 'Mother Person', sex='F')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-aaaaaaaaaa')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-cccccccccc')
        self._seed_rel('p-cccccccccc', 'child', 'p-aaaaaaaaaa')
        self._seed_home()
        self._run(linked=True)
        ped = self._pedigree_section(self._read('index.html'))
        self.assertIn('ped-link-couple', ped)
        self.assertIn('Father Person', ped)
        self.assertIn('Mother Person', ped)

    def test_ahnentafel_excludes_non_genetic_parent(self):
        # #152 review fix (P2, SPEC §12.2): a parent-child edge whose backing
        # claim carries an explicit non-genetic `subtype` (adoptive/step/
        # foster/guardian/...) must never occupy an Ahnentafel slot - doing
        # so falsely presents a social/legal parent, and their whole
        # ancestor line behind them, as ordinary biological ancestry. The
        # excluded slot reads as an ordinary unresearched 'Unknown', not a
        # labelled non-genetic card (matches the review's own "filtering out
        # is simpler and safer" steer).
        self._seed_person('p-aaaaaaaaaa', 'Hub Person', sex='F')
        self._seed_person('p-bbbbbbbbbb', 'Adoptive Parent', sex='F')
        self._seed_person('p-cccccccccc', 'Bio Parent', sex='M')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb', claim_id='c-1111111111')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-aaaaaaaaaa')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-cccccccccc')
        self._seed_rel('p-cccccccccc', 'child', 'p-aaaaaaaaaa')
        self._seed_source('s-1111111111', 'Adoption Record',
                          people=('p-aaaaaaaaaa', 'p-bbbbbbbbbb'))
        self._seed_claim('c-1111111111', 's-1111111111', 'relationship',
                         'Hub was adopted by Adoptive Parent', status='accepted',
                         subtype='adoptive', persons=('p-bbbbbbbbbb', 'p-aaaaaaaaaa'),
                         roles={'p-bbbbbbbbbb': 'parent', 'p-aaaaaaaaaa': 'child'})
        self._seed_home()
        self._run(linked=True)
        ped = self._pedigree_section(self._read('index.html'))
        self.assertNotIn('Adoptive Parent', ped)
        self.assertIn('Bio Parent', ped)

    def test_restricted_genetic_claim_does_not_confirm_ancestry_via_an_unrelated_public_claim(self):
        # Codex review (P1, PR #152 round): a pair can carry BOTH a public
        # NON-genetic claim (adoptive) and a restricted GENETIC (biological)
        # claim. `_has_public_claim(pid, other)` - a pair-wide "does ANY
        # public claim connect these two people, about anything" check - was
        # satisfied by the public adoptive claim alone, and that let the
        # RESTRICTED biological claim's genetic subtype set `entry['genetic']
        # = True` regardless, presenting a genetic ancestry relationship
        # whose only actual evidence is restricted (DNA/by-request) material.
        # Standalone mode with no public genetic evidence must show this
        # slot as an ordinary unresearched gap, the same as the
        # explicitly-non-genetic-subtype case just above - not the parent's
        # name.
        self._seed_person('p-aaaaaaaaaa', 'Hub Person', sex='F')
        self._seed_person('p-bbbbbbbbbb', 'Bio Or Adoptive Parent', sex='F')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb', claim_id='c-1111111111')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-aaaaaaaaaa')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb', claim_id='c-2222222222')
        self._seed_source('s-1111111111', 'Adoption Record',
                          people=('p-aaaaaaaaaa', 'p-bbbbbbbbbb'))
        self._seed_claim('c-1111111111', 's-1111111111', 'relationship',
                         'Hub was adopted by Bio Or Adoptive Parent', status='accepted',
                         subtype='adoptive', persons=('p-bbbbbbbbbb', 'p-aaaaaaaaaa'),
                         roles={'p-bbbbbbbbbb': 'parent', 'p-aaaaaaaaaa': 'child'})
        self._seed_source('s-2222222222', 'DNA Report', restricted='dna',
                          people=('p-aaaaaaaaaa', 'p-bbbbbbbbbb'))
        self._seed_claim('c-2222222222', 's-2222222222', 'relationship',
                         'DNA confirms Bio Or Adoptive Parent as biological parent',
                         status='accepted', subtype='biological',
                         persons=('p-bbbbbbbbbb', 'p-aaaaaaaaaa'),
                         roles={'p-bbbbbbbbbb': 'parent', 'p-aaaaaaaaaa': 'child'})
        self._seed_home()
        self._run(linked=False)
        ped = self._pedigree_section(self._read('index.html'))
        self.assertNotIn('Bio Or Adoptive Parent', ped)

    def test_unknown_sex_parents_get_no_branch_color(self):
        # #152 review fix (P2): when NEITHER parent has a known sex, which
        # one lands in Ahnentafel slot 2 (paternal) vs 3 (maternal) is pure
        # query/iteration order, not evidence - so tinting either slot's
        # line paternal/maternal would assert a fact nobody actually
        # recorded, and the two colors could even swap between builds.
        self._seed_person('p-aaaaaaaaaa', 'Hub Person', sex='F')
        self._seed_person('p-bbbbbbbbbb', 'Parent One', sex='unknown')
        self._seed_person('p-cccccccccc', 'Parent Two', sex='unknown')
        for parent in ('p-bbbbbbbbbb', 'p-cccccccccc'):
            self._seed_rel('p-aaaaaaaaaa', 'parent', parent)
            self._seed_rel(parent, 'child', 'p-aaaaaaaaaa')
        self._seed_home()
        self._run(linked=True)
        ped = self._pedigree_section(self._read('index.html'))
        self.assertNotIn('ped-branch-1', ped)
        self.assertNotIn('ped-branch-2', ped)

    def test_one_known_sex_parent_keeps_branch_color_when_co_parent_is_unknown(self):
        # The companion case: only the DEFAULTED slot loses its color. A
        # parent whose sex genuinely IS on record keeps the branch color its
        # own recorded sex derives - the fix is not "disable all coloring
        # whenever any parent's sex is unknown," only "never color a slot
        # whose OWN placement in slot 2 vs 3 was not actually evidenced."
        self._seed_person('p-aaaaaaaaaa', 'Hub Person', sex='F')
        self._seed_person('p-bbbbbbbbbb', 'Known Mother', sex='F')
        self._seed_person('p-cccccccccc', 'Unknown Parent', sex='unknown')
        for parent in ('p-bbbbbbbbbb', 'p-cccccccccc'):
            self._seed_rel('p-aaaaaaaaaa', 'parent', parent)
            self._seed_rel(parent, 'child', 'p-aaaaaaaaaa')
        self._seed_home()
        self._run(linked=True)
        ped = self._pedigree_section(self._read('index.html'))
        self.assertIn('ped-branch-2', ped)     # Known Mother's line - genuinely derived
        self.assertNotIn('ped-branch-1', ped)  # Unknown Parent's line - defaulted, no evidence

    def test_stub_ancestor_renders_unlinked_plain_name(self):
        # docs/CUSTOMIZING_SITE.md's click-through promise is qualified to
        # ancestors that already have a page in this build (#152 review fix,
        # doc wording only - `_chart_entry` already behaved this way; this
        # locks the behavior in so the corrected doc text keeps matching
        # it). A stub-tier ancestor gets no page outside workbench, so
        # `_chart_entry` renders them as a plain, unlinked name - never a
        # dead or guessed link.
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_person('p-bbbbbbbbbb', 'Stub Ancestor', tier='stub')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-aaaaaaaaaa')
        self._seed_home()
        self._run(linked=True)
        ped = self._pedigree_section(self._read('index.html'))
        self.assertIn('<span class="ped-name">Stub Ancestor</span>', ped)

    def test_axis_label_and_caption_present(self):
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_home()
        self._run(linked=True)
        home = self._read('index.html')
        self.assertIn('ped-axis-label', home)
        self.assertIn("Hub Person&#39;s family, tracing back through the generations", home)

    def test_wrap_static_enhancement_script_included(self):
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_home()
        self._run(linked=True)
        home = self._read('index.html')
        self.assertIn('vendor/fha-tree.js', home)
        self.assertIn('FhaTree.wrapStatic', home)
        self.assertIn('id="fha-pedigree"', home)

    def test_redaction_safe_hub_falls_back_to_closest_eligible_ancestor(self):
        # The configured hub is living; standalone must not open on a card
        # naming them - it substitutes the closest non-living ancestor on
        # their recorded line instead (their own parent here).
        self._seed_person('p-aaaaaaaaaa', 'Living Owner', living='true')
        self._seed_person('p-bbbbbbbbbb', 'Deceased Parent')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-aaaaaaaaaa')
        self._seed_home()
        self._run(linked=False)
        home = self._read('index.html')
        self.assertIn('Deceased Parent', self._pedigree_section(home))
        self.assertNotIn('Living Owner', home)   # nowhere on the page - never gets a page
        self.assertIn("Deceased Parent&#39;s family", home)

    def test_redaction_safe_hub_skips_a_parent_tie_backed_only_by_a_restricted_source(self):
        # Blocker fix (PR #152): `_apex_ancestor` walked `parent` edges to
        # find a publishable substitute hub but never checked whether the
        # TIE crossed was itself public - unlike every other ancestor walk
        # in this file. A living owner's only recorded parent, tied to them
        # SOLELY by a claim sourced from a hard-restricted source, must not
        # be promoted to be the site's public-facing hub just because the
        # PARENT themselves happens to be deceased and otherwise
        # unrestricted - the restricted source exists precisely to keep that
        # tie off the public site. With no other path to an eligible
        # ancestor, this must fall all the way through to the no-eligible-
        # ancestor blank-hub fallback, exactly as if the parent were not
        # recorded at all.
        self._seed_person('p-aaaaaaaaaa', 'Alice Living', living='true')
        self._seed_person('p-bbbbbbbbbb', 'Bob Restricted Parent')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-aaaaaaaaaa')
        self._seed_source('s-2222222222', 'Restricted Source', restricted=1,
                          people=('p-aaaaaaaaaa', 'p-bbbbbbbbbb'))
        self._seed_claim('c-2222222222', 's-2222222222', 'relationship',
                         'Bob Restricted Parent is the parent of Alice Living',
                         status='accepted', persons=('p-aaaaaaaaaa', 'p-bbbbbbbbbb'),
                         roles={'p-aaaaaaaaaa': 'child', 'p-bbbbbbbbbb': 'parent'})
        self._seed_home()
        self._run(linked=False)
        home = self._read('index.html')
        # Bob is otherwise an ordinary curated/deceased person, so he still
        # gets his own page and a site-wide People-index entry - that part
        # is correct and unrelated to this bug. What must NOT happen is Bob
        # becoming the PEDIGREE HUB: no card, link, or "Bob's family..."
        # caption inside the pedigree section itself.
        ped = self._pedigree_section(home)
        self.assertNotIn('Bob Restricted Parent', ped)
        self.assertNotIn('persons/p-bbbbbbbbbb.html', ped)
        self.assertNotIn('Alice Living', home)   # nowhere on the page - never gets a page
        self.assertIn('No ancestor eligible to publish was found', home)

    def test_linked_mode_never_substitutes_the_hub(self):
        self._seed_person('p-aaaaaaaaaa', 'Living Owner', living='true')
        self._seed_person('p-bbbbbbbbbb', 'Deceased Parent')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-aaaaaaaaaa')
        self._seed_home()
        self._run(linked=True)
        self.assertIn('Living Owner', self._read('index.html'))

    def test_no_eligible_ancestor_renders_a_blank_hub_with_a_note(self):
        # The hub is living and has NO recorded parents at all, so the
        # redaction-safe walk finds nobody eligible anywhere on the line -
        # the decided fallback: JUST the hub's own blank card (the hub is
        # still living/redacted), zero ancestor columns, and a plain-language
        # note instead of a chart mostly built of blank cards.
        #
        # Review fix (PR #152): a deceased spouse used to still draw on this
        # blank card - real name, dates, and a working link, bracketed
        # directly onto the living hub's own row - which outs a living
        # person's specific close relative on the site's highest-traffic
        # page. `_build_family_wings` now withholds ALL of the hub's own
        # spouse/child/sibling entries once the hub itself is redacted (same
        # "dropped outright, no placeholder" rule already applied to every
        # OTHER redacted person), so the fallback shows nothing beside the
        # blank card at all.
        self._seed_person('p-aaaaaaaaaa', 'Living Owner', living='true')
        self._seed_person('p-bbbbbbbbbb', 'Deceased Spouse')
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-bbbbbbbbbb')
        self._seed_home()
        self._run(linked=False)
        home = self._read('index.html')
        self.assertNotIn('Living Owner', home)
        # Deceased Spouse is otherwise ordinary/curated, so - like Bob above -
        # they still get their own page and a site-wide People-index entry;
        # what must NOT happen is drawing on the blank hub's own pedigree row.
        self.assertNotIn('Deceased Spouse', self._pedigree_section(home))
        self.assertNotIn('ped-axis-label', home)    # no ancestor columns to caption
        self.assertIn('No ancestor eligible to publish was found', home)
        self.assertIn('cannot be shown here without naming a living person', home)
        self.assertIn('cannot be shown here without naming a living person', home)

    def test_linked_mode_still_shows_the_hub_family_row_with_no_eligible_ancestor(self):
        # The silence rule above is standalone-only: `--linked`/workbench
        # never substitutes or redacts the configured hub in the first
        # place (`_build_home_pedigree`'s own contract), so the same shape -
        # a living hub with no recorded parents - still draws its real
        # spouse/child/sibling row in the local preview.
        self._seed_person('p-aaaaaaaaaa', 'Living Owner', living='true')
        self._seed_person('p-bbbbbbbbbb', 'Deceased Spouse')
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-bbbbbbbbbb')
        self._seed_home()
        self._run(linked=True)
        home = self._read('index.html')
        self.assertIn('Living Owner', home)
        self.assertIn('Deceased Spouse', home)

    def test_workbench_unknown_add_affordance_reaches_the_home_pedigree(self):
        # The existing 'Unknown - add' workbench affordance (missing_parent_of)
        # is threaded through unchanged - a known person with an unresearched
        # parent gets the clickable placeholder on the home pedigree too.
        self._seed_person('p-aaaaaaaaaa', 'Hub Person')
        self._seed_home()
        res = self._run(linked=True, workbench=True)
        self.assertEqual(res['status'], 'ok')
        home = self._read('index.html')
        self.assertIn('Unknown', home)
        self.assertIn('data-wb-open="tpl-add-family"', home)


def _ped_link_row_collisions(svg):
    """Every genuine collinear overlap between two DIFFERENT `ped-link`
    horizontal segments in a pedigree/family SVG: same y, a real
    (non-zero-length) shared x-range, and the two segments do NOT share a
    common endpoint. A shared endpoint is the correct "one line forks here"
    convention used throughout this chart - an ancestor elbow leaving the
    same card toward two different grandparent slots, a couple bracket's own
    branch-to-trunk stub meeting its own children's spokes at the trunk, and
    so on - not a bug. #120's three reported collisions (subject-alone lane
    vs. the first spouse's bracket departure; the first marriage's branch
    point vs. a subject-alone child; a spouse's own row vs. a subject-alone
    child) were all this OTHER shape: two segments with DIFFERENT origins
    that still ran on top of each other along a real stretch. Used to check
    quantitatively (real coordinates, not eyeballing markup) that no such
    overlap remains."""
    segs = []
    for cls, d in re.findall(r'<path class="([^"]*)" d="([^"]+)"/>', svg):
        if 'ped-link' not in cls:
            continue
        pts = []
        cur = None
        for op, val in re.findall(r'([MHV])(-?\d+(?:,-?\d+)?)', d):
            if op == 'M':
                x, y = map(int, val.split(','))
                cur = (x, y)
            elif op == 'H':
                cur = (int(val), cur[1])
            else:  # 'V'
                cur = (cur[0], int(val))
            pts.append(cur)
        segs.extend(zip(pts, pts[1:]))
    collisions = []
    for i, (a1, b1) in enumerate(segs):
        for a2, b2 in segs[i + 1:]:
            if a1[1] != b1[1] or a2[1] != b2[1] or a1[1] != a2[1]:
                continue                                   # not both horizontal on the same row
            lo1, hi1 = sorted((a1[0], b1[0]))
            lo2, hi2 = sorted((a2[0], b2[0]))
            lo, hi = max(lo1, lo2), min(hi1, hi2)
            if hi - lo <= 0:
                continue                                   # touching at a point (or not at all)
            if a1 in (a2, b2) or b1 in (a2, b2):
                continue                                   # shared endpoint - an intentional fork
            collisions.append(((a1, b1), (a2, b2)))
    return collisions


class FamilyChartTests(_Base):
    """Win 1 (plan 17): the person-page pedigree grows spouse + children
    columns (children left, subject + spouse(s), parents, grandparents right -
    see private/wireframes/person.html for the illustrative layout). `_seed_rel`
    mirrors index.py's derived edge directions: `child` is written on the
    PARENT's row pointing at the child (`person_id`=parent, `other_id`=child);
    `spouse` is reciprocal but a single direction is enough for a page built
    from that person's own point of view."""

    def test_family_chart_shows_spouse_and_children(self):
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', surname='Hartley')
        self._seed_person('p-bbbbbbbbbb', 'Margaret Cole', surname='Cole')
        self._seed_person('p-cccccccccc', 'Ethel Hartley', surname='Hartley')
        self._seed_person('p-dddddddddd', 'Calvin Hartley', surname='Hartley')
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-bbbbbbbbbb')
        self._seed_rel('p-aaaaaaaaaa', 'child', 'p-cccccccccc')
        self._seed_rel('p-aaaaaaaaaa', 'child', 'p-dddddddddd')
        self._run(linked=True)
        page = self._read('persons/p-aaaaaaaaaa.html')
        # A children column pushes the chart to 4 columns - the compact variant.
        self.assertIn('class="pedigree pedigree-family"', page)
        self.assertIn('Margaret Cole', page)
        self.assertIn('Ethel Hartley', page)
        self.assertIn('Calvin Hartley', page)
        # Chart heading tracks the same spouse-or-children test the SVG
        # aria-label uses (site.py chart_title): a family chart says "Family".
        self.assertIn('Family</summary>', page)

    def test_ancestor_only_pedigree_unchanged_without_family(self):
        # No spouse/children at all: today's ancestor-only shape is preserved
        # exactly - plain `pedigree` class, no compact family variant, and the
        # heading reads "Ancestors" (chart honesty: no spouse/child column
        # means it isn't a family chart).
        self._seed_person('p-aaaaaaaaaa', 'Child Carl', surname='Carl')
        self._seed_person('p-bbbbbbbbbb', 'Parent Pat', surname='Pat')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb')
        self._seed_rel('p-bbbbbbbbbb', 'child', 'p-aaaaaaaaaa')
        self._run(linked=True)
        page = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('class="pedigree"', page)
        self.assertNotIn('pedigree-family', page)
        self.assertIn('Ancestors</summary>', page)

    def test_family_chart_renders_with_no_known_ancestors(self):
        # Win 1 drops the old "only if >=1 known ancestor" gate: a subject
        # with zero recorded parents but a spouse still gets a family chart.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_person('p-bbbbbbbbbb', 'Margaret Cole')
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-bbbbbbbbbb')
        self._run(linked=True)
        page = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('class="pedigree"', page)
        self.assertIn('Margaret Cole', page)
        # #119 regression: zero known parents (an empty `labels` beyond the
        # subject) used to collapse the reserved ancestor band down to just
        # the subject's own row - but the two 'Unknown' parent placeholders
        # still draw at their normal offset rows regardless (the render dict
        # always adds them once the subject draws), so they landed outside
        # the too-small viewBox and were silently clipped by the SVG's
        # default overflow:hidden. Both placeholders must fall entirely
        # inside the computed viewBox's numeric bounds, not merely be
        # present somewhere in the markup.
        svg_match = re.search(
            r'<svg class="pedigree[^"]*" viewBox="0 0 ([\d.]+) ([\d.]+)"[^>]*>(.*?)</svg>',
            page, re.S)
        self.assertIsNotNone(svg_match)
        vb_w, vb_h = float(svg_match.group(1)), float(svg_match.group(2))
        placeholders = re.findall(
            r'<foreignObject x="(-?\d+)" y="(-?\d+)" width="(\d+)" height="(\d+)">'
            r'<div[^>]*class="ped-node ped-empty"', svg_match.group(3))
        self.assertEqual(len(placeholders), 2)    # both parent slots unknown
        for x, y, w, h in placeholders:
            x, y, w, h = int(x), int(y), int(w), int(h)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, vb_w)
            self.assertLessEqual(y + h, vb_h)

    def test_subject_alone_lane_does_not_collide_with_first_spouse_bracket(self):
        # #120: a blended family - a subject-alone children lane (kids with
        # no drawn co-parent) plus at least one drawn spouse with their own
        # children - used to draw the first spouse's couple bracket leaving
        # the subject's card on the EXACT row the subject-alone lane's own
        # horizontal segment already occupies (the bracket's stagger offset
        # is indexed on the spouse loop alone, and evaluates to zero for
        # spouse index 0). That is a true collinear overlap between two
        # lanes, contradicting the routing code's own comment that "no two
        # lanes share a collinear segment". Both segments leave the subject
        # at the same x (the subject's own column) - they must not also
        # share the same y, or they lie on the same line.
        svg = site._render_pedigree_svg(
            {1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}}},
            spouses=[{'name': 'Spouse', 'id': 'p-bbbbbbbbbb', 'url': None, 'dates': {}}],
            children=[{'name': 'Alone Kid', 'co_parents': [], 'url': None, 'dates': {}},
                      {'name': 'Spouse Kid', 'co_parents': ['p-bbbbbbbbbb'],
                       'url': None, 'dates': {}}])
        # The subject-alone lane's own leg: a plain "leave the subject, go to
        # the trunk" horizontal with no V/H after it - unlike a couple
        # bracket, which always closes back to the x it left from.
        alone = re.search(r'<path class="ped-link" d="M(\d+),(\d+) H(\d+)"/>', svg)
        self.assertIsNotNone(alone)
        alone_x, alone_y = int(alone.group(1)), int(alone.group(2))
        # The first spouse's couple bracket: leaves the subject, drops to the
        # spouse's row, and returns to the SAME x it left from (backreference
        # \1) - the closed shape that marks a couple bracket in this file's
        # other tests too.
        bracket = re.search(
            r'<path class="ped-link" d="M(\d+),(\d+) H(\d+) V(\d+) H\1"/>', svg)
        self.assertIsNotNone(bracket)
        bracket_x, bracket_y = int(bracket.group(1)), int(bracket.group(2))
        self.assertEqual(alone_x, bracket_x)      # both leave the subject's own column
        self.assertNotEqual(alone_y, bracket_y)   # but not on the same row - no collinear overlap
        self.assertEqual(_ped_link_row_collisions(svg), [])

    def test_first_marriage_branch_point_does_not_collide_with_a_subject_alone_child(self):
        # #120, reopened a second time after the stagger fix above: the first
        # marriage's branch-to-children point is the arithmetic MIDPOINT of
        # the subject's and spouse's rows - which, with exactly one spouse,
        # is always the exact same row the combined children band centres
        # on too (both are the midpoint of the same two rows). An ODD total
        # child count then puts one child dead-centre on that row: two
        # subject-alone kids (drawn first, nearest the subject) plus the
        # spouse's own one kid is three children total, so the SECOND
        # subject-alone child landed exactly on the branch point's row -
        # the branch stub (a short horizontal leaving the couple junction
        # toward the trunk) ran collinear with that child's own spoke line
        # into the same children column.
        svg = site._render_pedigree_svg(
            {1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}}},
            spouses=[{'name': 'Spouse', 'id': 'p-bbbbbbbbbb', 'url': None, 'dates': {}}],
            children=[{'name': 'Alone Kid 1', 'co_parents': [], 'url': None, 'dates': {}},
                      {'name': 'Alone Kid 2', 'co_parents': [], 'url': None, 'dates': {}},
                      {'name': 'Spouse Kid', 'co_parents': ['p-bbbbbbbbbb'],
                       'url': None, 'dates': {}}])
        self.assertEqual(_ped_link_row_collisions(svg), [])
        # Pin down the actual mechanism, not just the absence of a collision:
        # the branch point is the couple bracket's own midpoint row (subj_y +
        # spouse_y) / 2 - extract it from the bracket path itself and check
        # it against the specific child that used to sit on it.
        y_of = {}
        for x, y, h, inner in re.findall(
                r'<foreignObject x="(-?\d+)" y="(-?\d+)" width="\d+" height="(\d+)">(.*?)</foreignObject>',
                svg, re.S):
            m = re.search(r'ped-name[^>]*>([^<]+)<', inner)
            if m:
                y_of[m.group(1)] = int(y) + int(h) // 2
        # The branch stub's y is the couple bracket's own midpoint row -
        # extract it directly from the bracket path (subj_y + spouse_y) / 2.
        bracket = re.search(r'<path class="ped-link" d="M(\d+),(\d+) H(\d+) V(\d+) H\1"/>', svg)
        self.assertIsNotNone(bracket)
        branch_y = (int(bracket.group(2)) + int(bracket.group(4))) // 2
        self.assertNotEqual(branch_y, y_of['Alone Kid 2'])

    def test_spouse_row_does_not_collide_with_a_subject_alone_child(self):
        # #120, third instance: a spouse's OWN card (and the couple
        # bracket's final leg routing into it) landed on the same row as a
        # subject-alone child once enough subject-alone kids pushed the
        # combined children band that far - five subject-alone children plus
        # the spouse's own one child (six total) reproduces the reported
        # shape (a real page with six children, five subject-alone).
        children = [{'name': f'Alone Kid {i}', 'co_parents': [], 'url': None, 'dates': {}}
                    for i in range(5)]
        children.append({'name': 'Spouse Kid', 'co_parents': ['p-bbbbbbbbbb'],
                          'url': None, 'dates': {}})
        svg = site._render_pedigree_svg(
            {1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}}},
            spouses=[{'name': 'Spouse', 'id': 'p-bbbbbbbbbb', 'url': None, 'dates': {}}],
            children=children)
        self.assertEqual(_ped_link_row_collisions(svg), [])
        y_of = {}
        for x, y, h, inner in re.findall(
                r'<foreignObject x="(-?\d+)" y="(-?\d+)" width="\d+" height="(\d+)">(.*?)</foreignObject>',
                svg, re.S):
            m = re.search(r'ped-name[^>]*>([^<]+)<', inner)
            if m:
                y_of[m.group(1)] = int(y) + int(h) // 2
        self.assertNotEqual(y_of['Spouse'], y_of['Alone Kid 3'])

    def test_two_spouses_and_subject_alone_lane_have_no_row_collisions(self):
        # #120's own repro only requires ONE spouse; a blended family with
        # TWO drawn marriages plus a subject-alone lane stacks three lanes
        # into the same shared children band, giving the row grid even more
        # chances to re-align with a reserved row one lane over. This exact
        # shape (3 subject-alone kids, 1 kid by the first wife, 1 by the
        # second) produces three separate collisions pre-fix - confirmed by
        # running this same construction with the fix reverted.
        svg = site._render_pedigree_svg(
            {1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}}},
            spouses=[{'name': 'Wife One', 'id': 'p-bbbbbbbbbb', 'url': None, 'dates': {}},
                     {'name': 'Wife Two', 'id': 'p-cccccccccc', 'url': None, 'dates': {}}],
            children=[{'name': 'Alone 1', 'co_parents': [], 'url': None, 'dates': {}},
                      {'name': 'Alone 2', 'co_parents': [], 'url': None, 'dates': {}},
                      {'name': 'Alone 3', 'co_parents': [], 'url': None, 'dates': {}},
                      {'name': 'W1 Kid', 'co_parents': ['p-bbbbbbbbbb'],
                       'url': None, 'dates': {}},
                      {'name': 'W2 Kid', 'co_parents': ['p-cccccccccc'],
                       'url': None, 'dates': {}}])
        self.assertEqual(_ped_link_row_collisions(svg), [])

    def test_single_lane_children_band_is_never_nudged(self):
        # The multi-lane fix above must be a true no-op for any chart with
        # only ONE co-parent lane in the children gap - there is no OTHER
        # lane to land on top of, so nudging would just be an unrequested
        # position change. A single spouse with their own 3 kids (no
        # subject-alone lane) is exactly the shape where, pre-#120's first
        # fix, the middle child already coincided with the couple's own
        # branch point - which is fine (same lane, the "line forks here"
        # convention), and must still coincide exactly after this fix, or
        # the fix moved a chart it was never supposed to touch.
        svg = site._render_pedigree_svg(
            {1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}}},
            spouses=[{'name': 'Spouse', 'id': 'p-bbbbbbbbbb', 'url': None, 'dates': {}}],
            children=[{'name': 'Kid1', 'co_parents': ['p-bbbbbbbbbb'], 'url': None, 'dates': {}},
                      {'name': 'Kid2', 'co_parents': ['p-bbbbbbbbbb'], 'url': None, 'dates': {}},
                      {'name': 'Kid3', 'co_parents': ['p-bbbbbbbbbb'], 'url': None, 'dates': {}}])
        y_of = {}
        for x, y, h, inner in re.findall(
                r'<foreignObject x="(-?\d+)" y="(-?\d+)" width="\d+" height="(\d+)">(.*?)</foreignObject>',
                svg, re.S):
            m = re.search(r'ped-name[^>]*>([^<]+)<', inner)
            if m:
                y_of[m.group(1)] = int(y) + int(h) // 2
        bracket = re.search(r'<path class="ped-link" d="M(\d+),(\d+) H(\d+) V(\d+) H\1"/>', svg)
        self.assertIsNotNone(bracket)
        branch_y = (int(bracket.group(2)) + int(bracket.group(4))) // 2
        self.assertEqual(branch_y, y_of['Kid2'])  # exact same-lane alignment, unchanged

    def test_family_chart_multiple_spouses_stack(self):
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_person('p-bbbbbbbbbb', 'First Wife')
        self._seed_person('p-cccccccccc', 'Second Wife')
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-bbbbbbbbbb')
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-cccccccccc')
        self._run(linked=True)
        page = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('First Wife', page)
        self.assertIn('Second Wife', page)

    def test_couple_junction_joins_before_children_split(self):
        # Owner request (review 2026-07-17): the subject's and spouse's lines
        # come together at the couple's junction FIRST, and only then does one
        # line split to their children - the ancestor elbow, mirrored.
        svg = site._render_pedigree_svg(
            {1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}}},
            spouses=[{'name': 'Spouse', 'id': 'p-bbbbbbbbbb', 'url': None, 'dates': {}}],
            children=[{'name': 'Kid', 'co_parents': ['p-bbbbbbbbbb'], 'url': None, 'dates': {}}])
        # The couple bracket: a path that leaves the subject, drops to the
        # spouse, and returns to the same column edge (start x == end x) -
        # ancestor elbows never close back on their own x.
        brackets = [m for m in re.findall(
            r'<path class="ped-link" d="M(\d+),(\d+) H(\d+) V(\d+) H(\d+)"/>', svg)
            if m[0] == m[4]]
        self.assertEqual(len(brackets), 1)
        jx = int(brackets[0][2])
        mid_y = (int(brackets[0][1]) + int(brackets[0][3])) // 2
        # One line leaves the couple's MIDPOINT (not the subject's own row)
        # toward the children trunk.
        self.assertIn(f'M{jx},{mid_y} H', svg)

    def test_children_group_by_their_other_parent(self):
        # A person with kids by two spouses: each couple gets its own bracket
        # and its own trunk; a child with no recorded co-parent hangs off the
        # subject alone. Kids D+E are with spouse B, kid F with spouse C, kid
        # G has no second parent recorded.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_person('p-bbbbbbbbbb', 'First Wife')
        self._seed_person('p-cccccccccc', 'Second Wife')
        self._seed_person('p-dddddddddd', 'Kid Dee')
        self._seed_person('p-eeeeeeeeee', 'Kid Eve')
        self._seed_person('p-ffffffffff', 'Kid Eff')
        self._seed_person('p-gggggggggg', 'Kid Gee')
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-bbbbbbbbbb')
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-cccccccccc')
        for kid in ('p-dddddddddd', 'p-eeeeeeeeee', 'p-ffffffffff', 'p-gggggggggg'):
            self._seed_rel('p-aaaaaaaaaa', 'child', kid)
            self._seed_rel(kid, 'parent', 'p-aaaaaaaaaa')
        self._seed_rel('p-dddddddddd', 'parent', 'p-bbbbbbbbbb')
        self._seed_rel('p-eeeeeeeeee', 'parent', 'p-bbbbbbbbbb')
        self._seed_rel('p-ffffffffff', 'parent', 'p-cccccccccc')
        self._run(linked=True)
        page = self._read('persons/p-aaaaaaaaaa.html')

        # Two couples -> two closed brackets (start x == end x), at DIFFERENT
        # junction x-stations so a second marriage never overlaps the first.
        # (The class is ped-link for the first marriage, ped-link-later for
        # every later one - the [^"]* absorbs the modifier.)
        brackets = [m for m in re.findall(
            r'<path class="ped-link[^"]*" d="M(\d+),(\d+) H(\d+) V(\d+) H(\d+)"/>', page)
            if m[0] == m[4]]
        self.assertEqual(len(brackets), 2)
        self.assertNotEqual(brackets[0][2], brackets[1][2])

        # Card centre-y by name, from the foreignObject geometry.
        y_of = {}
        for x, y, h, inner in re.findall(
                r'<foreignObject x="(-?\d+)" y="(-?\d+)" width="\d+" height="(\d+)">(.*?)</foreignObject>',
                page, re.S):
            m = re.search(r'ped-name[^>]*>([^<]+)<', inner)
            if m:
                y_of[m.group(1)] = int(y) + int(h) // 2

        # Children ticks are the horizontal-only links leaving the children
        # column's right edge; map each child's row to its trunk x.
        children_right = min(x for x, *_rest in
                             [(int(a), b) for a, b, _c in re.findall(
                                 r'<path class="ped-link[^"]*" d="M(\d+),(\d+) H(\d+)"/>', page)])
        trunk_of = {}
        for a, b, c in re.findall(r'<path class="ped-link[^"]*" d="M(\d+),(\d+) H(\d+)"/>', page):
            if int(a) == children_right:
                trunk_of[int(b)] = int(c)
        tick = {name: trunk_of[y_of[name]] for name in ('Kid Dee', 'Kid Eve', 'Kid Eff', 'Kid Gee')}
        # Same couple -> same trunk; different couples (and the no-co-parent
        # kid) -> different trunks.
        self.assertEqual(tick['Kid Dee'], tick['Kid Eve'])
        self.assertNotEqual(tick['Kid Dee'], tick['Kid Eff'])
        self.assertNotEqual(tick['Kid Dee'], tick['Kid Gee'])
        self.assertNotEqual(tick['Kid Eff'], tick['Kid Gee'])

    def test_later_marriage_renders_dotted_and_branches_at_the_spouse_row(self):
        # Owner decision (review 2026-07-17): the first marriage keeps the
        # solid join-then-split bracket; a later marriage's whole lane is
        # dotted (ped-link-later) and its children branch at that spouse's
        # OWN row - the subject/spouse-2 midpoint always lands exactly on
        # spouse 1's row, which read as spouse 1's line. The two brackets
        # must also leave the subject card at different y so the second
        # never retraces the first.
        svg = site._render_pedigree_svg(
            {1: {'name': 'Subject', 'url': None, 'redacted': False, 'dates': {}}},
            spouses=[{'name': 'Wife One', 'id': 'p-bbbbbbbbbb', 'url': None, 'dates': {}},
                     {'name': 'Wife Two', 'id': 'p-cccccccccc', 'url': None, 'dates': {}}],
            children=[{'name': 'Kid B', 'co_parents': ['p-bbbbbbbbbb'], 'url': None, 'dates': {}},
                      {'name': 'Kid C', 'co_parents': ['p-cccccccccc'], 'url': None, 'dates': {}}])
        solid = re.findall(r'<path class="ped-link" d="M(\d+),(\d+) H(\d+) V(\d+) H(\d+)"/>', svg)
        later = re.findall(r'<path class="ped-link ped-link-later" d="M(\d+),(\d+) H(\d+) V(\d+) H(\d+)"/>', svg)
        solid_brackets = [m for m in solid if m[0] == m[4]]
        later_brackets = [m for m in later if m[0] == m[4]]
        self.assertEqual(len(solid_brackets), 1)
        self.assertEqual(len(later_brackets), 1)
        # Different attachment y on the subject card - no retraced segment.
        self.assertNotEqual(solid_brackets[0][1], later_brackets[0][1])
        # The later couple's children branch leaves the junction at the
        # SPOUSE's row (the bracket's own bottom y), rendered dotted too.
        jx, spouse2_y = later_brackets[0][2], later_brackets[0][3]
        self.assertIn(f'<path class="ped-link ped-link-later" d="M{jx},{spouse2_y} H', svg)
        # The first couple's branch still leaves at the couple midpoint.
        jx1 = solid_brackets[0][2]
        mid_y = (int(solid_brackets[0][1]) + int(solid_brackets[0][3])) // 2
        self.assertIn(f'<path class="ped-link" d="M{jx1},{mid_y} H', svg)

    def test_spouses_order_by_marriage_date_not_id(self):
        # The solid "first marriage" bracket must mean the EARLIEST marriage:
        # spouse ids here sort z-wife before a-wife by date, the reverse of
        # their id order, so an id-ordered chart would dot the wrong wife.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley')
        self._seed_person('p-bbbbbbbbbb', 'Second By Date')
        self._seed_person('p-cccccccccc', 'First By Date')
        self.conn.execute(
            'INSERT INTO relationships(person_id, rel, other_id, claim_id, date_start) '
            "VALUES ('p-aaaaaaaaaa','spouse','p-bbbbbbbbbb','c-rrrrrrrrrr','1903')")
        self.conn.execute(
            'INSERT INTO relationships(person_id, rel, other_id, claim_id, date_start) '
            "VALUES ('p-aaaaaaaaaa','spouse','p-cccccccccc','c-ssssssssss','1898')")
        self._run(linked=True)
        page = self._read('persons/p-aaaaaaaaaa.html')
        y_of = {}
        for _x, y, h, inner in re.findall(
                r'<foreignObject x="(-?\d+)" y="(-?\d+)" width="\d+" height="(\d+)">(.*?)</foreignObject>',
                page, re.S):
            m = re.search(r'ped-name[^>]*>([^<]+)<', inner)
            if m:
                y_of[m.group(1)] = int(y) + int(h) // 2
        # Earlier marriage stacks nearer the subject (drawn first).
        self.assertLess(y_of['First By Date'], y_of['Second By Date'])

    def test_family_chart_redacts_living_spouse_and_child_standalone(self):
        # The non-negotiable case: a living spouse/child must never leak a
        # name or date into the standalone SVG, and (since there is no
        # 'Unknown' placeholder a child/spouse column can fall back to,
        # unlike an ancestor slot) they are omitted outright rather than
        # shown as a redaction chip.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', living='false')
        self._seed_person('p-bbbbbbbbbb', 'Living Spouse', living='true')
        self._seed_person('p-cccccccccc', 'Living Child', living='true')
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-bbbbbbbbbb')
        self._seed_rel('p-aaaaaaaaaa', 'child', 'p-cccccccccc')
        self._run(linked=False)
        standalone = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Living Spouse', standalone)
        self.assertNotIn('Living Child', standalone)
        # Everything that would have appeared was redacted, and there are no
        # ancestors either - the whole chart is correctly absent, not shown
        # empty (matches the pre-win-1 "no ancestors -> no chart" behavior).
        self.assertNotIn('class="pedigree"', standalone)

        self._run(linked=True)
        linked = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Living Spouse', linked)
        self.assertIn('Living Child', linked)

    def test_family_chart_redacted_child_omitted_deceased_sibling_kept(self):
        # A mixed household: one living child is dropped, one deceased child
        # still shows - proves the redaction is per-child, not all-or-nothing.
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', living='false')
        self._seed_person('p-bbbbbbbbbb', 'Living Child', living='true')
        self._seed_person('p-cccccccccc', 'Deceased Child', living='false')
        self._seed_rel('p-aaaaaaaaaa', 'child', 'p-bbbbbbbbbb')
        self._seed_rel('p-aaaaaaaaaa', 'child', 'p-cccccccccc')
        self._run(linked=False)
        page = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Living Child', page)
        self.assertIn('Deceased Child', page)


class DraftExclusionTests(_Base):
    """Unaccepted `<!-- AI-DRAFT ... -->` prose (AGENTS.md: draft prose stays
    inside its markers until the human accepts it via `fha confirm draft`)
    must never publish, and no AI marker may surface as visible page text."""

    def test_draft_block_excluded_human_prose_after_marker_kept(self):
        body = ('# Thomas\n## Biography\n'
                'Drafted census claim [S-1111111111].\n\n'
                'Second drafted paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - drafted from census -->\n\n'
                'A human-written paragraph that stays.\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=body)
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._run(linked=False)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('A human-written paragraph that stays.', html)
        self.assertNotIn('Drafted census claim', html)
        self.assertNotIn('Second drafted paragraph', html)
        self.assertNotIn('AI-DRAFT', html)

    def test_accepted_prose_published_marker_invisible(self):
        body = ('# T\n## Biography\n'
                'An accepted paragraph of biography.\n\n'
                '<!-- AI-ACCEPTED 2026-06-01 claude-x - drafted (accepted 2026-06-20) -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        self._run(linked=False)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('An accepted paragraph of biography.', html)
        self.assertNotIn('AI-ACCEPTED', html)

    def test_extend_flow_accepted_kept_new_draft_excluded(self):
        # The write-biography extend flow: an accepted block, then a fresh
        # draft appended below it. The accepted marker bounds the new block.
        body = ('# T\n## Biography\n'
                'The accepted early-life paragraph.\n\n'
                '<!-- AI-ACCEPTED 2026-06-01 claude-x - v1 (accepted 2026-06-20) -->\n\n'
                'A new unreviewed paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - v2 -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        self._run(linked=False)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('The accepted early-life paragraph.', html)
        self.assertNotIn('A new unreviewed paragraph.', html)
        self.assertNotIn('AI-DRAFT', html)
        self.assertNotIn('AI-ACCEPTED', html)

    def test_all_draft_biography_renders_like_no_biography(self):
        body = ('# T\n## Biography\n'
                'Entirely drafted paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - note -->\n\n'
                '## Stories\nA human tale.\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('<h2>Biography</h2>', html)     # no stray heading
        self.assertNotIn('Entirely drafted', html)
        self.assertIn('<h2>Stories</h2>', html)
        self.assertIn('A human tale.', html)

    def test_unmarked_prose_directly_above_draft_withheld_failsafe(self):
        # The block START is not syntactically encoded, so prose sitting
        # directly above a draft run (no marker/heading between) cannot be
        # told apart from the draft. It is withheld too - fail-closed is the
        # only safe direction for a publication path; it returns on accept.
        body = ('# T\n## Biography\n'
                'Older unmarked paragraph.\n\n'
                'Drafted paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - note -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Older unmarked paragraph.', html)
        self.assertNotIn('Drafted paragraph.', html)

    def test_stories_draft_excluded(self):
        body = ('# T\n## Biography\nHuman bio.\n\n'
                '## Stories\nA drafted tale.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - story -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        self._run(linked=False)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Human bio.', html)
        self.assertNotIn('A drafted tale.', html)
        self.assertNotIn('<h2>Stories</h2>', html)       # emptied section skipped

    def test_linked_mode_also_excludes_drafts(self):
        # The dev preview skips privacy redaction, but a draft is not privacy
        # material - it is not-yet-content, and the marker would render as
        # escaped junk. Both modes exclude it.
        body = ('# T\n## Biography\nDrafted paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - note -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Drafted paragraph.', html)
        self.assertNotIn('AI-DRAFT', html)

    # The marker-grammar unit tests moved to tests/test_lib_text.py with the
    # function itself (site consumes _lib.strip_unaccepted_drafts now); the
    # tests below cover site's own half of the contract - what a damaged
    # marker does to the built page.

    def test_damaged_marker_withholds_prose_and_warns(self):
        # X1 fail-closed: an unterminated marker means draft and accepted
        # prose can no longer be told apart. The page still builds, but its
        # whole prose surface is withheld, and one warning names the file
        # and the fix. The old behavior published the draft + the dangling
        # marker into the standalone site.
        body = ('# T\n## Biography\n'
                'Human paragraph.\n\n'
                'Drafted paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - note missing its arrow\n\n'
                '## Stories\nA human tale.\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=body)
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')            # build completes
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Drafted paragraph.', html)     # the leak, closed
        self.assertNotIn('AI-DRAFT', html)
        self.assertNotIn('Human paragraph.', html)       # withheld entirely
        self.assertNotIn('A human tale.', html)          # both sections
        warnings = [m for m in res['messages'] if 'damaged' in m]
        self.assertEqual(len(warnings), 1)               # one warning, not two
        self.assertIn('draft marker', warnings[0])
        self.assertIn('people/', warnings[0])            # names the file
        self.assertIn('rebuild', warnings[0])            # names the fix

    def test_wrap_style_marker_withholds_not_leaks(self):
        # Wrap-style authoring (marker above + /AI-DRAFT below) used to cut
        # the HUMAN text above and publish the draft below it. Fail closed.
        body = ('# T\n## Biography\n'
                'Human paragraph above.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - wrap -->\n'
                'Wrapped draft paragraph.\n'
                '<!-- /AI-DRAFT -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Wrapped draft paragraph.', html)
        self.assertNotIn('AI-DRAFT', html)
        self.assertTrue(any('damaged' in m for m in res['messages']))


class ScaffoldingBlockExclusionTests(_Base):
    """#75/#76: the visible purpose blockquote (every scaffolded document)
    and the `## Sources` GENERATED-BEGIN/END region (person profiles only)
    are instructions for whoever edits the record in the working archive,
    never content for a site visitor.

    Both are structurally excluded here, not filtered by a dedicated strip
    step the way `_strip_scaffolding_blocks` does for `fha packet`:
    `_extract_section` (person prose) and the source page's own `## Notes`
    reader key off a specific `## Heading` line and start capturing strictly
    after it, so anything sitting between the H1 and the first `##` heading -
    exactly where the purpose block always lives (SPEC §16a) - is never
    reachable in the first place. `_person_sources` (the per-person source
    footnote list) is built straight from the SQLite index, never by reading
    the profile's own `## Sources` region text. This class pins that
    structural guarantee down as a regression test, so it cannot go silently
    unnoticed if either reader's start boundary ever changes."""

    def test_purpose_block_and_sources_region_absent_from_person_page(self):
        body = (
            '# Thomas Hartley\n\n'
            "> **This person's record - yours to write.** The main page for "
            "this person: summary,\n"
            '> biography, relationships.\n\n'
            '## Sources\n'
            '<!-- GENERATED-BEGIN sources-index by sources-index on 2026-08-01 -->\n\n'
            '*(Generated by `sources-index` - do not edit; regenerate instead.)*\n\n'
            '**Census:** [[S-1111111111]]\n\n'
            '<!-- GENERATED-END sources-index -->\n\n'
            '## Biography\nHuman-written biography paragraph.\n\n'
            '## Research Notes\nA lead worth chasing.\n'
        )
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=body)
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        for linked in (False, True):
            with self.subTest(linked=linked):
                res = self._run(linked=linked)
                self.assertEqual(res['status'], 'ok')
                html = self._read('persons/p-aaaaaaaaaa.html')
                self.assertNotIn('yours to write', html)
                self.assertNotIn('GENERATED-BEGIN', html)
                self.assertNotIn('GENERATED-END', html)
                self.assertNotIn('do not edit; regenerate instead', html)
                # The rest of the page still builds normally - this is
                # exclusion of the scaffolding specifically, not a build
                # failure that happens to blank the whole page.
                self.assertIn('Human-written biography paragraph.', html)
                self.assertIn('A lead worth chasing.', html)

    def test_purpose_block_absent_from_source_page(self):
        frontmatter = (
            '---\nid: s-1111111111\ntitle: A Letter\nsource_type: letter\n'
            'citation: "A citation you can check."\n---\n\n'
            "> **This source's record - yours to write.** The citation and "
            "claims for one piece\n"
            "> of evidence. `fha process` scaffolded this file; everything "
            "below is yours to\n"
            '> correct and add to.\n\n'
            '## Claims\n```yaml\n```\n'
        )
        self._seed_source('s-1111111111', 'A Letter', frontmatter=frontmatter)
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')
        html = self._read('sources/s-1111111111.html')
        self.assertNotIn('yours to write', html)
        self.assertIn('A citation you can check.', html)   # page still built normally


class UnfilledPlaceholderSectionTests(_Base):
    """#125: a freshly-scaffolded person's Biography/Research Notes sections
    hold nothing but the record template's own authoring instructions until
    a human replaces them (`render_person_body_scaffold` /
    `ensure_person_body_sections`, SPEC §16). Before this fix, `_person_prose`
    only recognised `*(none yet)*` as "still empty" - so that placeholder
    text rendered on the generated page verbatim, as if it were the
    person's real, finished biography and research notes. It must be
    treated exactly like an actually-empty section: heading and all omitted."""

    def test_freshly_scaffolded_placeholders_omitted_standalone(self):
        # The exact shape `fha person new`/`fha stubs` write for a person
        # nobody has edited yet - built from the SAME renderer the scaffold
        # itself calls, not a hand-typed copy of its wording, so this test
        # cannot drift from what is actually scaffolded.
        body = render_person_body_scaffold('Thomas Hartley')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=body)
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Write their story in plain sentences', html)
        self.assertNotIn('gentle to-do list', html)
        self.assertNotIn('Open questions, hunches, and brick walls', html)
        self.assertNotIn('Delete this line', html)
        self.assertNotIn('<h2>Biography</h2>', html)
        self.assertNotIn('<h2>Research Notes</h2>', html)
        # No stray literal markdown punctuation either (the issue's second
        # symptom - raw backticks reads as broken output even standalone
        # from the "is this real content" question).
        self.assertNotIn('`(TODO: import source)`', html)

    def test_freshly_scaffolded_placeholders_omitted_linked(self):
        # The Research Notes placeholder embeds a `<!-- private -->` example
        # block, which `apply_private_fence` treats differently per build
        # mode (unwrapped here, dropped in standalone) - so this must be
        # checked in ITS OWN test, not assumed to follow from the standalone
        # case above.
        body = render_person_body_scaffold('Thomas Hartley')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=body)
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Write their story in plain sentences', html)
        self.assertNotIn('Open questions, hunches, and brick walls', html)
        self.assertNotIn('This block stays in your local', html)  # the private-fence example
        self.assertNotIn('<h2>Biography</h2>', html)
        self.assertNotIn('<h2>Research Notes</h2>', html)

    def test_real_biography_content_still_publishes(self):
        # The positive case: genuine prose that happens to share a few words
        # with the placeholder must NOT be mistaken for it - only an exact,
        # whole-section match is treated as unfilled.
        body = ('# Thomas Hartley\n\n'
                '## Biography\n'
                'Write their story? He already lived one worth telling: born '
                'in 1840 in New York, Thomas crossed the plains twice before '
                'he turned thirty.\n\n'
                '## Research Notes\n'
                'Open questions remain about his first wife - keep looking '
                'in the Carrow County probate index.\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=body)
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('<h2>Biography</h2>', html)
        self.assertIn('crossed the plains twice', html)
        self.assertIn('<h2>Research Notes</h2>', html)
        self.assertIn('Carrow County probate index', html)

    def test_one_section_filled_other_still_placeholder(self):
        # The two sections are independent: filling in one must not affect
        # whether the other (still untouched) publishes.
        body = render_person_body_scaffold('Thomas Hartley').replace(
            "## Biography\nWrite their story in plain sentences. Uncited prose is welcome - "
            "it's story and\ncontext, never treated as proven fact. Mark anything you mean to "
            "back up later\nwith `(TODO: import source)` and a tool will keep it on a gentle "
            "to-do list.\n",
            '## Biography\nBorn in 1840 in New York, Thomas grew up on his '
            "father's farm.\n",
        )
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=body)
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('<h2>Biography</h2>', html)
        self.assertIn("father's farm", html)
        self.assertNotIn('<h2>Research Notes</h2>', html)
        self.assertNotIn('Open questions, hunches, and brick walls', html)

    def test_placeholder_saved_by_a_windows_editor_is_still_omitted(self):
        # The whole-path version of test_templates' CRLF/trailing-space unit
        # tests. The archive owner is a non-technical genealogist editing
        # plain files in whatever editor he has; Notepad writes CRLF and
        # plenty of editors leave a trailing space behind. Neither changes a
        # word of what the section says, so neither may put the authoring
        # instructions back on the published page - which a byte-for-byte
        # comparison against the scaffold constant would do.
        body = render_person_body_scaffold('Thomas Hartley')
        body = '\n'.join(line + ' ' if line.strip() else line
                         for line in body.split('\n')).replace('\n', '\r\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=body)
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Write their story in plain sentences', html)
        self.assertNotIn('Open questions, hunches, and brick walls', html)
        self.assertNotIn('<h2>Biography</h2>', html)
        self.assertNotIn('<h2>Research Notes</h2>', html)


class LinkSchemeTests(unittest.TestCase):
    """Markdown-link URLs allowlist http/https/mailto; a javascript:/data:
    (or any other scheme-bearing) URL renders its label as plain text - the
    stored-XSS guard for prose published by the site."""

    def _html(self, prose):
        return site._prose_to_html(prose, lambda t, d=None: t)

    def test_javascript_uri_not_linked(self):
        out = self._html('[click](javascript:alert%281%29)')
        self.assertNotIn('<a ', out)
        self.assertNotIn('javascript', out)
        self.assertIn('click', out)

    def test_uppercase_scheme_not_linked(self):
        out = self._html('[x](JAVASCRIPT:alert%281%29)')
        self.assertNotIn('<a ', out)

    def test_data_uri_not_linked(self):
        out = self._html('[x](data:text/html,hello)')
        self.assertNotIn('<a ', out)
        self.assertNotIn('data:', out)

    def test_https_still_links(self):
        out = self._html('[site](https://example.org/page)')
        self.assertIn('<a href="https://example.org/page">site</a>', out)

    def test_mailto_still_links(self):
        out = self._html('[mail](mailto:a@b.example)')
        self.assertIn('<a href="mailto:a@b.example">mail</a>', out)

    def test_relative_url_still_links(self):
        out = self._html('[p](sub/page.html)')
        self.assertIn('<a href="sub/page.html">p</a>', out)

    def test_colon_after_first_slash_is_relative(self):
        out = self._html('[p](files/a:b.html)')
        self.assertIn('<a href="files/a:b.html">p</a>', out)

    def test_helper_scheme_matrix(self):
        self.assertIsNone(site._safe_link_href('javascript:x'))
        self.assertIsNone(site._safe_link_href('data:text/html,x'))
        self.assertIsNone(site._safe_link_href('vbscript:x'))
        self.assertIsNone(site._safe_link_href('file:///etc/passwd'))
        self.assertEqual(site._safe_link_href('HTTPS://X'), 'HTTPS://X')
        self.assertEqual(site._safe_link_href('#top'), '#top')


class OutputGuardTests(_Base):
    """`_reset_output` clears generically named subtrees, so a rebuild must
    first prove the --out folder is fha site's own: the `.fha-site` marker
    (stamped by every successful build), an empty/new folder, or the
    pre-marker legacy shape (index.html + vendor/fha-tree.js)."""

    def _run_to(self, out, *, dry_run=False):
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'index.sqlite', (future, future))
        return site.run_site(self.archive_root, out, linked=True, dry_run=dry_run)

    def test_refuses_nonempty_unowned_out_dir(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        out = self.archive_root / 'exports'
        (out / 'sources').mkdir(parents=True)            # shares a site subtree name
        (out / 'sources' / 'precious.txt').write_text('mine', encoding='utf-8')
        (out / 'notes.txt').write_text('also mine', encoding='utf-8')
        res = self._run_to(out)
        self.assertEqual(res['status'], 'bad-output')
        self.assertTrue(any("wasn't created by fha site" in m for m in res['messages']))
        # Nothing was deleted and nothing was built.
        self.assertEqual((out / 'sources' / 'precious.txt').read_text(encoding='utf-8'), 'mine')
        self.assertTrue((out / 'notes.txt').exists())
        self.assertFalse((out / 'index.html').exists())

    def test_empty_dir_builds_and_gains_marker(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        out = self.archive_root / 'exports'
        out.mkdir()
        res = self._run_to(out)
        self.assertEqual(res['status'], 'ok')
        self.assertTrue((out / '.fha-site').exists())

    def test_marked_dir_rebuilds(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self.assertEqual(self._run_to(self.out_dir)['status'], 'ok')
        self.assertTrue((self.out_dir / '.fha-site').exists())
        self.assertEqual(self._run_to(self.out_dir)['status'], 'ok')

    def test_legacy_prior_build_without_marker_rebuilds(self):
        # A site built before the marker shipped has index.html +
        # vendor/fha-tree.js but no .fha-site; it is accepted and gains the
        # marker on the rebuild (documented back-compat).
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self.assertEqual(self._run_to(self.out_dir)['status'], 'ok')
        (self.out_dir / '.fha-site').unlink()            # simulate the pre-marker build
        res = self._run_to(self.out_dir)
        self.assertEqual(res['status'], 'ok')
        self.assertTrue((self.out_dir / '.fha-site').exists())

    def test_interrupted_first_build_does_not_lock_output_dir(self):
        # X3 (round-2 finding 13): a crash/Ctrl-C after the reset but before
        # index.html used to leave a non-empty folder with no marker and no
        # index.html - the next run refused it as "wasn't created by fha
        # site" with no way out. The marker is now stamped the moment
        # _reset_output succeeds (the tool owns the dir it just cleared), so
        # the rerun simply rebuilds.
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        out = self.archive_root / 'exports'
        original = site._SiteBuilder.build_index_page

        def _boom(builder_self):
            raise RuntimeError('simulated mid-build crash')

        site._SiteBuilder.build_index_page = _boom
        try:
            with self.assertRaises(RuntimeError):
                self._run_to(out)
        finally:
            site._SiteBuilder.build_index_page = original
        # The interrupted build left the poison shape: files present (vendor
        # was copied), but no index.html - and, now, the ownership marker.
        self.assertTrue((out / 'vendor' / 'fha-tree.js').exists())
        self.assertFalse((out / 'index.html').exists())
        self.assertTrue((out / '.fha-site').exists())
        res = self._run_to(out)
        self.assertEqual(res['status'], 'ok')            # rebuilt, not refused
        self.assertTrue((out / 'index.html').exists())

    def test_dry_run_lists_would_remove_and_deletes_nothing(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self.assertEqual(self._run_to(self.out_dir)['status'], 'ok')
        before = sorted(str(p) for p in self.out_dir.rglob('*'))
        res = self._run_to(self.out_dir, dry_run=True)
        self.assertEqual(res['status'], 'dry-run')
        preview = res['reset_preview']
        self.assertIn('index.html', preview)
        self.assertIn('persons/', preview)
        self.assertEqual(before, sorted(str(p) for p in self.out_dir.rglob('*')))

    def test_dry_run_fresh_dir_has_empty_preview(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        res = self._run_to(self.out_dir, dry_run=True)
        self.assertEqual(res['status'], 'dry-run')
        self.assertEqual(res['reset_preview'], [])
        self.assertFalse(self.out_dir.exists())


class ProseConverterTests(unittest.TestCase):
    def test_headings_bold_lists_links(self):
        ident = lambda t: f'TOK({t})'  # noqa: E731
        out = site._prose_to_html(
            '## A Heading\n\nParagraph with **bold** and a [label](http://x).\n\n- one\n- two\n',
            ident)
        self.assertIn('<h3>A Heading</h3>', out)
        self.assertIn('<strong>bold</strong>', out)
        self.assertIn('<a href="http://x">label</a>', out)
        self.assertIn('<ul><li>one</li><li>two</li></ul>', out)

    def test_html_in_prose_is_escaped(self):
        out = site._prose_to_html('A <script>alert(1)</script> line.', lambda t: t)
        self.assertNotIn('<script>', out)
        self.assertIn('&lt;script&gt;', out)

    def test_token_delegated_to_renderer(self):
        out = site._prose_to_html('Born in [S-1111111111] year.', lambda t: f'<a>{t}</a>')
        self.assertIn('<a>S-1111111111</a>', out)

    # -- #144 review findings on _scrub_internal_encoding / _translate_date_before --

    def test_claim_id_paren_removal_reinserts_missing_space(self):
        # Finding 1: `_CLAIM_ID_PAREN_RE` eats an optional LEADING space along
        # with the parenthetical. When nothing separates the closing paren
        # from the next word, removing the match used to weld the two
        # surrounding words together ("recordconfirms" instead of "record
        # confirms") because there was never a trailing space to fall back on.
        out = site._scrub_internal_encoding('The record (C-4kx9m2p7qr)confirms the date.')
        self.assertEqual(out, 'The record confirms the date.')

    def test_claim_id_paren_removal_does_not_double_space(self):
        # The finding-1 fix must not overcorrect: when a space (or
        # punctuation) already follows the parenthetical, no second space
        # should be inserted.
        out = site._scrub_internal_encoding('The record (C-4kx9m2p7qr) confirms the date.')
        self.assertEqual(out, 'The record confirms the date.')
        out2 = site._scrub_internal_encoding('The record (C-4kx9m2p7qr).')
        self.assertEqual(out2, 'The record.')

    def test_before_bracket_with_invalid_day_left_unchanged(self):
        # Finding 2: a syntactically-matching but calendrically impossible
        # bound (February has no 31st) must not be translated to the
        # nonsensical "before February 31, 1900", and must not be silently
        # reduced to "before 1900" by dropping the bad groups either - the
        # original text is left exactly as written.
        out = site._scrub_internal_encoding('The land sold [..1900-02-31], records show.')
        self.assertIn('[..1900-02-31]', out)
        self.assertNotIn('before', out)

    def test_before_bracket_with_invalid_month_left_unchanged(self):
        # Finding 2, the other half: an invalid MONTH (13) must not be
        # silently truncated to "before 1900" either.
        out = site._scrub_internal_encoding('The land sold [..1900-13-01], records show.')
        self.assertIn('[..1900-13-01]', out)
        self.assertNotIn('before', out)

    def test_before_bracket_with_valid_date_still_translates(self):
        # Guards that finding 2's validation doesn't overreach: a real,
        # valid calendar date still translates exactly as before.
        out = site._scrub_internal_encoding('The land sold [..1900-02-28], records show.')
        self.assertIn('before February 28, 1900', out)
        self.assertNotIn('[..1900-02-28]', out)

    def test_before_bracket_as_interval_start_is_translated_in_full(self):
        # #144 finding 5 originally had `_translate_date_before` leave the
        # WHOLE interval untouched here, since translating only the
        # bracketed half would have produced the broken hybrid
        # "before 1900/1910", half English and half raw. #167 finding 3
        # completes the job instead of just avoiding the broken hybrid:
        # `_translate_date_before_slash` now renders BOTH bounds in plain
        # English, so raw `[..1900]/1910`-style notation no longer reaches
        # the reader-facing page for this shape. This replaces the old
        # "left untouched" expectation, which is intentionally no longer
        # true for this exact input.
        out = site._scrub_internal_encoding('They lived there [..1900]/1910, per the deed.')
        self.assertIn('before 1900 to 1910', out)
        self.assertNotIn('[..1900]/1910', out)

    def test_before_bracket_as_interval_end_is_translated_in_full(self):
        # #167 finding 3, the mirrored shape: certain start, uncertain end.
        out = site._scrub_internal_encoding('They lived there 1900/[..1910], per the deed.')
        self.assertIn('1900 to before 1910', out)
        self.assertNotIn('1900/[..1910]', out)

    def test_before_bracket_interval_with_month_day_both_sides(self):
        # #167 finding 3: the month/day formatting used for a lone bracket
        # (`_format_edtf_ymd`, shared with `_translate_date_before`) applies
        # to EACH side of the interval independently.
        out = site._scrub_internal_encoding(
            'Span: [..1900-05-03]/1910-06, per the deed.')
        self.assertIn('before May 3, 1900 to June 1910', out)
        out2 = site._scrub_internal_encoding(
            'Span: 1900-06/[..1910-05-03], per the deed.')
        self.assertIn('June 1900 to before May 3, 1910', out2)

    def test_before_bracket_interval_invalid_bracket_side_left_whole_untouched(self):
        # #167 finding 3: the same calendar validation `_translate_date_before`
        # applies to a lone bracket applies to the bracketed side of an
        # interval - an impossible date (no month 13) leaves the WHOLE
        # interval untouched rather than translate a bogus bound.
        out = site._scrub_internal_encoding(
            'Span: [..1900-13-01]/1910, per the deed.')
        self.assertIn('[..1900-13-01]/1910', out)
        self.assertNotIn('before', out)

    def test_before_bracket_interval_invalid_plain_side_left_whole_untouched(self):
        # #167 finding 3, mirrored: an impossible date on the PLAIN side
        # (no day 31 in February) also leaves the whole interval untouched.
        out = site._scrub_internal_encoding(
            'Span: [..1900]/1910-02-31, per the deed.')
        self.assertIn('[..1900]/1910-02-31', out)
        self.assertNotIn('before', out)

    def test_before_bracket_interval_both_sides_bracketed_left_untouched(self):
        # #167 finding 3: `[..YYYY]/[..YYYY]` (both sides bracketed) is
        # deliberately OUT of scope - `_lib.humanize_edtf`, the model for
        # this project's interval wording, has no established phrasing for
        # a bracket on both sides of a slash - so this shape still falls
        # through to `_translate_date_before`'s own adjacency guard and is
        # left exactly as written, same as before this fix.
        out = site._scrub_internal_encoding(
            'They lived there [..1900]/[..1910], per the deed.')
        self.assertIn('[..1900]/[..1910]', out)
        self.assertNotIn('before', out)

    def test_before_bracket_interval_digit_run_not_mistaken_for_bound(self):
        # #167 finding 3: the plain side's year must not swallow (or be
        # swallowed by) an adjacent digit that isn't really part of the
        # date - guards against a regex that's too loose about where the
        # plain bound starts/ends.
        out = site._scrub_internal_encoding('X [..1900]/19105 Y')
        self.assertIn('[..1900]/19105', out)
        self.assertNotIn('before', out)
        out2 = site._scrub_internal_encoding('X 21900/[..1910] Y')
        self.assertIn('21900/[..1910]', out2)
        self.assertNotIn('before', out2)

    # -- adversarial review follow-up on #167 finding 3 (plain-side qualifier) --

    def test_before_bracket_interval_trailing_plain_qualifier_uncertain(self):
        # Codex's exact repro: the plain RIGHT-hand bound carries a trailing
        # `?` (uncertain). Before this fix the regex's plain-side
        # alternative matched only the unqualified `1910` prefix, leaving
        # the `?` dangling, unconsumed, surviving literally in the output
        # right after "1910". The qualifier must now be consumed as part of
        # the match and humanized with this archive's existing "(unconfirmed)"
        # wording (`_lib._humanize_edtf_bound`), not left dangling.
        out = site._scrub_internal_encoding('They lived there [..1900]/1910?, per the deed.')
        self.assertIn('before 1900 to 1910 (unconfirmed)', out)
        self.assertNotIn('[..1900]/1910?', out)
        self.assertNotIn('1910?', out)

    def test_before_bracket_interval_leading_plain_qualifier_uncertain(self):
        # Codex's mirrored repro: the plain LEFT-hand bound carries the `?`.
        # Codex reports this shape was "left untouched" entirely - the
        # un-widened plain-side pattern could not reach past the `?` to find
        # the `/` right after it, so `_DATE_BEFORE_SLASH_RE` never matched
        # here at all.
        out = site._scrub_internal_encoding('They lived there 1900?/[..1910], per the deed.')
        self.assertIn('1900 (unconfirmed) to before 1910', out)
        self.assertNotIn('1900?/[..1910]', out)
        self.assertNotIn('1900?', out)

    def test_before_bracket_interval_plain_qualifier_approximate(self):
        # The archive's OTHER qualifier character, `~` (approximate), reads
        # as "about ..." (`_lib._humanize_edtf_bound`) rather than
        # "(unconfirmed)" - both qualifier characters, not just `?`, must be
        # consumed and humanized.
        out = site._scrub_internal_encoding('They lived there [..1900]/1910~, per the deed.')
        self.assertIn('before 1900 to about 1910', out)
        self.assertNotIn('1910~', out)

    def test_before_bracket_interval_qualified_plain_side_invalid_date_left_whole_untouched(self):
        # The existing "invalid date leaves the whole match untouched" guard
        # must still hold with a qualifier present alongside a genuinely
        # invalid date component (no month 13) - the qualifier is a
        # confidence marker, not part of calendar validity, so it does not
        # change which side is invalid or rescue the invalid one. The WHOLE
        # match, qualifier included, is left exactly as written.
        out = site._scrub_internal_encoding(
            'Span: [..1900]/1910-13-01?, per the deed.')
        self.assertIn('[..1900]/1910-13-01?', out)
        self.assertNotIn('before', out)

    # -- further adversarial review of #167 finding 3 (component-level qualifier) --

    def test_before_bracket_interval_component_level_approximate_month(self):
        # Codex's fresh repro: the plain bound's `~` sits COMPONENT-LEVEL,
        # attached to just the month ("1910-~06" = "1910, approximately
        # June" - the year is certain, only the month is a guess), not
        # trailing the whole date the way `pq1`/`pq2` already handled. Before
        # this fix the plain-side pattern stopped at the unqualified `1910`
        # prefix, leaving `-~06` dangling and untranslated right after it.
        out = site._scrub_internal_encoding(
            'They lived there [..1900]/1910-~06, per the deed.')
        self.assertIn('before 1900 to about June 1910', out)
        self.assertNotIn('[..1900]/1910-~06', out)
        self.assertNotIn('~06', out)

    def test_before_bracket_interval_component_level_approximate_month_mirrored(self):
        # Codex's mirrored repro: the plain LEFT-hand bound carries the
        # component-level `~`. Before this fix `_DATE_BEFORE_SLASH_RE` could
        # not reach past `-~06` to find the `/`, so this shape did not match
        # at all and stayed raw.
        out = site._scrub_internal_encoding(
            'They lived there 1910-~06/[..1900], per the deed.')
        self.assertIn('about June 1910 to before 1900', out)
        self.assertNotIn('1910-~06/[..1900]', out)
        self.assertNotIn('~06', out)

    def test_before_bracket_interval_component_level_approximate_day(self):
        # The same component-level `~` also legally attaches to just the DAY
        # (`_lib._EDTF_PATTERNS`'s `~?` before either the month OR the day
        # digit pair) with the month plain and certain.
        out = site._scrub_internal_encoding(
            'Span: [..1900]/1910-06-~15, per the deed.')
        self.assertIn('before 1900 to about June 15, 1910', out)
        self.assertNotIn('~15', out)

    def test_before_bracket_interval_component_qualifier_and_trailing_uncertain(self):
        # A trailing `?` and a leading component-level `~` are not mutually
        # exclusive in this dialect ("1910-~06?" is syntactically valid) -
        # when both are present, "unconfirmed" wins over "about", the same
        # precedence `_lib._humanize_edtf_bound` already applies when one
        # component carries both markers at once.
        out = site._scrub_internal_encoding(
            'Span: [..1900]/1910-~06?, per the deed.')
        self.assertIn('before 1900 to June 1910 (unconfirmed)', out)
        self.assertNotIn('about', out)
        self.assertNotIn('~06?', out)

    def test_before_bracket_interval_invalid_component_qualifier_left_whole_untouched(self):
        # The existing "invalid date leaves the whole match untouched" guard
        # extends to a component-level qualifier too: a `~` attached to a
        # genuinely impossible month (no month 13) is still calendrically
        # invalid regardless of the marker, and `is_valid_edtf` rejects it -
        # so the WHOLE match, marker included, is left exactly as written
        # rather than rendering the nonsensical "about 1910-~13" reading.
        out = site._scrub_internal_encoding(
            'Span: [..1900]/1910-~13, per the deed.')
        self.assertIn('[..1900]/1910-~13', out)
        self.assertNotIn('before', out)
        self.assertNotIn('about', out)

    def test_standalone_before_bracket_still_translates(self):
        # Guards that the finding-5 interval fix doesn't overreach: a
        # bracket that is NOT adjacent to a slash still translates normally.
        out = site._scrub_internal_encoding('They emigrated [..1905], settling nearby.')
        self.assertIn('before 1905', out)
        self.assertNotIn('[..1905]', out)

    def test_link_target_with_before_bracket_stays_a_working_link(self):
        # Finding 3: scrubbing the whole raw block BEFORE `_inline_html`
        # identified markdown links rewrote a `[..YYYY]`-shaped substring
        # INSIDE the URL, inserting a space that broke `_INLINE_RE`'s link
        # match entirely - the link rendered as dead literal text, not a
        # link at all.
        ident = lambda t: f'TOK({t})'  # noqa: E731
        out = site._prose_to_html(
            '[record](https://example.test/search/[..1905])', ident)
        self.assertIn('<a href="https://example.test/search/[..1905]">record</a>', out)
        self.assertNotIn('before 1905', out)

    def test_link_target_with_claim_id_paren_not_silently_truncated(self):
        # Finding 3, the claim-id case: the old order let
        # `_CLAIM_ID_PAREN_RE` silently DELETE a "(C-xxxxxxxxxx)" chunk out
        # of the URL before the link was ever matched, producing a link
        # that looked well-formed but silently pointed at the wrong
        # (truncated) target. The id must survive inside the href.
        #
        # P2 (PR #158 follow-up), the test-quality half: this test used to
        # only assert the id substring survived SOMEWHERE in the output -
        # which passed even though `_INLINE_RE`'s `lurl` group stopped at
        # the claim id's OWN closing paren (it did not count balanced
        # parens), producing a link that was still corrupted:
        # `<a href="...ref=(C-4kx9m2p7qr">record</a>)` - a TRUNCATED href
        # (missing its closing paren) plus a stray trailing ')' leaking as
        # literal text after `</a>`. Assert the complete, single anchor tag
        # (full href, no trailing ')') rather than substring presence.
        ident = lambda t: f'TOK({t})'  # noqa: E731
        out = site._prose_to_html(
            '[record](https://example.test/search?ref=(C-4kx9m2p7qr))', ident)
        self.assertEqual(
            out,
            '<p><a href="https://example.test/search?ref=(C-4kx9m2p7qr)">record</a></p>')

    def test_link_display_text_is_still_scrubbed(self):
        # The finding-3 fix protects the URL TARGET only - a link's visible
        # label is reader-facing prose like any other and must still be
        # scrubbed.
        ident = lambda t: f'TOK({t})'  # noqa: E731
        out = site._prose_to_html(
            '[The record (C-4kx9m2p7qr)](https://example.test/page)', ident)
        self.assertNotIn('C-4kx9m2p7qr', out)
        self.assertIn('<a href="https://example.test/page">The record</a>', out)

    def test_wikilink_display_label_claim_id_is_scrubbed(self):
        # P2 (PR #158 follow-up): a LABELED wikilink's display text
        # (`[[S-id|label]]`) is reader-facing prose exactly like a markdown
        # link's label (see test_link_display_text_is_still_scrubbed above) -
        # it must be scrubbed of an internal claim-id parenthetical before it
        # reaches render_token. The wikilink TARGET must stay untouched so
        # the link still resolves.
        seen = {}

        def render_token(tok, disp=None):
            seen['tok'], seen['disp'] = tok, disp
            return f'TOK({tok}|{disp})'

        out = site._prose_to_html(
            '[[S-1111111111|the record (C-4kx9m2p7qr)]]', render_token)
        self.assertNotIn('C-4kx9m2p7qr', out)
        self.assertEqual(seen['tok'], 'S-1111111111')       # target untouched
        self.assertNotIn('C-4kx9m2p7qr', seen['disp'] or '')

    # -- #167 (Codex P2 findings left over from PR #158) --

    def test_wikilink_label_with_date_bracket_notation_still_links(self):
        # #167 finding 2 (privacy-adjacent): a wikilink display label that
        # legitimately contains SPEC §11 before-date notation, e.g.
        # `record filed [..1905]`, used to trip `_INLINE_RE`'s `wdisp` group
        # (`[^\[\]]*` cannot match ANY bracket character) - the whole
        # `[[...]]` construct then failed to match as a wikilink at all and
        # fell through as ordinary literal text, which only gets HTML-
        # escaped, never scanned for bare ids. The raw internal source id
        # leaked onto the page, unlinked and unscrubbed. Assert the FULL
        # pipeline: a real citation link is produced (render_token is
        # actually called, proving `_INLINE_RE` matched the wikilink
        # alternative), its label comes back scrubbed AND humanized, and the
        # raw id string appears nowhere unlinked in the output.
        seen = {}

        def render_token(tok, disp=None):
            seen['tok'], seen['disp'] = tok, disp
            return f'<a href="../sources/{tok}.html">{disp}</a>'

        out = site._prose_to_html(
            '[[S-1111111111|record filed [..1905]]]', render_token)
        self.assertEqual(seen['tok'], 'S-1111111111')
        self.assertEqual(seen['disp'], 'record filed before 1905')
        self.assertIn('<a href="../sources/S-1111111111.html">record filed before 1905</a>', out)
        # The raw id must not appear anywhere OUTSIDE the link render_token
        # itself produced - i.e. not leaked as unlinked literal text.
        self.assertEqual(out.count('S-1111111111'), 1)
        self.assertNotIn('[..1905]', out)

    def test_wikilink_plain_label_unaffected_by_bracket_tolerant_wdisp(self):
        # #167 finding 2 regression guard: an ordinary label with no
        # brackets at all must behave exactly as before the `wdisp` widening.
        seen = {}

        def render_token(tok, disp=None):
            seen['tok'], seen['disp'] = tok, disp
            return f'TOK({tok}|{disp})'

        out = site._prose_to_html('[[S-1111111111|plain label]]', render_token)
        self.assertEqual(seen['tok'], 'S-1111111111')
        self.assertEqual(seen['disp'], 'plain label')
        self.assertIn('TOK(S-1111111111|plain label)', out)

    def test_wikilink_bracket_label_does_not_swallow_following_text(self):
        # #167 finding 2, greediness guard: the bracket-tolerant `wdisp`
        # must stop at the wikilink's OWN closing `]]` rather than reach
        # past it into unrelated bracketed text later in the same block.
        def render_token(tok, disp=None):
            return f'TOK({tok}|{disp})'

        out = site._prose_to_html(
            '[[S-1111111111|see [bar] here]] and then [S-2222222222] too.', render_token)
        self.assertIn('TOK(S-1111111111|see [bar] here)', out)
        self.assertIn('TOK(S-2222222222|None)', out)     # legacy [ID] token still parses after

    # -- adversarial review follow-up on #167 (both findings incomplete) --

    def test_wikilink_missing_close_bracket_does_not_leak_id(self):
        # Problem 1 repro 1: `wdisp`'s one-level bracket tolerance (#167
        # finding 2) does not save a construct with genuinely UNBALANCED
        # brackets - here the label's own `[oops]` closes, but the
        # wikilink's own trailing `]]` is missing one `]`. No fixed-depth
        # regex can match this as a wikilink, so it falls through as
        # literal text; the DEFENSE-IN-DEPTH backstop (`_BARE_ID_RE`) must
        # still keep the raw source id off the page.
        out = site._inline_html('[[S-1111111111|text [oops]]', lambda t, d=None: t)
        self.assertNotIn('S-1111111111', out)
        self.assertIn('[record]', out)

    def test_wikilink_swapped_bracket_order_does_not_leak_id(self):
        # Problem 1 repro 2: `][` in that order (not a balanced `[...]`
        # unit at all) - same failure mode, same backstop.
        out = site._inline_html('[[S-1111111111|text ][ stuff]]', lambda t, d=None: t)
        self.assertNotIn('S-1111111111', out)
        self.assertIn('[record]', out)

    def test_wikilink_nested_two_levels_does_not_leak_id(self):
        # Problem 1 repro 3: nesting two levels deep is past what ANY
        # fixed-depth regex (including the one-level-tolerant `wdisp`) can
        # balance - same failure mode, same backstop.
        out = site._inline_html('[[S-1111111111|text [a[b]c]]]', lambda t, d=None: t)
        self.assertNotIn('S-1111111111', out)
        self.assertIn('[record]', out)

    def test_markdown_link_label_bracket_notation_does_not_leak_id(self):
        # Problem 1 repro 4: the IDENTICAL vulnerability in `ltext` (a
        # markdown link's label), which - unlike `wdisp` - had never been
        # widened at all before this fix. Widening `ltext` the same way
        # `wdisp` was widened (#167 finding 2 continued) means this
        # particular one-level-nested shape now matches as a REAL, WORKING
        # link - a strictly better outcome than falling through to the
        # backstop - with the label's bare id still caught and redacted by
        # the same backstop that protects a failed-match run.
        out = site._inline_html(
            '[see S-1111111111 [..1905]](https://example.test/x)', lambda t, d=None: t)
        self.assertNotIn('S-1111111111', out)
        self.assertEqual(
            out, '<a href="https://example.test/x">see [record] before 1905</a>')

    # -- Codex review of #167 finding 3 (wikilink-vs-markdown-link precedence) --

    def test_wikilink_followed_by_parens_not_swallowed_as_markdown_link(self):
        # Codex's exact repro: the `ltext` bracket-nesting widening (above)
        # made a `[[S-id|source]]` wikilink immediately followed by `(note)`
        # look, from the markdown-link alternative's perspective, like a
        # label consisting of one balanced-bracket unit followed by a URL -
        # `<a href="note">[S-1111111111|source]</a>`, losing the internal
        # citation entirely. Precedence must go to the wikilink: the citation
        # renders through `render_token` and `(note)` survives as literal
        # trailing text, matching the pre-widening behaviour.
        def render_token(tok, disp=None):
            return f'<a href="../sources/{tok}.html">{disp}</a>'

        out = site._inline_html('[[S-1111111111|source]](note)', render_token)
        self.assertEqual(
            out, '<a href="../sources/S-1111111111.html">source</a>(note)')

    def test_genuine_markdown_link_still_works_after_wikilink_precedence_fix(self):
        # An ordinary markdown link (no embedded `[[...]]` at all) must be
        # completely unaffected by giving the wikilink alternative
        # precedence.
        out = site._inline_html('[label](https://example.test/x)', lambda t, d=None: t)
        self.assertEqual(out, '<a href="https://example.test/x">label</a>')

    def test_markdown_link_label_with_single_bracket_date_notation_still_works(self):
        # The tension the precedence fix has to resolve: a markdown-link
        # label that legitimately STARTS with one bracketed unit (the #167
        # finding-2-continued widening's whole point) - here SPEC §11
        # before-date notation right at the front of the label - must still
        # match as a markdown link, not be mistaken for a wikilink start
        # just because it also begins with `[`. The wikilink alternative
        # requires an immediate `]]` to close, which this shape never has
        # (only one `]` follows the date bracket, then more label text), so
        # it correctly falls through to the markdown-link alternative.
        out = site._inline_html('[see [..1900] record](url)', lambda t, d=None: t)
        self.assertEqual(out, '<a href="url">see before 1900 record</a>')

    def test_bare_id_backstop_does_not_fire_inside_a_matched_targets_own_id(self):
        # The backstop must never re-touch or corrupt an id `_INLINE_RE`
        # already correctly turned into a real citation link - it only ever
        # sees a construct's LABEL (or literal text between constructs),
        # never a link/wikilink/token's own TARGET. A wikilink whose LABEL
        # happens to mention a *different* id as plain descriptive text
        # exercises both halves of that invariant at once: the real target
        # (`S-2222222222`) must resolve to a normal working link, while the
        # incidental bare id sitting in the label text still gets redacted.
        seen = {}

        def render_token(tok, disp=None):
            seen['tok'], seen['disp'] = tok, disp
            return f'TOK({tok}|{disp})'

        out = site._inline_html(
            '[[S-2222222222|S-1111111111 mentioned]]', render_token)
        self.assertEqual(seen['tok'], 'S-2222222222')          # target untouched
        self.assertNotIn('S-1111111111', out)
        self.assertIn('[record] mentioned', out)

    def test_claim_id_paren_before_bold_keeps_separating_space(self):
        # #167 finding 1: `_scrub_internal_encoding` runs per literal-text
        # RUN inside `_inline_html`'s loop. When a claim-id parenthetical
        # sits at the very END of a run that is immediately followed - with
        # no space of its own - by a DIFFERENT matched inline construct
        # (here, `**bold**`), the substring handed to
        # `_scrub_internal_encoding` ends exactly at the match, so
        # `_strip_claim_id_paren`'s own "what follows?" check saw nothing
        # and dropped the separating space entirely - "record" and
        # "confirms" welded together with no space between them.
        ident = lambda t, d=None: t  # noqa: E731
        out = site._inline_html('The record (C-4kx9m2p7qr)**confirms**', ident)
        self.assertEqual(out, 'The record <strong>confirms</strong>')

    def test_claim_id_paren_before_markdown_link_keeps_separating_space(self):
        # #167 finding 1, the symmetric case named in the issue: the same
        # glue happens right before a markdown link.
        ident = lambda t, d=None: t  # noqa: E731
        out = site._inline_html(
            'The record (C-4kx9m2p7qr)[confirms](https://example.test)', ident)
        self.assertEqual(out, 'The record <a href="https://example.test">confirms</a>')

    def test_claim_id_paren_before_wikilink_keeps_separating_space(self):
        # #167 finding 1: the adjacency bug applies to ANY matched inline
        # construct following with no gap, not just bold/markdown-link -
        # including a wikilink, whose rendered HTML is opaque to
        # `_inline_html` (it cannot peek inside `render_token`'s output).
        def render_token(tok, disp=None):
            return f'<a>{tok}</a>'

        out = site._inline_html('The record (C-4kx9m2p7qr)[[S-1111111111]]', render_token)
        self.assertEqual(out, 'The record <a>S-1111111111</a>')

    def test_claim_id_paren_spacing_fix_does_not_double_space_at_boundary(self):
        # #167 finding 1 must not overcorrect: when a real space already
        # separates the parenthetical from the following construct, no
        # second space is inserted.
        ident = lambda t, d=None: t  # noqa: E731
        out = site._inline_html('The record (C-4kx9m2p7qr) **confirms**', ident)
        self.assertEqual(out, 'The record <strong>confirms</strong>')

    def test_claim_id_paren_at_true_end_of_block_still_drops_cleanly(self):
        # #167 finding 1 regression guard: when nothing follows the
        # parenthetical at all (the TRUE end of the block, not merely the
        # end of one internal literal-text run), the old "drop with no
        # trailing space" behavior is unaffected - there is no boundary
        # character to consult and none is invented.
        ident = lambda t, d=None: t  # noqa: E731
        out = site._inline_html('The record (C-4kx9m2p7qr)', ident)
        self.assertEqual(out, 'The record')

    # -- adversarial review follow-up on #167 finding 1 (boundary sentinel) --

    def test_claim_id_paren_before_bold_starting_with_punctuation_no_space(self):
        # Problem 2 repro 1: the prior fix's `_INLINE_BOUNDARY_CHAR`
        # sentinel always claimed "a word character follows," so a space
        # got inserted even when the bold text's REAL first character is
        # punctuation. The real first character (`m.group('bold')[0]`) must
        # be used instead, so no space is added before "!important".
        ident = lambda t, d=None: t  # noqa: E731
        out = site._inline_html('(C-4kx9m2p7qr)**!important**', ident)
        self.assertEqual(out, '<strong>!important</strong>')

    def test_claim_id_paren_before_markdown_link_starting_with_punctuation_no_space(self):
        # Problem 2 repro 2: same regression, a markdown link whose label
        # starts with punctuation.
        ident = lambda t, d=None: t  # noqa: E731
        out = site._inline_html('(C-4kx9m2p7qr)[!see](https://example.test)', ident)
        self.assertEqual(out, '<a href="https://example.test">!see</a>')

    def test_claim_id_paren_before_wikilink_label_starting_with_punctuation_no_space(self):
        # Problem 2 repro 3: same regression, a wikilink whose display
        # label starts with punctuation.
        def render_token(tok, disp=None):
            return f'<a>{disp}</a>'

        out = site._inline_html('(C-4kx9m2p7qr)[[S-1111111111|!see]]', render_token)
        self.assertEqual(out, '<a>!see</a>')

    # -- Codex review of #167 finding 2 (boundary sentinel used the RAW label) --

    def test_claim_id_paren_before_markdown_link_label_starting_with_date_bracket(self):
        # Codex's exact repro: the boundary check used to peek `ltext`'s RAW
        # first character (here `[`, from embedded `[..1905]` date
        # notation - not alphanumeric) instead of the SCRUBBED label's first
        # character (`b`, from "before 1905" - alphanumeric, once
        # `_DATE_BEFORE_RE` has translated the bracket). The raw peek wrongly
        # concluded no space was needed, so "The record" and "before 1905
        # confirms" ran together with no separating space. The boundary must
        # be computed from the scrubbed label, exactly as the wikilink
        # branch already does for `wdisp`.
        ident = lambda t, d=None: t  # noqa: E731
        out = site._inline_html(
            'The record (C-4kx9m2p7qr)[[..1905] confirms](https://x)', ident)
        self.assertEqual(
            out, 'The record <a href="https://x">before 1905 confirms</a>')

    def test_claim_id_paren_before_bold_starting_with_date_bracket(self):
        # Codex: "the bold branch has the same defect" - identical repro,
        # `**bold**` instead of a markdown link.
        ident = lambda t, d=None: t  # noqa: E731
        out = site._inline_html('(C-4kx9m2p7qr)**[..1905] confirms**', ident)
        self.assertEqual(out, ' <strong>before 1905 confirms</strong>')

    def test_bare_id_redaction_reinserts_missing_space(self):
        # `_BARE_ID_LABEL` is never dropped to nothing (unlike a claim-id
        # paren), but it still ends in a non-word `]` - the same welding
        # risk applies when a bare id sits at the very end of a literal-text
        # run immediately followed by a different matched construct with no
        # gap of its own.
        ident = lambda t, d=None: t  # noqa: E731
        out = site._inline_html('See S-1111111111**note**', ident)
        self.assertEqual(out, 'See [record] <strong>note</strong>')

    def test_bare_id_redaction_does_not_double_space(self):
        # Mirrors `_strip_claim_id_paren`'s own no-double-space guard: when
        # a real space (or punctuation) already follows the bare id, no
        # second space is inserted.
        out = site._scrub_internal_encoding('See S-1111111111 mentioned.')
        self.assertEqual(out, 'See [record] mentioned.')
        out2 = site._scrub_internal_encoding('See S-1111111111.')
        self.assertEqual(out2, 'See [record].')

    def test_two_bare_ids_glued_together_both_redact(self):
        # Adversarial review: a plain non-alphanumeric boundary treats a
        # SECOND id butted directly against the first, with no separator at
        # all, as "embedded inside a longer word" - the same shape the
        # boundary is deliberately guarding an ordinary word against - so
        # neither id's lookaround could ever close and BOTH leaked verbatim.
        out = site._scrub_internal_encoding('S-1111111111S-2222222222')
        self.assertEqual(out, '[record] [record]')

    def test_three_bare_ids_glued_together_all_redact(self):
        out = site._scrub_internal_encoding('S-1111111111P-3333333333C-4444444444')
        self.assertEqual(out, '[record] [record] [record]')

    def test_bare_id_still_ignored_inside_an_ordinary_word(self):
        # The zero-separator adjacency fix must not reopen the hole it's
        # named after: an id-shaped substring genuinely embedded inside a
        # longer alphanumeric run (not another complete id butted against
        # it) stays untouched.
        out = site._scrub_internal_encoding('wordS-1111111111word')
        self.assertEqual(out, 'wordS-1111111111word')

    def test_boundary_char_reads_the_actual_render_not_the_label(self):
        # Adversarial review, round 3: `render_token` does not always honor
        # the display text it's handed - a wikilink naming a redacted living
        # person ignores `in_display` entirely and substitutes fixed
        # replacement markup instead. The label here (a bare citation id)
        # scrubs to `[record]`, starting with `[` - not a word character -
        # but the ACTUAL render is "Living Person", which very much is one.
        # Computing the boundary from the label said no space was needed;
        # computing it from the render (what a reader truly sees) says one
        # is. Without the fix, "The record" and "Living Person" run
        # together with no space between them.
        render_token = lambda t, d=None: '<span class="redacted">Living Person</span>'  # noqa: E731
        out = site._inline_html(
            'The record (C-4kx9m2p7qr)[[P-1111111111|S-2222222222]]', render_token)
        self.assertEqual(
            out, 'The record <span class="redacted">Living Person</span>')

    def test_boundary_char_from_render_does_not_double_space(self):
        # Mirror of the no-double-space guard: when the actual render starts
        # with punctuation (not a word character), no space is inserted -
        # even though the discarded label (starting with the word character
        # "M", from "Margaret") would have said one was needed.
        render_token = lambda t, d=None: '<span class="redacted">!withheld</span>'  # noqa: E731
        out = site._inline_html('The record (C-4kx9m2p7qr)[[S-1111111111|Margaret]]', render_token)
        self.assertEqual(out, 'The record<span class="redacted">!withheld</span>')


class WorkbenchModeTests(_Base):
    """Workbench mode (serve-only) adds editing chrome and provisional vitals,
    and NONE of it may leak into a standalone or plain-linked build (the plan-17
    symmetry rule). Also: workbench requires linked."""

    _CTX = {'port': 8765, 'csrf_token': 'abc123', 'review_count': 2, 'inbox_count': 1}

    def _run_wb(self, *, workbench=True, linked=True):
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'index.sqlite', (future, future))
        return site.run_site(self.archive_root, self.out_dir, linked=linked,
                             workbench=workbench, workbench_context=self._CTX)

    def test_workbench_requires_linked(self):
        self.conn.commit()
        r = site.run_site(self.archive_root, self.out_dir, linked=False, workbench=True)
        self.assertFalse(r.ok)

    def test_cp1252_person_record_does_not_crash_workbench_extras(self):
        # The workbench-only enrichment reads (`_provisional_vital`,
        # `_person_hypothesis_ties`, `_hypothesis_parent_ids`,
        # `_build_family_wings`'s hypothesis branch) already caught a bad
        # decode through a bare `except Exception` before this fix - this
        # proves the explicit `on_decode_error` wiring did not change that:
        # the page still builds and the human still gets at least one
        # readable, plain-language warning naming the file - never a raw
        # UnicodeDecodeError traceback out of any of them.
        self._seed_person('p-aaaaaaaaaa', 'Prov Person', tier='curated',
                          frontmatter_extra='birth: 1923')
        broken = self.archive_root / 'people' / 'person__test_p-aaaaaaaaaa.md'
        broken.write_bytes(
            ('---\nid: p-aaaaaaaaaa\nname: Prov Person\nbirth: 1923\n'
             '---\n\n## Biography\n\nBorn in Kraków.\n').encode('cp1252'))
        res = self._run_wb()
        self.assertTrue(res.ok, res.messages)
        self.assertTrue((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())
        wb = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('estimate - unsourced', wb)   # provisional vital could not be read
        messages = res['messages']
        file_warnings = [m for m in messages if 'person__test_p-aaaaaaaaaa.md' in m]
        self.assertTrue(file_warnings, messages)
        self.assertTrue(all("isn't saved as UTF-8 text" in m for m in file_warnings),
                        file_warnings)
        self.assertFalse(any('codec' in m for m in messages), messages)

    def test_cp1252_stub_record_is_still_named_in_workbench(self):
        # Guards the reason `_hypothesis_parent_ids` and `_provisional_vital`
        # are allowed to stay silent about an undecodable file: workbench mode
        # builds a page for EVERY person in the index, stubs included, so
        # `_person_prose` names the file on that person's own page no matter
        # which ancestor walk reached them. If `prepare()`'s person_pages loop
        # ever stopped giving stubs a page, those silent sites would become a
        # real coverage gap and this test is what says so.
        self._seed_person('p-aaaaaaaaaa', 'Curated Kid', tier='curated')
        self._seed_person('p-bbbbbbbbbb', 'Stub Parent', tier='stub',
                          surname='Stub')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb')
        broken = self.archive_root / 'people' / 'stub__test_p-bbbbbbbbbb.md'
        broken.write_bytes(
            ('---\nid: p-bbbbbbbbbb\nname: Stub Parent\n'
             '---\n\n## Biography\n\nBorn in Krak\u00f3w.\n').encode('cp1252'))
        res = self._run_wb()
        self.assertTrue(res.ok, res.messages)
        self.assertTrue((self.out_dir / 'persons' / 'p-bbbbbbbbbb.html').exists())
        messages = res['messages']
        self.assertTrue(
            any('stub__test_p-bbbbbbbbbb.md' in m and "isn't saved as UTF-8 text" in m
                for m in messages), messages)

    def test_provisional_vital_shows_in_workbench_only(self):
        # A curated person with an unsourced birth: estimate and no birth claim.
        self._seed_person('p-bbbbbbbbbb', name='Prov Person', living='false',
                          tier='curated', frontmatter_extra='birth: 1923')
        # Workbench: the estimate appears, marked.
        self._run_wb()
        wb = self._read('persons/p-bbbbbbbbbb.html')
        self.assertIn('estimate - unsourced', wb)
        self.assertIn('fha serve', wb)          # serve bar
        self.assertIn('name="fha-csrf"', wb)     # CSRF meta
        # One source of truth (_lib.PROVISIONAL_VITAL_FIELDS): the same set that
        # decides which vitals get a provisional slot is handed to workbench.js
        # as a meta tag, sorted so the content is deterministic across runs.
        self.assertIn('<meta name="fha-provisional" content="birth death">', wb)
        # Standalone build of the SAME archive: none of it.
        import shutil as _sh
        _sh.rmtree(self.out_dir, ignore_errors=True)
        self._run(linked=False)
        std = self._read('persons/p-bbbbbbbbbb.html')
        self.assertNotIn('estimate - unsourced', std)
        self.assertNotIn('fha serve', std)
        self.assertNotIn('name="fha-csrf"', std)
        self.assertNotIn('name="fha-provisional"', std)

    def test_vital_edit_links_on_every_summary_row(self):
        # Owner decision, live review 2026-07-16 (reversing the round-2 PR #30
        # removal): EVERY summary row carries an edit affordance, per the
        # approved wireframe. A SOURCED vital's edit opens the CLAIM editor
        # (tpl-claim-edit) with the claim id + current data prefilled - the
        # claim context whose absence justified the earlier removal - so
        # editing changes the actual fact instead of minting a duplicate.
        self._seed_person('p-aaaaaaaaaa', name='Accepted Person', living='false',
                          tier='curated', frontmatter_extra='birth: 1899')
        self._seed_source('s-1111111111', 'Birth Record')
        self._seed_claim('c-1111111111', 's-1111111111', 'birth', '1900',
                         status='accepted', date_edtf='1900', persons=('p-aaaaaaaaaa',))
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        # The accepted claim wins over the frontmatter estimate (existing
        # contract) - and its edit link carries the claim's own context.
        self.assertNotIn('estimate - unsourced', wb)
        self.assertIn('wb-vital-edit', wb)
        self.assertIn('tpl-claim-edit', wb)
        self.assertIn('C-1111111111', wb)
        self.assertIn('data-wb-prefill', wb)

        self._seed_person('p-bbbbbbbbbb', name='Provisional Person', living='false',
                          tier='curated', frontmatter_extra='birth: 1923')
        self._run_wb()
        prov = self._read('persons/p-bbbbbbbbbb.html')
        self.assertIn('estimate - unsourced', prov)
        self.assertIn('wb-vital-edit', prov)
        # A vital with neither claim nor estimate gets a visible "not
        # recorded" row with a one-click add (wireframe).
        self.assertIn('not recorded', prov)

    def test_provisional_place_only_estimate_still_gets_a_row(self):
        # P2 codex finding (round 1, PR #31): a birth_place: with no birth:
        # (the normal "born in Kansas, no idea when" family knowledge the
        # set-estimate flags write) used to emit no row at all.
        self._seed_person('p-dddddddddd', name='Place Only', living='false',
                          tier='curated', frontmatter_extra='birth_place: Kansas')
        self._run_wb()
        wb = self._read('persons/p-dddddddddd.html')
        self.assertIn('estimate - unsourced', wb)
        self.assertIn('Kansas', wb)

    def test_provisional_prefill_keeps_date_and_place_separate(self):
        # The folded display string ("1923 - Kansas") must never reach the
        # edit modal's mdate field - person.estimate refuses it as a date.
        # The prefill carries mdate and mplace independently.
        self._seed_person('p-eeeeeeeeee', name='Date And Place', living='false',
                          tier='curated',
                          frontmatter_extra='birth: 1923\nbirth_place: Kansas')
        self._run_wb()
        wb = self._read('persons/p-eeeeeeeeee.html')
        self.assertIn('estimate - unsourced', wb)
        # Display still joins them for reading...
        self.assertIn('1923', wb)
        self.assertIn('Kansas', wb)
        # ...but the prefill JSON has them as separate fields.
        self.assertIn('"mdate": "1923"', wb)
        self.assertIn('"mplace": "Kansas"', wb)
        self.assertNotIn('"mdate": "1923 - Kansas"', wb)

    def test_provisional_vital_reads_the_record_once_per_person(self):
        # _provisional_vital's own docstring says it "runs up to four times
        # per page (birth/death x date/place)" - and, before this fix, each
        # call independently did a fresh read_record() (full file read +
        # YAML/markdown parse) of the SAME person record, discarding the
        # parse the previous call had just done. One parse per person per
        # build is enough; _person_summary asks for all four fields off the
        # SAME frontmatter snapshot.
        self._seed_person('p-aaaaaaaaaa', 'Prov Person', living='false', tier='curated',
                          frontmatter_extra=('birth: 1923\nbirth_place: Kansas\n'
                                             'death: 1990\ndeath_place: Texas'))
        builder = site._SiteBuilder(self.conn, self.archive_root, {}, self.out_dir,
                                    linked=True, workbench=True)
        builder.prepare()
        with unittest.mock.patch.object(
            site, 'read_record', wraps=site.read_record
        ) as spy:
            for field in ('birth', 'birth_place', 'death', 'death_place'):
                builder._provisional_vital('p-aaaaaaaaaa', field)
        self.assertEqual(spy.call_count, 1)

    def test_note_entry_edit_payload_is_the_as_written_text(self):
        # P2 codex finding (round 2, PR #31): the per-entry Stories/Research
        # edit buttons built old_text from the DISPLAY-filtered section, but
        # person.edit_note matches the exact on-disk paragraph - an entry
        # carrying an AI-ACCEPTED provenance marker or sitting in a private
        # fence could never be matched ("entry not found"), and the
        # replacement seed had the markers laundered away. The payload must
        # be the entry as written; only the rendered HTML is filtered.
        body = ('# Priya\n## Stories\n\n'
                'A kept memory. <!-- AI-ACCEPTED 2026-06-01 claude-x - v1 (accepted 2026-06-20) -->\n\n'
                '<!-- private -->\n\n'
                'A private story.\n\n'
                '<!-- /private -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Priya Rao', tier='curated', body=body)
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        # Both entries are shown, markers stripped from the rendered prose.
        rendered = wb.split('<template id="tpl-confirm">')[0]
        self.assertIn('A kept memory.', rendered)
        self.assertIn('A private story.', rendered)
        self.assertNotIn('&lt;!-- AI-ACCEPTED', rendered.split('data-wb-args')[0])
        # The edit payload (data-wb-args old_text / prefill text) carries the
        # marker exactly as written (tojson escapes '<' as <).
        self.assertIn('A kept memory. \\u003c!-- AI-ACCEPTED', wb)

    def test_hypothesis_parent_fills_the_pedigree_slot(self):
        # P2 codex finding (round 3, PR #31): a parent added through the
        # add-family flow lives only as a frontmatter hypothesis (never
        # indexed), so the pedigree's slot map - built from indexed
        # accepted edges - still drew 'Unknown - add' and a second click
        # minted a duplicate parent stub. In workbench mode the hypothesis
        # parent occupies the slot, visibly tagged; standalone stays
        # claims-only.
        rel = ('relationships:\n'
               '  - to: "[[p-bbbbbbbbbb|Hyp Parent]]"\n'
               '    type: parent\n'
               '    status: hypothesis')
        self._seed_person('p-aaaaaaaaaa', name='Child Person', living='false',
                          tier='curated', frontmatter_extra=rel)
        self._seed_person('p-bbbbbbbbbb', name='Hyp Parent', living='false',
                          tier='curated')
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('ped-hypothesis', wb)
        self.assertIn('unsourced hypothesis', wb)
        # The add-family lookup's generic '+ create' suppression travels on
        # the modal markup (data-wb-nocreate) - creation belongs to the
        # modal's own typed-name path (round-3 codex sibling finding).
        self.assertIn('data-wb-nocreate', wb)
        # Standalone build of the same archive: the unsourced tie stays home.
        import shutil as _sh
        _sh.rmtree(self.out_dir, ignore_errors=True)
        self._run(linked=False)
        std = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('ped-hypothesis', std)
        self.assertNotIn('Hyp Parent', std)

    def test_hypothesis_spouse_and_child_join_the_chart_wings(self):
        # P2 codex finding (round 5, PR #31): the add-family flow's spouse/
        # child hypotheses showed on the family strip but the chart's wings
        # read only the indexed (claim-backed) relationships table, so the
        # just-added family member was invisible in the Family chart. In
        # workbench mode they join the wings as dashed ped-hypothesis cards;
        # standalone stays claims-only.
        rel = ('relationships:\n'
               '  - to: "[[p-bbbbbbbbbb|Hyp Spouse]]"\n'
               '    type: spouse\n'
               '    status: hypothesis\n'
               '  - to: "[[p-cccccccccc|Hyp Child]]"\n'
               '    type: child\n'
               '    status: hypothesis')
        self._seed_person('p-aaaaaaaaaa', name='Subject Person', living='false',
                          tier='curated', frontmatter_extra=rel)
        self._seed_person('p-bbbbbbbbbb', name='Hyp Spouse', living='false',
                          tier='curated')
        self._seed_person('p-cccccccccc', name='Hyp Child', living='false',
                          tier='curated')
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('ped-hypothesis', wb)
        self.assertIn('Hyp Spouse', wb)
        self.assertIn('Hyp Child', wb)
        import shutil as _sh
        _sh.rmtree(self.out_dir, ignore_errors=True)
        self._run(linked=False)
        std = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('ped-hypothesis', std)
        self.assertNotIn('Hyp Spouse', std)
        self.assertNotIn('Hyp Child', std)

    def test_no_workbench_chrome_leaks_into_standalone(self):
        self._seed_person('p-cccccccccc', name='Plain Person', living='false',
                          tier='curated', frontmatter_extra='birth: 1900')
        self._seed_source('s-1111111111', title='S1')
        self._run(linked=False)
        for rel in ('index.html', 'persons/p-cccccccccc.html', 'sources/s-1111111111.html'):
            out = self._read(rel)
            for leak in ('fha serve', 'name="fha-csrf"', 'estimate - unsourced',
                         'workbench.js', 'data-wb-open', '/root/', 'name="fha-provisional"'):
                self.assertNotIn(leak, out, f'{leak!r} leaked into standalone {rel}')

    def test_milestone_modal_lists_cited_sources_and_paste_option(self):
        # Fix 4: the milestone modal's Source picker must offer this person's
        # own cited sources (never a raw S-id the human has to type from
        # memory) plus the paste-an-S-id escape hatch; and the milestone
        # openers must carry the person's display name so a sourced claim
        # composes as "birth of Jane Doe", never a bare P-id.
        self._seed_person('p-aaaaaaaaaa', name='Milestone Person', living='false')
        self._seed_source('s-1111111111', title='1900 Census', people=('p-aaaaaaaaaa',))
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('<option value="S-1111111111">S-1111111111 - 1900 Census</option>', wb)
        self.assertIn('<option value="__paste__">paste an S-id&hellip;</option>', wb)
        self.assertIn('"subject_name": "Milestone Person"', wb)

    def test_root_asset_url_percent_encodes_special_characters(self):
        # P2 codex finding (PR #30): an asset filename containing a URL
        # delimiter (`#`, `?`) was written verbatim into the workbench
        # `/root/<alias>/<relpath>` href. The BROWSER strips a `#`
        # fragment or `?` query before the request ever reaches serve, so
        # `_resolve_root_request` got a truncated path and 404'd even
        # though the file exists on disk.
        self._seed_source('s-1111111111', 'Has Odd Filename')
        asset_rel = 'documents/census/family #2 record.txt'
        asset_path = self.archive_root / asset_rel
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_text('page text', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', asset_rel, 'page-1'))
        self._run_wb()
        wb = self._read('sources/s-1111111111.html')
        self.assertIn('/root/documents/census/family%20%232%20record.txt', wb)
        # The raw, unencoded '#' never appears mid-href (it would truncate
        # the URL at the browser before the request is even sent).
        self.assertNotIn('href="/root/documents/census/family #2', wb)

    def test_milestone_modal_omits_uncited_source(self):
        # A source that does not cite this person must not appear in their
        # picker - the list is scoped per person, not archive-wide.
        self._seed_person('p-aaaaaaaaaa', name='Milestone Person', living='false')
        self._seed_person('p-bbbbbbbbbb', name='Other Person', living='false')
        self._seed_source('s-1111111111', title='Someone Else Census', people=('p-bbbbbbbbbb',))
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Someone Else Census', wb)

    def test_certificate_source_type_option_removed(self):
        # P2 codex finding (round 3, PR #30): the "File as a source" modal
        # offered `certificate`, which is not in the SOURCE_TYPES controlled
        # vocabulary (`vital-record` covers it) - choosing it always got
        # refused by process.file. The option list must only ever offer
        # values `process.file` actually accepts.
        self._seed_person('p-aaaaaaaaaa', name='Test Person', living='false', tier='curated')
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('<option>certificate</option>', wb)
        self.assertIn('<option>vital-record</option>', wb)

    def test_ephemera_source_type_option_offered(self):
        # #114 follow-up (Codex review, PR #178): `ephemera` joined the
        # SOURCE_TYPES controlled vocabulary but the two "file it as a
        # source" modals (single file and bundle) still offered only the
        # older, fixed option list - a genealogist filing from the browser
        # workbench had no way to pick it. Both selects must offer it now.
        self._seed_person('p-aaaaaaaaaa', name='Test Person', living='false', tier='curated')
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        self.assertEqual(wb.count('<option>ephemera</option>'), 2,
                          'both the single-file and bundle "file as a source" '
                          'modals must offer ephemera')

    def test_living_modal_prefills_current_value_not_a_hardcoded_default(self):
        # P2 codex finding (round 3, PR #30): the Change-living modal always
        # defaulted its radio group to "deceased" regardless of the actual
        # person, so previewing/applying without manually re-picking could
        # flip a living/unknown person to deceased. The template no longer
        # hardcodes a checked default; the opener passes the person's real
        # current value for workbench.js's data-wb-fill to preselect.
        self._seed_person('p-aaaaaaaaaa', name='Living Person', living='true', tier='curated')
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('value="false" checked', wb)
        self.assertIn('data-wb-fill="value=true"', wb)

    def test_add_event_button_prefills_the_page_person_into_persons_field(self):
        # P2 codex finding (round 4, PR #30): "Add a timeline event" fixes
        # `persons` to the page person via `data-wb-args`, but the modal also
        # has a blank `name="persons"` input - collect() lets any non-blank
        # form field override a fixed arg of the same name, so typing a
        # second participant instead of retyping the page person's own P-id
        # silently dropped them from their own new event. The opener must
        # prefill the field (via data-wb-fill) so it never starts blank.
        self._seed_person('p-aaaaaaaaaa', name='Test Person', living='false', tier='curated')
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('data-wb-fill="persons=P-aaaaaaaaaa"', wb)

    def test_edit_biography_modal_prefills_existing_prose_not_blank(self):
        # P2 codex finding (round 4, PR #30): the biography editor is a
        # whole-section REPLACE (`person.edit --section biography`), but its
        # textarea started empty even for a person who already has prose -
        # previewing/applying a small addition without retyping the whole
        # section first would silently delete the existing biography.
        bio = ('# Priya\n## Biography\n'
               'Priya emigrated in 1962 and worked as a teacher.\n')
        self._seed_person('p-aaaaaaaaaa', 'Priya Rao', tier='curated', body=bio)
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn(
            '<textarea name="text" class="wb-target" style="min-height:12rem">'
            'Priya emigrated in 1962 and worked as a teacher.',
            wb)

    def test_edit_biography_modal_prefills_a_pending_ai_draft_verbatim(self):
        # P2 codex finding (round 5, PR #30): `biography_raw` was built from
        # `bio` AFTER `strip_unaccepted_drafts` stripped the pending
        # `<!-- AI-DRAFT ... -->` block out (the same variable the RENDERED
        # HTML is built from) - so the editor's prefill silently omitted an
        # unaccepted draft. Applying any small human edit from that prefill
        # would have deleted the draft outright, bypassing `fha confirm
        # draft` entirely. The editor must show the section exactly as
        # written, draft marker included, even though the published HTML
        # correctly excludes it.
        body = ('# Priya\n## Biography\n'
                'A human-written paragraph that stays.\n\n'
                '<!-- AI-ACCEPTED 2026-06-01 claude-x - v1 (accepted 2026-06-20) -->\n\n'
                'An unreviewed AI-drafted paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - v2 -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Priya Rao', tier='curated', body=body)
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        # Published HTML (the article body, BEFORE the modal <template>s that
        # hold the editor prefill - the draft text legitimately appears
        # there too, checked separately below): the accepted paragraph
        # survives (marker stripped), the still-pending draft is excluded.
        rendered = wb.split('<template id="tpl-confirm">')[0]
        self.assertIn('A human-written paragraph that stays.', rendered)
        self.assertNotIn('An unreviewed AI-drafted paragraph.', rendered)
        # Editor prefill: the whole section exactly as written - both markers
        # and the pending draft paragraph intact.
        self.assertIn(
            '<textarea name="text" class="wb-target" style="min-height:12rem">'
            'A human-written paragraph that stays.\n\n'
            '&lt;!-- AI-ACCEPTED 2026-06-01 claude-x - v1 (accepted 2026-06-20) --&gt;\n\n'
            'An unreviewed AI-drafted paragraph.\n\n'
            '&lt;!-- AI-DRAFT 2026-07-01 claude-x - v2 --&gt;',
            wb)

    def test_edit_biography_modal_prefills_a_private_fence_verbatim(self):
        # P2 codex finding (round 7, PR #30): `bio_as_written` was captured
        # AFTER `apply_private_fence(..., drop=False)` had already stripped
        # the `<!-- private -->`/`<!-- /private -->` marker comments (kept
        # in workbench/linked mode, but with the markers themselves
        # removed) - so the editor prefill showed private prose with no
        # fence at all. Applying any small edit from that prefill would
        # have replaced the whole section with unfenced text, publishing
        # the previously-private paragraph on a later standalone build.
        # The editor must show the fence markers exactly as written.
        body = ('# Priya\n## Biography\n'
                'A public paragraph.\n\n'
                '<!-- private -->\nA private paragraph.\n<!-- /private -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Priya Rao', tier='curated', body=body)
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        # Published (linked/workbench) HTML: private prose kept, marker stripped.
        rendered = wb.split('<template id="tpl-confirm">')[0]
        self.assertIn('A private paragraph.', rendered)
        self.assertNotIn('&lt;!-- private --&gt;', rendered)
        # Editor prefill: the fence markers are still there, verbatim.
        self.assertIn(
            '<textarea name="text" class="wb-target" style="min-height:12rem">'
            'A public paragraph.\n\n'
            '&lt;!-- private --&gt;\nA private paragraph.\n&lt;!-- /private --&gt;',
            wb)

    def test_family_strip_shows_an_unsourced_relate_hypothesis_in_workbench(self):
        # P2 codex finding (round 7, PR #30): the family strip's "+ add"
        # button runs `person.relate`, which (by design, SPEC §9) writes
        # ONLY an unsourced `relationships:` hypothesis entry on the record
        # file - it never reaches the `relationships` index table the strip
        # is normally built from. Previewing/applying the button's own
        # write left the strip showing nothing, as if the write had failed.
        # Workbench mode must surface the hypothesis (tagged so it is never
        # mistaken for a sourced tie); a standalone/plain-linked build must
        # never show it (it isn't a fact yet).
        self._seed_person('p-bbbbbbbbbb', 'Margaret Cole', tier='stub')
        self._seed_person(
            'p-aaaaaaaaaa', 'Thomas Hartley', tier='curated',
            frontmatter_extra=(
                'relationships:\n'
                '  - to: "[[P-bbbbbbbbbb|Margaret Cole]]"\n'
                '    type: parent\n'
                '    status: hypothesis'))
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Margaret Cole', wb)
        self.assertIn('wb-hypothesis-tag', wb)
        self.assertIn('(hypothesis)', wb)

        # Standalone build of the SAME archive: the hypothesis never appears
        # (not a sourced fact, and `_person_hypothesis_ties` is workbench-only).
        import shutil as _sh
        _sh.rmtree(self.out_dir, ignore_errors=True)
        self._run(linked=False)
        std = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Margaret Cole', std)
        self.assertNotIn('wb-hypothesis-tag', std)
        self.assertNotIn('hypothesis', std)

    def test_record_strip_on_home_and_discoveries_workbench_only(self):
        # Owner request (review 2026-07-17): every page opens with the same
        # jump-to-the-plain-file strip the person/source pages have. The home
        # page is backed by notes/home.md, discoveries by notes/discoveries.md
        # (place pages carry the identically-gated strip for places.yaml).
        # None of it may leak into a standalone build (plan-17 symmetry rule).
        self._seed_person('p-aaaaaaaaaa', 'Anyone', tier='curated')
        self._run_wb()
        home = self._read('index.html')
        self.assertIn('wb-record', home)
        self.assertIn('notes/home.md', home)
        self.assertIn('data-wb-open-file="notes/home.md"', home)
        disc = self._read('discoveries.html')
        self.assertIn('data-wb-open-file="notes/discoveries.md"', disc)

        import shutil as _sh
        _sh.rmtree(self.out_dir, ignore_errors=True)
        self._run(linked=False)
        self.assertNotIn('wb-record', self._read('index.html'))

    def test_family_strip_hypothesis_suppressed_when_claim_backs_the_same_tie(self):
        # The normal lifecycle: "+ add" writes a hypothesis first, the tie is
        # sourced later in review, and the hypothesis entry stays on the record
        # until lint walks the human through linking its claim. In that window
        # the strip has BOTH a claim-backed edge and a hypothesis for the same
        # pair - it must show the person once (the sourced form), never a
        # duplicate "(hypothesis)" row beside the real one.
        self._seed_person('p-bbbbbbbbbb', 'Louisa Denton', tier='stub')
        self._seed_person(
            'p-aaaaaaaaaa', 'Calvin Hartley', tier='curated',
            frontmatter_extra=(
                'relationships:\n'
                '  - to: "[[P-bbbbbbbbbb|Louisa Denton]]"\n'
                '    type: spouse\n'
                '    status: hypothesis'))
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-bbbbbbbbbb')
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        strip = wb.split('<template id="tpl-confirm">')[0]
        self.assertIn('Louisa Denton', strip)
        self.assertNotIn('wb-hypothesis-tag', strip)

    def test_edit_biography_modal_empty_when_no_biography_yet(self):
        self._seed_person('p-aaaaaaaaaa', 'No Bio Yet', tier='curated', body='# No Bio Yet\n')
        self._run_wb()
        wb = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn(
            '<textarea name="text" class="wb-target" style="min-height:12rem"></textarea>', wb)

    def test_edit_home_intro_modal_prefills_existing_text_not_blank(self):
        # Same whole-section REPLACE risk as the biography editor above, for
        # notes/home.md (`home.edit`).
        home_md = self.archive_root / 'notes' / 'home.md'
        home_md.parent.mkdir(parents=True, exist_ok=True)
        home_md.write_text('Welcome to the Rao family archive.\n', encoding='utf-8')
        self._run_wb()
        idx = self._read('index.html')
        self.assertIn(
            '<textarea name="text" style="min-height:12rem">Welcome to the Rao family archive.',
            idx)

    def test_edit_home_intro_modal_prefills_a_pending_ai_draft_verbatim(self):
        # P2 codex finding (round 7, PR #30): `intro_raw` was assigned from
        # `body` AFTER `strip_unaccepted_drafts` had reassigned it to the
        # stripped copy - the same bug the round-5 biography fix addressed,
        # just not caught here at the time. The homepage editor must show
        # notes/home.md exactly as written, pending draft marker included.
        home_md = self.archive_root / 'notes' / 'home.md'
        home_md.parent.mkdir(parents=True, exist_ok=True)
        home_md.write_text(
            'An unreviewed AI-drafted paragraph.\n\n'
            '<!-- AI-DRAFT 2026-07-01 claude-x - v1 -->\n\n'
            'A human-written welcome that stays.\n',
            encoding='utf-8')
        self._run_wb()
        idx = self._read('index.html')
        rendered = idx.split('<template id="tpl-confirm">')[0]
        self.assertIn('A human-written welcome that stays.', rendered)
        self.assertNotIn('An unreviewed AI-drafted paragraph.', rendered)
        self.assertIn(
            '<textarea name="text" style="min-height:12rem">'
            'An unreviewed AI-drafted paragraph.\n\n'
            '&lt;!-- AI-DRAFT 2026-07-01 claude-x - v1 --&gt;\n\n'
            'A human-written welcome that stays.',
            idx)

    def test_modals_render_without_error_on_a_page_with_no_person_in_context(self):
        # The biography-prefill fix above references `person.biography_raw`
        # from inside _modals.html, which is included on EVERY workbench
        # page (source pages, index, etc.) - not just person pages, where
        # `person` is never in the template context at all. A naive
        # `person.biography_raw` reference raises UndefinedError on those
        # pages instead of rendering blank.
        self._seed_source('s-1111111111', 'A Source')
        r = self._run_wb()
        self.assertTrue(r.ok, r.messages)
        self._read('sources/s-1111111111.html')


class OpenQuestionsSectionTests(_Base):
    """Issue #117: a person referenced by a `## Q:` block's `refs:` sees that
    question on their own page. Gated on `linked` (workbench always implies
    linked, see the _SiteBuilder constructor), not narrowed to `workbench`
    alone: a `## Q:` block still carries no `restricted:` field of its own,
    but `--linked` is this codebase's own established boundary for
    real-but-not-yet-publishable content (`drop_private=not self.linked`,
    `_person_is_redacted`'s `self.linked` check, both elsewhere on this same
    page) - an owner's own local `--linked` preview never leaves their
    machine, so it is treated the same way. The STANDALONE (published) build
    is the one place this must never appear (issue #117's reopening: it was
    gated so narrowly - workbench-only - that even the reporter's own
    unredacted `--linked` preview build showed nothing)."""

    def _write_questions_md(self, text: str) -> None:
        path = self.archive_root / 'notes' / 'questions.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def test_open_question_shows_in_linked_and_workbench_not_standalone(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe', tier='curated')
        self._write_questions_md(
            '# Open Questions\n\n'
            '## Q: When did Jane arrive in Kansas?\n'
            '- origin: human\n'
            '- status: open\n'
            '- refs: [P-aaaaaaaaaa]\n'
            '- context:\n'
            '  - (human, 2026-06-01) Census says 1880, no earlier record found.\n'
        )
        wb = self._run(linked=True, workbench=True)
        self.assertTrue(wb.ok, wb.messages)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Open Questions', html)
        self.assertIn('When did Jane arrive in Kansas?', html)
        self.assertIn('Census says 1880', html)
        self.assertIn('notes/questions.md', html)   # provenance caption

        # Plain `--linked` (no workbench) - the archive owner's own
        # unredacted local preview: the same section, same content. This is
        # the case issue #117's reopening found broken (nothing rendered).
        import shutil as _sh
        _sh.rmtree(self.out_dir, ignore_errors=True)
        lo = self._run(linked=True, workbench=False)
        self.assertTrue(lo.ok, lo.messages)
        lo_html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Open Questions', lo_html)
        self.assertIn('When did Jane arrive in Kansas?', lo_html)
        self.assertIn('Census says 1880', lo_html)
        self.assertIn('notes/questions.md', lo_html)

        # Standalone (public) build of the SAME archive: none of it. The
        # privacy boundary that matters - published output never carries an
        # un-vetted question about a possibly-living third party - must hold.
        _sh.rmtree(self.out_dir, ignore_errors=True)
        std = self._run(linked=False)
        self.assertTrue(std.ok, std.messages)
        std_html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Open Questions', std_html)
        self.assertNotIn('When did Jane arrive in Kansas?', std_html)
        self.assertNotIn('Census says 1880', std_html)

    def test_a_ref_naming_the_same_person_twice_does_not_double_render(self):
        # A `refs:` list is free-text-typed by a human; `refs: [P-x, P-x]` is
        # a plausible copy-paste slip. That must not render the SAME question
        # twice on P-x's own page - unlike a question legitimately naming
        # several different people, which correctly appears on each of them.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe', tier='curated')
        self._write_questions_md(
            '## Q: Repeated ref question\n'
            '- status: open\n'
            '- refs: [P-aaaaaaaaaa, P-aaaaaaaaaa]\n'
            '- context:\n'
            '  - (human, 2026-06-01) One question, one person, named twice.\n'
        )
        wb = self._run(linked=True, workbench=True)
        self.assertTrue(wb.ok, wb.messages)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertEqual(html.count('Repeated ref question'), 1)

    def test_answered_question_is_not_surfaced(self):
        # Only OPEN questions are a live pointer; an answered/closed one is
        # settled research, not something a page should keep raising.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe', tier='curated')
        self._write_questions_md(
            '# Open Questions\n\n'
            '## Q: When did Jane arrive in Kansas?\n'
            '- origin: human\n'
            '- status: answered [[S-1111111111]]\n'
            '- refs: [P-aaaaaaaaaa]\n'
            '- context:\n'
            '  - (human, 2026-06-01) 1880 census confirms arrival.\n'
        )
        wb = self._run(linked=True, workbench=True)
        self.assertTrue(wb.ok, wb.messages)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('When did Jane arrive in Kansas?', html)

    def test_question_in_a_different_persons_research_file_surfaces_here_too(self):
        # The issue's own open design question: does a question logged in
        # Person B's research file, but `refs:`-ing Person A, show on Person
        # A's page too? Implemented answer: yes - inherited for free from
        # `_lib.parse_questions` reading every person's research file, not a
        # new decision made in site.py. This was a live open question in the
        # issue, not a settled contract - flagged again in the PR body.
        #
        # Run under plain `--linked` (no workbench), not just workbench: the
        # #117 reopening's whole finding was that a plain `--linked` preview
        # showed nothing, so the cross-reference case needs to be proven in
        # exactly the mode that was reported broken, not only in workbench.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe', tier='curated')
        self._seed_person('p-bbbbbbbbbb', 'Bob Roe', tier='curated', surname='Roe')
        research_path = self.archive_root / 'people' / 'roe__test_research_p-bbbbbbbbbb.md'
        research_path.write_text(
            '---\nid: p-bbbbbbbbbb\ncreated: 2026-06-01\n---\n\n'
            '## Open Questions\n\n'
            '## Q: Are Jane and Bob related?\n'
            '- origin: human\n'
            '- status: open\n'
            '- refs: [P-aaaaaaaaaa, P-bbbbbbbbbb]\n'
            '- context:\n'
            '  - (human, 2026-06-01) Both lived in the same county in 1880.\n'
            '\n## Hypotheses\n\n*(none yet)*\n\n## Research Log\n\n*(none yet)*\n',
            encoding='utf-8')
        lo = self._run(linked=True, workbench=False)
        self.assertTrue(lo.ok, lo.messages)
        for pid in ('p-aaaaaaaaaa', 'p-bbbbbbbbbb'):
            html = self._read(f'persons/{pid}.html')
            self.assertIn('Are Jane and Bob related?', html, pid)
            self.assertIn('Both lived in the same county', html, pid)
            # #117 block-scoping guard: `_lib.parse_question_blocks` splits
            # purely on `## Q:` boundaries, so the raw block for the LAST
            # question in a file runs through to end-of-file - the sibling
            # `## Hypotheses`/`## Research Log` sections that follow it here
            # would leak onto the page as if part of the question without
            # site.py's own trim (`_question_block_body`). Neither heading
            # word appears anywhere else on a person page (no template ever
            # emits it), so its presence at all means the trim regressed.
            self.assertNotIn('Hypotheses', html, pid)
            self.assertNotIn('Research Log', html, pid)

    def test_by_request_persons_own_question_is_withheld_from_every_page(self):
        # PR #179 review, finding 1 (P1, privacy leak): `restricted:
        # by-request` is SPEC §19's one no-override tier ("honored by every
        # export path with no opt-in") - stronger than the plain
        # restricted/living gates every OTHER check in this class relaxes
        # under `--linked` (a deliberate, tested design:
        # UnreadableRecordPrivacyTests.test_linked_mode_is_unchanged pins it
        # for a by-request person's own name/vitals/bio, and this fix does
        # not reopen that). Before this fix, `_load_open_questions` read a
        # `## Q:` block's home research file with no restriction check at
        # all, so Rae's own private research note - `refs:`-naming both
        # herself AND a different, unrestricted relative - rendered in full
        # on BOTH pages under a plain `--linked` preview: exactly the leak
        # this pins closed, on both sides (her own page, and the
        # cross-referenced relative's).
        self._seed_person('p-aaaaaaaaaa', 'Reticent Rae', surname='Rae',
                          frontmatter_extra='restricted: by-request')
        self._seed_person('p-bbbbbbbbbb', 'Open Otto', surname='Otto')
        research_path = self.archive_root / 'people' / 'rae__test_research_p-aaaaaaaaaa.md'
        research_path.write_text(
            '---\nid: p-aaaaaaaaaa\ncreated: 2026-06-01\n---\n\n'
            '## Open Questions\n\n'
            "## Q: Was Rae really Otto's half-sibling?\n"
            '- origin: human\n'
            '- status: open\n'
            '- refs: [P-aaaaaaaaaa, P-bbbbbbbbbb]\n'
            '- context:\n'
            '  - (human, 2026-06-01) A sealed adoption record hints at this; '
            'Rae asked never to have it discussed.\n',
            encoding='utf-8')
        lo = self._run(linked=True, workbench=False)
        self.assertTrue(lo.ok, lo.messages)
        for pid in ('p-aaaaaaaaaa', 'p-bbbbbbbbbb'):
            html = self._read(f'persons/{pid}.html')
            self.assertNotIn('half-sibling', html, pid)
            self.assertNotIn('sealed adoption record', html, pid)
            self.assertNotIn('Open Questions', html, pid)

    def test_by_request_person_named_only_in_a_refs_list_is_still_withheld(self):
        # Adversarial review of PR #179, round 2: the finding above pins the
        # case where the by-request person's OWN file is the question's
        # home. This is the reverse - the home file belongs to an ORDINARY,
        # unrestricted person (Otto), and the question merely `refs:` Rae, a
        # by-request relative, on top of him. Before this fix, only the
        # home file's owner was checked, so a question homed anywhere else
        # could still fan a by-request person's private research onto HER
        # page via `refs:` alone.
        #
        # PR #179 POST-MERGE review, finding 1 (P1, privacy leak): this test
        # used to assert that Otto's own page kept showing the block in
        # full - the per-ref loop only skipped Rae's OWN listing, so the
        # identical unredacted text (Rae's sealed adoption record, exactly
        # the sensitive detail she asked never to have discussed) still
        # rendered on Otto's page. That was the bug, not a documented
        # boundary: a `## Q:` block naming a by-request person anywhere in
        # its `refs:` is one indivisible piece of private research about
        # her, and it must be withheld from EVERY page it would otherwise
        # have appeared on, Otto's included - not just kept off her own.
        self._seed_person('p-aaaaaaaaaa', 'Reticent Rae', surname='Rae',
                          frontmatter_extra='restricted: by-request')
        self._seed_person('p-bbbbbbbbbb', 'Open Otto', surname='Otto')
        research_path = self.archive_root / 'people' / 'otto__test_research_p-bbbbbbbbbb.md'
        research_path.write_text(
            '---\nid: p-bbbbbbbbbb\ncreated: 2026-06-01\n---\n\n'
            '## Open Questions\n\n'
            "## Q: Was Rae really Otto's half-sibling?\n"
            '- origin: human\n'
            '- status: open\n'
            '- refs: [P-bbbbbbbbbb, P-aaaaaaaaaa]\n'
            '- context:\n'
            '  - (human, 2026-06-01) A sealed adoption record hints at this; '
            'Rae asked never to have it discussed.\n',
            encoding='utf-8')
        lo = self._run(linked=True, workbench=False)
        self.assertTrue(lo.ok, lo.messages)
        rae_html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('half-sibling', rae_html)
        self.assertNotIn('sealed adoption record', rae_html)
        otto_html = self._read('persons/p-bbbbbbbbbb.html')
        self.assertNotIn('half-sibling', otto_html)
        self.assertNotIn('sealed adoption record', otto_html)
        self.assertNotIn('Open Questions', otto_html)

    def test_mixed_refs_question_is_withheld_from_every_page_it_names(self):
        # PR #179 post-merge review, finding 1 (P1, privacy leak),
        # dedicated regression: a single question naming THREE people -
        # Rae (by-request) plus two ordinary, unrestricted relatives - must
        # vanish from all three pages, not just Rae's own. The bug this
        # pins was in the per-ref loop filtering DESTINATIONS one at a time
        # (skip only the by-request ref's own listing) rather than the
        # question itself (any by-request ref voids the whole block,
        # everywhere) - a question naming more than two people makes that
        # distinction unambiguous: it is not enough for the fix to work
        # for one unrestricted bystander, it has to hold for all of them.
        self._seed_person('p-aaaaaaaaaa', 'Reticent Rae', surname='Rae',
                          frontmatter_extra='restricted: by-request')
        self._seed_person('p-bbbbbbbbbb', 'Open Otto', surname='Otto')
        self._seed_person('p-cccccccccc', 'Candid Cora', surname='Cora')
        self._write_questions_md(
            "## Q: Were Rae, Otto, and Cora all siblings?\n"
            '- origin: human\n'
            '- status: open\n'
            '- refs: [P-bbbbbbbbbb, P-aaaaaaaaaa, P-cccccccccc]\n'
            '- context:\n'
            '  - (human, 2026-06-01) A sealed adoption record ties the three '
            'together; Rae asked never to have it discussed.\n'
        )
        lo = self._run(linked=True, workbench=False)
        self.assertTrue(lo.ok, lo.messages)
        for pid in ('p-aaaaaaaaaa', 'p-bbbbbbbbbb', 'p-cccccccccc'):
            html = self._read(f'persons/{pid}.html')
            self.assertNotIn('siblings', html, pid)
            self.assertNotIn('sealed adoption record', html, pid)
            self.assertNotIn('Open Questions', html, pid)

    def test_research_file_whose_frontmatter_id_disagrees_with_its_filename_fails_closed(self):
        # Adversarial review of PR #179, round 2: a research companion's
        # filename and its own `id:` field are supposed to always agree
        # (`fha lint`'s E003 flags a mismatch as an archive error), but
        # while that inconsistency sits unresolved, the origin by-request
        # check must not trust the filename alone - it cannot tell which of
        # the two persons this file really belongs to, so it withholds
        # rather than guess. This file's NAME claims Otto (unrestricted);
        # its own `id:` says Rae (by-request) - the mismatch itself, either
        # direction, must fail closed.
        self._seed_person('p-aaaaaaaaaa', 'Reticent Rae', surname='Rae',
                          frontmatter_extra='restricted: by-request')
        self._seed_person('p-bbbbbbbbbb', 'Open Otto', surname='Otto')
        mismatched = self.archive_root / 'people' / 'otto__test_research_p-bbbbbbbbbb.md'
        mismatched.write_text(
            '---\nid: p-aaaaaaaaaa\ncreated: 2026-06-01\n---\n\n'
            '## Open Questions\n\n'
            '## Q: Mismatched filename vs frontmatter id\n'
            '- origin: human\n'
            '- status: open\n'
            '- refs: [P-bbbbbbbbbb]\n'
            '- context:\n'
            '  - (human, 2026-06-01) Should never render while this mismatch stands.\n',
            encoding='utf-8')
        lo = self._run(linked=True, workbench=False)
        self.assertTrue(lo.ok, lo.messages)
        html = self._read('persons/p-bbbbbbbbbb.html')
        self.assertNotIn('Mismatched filename vs frontmatter id', html)
        self.assertNotIn('Should never render', html)
        messages = lo['messages']
        self.assertTrue(any('E003' in m or 'mismatch' in m.lower() for m in messages), messages)

    def test_by_request_persons_id_less_research_companion_still_fails_closed(self):
        # PR #179 post-merge review, finding 2 (P1, privacy leak): a
        # research companion named BEFORE its id was minted
        # (`{surname}__{given}_research.md`, no trailing `_P-...` - Codex's
        # own example is `rae__reticent_research.md`) is a real,
        # `is_person_file_kind`-documented state, and `_lib.parse_questions`
        # already reads such a file's own frontmatter `id:` to know whose
        # research it is. The origin by-request check used to read only the
        # FILENAME for that id, get `parse_filename() is None`, and treat
        # that as "not a person research file - nothing to check" - so
        # Rae's by-request research published in full on Otto's page,
        # solely because her companion happened to be named before she
        # (or her id) existed on disk in filename form. Her own id lives
        # only in frontmatter here, and the question's `refs:` names only
        # Otto - not Rae herself - so this isolates the ORIGIN-file check
        # specifically (finding 1's per-ref fix would not catch this on its
        # own, since Rae's id never appears in `refs:` at all).
        self._seed_person('p-aaaaaaaaaa', 'Reticent Rae', surname='Rae',
                          frontmatter_extra='restricted: by-request')
        self._seed_person('p-bbbbbbbbbb', 'Open Otto', surname='Otto')
        id_less = self.archive_root / 'people' / 'rae__reticent_research.md'
        id_less.write_text(
            '---\nid: p-aaaaaaaaaa\ncreated: 2026-06-01\n---\n\n'
            '## Open Questions\n\n'
            "## Q: Was Otto really involved in Rae's sealed adoption?\n"
            '- origin: human\n'
            '- status: open\n'
            '- refs: [P-bbbbbbbbbb]\n'
            '- context:\n'
            '  - (human, 2026-06-01) A sealed adoption record hints at this; '
            'Rae asked never to have it discussed.\n',
            encoding='utf-8')
        lo = self._run(linked=True, workbench=False)
        self.assertTrue(lo.ok, lo.messages)
        otto_html = self._read('persons/p-bbbbbbbbbb.html')
        self.assertNotIn("sealed adoption", otto_html)
        self.assertNotIn('Open Questions', otto_html)
        rae_html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('sealed adoption', rae_html)

    def test_id_less_research_companion_with_unreadable_frontmatter_fails_closed(self):
        # The other half of finding 2: when the filename carries no P-id AND
        # its own frontmatter cannot supply one either (undecodable file, or
        # simply no `id:` field at all), there is no owner to check against
        # at all - this must withhold rather than publish on a guess,
        # matching every other "can't verify" case in this class.
        self._seed_person('p-bbbbbbbbbb', 'Open Otto', surname='Otto')
        id_less = self.archive_root / 'people' / 'ghost__unminted_research.md'
        id_less.write_text(
            '---\ncreated: 2026-06-01\n---\n\n'
            '## Open Questions\n\n'
            '## Q: No id anywhere at all\n'
            '- origin: human\n'
            '- status: open\n'
            '- refs: [P-bbbbbbbbbb]\n'
            '- context:\n'
            '  - (human, 2026-06-01) Should never render without a resolvable owner.\n',
            encoding='utf-8')
        lo = self._run(linked=True, workbench=False)
        self.assertTrue(lo.ok, lo.messages)
        otto_html = self._read('persons/p-bbbbbbbbbb.html')
        self.assertNotIn('No id anywhere at all', otto_html)
        self.assertNotIn('Should never render', otto_html)

    def test_dangling_ref_names_the_actual_origin_with_a_repair_step_once(self):
        # PR #179 post-merge review, finding 3 (P2): a `refs:` P-id absent
        # from `person_meta` (a typo, or a stale index) used to produce a
        # warning claiming the question was "filed under {pid}'s research
        # companion" - accurate only when pid IS a research file's own
        # owner (the origin check), and simply wrong for a `refs:` TARGET
        # that was never any file's own owner. The fix must name the
        # question's real origin file and an accurate repair step (check
        # that file's `refs:` list for a typo, or run `fha index` in case
        # the index is stale) instead.
        #
        # Also covers finding 4 (P2, perf): the SAME dangling id is
        # referenced from TWO different files here (notes/questions.md and
        # Jane's own research companion) - unmemoized, that used to mean
        # two full profile-lookup attempts and two appended warnings; this
        # asserts exactly one.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe', surname='Doe')
        self._write_questions_md(
            '## Q: Is the mystery cousin related?\n'
            '- status: open\n'
            '- refs: [P-aaaaaaaaaa, P-zzzzzzzzzz]\n'
            '- context:\n'
            "  - (human, 2026-06-01) Probably a typo'd id.\n"
        )
        research_path = self.archive_root / 'people' / 'doe__test_research_p-aaaaaaaaaa.md'
        research_path.write_text(
            '---\nid: p-aaaaaaaaaa\ncreated: 2026-06-01\n---\n\n'
            '## Open Questions\n\n'
            '## Q: A second, unrelated mention of the same dangling id\n'
            '- status: open\n'
            '- refs: [P-aaaaaaaaaa, P-zzzzzzzzzz]\n'
            '- context:\n'
            '  - (human, 2026-06-01) Same dangling id, a different file entirely.\n',
            encoding='utf-8')
        res = self._run(linked=True, workbench=False)
        self.assertTrue(res.ok, res.messages)
        messages = res['messages']
        dangling = [m for m in messages if 'p-zzzzzzzzzz' in m.lower()]
        self.assertEqual(
            len(dangling), 1,
            'the dangling-ref warning must be memoized per pid, not repeated '
            f'once per reference to it: {messages}')
        msg = dangling[0].lower()
        # Names an actual file - either question's real origin is a correct
        # answer here (whichever one this build happened to check first) -
        # never a claim that the dangling id owns some research companion
        # of its own.
        self.assertTrue(
            'notes/questions.md' in msg or 'doe__test_research_p-aaaaaaaaaa.md' in msg,
            dangling[0])
        self.assertTrue('refs' in msg or 'fha index' in msg, dangling[0])
        # Neither question survives - both are voided by the same dangling,
        # fail-closed ref, exactly as a real by-request ref would void them.
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('mystery cousin', html)
        self.assertNotIn('unrelated mention', html)

    def test_person_is_by_request_reads_the_profile_once_per_pid(self):
        # PR #179 post-merge review, finding 4 (P2, perf): before this fix,
        # only the ORIGIN check was memoized (`_by_request_origin_cache`) -
        # `_person_is_by_request` itself re-read and re-parsed the same
        # profile record from scratch on every call. A `refs:` check runs
        # once per person named on every open question, so an archive where
        # hundreds of questions all name one central, heavily-connected
        # ancestor used to re-read that one file hundreds of times over a
        # single build. Same pattern as
        # PersonPageTests.test_provisional_vital_reads_the_record_once_per_person.
        self._seed_person('p-aaaaaaaaaa', 'Central Ancestor', surname='Ancestor')
        builder = site._SiteBuilder(self.conn, self.archive_root, {}, self.out_dir,
                                    linked=True, workbench=True)
        builder.prepare()
        with unittest.mock.patch.object(
            site, 'read_record', wraps=site.read_record
        ) as spy:
            for _ in range(5):
                self.assertFalse(builder._person_is_by_request('p-aaaaaaaaaa'))
        self.assertEqual(spy.call_count, 1)

    def test_plain_restricted_persons_question_still_renders_under_linked(self):
        # Not over-broadened: by-request is SPEC §19's ONE no-override tier.
        # A plain `restricted: true` origin - a lesser tier other export
        # paths can unlock via --include-restricted - must still surface
        # their open questions under `--linked`, exactly as `--linked`
        # already shows that same person's own restricted claims/sources/
        # name in full (SourcePageTests.
        # test_standalone_shows_accepted_only_linked_shows_everything, etc.).
        self._seed_person('p-aaaaaaaaaa', 'Plainly Restricted Pat', surname='Pat',
                          frontmatter_extra='restricted: true')
        research_path = self.archive_root / 'people' / 'pat__test_research_p-aaaaaaaaaa.md'
        research_path.write_text(
            '---\nid: p-aaaaaaaaaa\ncreated: 2026-06-01\n---\n\n'
            '## Open Questions\n\n'
            '## Q: Where was Pat actually born?\n'
            '- status: open\n'
            '- refs: [P-aaaaaaaaaa]\n'
            '- context:\n'
            '  - (human, 2026-06-01) Two conflicting county records.\n',
            encoding='utf-8')
        lo = self._run(linked=True, workbench=False)
        self.assertTrue(lo.ok, lo.messages)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Where was Pat actually born?', html)
        self.assertIn('Two conflicting county records', html)

    def test_undecodable_questions_md_is_skipped_with_a_warning_not_a_crash(self):
        # PR #179 review, finding 2 (P2, crash/availability): `_lib.
        # parse_questions` used to read notes/questions.md with a plain
        # `path.read_text(encoding='utf-8')` behind an `except OSError` -
        # `UnicodeDecodeError` is a `ValueError`, so it escaped straight out
        # of `_load_open_questions`, through `prepare()`, and out of
        # `run_site` itself, which promises to always return a `Result`.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        bad = self.archive_root / 'notes' / 'questions.md'
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes('## Q: Kraków connection?\n- status: open\n'.encode('cp1252'))
        res = self._run(linked=True)
        self.assertTrue(res.ok, res.messages)
        messages = res['messages']
        self.assertTrue(
            any('notes/questions.md' in m and "isn't saved as UTF-8 text" in m
                for m in messages), messages)
        # One bad file costs only its own content - every other page builds fine.
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Kraków connection', html)

    def test_undecodable_research_file_is_skipped_others_still_index(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe', surname='Doe')
        self._seed_person('p-bbbbbbbbbb', 'Bob Roe', surname='Roe')
        bad_research = self.archive_root / 'people' / 'roe__test_research_p-bbbbbbbbbb.md'
        bad_research.write_bytes(
            ('---\nid: p-bbbbbbbbbb\n---\n\n## Open Questions\n\n'
             '## Q: Kraków connection?\n- status: open\n- refs: [P-bbbbbbbbbb]\n')
            .encode('cp1252'))
        self._write_questions_md(
            '## Q: Healthy question?\n- status: open\n- refs: [P-aaaaaaaaaa]\n'
            '- context:\n  - (human, 2026-06-01) Still readable.\n'
        )
        res = self._run(linked=True)
        self.assertTrue(res.ok, res.messages)
        messages = res['messages']
        self.assertTrue(
            any('roe__test_research_p-bbbbbbbbbb.md' in m
                and "isn't saved as UTF-8 text" in m for m in messages), messages)
        html_a = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Healthy question?', html_a)


# ── A person's vitals are their OWN, not their relatives' (#126) ─────────────
#
# A vital record names more than the person it is a record OF: a birth
# certificate names the baby, both parents, and the informant. Selecting a
# person's Born/Died/Married by "any accepted vital claim that NAMES them" put
# a son's birth date in his mother's summary box and on her chart node - a
# plain factual error, and one a reader has no way to spot. Same defect as #58
# (marriage pairing) and #118 (social edges), one query further on: the fix is
# the same one, `roles:` says who a claim is about (SPEC §8.3).

class VitalSubjectScopingTests(_Base):
    MOM = 'p-mmmmmmmmmm'
    SON = 'p-ssssssssss'

    def _seed_birth_certificate(self, roles):
        """The son's birth certificate, naming his mother as the parent."""
        self._seed_person(self.SON, 'Peter Marr')
        self._seed_person(self.MOM, 'Iris Marr')
        self._seed_source('s-1111111111', 'Birth certificate',
                          source_type='vital-record')
        self._seed_claim('c-1111111111', 's-1111111111', 'birth',
                         'Born at Riverton', status='accepted', date_edtf='1888',
                         place_text='Riverton', persons=(self.SON, self.MOM),
                         roles=roles)

    def test_mother_named_as_parent_gets_no_born_row_of_her_own(self):
        self._seed_birth_certificate({self.SON: 'child', self.MOM: 'parent'})
        self._run(linked=True)
        self.assertNotIn(
            '<dt>Born</dt>', self._read(f'persons/{self.MOM}.html'),
            "a mother named as `parent` on her son's birth certificate must "
            'not have his birth date rendered as her own')

    def test_the_child_the_claim_names_still_gets_his_born_row(self):
        # The other half of the two-sided rule: scoping must not cost the
        # person whose record it actually is their own summary line.
        self._seed_birth_certificate({self.SON: 'child', self.MOM: 'parent'})
        self._run(linked=True)
        html = self._read(f'persons/{self.SON}.html')
        self.assertIn('<dt>Born</dt>', html)
        self.assertIn('1888', html)

    def test_an_unroled_baby_is_still_read_as_the_subject(self):
        # `roles: {parent: [mother]}` with the baby left unmarked is an
        # ordinary way to write the claim. The claim said what the OTHER person
        # was, so the one it left unroled is the one it is about.
        self._seed_birth_certificate({self.MOM: 'parent'})
        self._run(linked=True)
        self.assertIn('<dt>Born</dt>', self._read(f'persons/{self.SON}.html'))
        self.assertNotIn('<dt>Born</dt>', self._read(f'persons/{self.MOM}.html'))

    def test_a_legacy_claim_naming_two_people_with_no_roles_gets_neither_a_born_row(
            self) -> None:
        # Two people, no roles: map at all - the claim has not said which of
        # them was born. Showing it as BOTH of their own births is exactly
        # this class's own bug (a mother's `Born: 1888` copied off her son's
        # birth certificate), reached through the unroled case instead of the
        # miscast one: the old back-compatibility bargain used to guess
        # "everyone" for a claim with zero role signal at all, which is #126
        # restated rather than fixed (#126, reopened - this exact shape is
        # what let the Died/Buried fields keep showing a relative's record
        # after the roled cases were already scoped correctly). A missing
        # Born row is recoverable by adding roles:; a false one is not.
        self._seed_birth_certificate(None)
        self._run(linked=True)
        self.assertNotIn('<dt>Born</dt>', self._read(f'persons/{self.MOM}.html'))
        self.assertNotIn('<dt>Born</dt>', self._read(f'persons/{self.SON}.html'))

    def test_a_legacy_claim_naming_only_the_child_keeps_its_old_behaviour(self) -> None:
        # One person named, no roles: map - nobody to be ambiguous about, so
        # the pre-#126 "the claim never said, so nothing is withheld"
        # back-compatibility bargain is still exactly right here.
        self._seed_person(self.SON, 'Peter Marr')
        self._seed_source('s-1111111111', 'Birth certificate',
                          source_type='vital-record')
        self._seed_claim('c-1111111111', 's-1111111111', 'birth',
                         'Born at Riverton', status='accepted', date_edtf='1888',
                         place_text='Riverton', persons=(self.SON,), roles=None)
        self._run(linked=True)
        self.assertIn('<dt>Born</dt>', self._read(f'persons/{self.SON}.html'))

    def test_chart_node_shows_only_the_subjects_own_life_dates(self):
        # The chart half of #126: `_person_vitals` labels every node, so the
        # same blind join printed "b. 1888" under the mother's name too - a
        # node whose dates belong to the person one row below it.
        self._seed_birth_certificate({self.SON: 'child', self.MOM: 'parent'})
        self._seed_rel(self.SON, 'parent', self.MOM)
        self._seed_rel(self.MOM, 'child', self.SON)
        self._run(linked=True)
        html = self._read(f'persons/{self.SON}.html')
        self.assertEqual(
            html.count('<span class="ped-dates">b. 1888</span>'), 1,
            'exactly one chart node - the son\'s own - may carry his birth '
            'year; his mother\'s ancestor slot must not repeat it')

    def test_a_relative_on_a_death_record_gets_no_died_row(self):
        # SPEC §8.3 has no role word for the deceased, so a death claim can
        # only say who the OTHERS were. The daughter who reported the death is
        # named `child`; the unroled person is the one who died.
        self._seed_person(self.SON, 'Peter Marr')
        self._seed_person(self.MOM, 'Iris Marr')
        self._seed_source('s-2222222222', 'Death certificate',
                          source_type='vital-record')
        self._seed_claim('c-2222222222', 's-2222222222', 'death',
                         'Died at Riverton', status='accepted', date_edtf='1920',
                         persons=(self.MOM, self.SON), roles={self.SON: 'child'})
        self._run(linked=True)
        self.assertNotIn('<dt>Died</dt>', self._read(f'persons/{self.SON}.html'))
        self.assertIn('<dt>Died</dt>', self._read(f'persons/{self.MOM}.html'))

    def test_a_parent_on_a_marriage_certificate_gets_no_married_row(self):
        # A marriage certificate ordinarily names the couple AND both sets of
        # parents; only `roles: spouse:` says which of them married.
        self._seed_person(self.SON, 'Peter Marr')
        self._seed_person(self.MOM, 'Iris Marr')
        self._seed_person('p-wwwwwwwwww', 'Ada Finch')
        self._seed_source('s-3333333333', 'Marriage record',
                          source_type='vital-record')
        self._seed_claim('c-3333333333', 's-3333333333', 'marriage',
                         'Married at Riverton', status='accepted', date_edtf='1910',
                         persons=(self.SON, 'p-wwwwwwwwww', self.MOM),
                         roles={self.SON: 'spouse', 'p-wwwwwwwwww': 'spouse',
                                self.MOM: 'parent'})
        self._run(linked=True)
        self.assertNotIn('<dt>Married</dt>', self._read(f'persons/{self.MOM}.html'))
        self.assertIn('<dt>Married</dt>', self._read(f'persons/{self.SON}.html'))

    def test_both_halves_of_a_couple_keep_married_when_only_one_is_roled(self):
        # `roles: {spouse: [P-a]}` with the partner left unroled is the typo
        # case `_lib.spouse_parties` documents - a mistyped id, a name that
        # stopped resolving - and that rule still reads the two as a couple, so
        # `fha index` mints the spouse edge and `fha gedcom` writes the MARR.
        # The summary block has to agree: dropping the partner's Married row
        # here would leave her page denying a marriage the rest of the archive
        # asserts about her.
        self._seed_person(self.SON, 'Peter Marr')
        self._seed_person('p-wwwwwwwwww', 'Ada Finch')
        self._seed_source('s-4444444444', 'Marriage record',
                          source_type='vital-record')
        self._seed_claim('c-4444444444', 's-4444444444', 'marriage',
                         'Married at Riverton', status='accepted', date_edtf='1910',
                         persons=(self.SON, 'p-wwwwwwwwww'),
                         roles={self.SON: 'spouse'})
        self._run(linked=True)
        self.assertIn('<dt>Married</dt>', self._read(f'persons/{self.SON}.html'))
        self.assertIn(
            '<dt>Married</dt>', self._read('persons/p-wwwwwwwwww.html'),
            'the partner a couple claim leaves unroled is still half of the '
            'couple `spouse_parties` derives, so her own Married row stands')


_FAN_LABEL_RE = re.compile(
    r'<text class="fan-label" font-size="([0-9.]+)"><textPath[^>]*>([^<]*)</textPath>')

# Largest label size each ring is allowed (site.py `fs_max`, indexed by
# generation) and the readable floor below which a name is shortened instead of
# shrunk further. Written down here so the tests below state the contract the
# renderer's docstring describes rather than echoing whatever it happens to emit.
_RING_MAX_FS = {1: 13.0, 2: 12.0, 3: 11.0}
_FS_FLOOR = 8.0


def _fan_labels(svg):
    """[(font_size, shown_text)] for every ancestor label in a fan SVG, in
    document order (the subject's hub label is a different class and excluded)."""
    return [(float(fs), text) for fs, text in _FAN_LABEL_RE.findall(svg)]


class FanChartLabelTests(_Base):
    """Issue #116: `_render_fan_svg` sizes each label to fit its own arc and
    writes that size as an SVG font-size presentation attribute. Presentation
    attributes carry zero specificity, so `.fan-label { font-size: 11px }` beat
    every computed size - each label was laid out for one size and drawn at
    another, and the long outer names ran off the end of their textPath. The
    stylesheet now keeps its default on the container instead, where it is
    inherited and so yields to a label's own attribute."""

    def _fan(self, num_to_name):
        """Fan SVG for {Ahnentafel number: name}, drawn to the same depth person
        pages use (site._FAN_GENERATIONS) so the arcs are the real ones."""
        labels = {1: {'name': 'Subject', 'url': None}}
        for num, nm in num_to_name.items():
            labels[num] = {'name': nm, 'url': None}
        return site._render_fan_svg(labels, site._FAN_GENERATIONS)

    def test_every_label_carries_its_own_font_size(self):
        # The stylesheet no longer sizes .fan-label, so the attribute is now
        # load-bearing: a label emitted without one would inherit the fallback
        # and lose its fit entirely.
        svg = self._fan({2: 'Calvin George Hartley', 3: 'Ada', 6: 'Harriet Frances Webb'})
        self.assertEqual(svg.count('class="fan-label"'), len(_fan_labels(svg)))
        self.assertEqual(len(_fan_labels(svg)), 3)

    def test_labels_are_sized_per_arc_not_one_flat_size(self):
        # The point of the auto-shrink: a long name and a short one on the same
        # ring get different sizes. One flat size for both is the #116 symptom.
        svg = self._fan({2: 'Bo', 3: 'Chastina Augusta Reed'})
        sizes = {fs for fs, _ in _fan_labels(svg)}
        self.assertEqual(len(sizes), 2, f'expected two distinct label sizes, got {sizes}')

    def test_a_name_the_shrink_can_fit_is_never_shortened(self):
        # 'Chastina Augusta Reed' is 21 characters: the shrink picks a size at
        # which the whole name fits, so shortening it is a contradiction. The
        # character budget used to be derived back out of that size, and float
        # rounding turned "exactly 21 fit" into 20 - an ellipsis on a name the
        # renderer had just made room for.
        for name in ('Chastina Augusta Reed', 'Caleb Comstock Hartley',
                     'Wilhelmina Cartwright'):
            for num in (2, 4, 8):
                svg = self._fan({num: name})
                fs, shown = _fan_labels(svg)[0]
                if fs > _FS_FLOOR:
                    self.assertEqual(shown, name,
                                     f'{name!r} shortened at {fs}px, above the {_FS_FLOOR}px floor')

    def test_a_short_name_keeps_its_rings_full_size(self):
        # The fix must not shrink the common case: a name with room to spare
        # renders at its ring's ceiling, not at some universally reduced size.
        for num, gen in ((2, 1), (4, 2), (8, 3)):
            svg = self._fan({num: 'Ada'})
            fs, shown = _fan_labels(svg)[0]
            self.assertEqual(shown, 'Ada')
            self.assertEqual(fs, _RING_MAX_FS[gen])

    def test_a_name_past_the_floor_is_shortened_and_kept_in_the_tooltip(self):
        # Below the readable floor the renderer stops shrinking and shortens
        # instead - never lossily, because the whole name rides a <title>.
        long_name = 'Maximilian Bartholomew Fitzwilliam Cholmondeley'
        svg = self._fan({8: long_name})
        fs, shown = _fan_labels(svg)[0]
        self.assertEqual(fs, _FS_FLOOR)
        self.assertTrue(shown.endswith('…'), shown)
        self.assertLess(len(shown), len(long_name))
        self.assertIn(f'<title>{long_name}</title>', svg)

    def test_a_label_is_shown_whole_or_sized_at_the_floor(self):
        # The renderer's contract in one line: shrink to fit, and shorten only
        # once shrinking has bottomed out. Swept across name lengths and rings
        # because the failure was a float edge that hit only some lengths.
        for num, gen in ((2, 1), (4, 2), (8, 3)):
            for length in range(1, 41):
                name = 'M' * length
                fs, shown = _fan_labels(self._fan({num: name}))[0]
                self.assertGreaterEqual(fs, _FS_FLOOR)
                self.assertLessEqual(fs, _RING_MAX_FS[gen])
                if shown != name:
                    self.assertEqual(fs, _FS_FLOOR,
                                     f'{length}-char name shortened at {fs}px on ring {gen}')

    def test_person_page_carries_the_computed_label_sizes(self):
        # End to end: the per-label size has to survive the site build into the
        # published HTML, not just exist inside the renderer.
        self._seed_person('p-aaaaaaaaaa', 'Ada Jane Hartley', surname='Hartley')
        self._seed_person('p-bbbbbbbbbb', 'Bo Ford', surname='Ford')
        self._seed_person('p-cccccccccc', 'Chastina Augusta Reed', surname='Reed')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-bbbbbbbbbb')
        self._seed_rel('p-aaaaaaaaaa', 'parent', 'p-cccccccccc')
        self._run(linked=True)
        page = self._read('persons/p-aaaaaaaaaa.html')
        labels = {text: fs for fs, text in _fan_labels(page)}
        self.assertEqual(set(labels), {'Bo Ford', 'Chastina Augusta Reed'})
        self.assertGreater(labels['Bo Ford'], labels['Chastina Augusta Reed'])


class TreeFitScaleTests(unittest.TestCase):
    """#152 review fix (P2): `fha-tree.js`'s `fit()` (both the collapsible-
    tree `render()` copy and the `wrapStatic()` copy for the static home
    pedigree) must be able to compute a scale BELOW `MIN_SCALE` for a chart
    tall/wide enough to need one - `MIN_SCALE` bounds manual zoom-out only
    (the -/+ buttons, wheel, pinch), not what "Fit chart to view" is allowed
    to compute. At the deep end of a configured home pedigree
    (`home_pedigree_generations` 7-8, the documented max) with a
    substantially-populated tree, the drawn chart can need a scale under 0.1
    to fit the at-most-620px viewport - clamping fit()'s own computation
    through `clampScale()` (as before this fix) left part of the chart
    permanently out of view with no way to zoom out any further. A
    regression here textually reintroduces exactly that clamp."""

    def test_fit_does_not_clamp_through_min_scale(self):
        js = (ROOT / 'tools' / 'templates' / 'vendor' / 'fha-tree.js').read_text(encoding='utf-8')
        # The pre-fix shape, in both fit() copies: the whole Math.min(...)
        # fit computation wrapped in clampScale(...), which floors it at
        # MIN_SCALE. Must not appear anywhere in the file.
        self.assertEqual(js.count('clampScale(Math.min(vpW / (contentW + FIT_PAD * 2)'), 0)
        # The fixed shape: MAX_SCALE is still respected as a ceiling (so a
        # tiny chart does not over-zoom on Fit), MIN_SCALE is not consulted
        # at all - present once in render()'s fit() and once in
        # wrapStatic()'s.
        self.assertEqual(js.count('Math.min(MAX_SCALE, vpW / (contentW + FIT_PAD * 2)'), 2)

    def test_zoom_out_does_not_reverse_direction_below_min_scale(self):
        # #152 follow-up review fix (P2, finding 2): zoomAt() used to run its
        # computed next scale through clampScale() unconditionally - fine
        # normally, but once fit() (fixed above) parks the view BELOW
        # MIN_SCALE for a very deep pedigree, that plain clamp RAISES a
        # further zoom-OUT request back up to MIN_SCALE, reversing the
        # requested direction (e.g. an outward 0.8x zoom from 0.05 computes
        # 0.04, clampScale(0.04) returns 0.1 - zoom out actually zooms in
        # 2x). zoomAt() must instead route through the shared
        # nextZoomScale() helper, which only re-applies the floor for the
        # opposite (zoom-in) direction. Checked in both fit()/zoomAt() copies
        # (render() and wrapStatic()) and in both committed example-site
        # copies, alongside the template.
        for path in (ROOT / 'tools' / 'templates' / 'vendor' / 'fha-tree.js',
                     ROOT / 'example-archive' / 'generated' / 'site' / 'vendor' / 'fha-tree.js',
                     ROOT / 'example-archive' / 'generated' / 'site-workbench' / 'vendor' / 'fha-tree.js'):
            with self.subTest(path):
                js = path.read_text(encoding='utf-8')
                self.assertIn('function nextZoomScale(sc, factor)', js)
                # The pre-fix shape must be gone from both zoomAt() copies.
                self.assertEqual(js.count('var next = clampScale(sc * factor);'), 0)
                # The fixed shape: present once per zoomAt() copy.
                self.assertEqual(js.count('var next = nextZoomScale(sc, factor);'), 2)

    def test_committed_showcase_vendor_copies_match_the_template(self):
        # The same staleness class #153 already fixed once for the
        # workbench showcase's stylesheet (CommittedShowcaseAssetTests): the
        # two example sites under example-archive/generated/ are committed,
        # browsable snapshots, so a fix that only touches
        # tools/templates/vendor/fha-tree.js still ships the old bug to
        # anyone reading those unless the committed copies are synced too.
        template = (ROOT / 'tools' / 'templates' / 'vendor' / 'fha-tree.js').read_text(encoding='utf-8')
        for rel in ('example-archive/generated/site/vendor/fha-tree.js',
                    'example-archive/generated/site-workbench/vendor/fha-tree.js'):
            with self.subTest(rel):
                self.assertEqual(
                    (ROOT / rel).read_text(encoding='utf-8'), template,
                    f'{rel} is stale - copy tools/templates/vendor/fha-tree.js over it verbatim.')


class FanChartStyleTests(unittest.TestCase):
    def test_fan_label_has_no_fixed_font_size(self):
        # Issue #116: _render_fan_svg() computes a per-label auto-shrink
        # font-size and writes it as an SVG presentation attribute; a CSS
        # font-size rule on .fan-label silently overrides that computed
        # value (SVG presentation attributes lose to any CSS rule, even a
        # plain class selector). Guard against reintroducing a fixed size.
        css = (ROOT / 'design' / 'styles.css').read_text(encoding='utf-8')
        # Anchored to the start of a line so a selector quoted inside one of
        # the surrounding comments cannot stand in for the rule itself.
        m = re.search(r'^\.fan-label\s*\{([^}]*)\}', css, re.M)
        self.assertIsNotNone(m, '.fan-label rule not found in design/styles.css')
        self.assertNotIn('font-size', m.group(1))

    def test_ped_axis_label_uses_fill_not_color(self):
        # #115 review finding: _render_pedigree_svg emits the orientation
        # caption ('ancestors ->') as a raw SVG <text> element, not HTML in a
        # foreignObject - SVG text paint is controlled by `fill`, not `color`,
        # unless something bridges the two with `fill: currentColor` (nothing
        # in this stylesheet does). A `color:` rule here silently renders the
        # caption in the SVG default black instead of the intended muted
        # meta-text gray. The working precedent a few lines away, .fan-label,
        # correctly uses `fill` for its own raw SVG <text> - this rule must
        # match it.
        css = (ROOT / 'design' / 'styles.css').read_text(encoding='utf-8')
        m = re.search(r'^\.ped-axis-label\s*\{([^}]*)\}', css, re.M | re.S)
        self.assertIsNotNone(m, '.ped-axis-label rule not found in design/styles.css')
        self.assertIn('fill:', m.group(1))
        self.assertNotIn('color:', m.group(1))

    def test_fan_chart_container_supplies_the_fallback_size(self):
        # Deleting the .fan-label size alone would leave a label with no
        # attribute inheriting the 1.05rem body text - a worse clip than the
        # 11px it replaced. The default belongs on the container, where it is
        # inherited and therefore loses to a label's own attribute.
        css = (ROOT / 'design' / 'styles.css').read_text(encoding='utf-8')
        m = re.search(r'^\.fan-chart\s*\{([^}]*)\}', css, re.M)
        self.assertIsNotNone(m, '.fan-chart rule not found in design/styles.css')
        self.assertIn('font-size', m.group(1))


class CommittedShowcaseAssetTests(unittest.TestCase):
    """`_copy_assets` copies design/styles.css verbatim into every build's
    assets/, and the two example sites under example-archive/generated/ are
    committed as browsable showcases. A stylesheet fix that stops at design/
    therefore still ships the old bug to anyone reading those - which is how
    the #116 fix first landed."""

    def test_committed_showcase_stylesheets_match_the_design_package(self):
        design = (ROOT / 'design' / 'styles.css').read_text(encoding='utf-8')
        for rel in ('example-archive/generated/site/assets/styles.css',
                    'example-archive/generated/site-workbench/assets/styles.css'):
            with self.subTest(rel):
                self.assertEqual(
                    (ROOT / rel).read_text(encoding='utf-8'), design,
                    f'{rel} is stale. Rebuild the standalone showcase with\n'
                    '  python3 tools/fha.py index --root example-archive\n'
                    '  python3 tools/fha.py site --root example-archive --standalone '
                    '--out generated/site\n'
                    'and copy design/styles.css over the site-workbench snapshot '
                    '(that one is built by `fha serve` and cannot be rebuilt '
                    'reproducibly - it embeds a per-process CSRF token).')

    def test_committed_showcase_person_pages_have_deferred_tree_render(self):
        # #152 follow-up review fix (P2, finding 3): the deferred-render fix
        # (FhaTree.render() now waits for the enclosing <details>' own
        # `toggle` event before running, instead of running unconditionally
        # at page load into a closed - so zero-width - <details>) landed in
        # tools/templates/_tree.html, but the committed example pages are
        # static HTML snapshots that only pick it up on a rebuild. Same
        # staleness class as the vendor-copy check above, checked against
        # both committed example sites' descendant-tree-carrying showcase
        # page (P-de957bcda1, the fixture person the #152 finding named).
        for rel in ('example-archive/generated/site/persons/p-de957bcda1.html',
                    'example-archive/generated/site-workbench/persons/p-de957bcda1.html'):
            with self.subTest(rel):
                html = (ROOT / rel).read_text(encoding='utf-8')
                self.assertIn('function renderTree()', html,
                              f'{rel} is stale - it still calls FhaTree.render() '
                              'unconditionally. Regenerate the example-archive site output.')
                self.assertIn("addEventListener('toggle'", html)


if __name__ == '__main__':
    unittest.main()
