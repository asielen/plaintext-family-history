"""
test_stem_links.py: a link that names a record's file, the way Obsidian writes
it, works everywhere the ID form works (SPEC §18).

When the owner types `[[` in Obsidian and picks Thomas Edward Hartley, Obsidian
writes `[[hartley__thomas_edward_p-de957bcda1|Thomas Edward Hartley]]`. Every
file stem ends in its ID (SPEC §13), so the tools read the ID off the end and
treat the link exactly as `[[p-de957bcda1|Thomas Edward Hartley]]`: the index,
lint, find, views, the family website, the exports and normalize-links.

The heavy check copies the example archive twice, rewrites every link in one
copy to its file-stem form, and compares what the tools make of each copy:
index rows, lint findings, website pages, WikiTree and GEDCOM text.

Temp copies throughout; the example archive itself is never written.
"""

import contextlib
import importlib.util
import io
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom
import index as index_mod
import lint
import normalize_links
import report
import views
import wikitree
from _lib import (
    ID_RE,
    build_alias_map,
    cited_source_ids,
    extract_bare_ids,
    extract_token_ids,
    extract_tokens,
    extract_wikilinks,
    link_field_refs,
    link_parts,
    resolve_ref,
    strip_link_wrapper,
    written_link_display,
)
from find import run_find_json

_spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site)

TOM = 'p-de957bcda1'
TOM_STEM = 'hartley__thomas_edward_p-de957bcda1'
SRC = 's-b237895f31'
SRC_STEM = 'campaign-card-1880_s-b237895f31'
CLAIM = 'c-2e6b1f9c45'


class LinkPartsTests(unittest.TestCase):
    """The one shared resolver every reader goes through."""

    def test_a_person_file_stem_reads_as_its_id(self):
        self.assertEqual(link_parts(TOM_STEM, 'Tom'), (TOM, 'Tom'))
        self.assertEqual(link_parts(TOM_STEM), (TOM, None))

    def test_a_source_file_stem_reads_as_its_id(self):
        self.assertEqual(link_parts(SRC_STEM), (SRC, None))

    def test_a_stale_stem_still_reads_as_its_id(self):
        # The record was renamed since the link was written: only the ID counts.
        self.assertEqual(link_parts('cole__maggie_old_name_p-de957bcda1'), (TOM, None))

    def test_a_source_link_shown_as_a_claim_id_is_the_claim(self):
        self.assertEqual(link_parts(SRC_STEM, CLAIM), (CLAIM, None))
        self.assertEqual(link_parts(SRC, CLAIM), (CLAIM, None))

    def test_a_person_link_shown_as_a_claim_id_stays_the_person(self):
        self.assertEqual(link_parts(TOM_STEM, CLAIM), (TOM, CLAIM))

    def test_names_and_hand_stems_come_back_as_written(self):
        self.assertEqual(link_parts(' Ken Smith ', ' Ken '), ('Ken Smith', 'Ken'))
        self.assertEqual(link_parts('grandmas-album'), ('grandmas-album', None))
        # An underscore with no ID after it is a hand stem, not a file stem.
        self.assertEqual(link_parts('my_notes'), ('my_notes', None))


