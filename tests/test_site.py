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
    photoindex_record_manifest, record_path_manifest, write_path_manifest,
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
                     surname='Person', body='# Test Person\n', frontmatter_extra=''):
        rel = f'people/{surname.lower()}__test_{pid}.md'
        path = self.archive_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        extra = f'{frontmatter_extra}\n' if frontmatter_extra else ''
        path.write_text(f'---\nid: {pid}\nname: {name}\n{extra}---\n{body}', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, name, surname, 'M', living, tier, 'active', rel),
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
                    place_text=None, persons=(), confidence=None, reviewed=None, negated=0):
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, value, status, date_edtf, date_min, place_text, '
            'confidence, reviewed, negated) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (cid, sid, ctype, value, status, date_edtf, (date_edtf or '')[:4] + '-01-01' if date_edtf else None, place_text,
             confidence, reviewed, negated),
        )
        for pos, pid in enumerate(persons):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position) VALUES (?,?,?)', (cid, pid, pos))

    def _seed_rel(self, pid, rel, other):
        self.conn.execute(
            'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
            (pid, rel, other, 'c-rrrrrrrrrr'))

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
        strip = html[html.index('class="photo-strip"'):]
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
        self.assertNotIn('<a href="../index.html">Home</a>'.replace('>', ' target="_blank">'), html)
        nav_block = html[html.index('site-nav'):html.index('</nav>')]
        self.assertNotIn('target="_blank"', nav_block)
        # The claim's person cross-link is same-site navigation, not a file.
        self.assertIn('../persons/p-aaaaaaaaaa.html', html)
        person_link_idx = html.index('../persons/p-aaaaaaaaaa.html')
        # Look at just that one anchor tag (up to its closing '>').
        tag_end = html.index('>', person_link_idx)
        self.assertNotIn('target="_blank"', html[person_link_idx:tag_end])


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
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._seed_place('l-1111111111', 'Fairview')
        self._seed_claim_at_place('c-1111111111', 's-1111111111', 'l-1111111111',
                                  'Lived in Fairview', ('p-aaaaaaaaaa',))
        self._run(linked=True)
        # Source page claims table and the person timeline both link the place.
        self.assertIn('../places/l-1111111111.html', self._read('sources/s-1111111111.html'))
        self.assertIn('../places/l-1111111111.html', self._read('persons/p-aaaaaaaaaa.html'))

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
    """M8.5: interactive trees - vendored renderer + adapter, build-time neutral
    tree JSON (descendants from the root person's apex on the home page, ancestor
    pedigree per curated person), redaction baked into the JSON."""

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

    def test_home_descendant_tree_from_apex(self):
        self._seed_rels_chain()
        self._run(linked=True)
        # Data artifact written for the apex (grandparent) in descendants mode.
        data = self.out_dir / 'data' / 'tree_p-cccccccccc_descendants.json'
        self.assertTrue(data.exists())
        tree = json.loads(data.read_text(encoding='utf-8'))
        self.assertEqual(tree['seed'], 'P-cccccccccc')
        self.assertEqual(tree['mode'], 'descendants')
        ids = {n['p_id'] for n in tree['nodes']}
        self.assertEqual(ids, {'P-aaaaaaaaaa', 'P-bbbbbbbbbb', 'P-cccccccccc'})  # whole line
        # Home page embeds the tree data + includes both vendor scripts.
        home = self._read('index.html')
        self.assertIn('fha-tree-data', home)
        self.assertIn('vendor/fha-tree.js', home)
        self.assertIn('vendor/tree-adapter.js', home)

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

    def test_tree_redacts_living_and_links_only_existing_pages(self):
        self._seed_rels_chain()
        # Make the grandparent (apex) living → must be "Living Person", no url.
        self.conn.execute("UPDATE persons SET living='true' WHERE id='p-cccccccccc'")
        self._run(linked=False)
        tree = json.loads(
            (self.out_dir / 'data' / 'tree_p-cccccccccc_descendants.json').read_text(encoding='utf-8'))
        by_id = {n['p_id']: n for n in tree['nodes']}
        self.assertEqual(by_id['P-cccccccccc']['name'], site._LIVING_LABEL)   # living apex redacted
        self.assertIsNone(by_id['P-cccccccccc']['url'])
        # Every node url that is set must point to a generated person page.
        for n in tree['nodes']:
            if n['url']:
                self.assertTrue((self.out_dir / 'persons' / Path(n['url']).name).exists())

    def test_no_tree_without_root_person(self):
        self._seed_person('p-aaaaaaaaaa', 'Solo')   # no fha.yaml root_person, no edges
        self._run(linked=True)
        self.assertNotIn('fha-tree-data', self._read('index.html'))

    def test_home_tree_bounds_initial_paint(self):
        # P2-3: the home descendant explorer passes a bounded initialDepth to the
        # renderer. The per-person page now shows a static pedigree (no interactive
        # renderer), so it carries no initialDepth.
        self._seed_rels_chain()
        self._run(linked=True)
        self.assertIn('initialDepth: 4', self._read('index.html'))
        person = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('initialDepth', person)
        self.assertIn('class="pedigree"', person)

    def test_relationship_cycle_terminates(self):
        # A cousin-marriage style cycle must not loop forever; the BFS visited
        # set bounds it and the node set is deduplicated. Exercised now via the
        # home descendant tree (the only interactive tree that remains).
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

    def test_mistyped_root_person_warns(self):
        self._seed_person('p-aaaaaaaaaa', 'Real Person')
        (self.archive_root / 'fha.yaml').write_text(
            'roots: {}\nroot_person: P-zzzzzzzzzz\n', encoding='utf-8')   # not in index
        res = self._run(linked=True)
        self.assertTrue(any('root_person' in m and 'not in the index' in m for m in res['messages']))
        self.assertNotIn('fha-tree-data', self._read('index.html'))


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


if __name__ == '__main__':
    unittest.main()