class TokenReaderTests(unittest.TestCase):

    def test_extract_tokens_reads_every_stem_form_like_the_id_form(self):
        text = (f'[[{TOM_STEM}|Tom]] [[{TOM}|Tom]] [[{SRC_STEM}]] [[{SRC}]] '
                f'[[{SRC_STEM}|{CLAIM}]] [[{SRC_STEM}#Claims|{CLAIM}]] [[{CLAIM}]]')
        got = [(t, d, f) for t, d, f, _span in extract_tokens(text)]
        self.assertEqual(got, [
            (TOM, 'Tom', None), (TOM, 'Tom', None), (SRC, None, None), (SRC, None, None),
            (CLAIM, None, None), (CLAIM, None, 'Claims'), (CLAIM, None, None),
        ])

    def test_the_heading_suffix_is_ignored_for_resolution(self):
        self.assertEqual(extract_token_ids(f'[[{TOM_STEM}#Biography|Tom]]'), [TOM])
        self.assertEqual(strip_link_wrapper(f'[[{SRC_STEM}#Notes]]'), SRC)

    def test_single_brackets_never_read_a_stem(self):
        # The legacy `[ID]` form is an ID only: `[see_p-…]` in prose is not a link.
        self.assertEqual(extract_token_ids(f'[{TOM}] and [see_{TOM}]'), [TOM])

    def test_wikilinks_hand_back_the_id_so_id_skips_skip_it(self):
        got = [(t, d) for t, d, _f, _s in extract_wikilinks(
            f'[[{TOM_STEM}|Tom]] [[Ken Smith]] [[{SRC_STEM}#Claims|{CLAIM}]]')]
        self.assertEqual(got, [(TOM, 'Tom'), ('Ken Smith', None), (CLAIM, None)])

    def test_frontmatter_link_fields_read_stems(self):
        refs = link_field_refs([f'[[{TOM_STEM}|Tom]]', [TOM_STEM], f'[[{SRC_STEM}|{CLAIM}]]'])
        self.assertEqual(refs, [TOM, TOM, CLAIM])

    def test_a_stem_link_has_no_written_name_unless_one_was_given(self):
        self.assertIsNone(written_link_display(f'[[{TOM_STEM}]]'))
        self.assertEqual(written_link_display(f'[[{TOM_STEM}|Tom]]'), 'Tom')
        self.assertEqual(written_link_display('[[Maggie]]'), 'Maggie')

    def test_bare_id_scans_find_the_id_at_the_end_of_a_stem(self):
        self.assertEqual(extract_bare_ids(f'people/{TOM_STEM}.md'), [TOM])
        # Still not inside a longer run of letters and digits.
        self.assertEqual(extract_bare_ids(f'x{TOM}'), [])
        self.assertEqual(extract_bare_ids(f'{TOM}x'), [])
        self.assertTrue(ID_RE.fullmatch(TOM))

    def test_cited_source_ids_reads_a_claim_link_as_its_source(self):
        lookup = {CLAIM: SRC}.get
        text = (f'[[{SRC_STEM}#Claims|{CLAIM}]] [[{CLAIM}]] [[a_s-4f5f215e60|c-0000000000]] '
                f'[[s-3m7k9n2p4q]] [[{TOM}]] [[c-1111111111]]')
        self.assertEqual(cited_source_ids(text, lookup),
                         [SRC, SRC, 's-4f5f215e60', 's-3m7k9n2p4q'])

    def test_an_alias_written_as_a_stem_is_not_read_down_to_its_id(self):
        # A stem in someone's aliases: is a handle of its own; reading it down
        # to another record's ID would make that ID a clash and drop it.
        records = [{'id': TOM, 'aliases': [TOM]},
                   {'id': 'p-0a1b2c3d4e', 'aliases': ['p-0a1b2c3d4e', f'old_{TOM}']}]
        amap = build_alias_map(records)
        self.assertEqual(resolve_ref(f'[[{TOM_STEM}]]', amap), TOM)
        self.assertEqual(amap[TOM], TOM)


# ── The example archive, in ID form and in file-stem form ─────────────────────

_ID = r'[0-9a-hjkmnp-tv-z]{10}'
_LINK = re.compile(rf'\[\[([PSC]-{_ID})((?:#[^\]|]*)?)((?:\|[^\]]*)?)\]\]', re.I)
UNKNOWN = 'p-0000000000'


def _stems(root: Path, lead: str) -> tuple[dict[str, str], dict[str, str]]:
    """{record id: file stem} (the shortest, so a person's profile rather than
    a companion view) and {claim id: source id}."""
    stems: dict[str, str] = {}
    claims: dict[str, str] = {}
    for p in root.rglob('*.md'):
        m = re.match(rf'^(.*)_([ps]-{_ID})$', p.stem, re.I)
        if not m or p.name.startswith('_TEMPLATE'):
            continue
        rid = m.group(2).lower()
        if rid not in stems or len(p.stem) < len(stems[rid]) - len(lead):
            stems[rid] = lead + p.stem
        if rid.startswith('s-'):
            for cm in re.finditer(rf'\bid:\s*(c-{_ID})', p.read_text(encoding='utf-8'), re.I):
                claims[cm.group(1).lower()] = rid
    return stems, claims


def _to_stem_form(root: Path, lead: str = '') -> dict[str, str]:
    """Rewrite every person, source and claim link under `root` to its
    file-stem form. Returns {stem-form link: the ID-form link it replaced},
    for reading the index's raw text columns back."""
    stems, claims = _stems(root, lead)
    back: dict[str, str] = {}

    def repl(m: re.Match) -> str:
        rid, frag, disp = m.group(1).lower(), m.group(2), m.group(3)
        if rid.startswith('c-'):
            if disp or claims.get(rid) not in stems:
                return m.group(0)
            new = f'[[{stems[claims[rid]]}#Claims|{rid}]]'
        elif rid in stems:
            new = f'[[{stems[rid]}{frag}{disp}]]'
        else:
            return m.group(0)
        back[new] = m.group(0)
        return new

    for p in list(root.rglob('*.md')) + list(root.rglob('*.yaml')):
        text = p.read_text(encoding='utf-8')
        new = _LINK.sub(repl, text)
        if new != text:
            p.write_text(new, encoding='utf-8', newline='')
    return back


def _to_claim_form(root: Path) -> int:
    """Rewrite every plain source citation `[[s-…]]` in the person profiles
    to the claim form `[[source_stem#Claims|c-…]]`, naming one of that
    source's own claims. Returns how many were rewritten."""
    stems, claims = _stems(root, '')
    first_claim: dict[str, str] = {}
    for cid, sid in sorted(claims.items()):
        first_claim.setdefault(sid, cid)
    n = 0

    def repl(m: re.Match) -> str:
        nonlocal n
        sid = m.group(1).lower()
        if sid not in first_claim or sid not in stems:
            return m.group(0)
        n += 1
        return f'[[{stems[sid]}#Claims|{first_claim[sid]}]]'

    for p in (root / 'people').rglob('*.md'):
        if p.name.startswith('_TEMPLATE') or '_view_' in p.stem:
            continue
        text = p.read_text(encoding='utf-8')
        new = re.sub(rf'\[\[(s-{_ID})\]\]', repl, text)
        if new != text:
            p.write_text(new, encoding='utf-8', newline='')
    return n


LIVING_SON = 'Alex James Bradford'
ROSTER = 'sources/other/hartley-lodge-roster_s-3m7k9n2p4q.md'
JAMES_PROFILE = ('people/002 James Robert Bradford [Alex]/'
                 'bradford__james_robert_p-2b3c4d5e6f.md')


def _copy_example(dest: Path, unknown_link: str) -> None:
    shutil.copytree(ROOT / 'example-archive', dest,
                    ignore=shutil.ignore_patterns('generated', '.cache'))
    # A published biography that links a person of unknown living status.
    profile = dest / JAMES_PROFILE
    text = profile.read_text(encoding='utf-8')
    profile.write_text(text.replace('## Stories', (
        f'His son [[p-0a1b2c3d4e|{LIVING_SON}]] still lives in Fairview.\n\n## Stories'), 1),
        encoding='utf-8', newline='')
    (dest / 'notes' / 'loose-ends.md').write_text(
        f'# Loose ends\n\nA link to nobody: {unknown_link}.\n', encoding='utf-8')
    # The lodge roster, cited in Thomas's biography, is kept out of public
    # output at the family's request, so his WikiTree export must refuse.
    roster = dest / ROSTER
    text = roster.read_text(encoding='utf-8')
    roster.write_text(text.replace('source_type:', 'restricted: by-request\nsource_type:', 1),
                      encoding='utf-8', newline='')


def _build(root: Path, out: Path) -> dict:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        index_mod.build_index(root, {})
        findings, _ = lint._run_lint_core(root, {})
        site_res = site.run_site(root, out, linked=False)
        wt = {pid: wikitree.run_wikitree(root, pid).data
              for pid in ('p-de957bcda1', 'p-2b3c4d5e6f')}
        ged = gedcom.run_gedcom(root, None, all_persons=True)['text']
        find = run_find_json(root, {}, query=TOM_STEM).data
    conn = sqlite3.connect(root / '.cache' / 'index.sqlite')
    conn.row_factory = sqlite3.Row
    queues = {}
    for pid in ('p-de957bcda1', 'p-2b3c4d5e6f'):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            out_path = views._generate_draft_queue(conn, pid, root)
            backlog = report._person_has_draft_queue_backlog(conn, root, pid)
        body = Path(out_path).read_text(encoding='utf-8') if out_path else ''
        queues[pid] = (re.sub(r'GENERATED[^\n]*', '', body), backlog)
    conn.row_factory = None
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        rows = {}
        for t in tables:
            try:
                rows[t] = [repr(r) for r in conn.execute(f'SELECT * FROM "{t}"')]
            except sqlite3.Error:
                continue
    finally:
        conn.close()
    return {
        'rows': rows,
        'lint': sorted((f.severity, f.code, Path(f.path).name if f.path else '', f.message)
                       for f in findings),
        'site_status': site_res['status'],
        'site': {p.relative_to(out).as_posix(): p.read_bytes()
                 for p in out.rglob('*') if p.is_file()},
        'wikitree': wt,
        # The header carries the export date; the records follow it.
        'gedcom': ged.split('0 @', 1)[1],
        'find': find,
        'queues': queues,
    }


class StemFormArchiveTests(unittest.TestCase):
    """Every reader gives the same answer for the stem-form archive."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='fha-381-'))
        id_root = cls.tmp / 'ids'
        _copy_example(id_root, f'[[{UNKNOWN}]]')
        cls.ids = _build(id_root, cls.tmp / 'site-ids')

        cls.variants = {}
        for name, lead in (('stems', ''), ('stale', 'renamed-since_')):
            root = cls.tmp / name
            _copy_example(root, f'[[hartley__nobody_{UNKNOWN}]]')
            back = _to_stem_form(root, lead)
            back[f'[[hartley__nobody_{UNKNOWN}]]'] = f'[[{UNKNOWN}]]'
            cls.variants[name] = (back, _build(root, cls.tmp / f'site-{name}'))

        # Every profile citation written in the claim form instead.
        root = cls.tmp / 'claims'
        _copy_example(root, f'[[{UNKNOWN}]]')
        cls.claim_rewrites = _to_claim_form(root)
        cls.claims = _build(root, cls.tmp / 'site-claims')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _each(self):
        """(name, read-back map, results) for the stem and stale-stem copies."""
        return [(name, back, got) for name, (back, got) in self.variants.items()]

    def test_the_fixture_really_is_in_stem_form(self):
        for form, back, _got in self._each():
            self.assertGreater(len(back), 50, form)
            self.assertTrue(any('#Claims|c-' in k for k in back), form)

    def test_index_rows_are_the_same(self):
        # Raw text columns (a claim's notes, a research-log line) keep the link
        # as written; read the stem forms back before comparing. The full-text
        # index's own storage tables hold encoded blobs, compared through the
        # text tables they are built from.
        for form, back, got in self._each():
            pattern = re.compile('|'.join(re.escape(k) for k in sorted(back, key=len, reverse=True)))
            self.assertEqual(set(got['rows']), set(self.ids['rows']), form)
            for table, rows in self.ids['rows'].items():
                if re.search(r'_(data|idx|docsize|config)$', table):
                    continue
                read_back = sorted(pattern.sub(lambda m: back[m.group(0)], r)
                                   for r in got['rows'][table])
                self.assertEqual(read_back, sorted(rows), f'{form}: {table}')

    def test_citations_were_indexed_at_all(self):
        rows = self.ids['rows']['citations']
        self.assertTrue(any(TOM in r for r in rows))
        self.assertTrue(any("'c-" in r for r in rows))

    def test_lint_findings_are_the_same(self):
        for form, _back, got in self._each():
            self.assertEqual(got['lint'], self.ids['lint'], form)

    def test_a_stem_ending_in_an_unknown_id_is_reported_like_the_unknown_id(self):
        about = [f for f in self.ids['lint'] if UNKNOWN in f[3].lower()]
        self.assertTrue(about, 'the ID-form archive reports the unknown ID')
        for form, _back, got in self._each():
            self.assertEqual([f for f in got['lint'] if UNKNOWN in f[3].lower()], about, form)

    def test_website_pages_are_byte_for_byte_the_same(self):
        self.assertEqual(self.ids['site_status'], 'ok')
        for form, _back, got in self._each():
            self.assertEqual(got['site_status'], 'ok', form)
            self.assertEqual(sorted(got['site']), sorted(self.ids['site']), form)
            differ = [k for k in self.ids['site'] if got['site'][k] != self.ids['site'][k]]
            self.assertEqual(differ, [], form)

    def test_links_to_living_people_are_withheld_the_same(self):
        # The example archive links its two people of unknown living status in
        # prose and claims; a stem link to them is withheld just like the ID
        # link, never shown by the name written after the bar.
        label = site._LIVING_LABEL.encode()
        page = 'persons/p-2b3c4d5e6f.html'
        withheld = self.ids['site'][page].count(label)
        self.assertGreater(withheld, 0)
        for form, got in [('ids', self.ids)] + [(f, g) for f, _b, g in self._each()]:
            self.assertEqual(got['site'][page].count(label), withheld, form)
            self.assertNotIn(LIVING_SON.encode(), got['site'][page], form)

    def test_exports_are_the_same(self):
        for form, _back, got in self._each():
            self.assertEqual(got['wikitree'], self.ids['wikitree'], form)
            self.assertEqual(got['gedcom'], self.ids['gedcom'], form)

    def test_find_takes_a_pasted_file_stem(self):
        hits = self.ids['find'].get('results') or []
        self.assertTrue(hits, self.ids['find'])
        self.assertEqual(str(hits[0].get('id', '')).lower(), TOM)
        for form, _back, got in self._each():
            self.assertEqual(got['find'], self.ids['find'], form)


    # The claim form cites the source that holds the claim (SPEC §8.7, §18).
    # Index rows differ by design (the citation row names the claim), so the
    # claim-form copy is compared on what a reader sees.

    def test_the_claim_form_copy_really_cites_by_claim(self):
        self.assertGreater(self.claim_rewrites, 10)

    def test_claim_form_lint_findings_are_the_same(self):
        self.assertEqual(self.claims['lint'], self.ids['lint'])

    def test_claim_form_website_pages_are_the_same(self):
        differ = [k for k in self.ids['site'] if self.claims['site'].get(k) != self.ids['site'][k]]
        self.assertEqual(differ, [])

    def test_claim_form_wikitree_is_the_same(self):
        self.assertEqual(self.claims['wikitree'], self.ids['wikitree'])

    def test_a_restricted_source_cited_by_claim_still_refuses_wikitree(self):
        for form, got in [('ids', self.ids), ('claims', self.claims)] + [
                (f, g) for f, _b, g in self._each()]:
            self.assertEqual(got['wikitree']['p-de957bcda1']['status'], 'restricted-sources', form)

    def test_claim_form_draft_queues_are_the_same(self):
        self.assertEqual(self.claims['queues'], self.ids['queues'])
        for form, _back, got in self._each():
            self.assertEqual(got['queues'], self.ids['queues'], form)


def _example_with(tmp: Path, name: str, edits: list[tuple[str, str, str]]) -> Path:
    """A copy of the example archive with (glob, old, new) text edits, indexed."""
    root = tmp / name
    shutil.copytree(ROOT / 'example-archive', root,
                    ignore=shutil.ignore_patterns('generated', '.cache'))
    for rel_glob, old, new in edits:
        path = next(root.glob(rel_glob))
        text = path.read_text(encoding='utf-8')
        assert old in text, (rel_glob, old)
        path.write_text(text.replace(old, new), encoding='utf-8', newline='')
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        index_mod.build_index(root, {})
    return root


TOM_PROFILE = 'people/040*/hartley__thomas_edward_p-de957bcda1.md'
MARRIAGE_CITE = 'there early in 1871 [[s-ea61339378]]'
RESTRICT_ROSTER = (ROSTER, 'source_type:', 'restricted: by-request\nsource_type:')


class ClaimFormCitationTests(unittest.TestCase):
    """A citation written as a claim link reads as the source holding it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='fha-381-cf-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_wikitree_refuses_a_restricted_source_cited_in_every_form(self):
        for form, link in (
                ('id', '[[s-3m7k9n2p4q]]'),
                ('claim', '[[s-3m7k9n2p4q#Claims|c-4q2p9k3n7m]]'),
                ('stem', '[[hartley-lodge-roster_s-3m7k9n2p4q#Claims|c-4q2p9k3n7m]]'),
                ('bare', '[[c-4q2p9k3n7m]]')):
            # Every citation of the roster in Thomas's profile written this way.
            root = _example_with(self.tmp, form, [
                RESTRICT_ROSTER, (TOM_PROFILE, '[[s-3m7k9n2p4q]]', link)])
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                res = wikitree.run_wikitree(root, TOM).data
            self.assertEqual(res['status'], 'restricted-sources', form)
            self.assertNotIn('Lodge', res.get('text') or '', form)

    def _wikitree_line(self, name: str, link: str) -> str:
        root = _example_with(self.tmp, name, [
            (TOM_PROFILE, MARRIAGE_CITE, f'there early in 1871 {link}')])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            text = wikitree.run_wikitree(root, TOM).data.get('text') or ''
        return next(ln for ln in text.splitlines() if 'early in 1871' in ln)

    def test_wikitree_cites_a_claim_link_by_its_source(self):
        for name, link in (('claim', '[[s-ea61339378#Claims|c-60730c8524]]'),
                           ('stem', '[[1871-marriage-notice-fairview_s-ea61339378#Claims|c-60730c8524]]'),
                           ('bare', '[[c-60730c8524]]')):
            line = self._wikitree_line(name, link)
            self.assertIn('early in 1871 <ref name="s-ea61339378"/>', line, link)
            self.assertNotIn('c-60730c8524', line, link)

    def test_an_unknown_claim_on_a_source_link_still_cites_the_source(self):
        line = self._wikitree_line('unknown', '[[s-ea61339378#Claims|c-0000000000]]')
        self.assertIn('early in 1871 <ref name="s-ea61339378"/>', line)
        self.assertNotIn('c-0000000000', line)

    def test_the_site_cites_an_unknown_claim_by_the_source_it_names(self):
        def page(name, link):
            root = _example_with(self.tmp, name, [
                (TOM_PROFILE, MARRIAGE_CITE, f'there early in 1871 {link}')])
            out = self.tmp / f'out-{name}'
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                site.run_site(root, out, linked=False)
            html = (out / 'persons' / 'p-de957bcda1.html').read_text(encoding='utf-8')
            return re.search(r'early in 1871(.{0,60})', html).group(1)
        self.assertEqual(page('unknown', '[[s-ea61339378#Claims|c-0000000000]]'),
                         page('plain', '[[s-ea61339378]]'))

    def test_lint_checks_a_summary_line_cited_by_claim_like_one_cited_by_source(self):
        # The Born line cites the marriage notice, which holds no birth claim.
        def w104(name, link):
            root = _example_with(self.tmp, name, [
                (TOM_PROFILE, '**Born:** about 1840 - New York [[s-4f5f215e60]]',
                 f'**Born:** about 1840 - New York {link}')])
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                findings, _ = lint._run_lint_core(root, {})
            return [f.message for f in findings if f.code == 'W104']
        by_source = w104('w104-id', '[[s-ea61339378]]')
        self.assertTrue(by_source)
        self.assertEqual(w104('w104-claim', '[[s-ea61339378#Claims|c-60730c8524]]'), by_source)

    def test_the_draft_queue_counts_a_claim_link_as_citing_its_source(self):
        root = _example_with(self.tmp, 'dq', [
            (TOM_PROFILE, '[[s-ea61339378]]', '[[s-ea61339378#Claims|c-60730c8524]]')])
        conn = sqlite3.connect(root / '.cache' / 'index.sqlite')
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            out = views._generate_draft_queue(conn, TOM, root)
            backlog = report._person_has_draft_queue_backlog(conn, root, TOM)
        self.assertNotIn('s-ea61339378', Path(out).read_text(encoding='utf-8'))
        self.assertFalse(backlog)


class NormalizeLinksTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='fha-381-nl-'))
        (self.root / 'people').mkdir()
        (self.root / 'sources' / 'other').mkdir(parents=True)
        (self.root / 'people' / f'{TOM_STEM}.md').write_text(
            f'---\nid: {TOM}\nname: Thomas Edward Hartley\nliving: false\n---\n\n'
            f'# Thomas Edward Hartley\n\n## Biography\n\nHe ran for clerk [[{SRC_STEM}]], '
            f'see [[{SRC_STEM}#Claims|{CLAIM}]].\n', encoding='utf-8')
        (self.root / 'sources' / 'other' / f'{SRC_STEM}.md').write_text(
            f'---\nid: {SRC}\ntitle: Campaign card\nsource_type: other\npeople:\n'
            f'  - "[[{TOM_STEM}]]"\n  - "[[renamed_{TOM}|Tom]]"\n---\n\n# Campaign card\n\n'
            f'## Claims\n```yaml\n- id: {CLAIM}\n  type: occupation\n'
            f'  persons: ["[[{TOM_STEM}|Thomas Edward Hartley]]"]\n  value: clerk\n'
            f'  status: suggested\n```\n', encoding='utf-8')
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_stem_links_are_already_settled(self):
        res = normalize_links.run_normalize_links(self.root, {}, write=True)
        profile = (self.root / 'people' / f'{TOM_STEM}.md').read_text(encoding='utf-8')
        self.assertIn(f'[[{SRC_STEM}]]', profile)
        self.assertIn(f'[[{SRC_STEM}#Claims|{CLAIM}]]', profile)
        source = (self.root / 'sources' / 'other' / f'{SRC_STEM}.md').read_text(encoding='utf-8')
        # A stem entry keeps its stem; one with no name gains the record's name.
        self.assertIn(f'- "[[{TOM_STEM}|Thomas Edward Hartley]]"', source)
        self.assertIn(f'- "[[renamed_{TOM}|Tom]]"', source)
        # The claim's persons: is settled one per line, its stem kept.
        self.assertEqual(source.count(f'- "[[{TOM_STEM}|Thomas Edward Hartley]]"'), 2)
        self.assertNotIn(f'[[{TOM}', source + profile)
        self.assertEqual(res.data.get('ambiguous') or [], [])


if __name__ == '__main__':
    unittest.main()
