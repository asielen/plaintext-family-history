"""
test_views_html.py - `--format md|html` for the generated content views.

The three content views (timeline / sources-index / draft-queue) and `views
refresh` take a --format switch: 'html' writes a standalone single-file page
under generated/views/ rendered from the SAME queries and line rendering as
the .md twin.  These tests pin the single-file-HTML conventions that other
fha artifacts inherit (TOOLING §7 D11):

  - the GENERATED marker is line 1, BEFORE <!DOCTYPE html>, and satisfies
    the exact `_lib.is_generated_text` ownership test the .md views use;
  - one inline <style> block; no external requests of any kind;
  - the visible not-for-publication banner (views are private, unredacted);
  - [[ID]] tokens render as plain styled text, never links;
  - `views clean` sweeps generated/views/ marker-per-file (hand-written
    files survive);
  - writes under generated/ never stale the index (exit 0; the .md path is
    unchanged and still stales it);
  - `views tree --format html` still refuses (exit 3) with a pointer to
    `fha site` - the HTML tree ships with the site's full-tree feature.
"""

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod
import views
from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    is_generated_text,
    load_fha_yaml,
    open_index_db,
)

PID = 'P-aaaaaaaaaa'
STUB = 'P-bbbbbbbbbb'
SID = 'S-1111111111'
COUPLE_DIR = '040 Cur Hartley + Ann Reed'

# Record text deliberately carries &, <, > so the escaping path is exercised
# end-to-end (a claim value or place with markup characters must never inject
# raw HTML into the page).
_CLAIMS = f"""- value: "Cur Hartley born about 1880 & baptized"
  id: C-1111111111
  type: birth
  persons: [{PID}]
  date: 1880~
  place_text: "Fairview <Kansas>"
  status: accepted
  reviewed: 2026-01-01
  confidence: medium
  information: primary
  evidence: direct
  notes: x.
- value: "Cur Hartley in the 1885 state census"
  id: C-2222222222
  type: census
  persons: [{PID}]
  date: 1885
  status: accepted
  reviewed: 2026-01-01
  confidence: medium
  information: primary
  evidence: direct
  notes: x.
- value: "Cur Hartley worked as a clerk"
  id: C-3333333333
  type: occupation
  persons: [{PID}]
  date: 1890
  status: suggested
  confidence: low
  information: primary
  evidence: direct
  notes: x.
"""


def _person(pid: str, name: str, tier: str) -> str:
    return (f'---\nid: {pid}\nname: {name}\nliving: false\ntier: {tier}\n---\n\n'
            f'# {name}\n\n## Biography\n\nUncited prose only.\n')


class _ViewsHtmlBase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'people' / COUPLE_DIR).mkdir(parents=True)
        (self.root / 'sources' / 'census').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text(
            'site:\n  archive_name: Test Archive\n'
            'roots:\n  documents: documents\n',
            encoding='utf-8')
        self.profile = (self.root / 'people' / COUPLE_DIR
                        / f'hartley__cur_{PID}.md')
        self.profile.write_text(_person(PID, 'Cur Hartley', 'curated'),
                                encoding='utf-8')
        (self.root / 'people' / 'stubs' / f'hartley__stub_{STUB}.md').write_text(
            _person(STUB, 'Stub Hartley', 'stub'), encoding='utf-8')
        (self.root / 'sources' / 'census' / f'test-census_{SID}.md').write_text(
            f'---\nid: {SID}\ntitle: Test Census & Notes\nsource_type: census\n---\n\n'
            f'## Claims\n```yaml\n{_CLAIMS}```\n',
            encoding='utf-8')
        self._reindex()

    def _reindex(self) -> None:
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def _gen_dir(self) -> Path:
        return self.root / 'generated' / 'views'

    def _runners(self):
        return (
            (views.run_timeline, 'timeline'),
            (views.run_sources_index, 'sources-index'),
            (views.run_draft_queue, 'draft-queue'),
        )


class HtmlFormatMatrixTests(_ViewsHtmlBase):
    def test_html_lands_under_generated_views_with_marker_before_doctype(self):
        for runner, kind in self._runners():
            self._reindex()
            res = runner(self.root, person_id=PID, fmt='html')
            self.assertEqual(res.exit_code, EXIT_CLEAN, kind)
            self.assertEqual(len(res.changed), 1, kind)
            out = Path(res.changed[0])
            self.assertEqual(out.parent, self._gen_dir(), kind)
            self.assertEqual(out.name, f'hartley__cur_{kind}_{PID}.html', kind)
            text = out.read_text(encoding='utf-8')
            lines = text.splitlines()
            # Convention 1: marker is line 1, doctype comes after it.
            self.assertTrue(
                lines[0].startswith(f'<!-- GENERATED by fha views {kind}'), kind)
            self.assertEqual(lines[1], '<!DOCTYPE html>', kind)
            # Ownership judged by the exact predicate the .md views use.
            self.assertTrue(
                is_generated_text(text, prefix='<!-- GENERATED by fha views'), kind)

    def test_default_md_output_unchanged_and_no_generated_folder(self):
        for runner, kind in self._runners():
            self._reindex()
            res = runner(self.root, person_id=PID)
            self.assertEqual(res.exit_code, EXIT_CLEAN, kind)
            out = Path(res.changed[0])
            self.assertEqual(out.parent, self.profile.parent, kind)
            self.assertEqual(out.suffix, '.md', kind)
        self.assertFalse((self.root / 'generated').exists())

    def test_unknown_format_is_refused_with_exit_3(self):
        res = views.run_timeline(self.root, person_id=PID, fmt='pdf')
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertFalse(res.changed)

    def test_stub_person_is_skipped_for_html_too(self):
        res = views.run_timeline(self.root, person_id=STUB, fmt='html')
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertFalse(res.changed)


class HtmlContentTests(_ViewsHtmlBase):
    def _pair(self, runner):
        """Generate the .md and .html twins for one view; return both texts."""
        res_md = runner(self.root, person_id=PID)
        md_text = Path(res_md.changed[0]).read_text(encoding='utf-8')
        self._reindex()
        res_html = runner(self.root, person_id=PID, fmt='html')
        html_text = Path(res_html.changed[0]).read_text(encoding='utf-8')
        return md_text, html_text

    def test_parity_every_md_token_appears_in_html(self):
        # Same-query invariant: every [[ID]] token the .md renders must appear
        # in the .html twin (as plain styled text, not a link).
        for runner, kind in self._runners():
            self._reindex()
            md_text, html_text = self._pair(runner)
            tokens = re.findall(r'\[\[([^\]|]+)', md_text)
            self.assertTrue(tokens, f'{kind}: fixture produced no tokens')
            for tid in tokens:
                self.assertIn(tid, html_text, kind)
            self.assertNotIn('<a ', html_text, kind)

    def test_timeline_sections_and_escaping(self):
        self._reindex()
        res = views.run_timeline(self.root, person_id=PID, fmt='html')
        text = Path(res.changed[0]).read_text(encoding='utf-8')
        self.assertIn('<h3>1880s</h3>', text)                 # decade section
        self.assertIn('<h2>Unreviewed</h2>', text)            # suggested claims
        self.assertIn('&amp; baptized', text)                 # & escaped
        self.assertIn('Fairview &lt;Kansas&gt;', text)        # <> escaped
        self.assertIn('<span class="cite">S-1111111111</span>', text)

    def test_banner_and_masthead_present_in_every_view(self):
        for runner, kind in self._runners():
            self._reindex()
            res = runner(self.root, person_id=PID, fmt='html')
            text = Path(res.changed[0]).read_text(encoding='utf-8')
            self.assertIn('Private research companion', text, kind)
            self.assertIn('not redacted for publication', text, kind)
            self.assertIn('Test Archive', text, kind)         # fha.yaml masthead

    def test_self_containment(self):
        # Convention 2: no external requests of any kind; exactly one inline
        # <style> block.
        for runner, kind in self._runners():
            self._reindex()
            res = runner(self.root, person_id=PID, fmt='html')
            low = Path(res.changed[0]).read_text(encoding='utf-8').lower()
            self.assertNotIn('http://', low, kind)
            self.assertNotIn('https://', low, kind)
            self.assertNotIn('<link', low, kind)
            self.assertIsNone(re.search(r'<script[^>]*\bsrc\s*=', low), kind)
            self.assertEqual(low.count('<style'), 1, kind)


class MarkerGuardTests(_ViewsHtmlBase):
    def test_handwritten_html_at_target_is_refused_untouched(self):
        target_dir = self._gen_dir()
        target_dir.mkdir(parents=True)
        target = target_dir / f'hartley__cur_timeline_{PID}.html'
        target.write_text('<p>my hand-made page</p>', encoding='utf-8')
        res = views.run_timeline(self.root, person_id=PID, fmt='html')
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertFalse(res.changed)
        self.assertEqual(target.read_text(encoding='utf-8'),
                         '<p>my hand-made page</p>')


class WriteErrorHandlingTests(_ViewsHtmlBase):
    """Codex P2 findings on the shared _lib write/render infra (tools/_lib.py):
    a companion write must never recreate a person folder that moved or was
    deleted since the index was last built, and a filesystem or template
    failure must report a plain error instead of leaking a raw traceback."""

    def test_stale_index_does_not_recreate_deleted_person_folder(self):
        folder = self.profile.parent
        shutil.rmtree(folder)
        for runner, kind in self._runners():
            res = runner(self.root, person_id=PID)
            self.assertEqual(res.exit_code, EXIT_FAILURE, kind)
            self.assertFalse(folder.exists(), kind)

    def test_write_oserror_reports_plain_error_not_traceback(self):
        orig = views.write_generated_file

        def boom(*args, **kwargs):
            raise OSError(28, 'No space left on device')

        views.write_generated_file = boom
        try:
            res = views.run_timeline(self.root, person_id=PID)
        finally:
            views.write_generated_file = orig
        self.assertEqual(res.exit_code, EXIT_FAILURE)

    def test_broken_template_reports_plain_error_not_traceback(self):
        orig = views.render_template

        def boom(name, **context):
            raise RuntimeError(f'the {name} template is missing or broken - boom')

        views.render_template = boom
        try:
            res = views.run_timeline(self.root, person_id=PID, fmt='html')
        finally:
            views.render_template = orig
        self.assertEqual(res.exit_code, EXIT_FAILURE)


class ExitCodeTests(_ViewsHtmlBase):
    def test_html_write_does_not_stale_index_md_write_does(self):
        # HTML lands under generated/, which newest_record_mtime never scans:
        # the strict freshness gate still opens afterwards.  The .md companion
        # write is byte-for-byte the old behavior - it stales the index.
        res = views.run_timeline(self.root, person_id=PID, fmt='html')
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        conn = open_index_db(self.root, ('persons',), strict=True)
        self.assertIsNotNone(conn)
        conn.close()
        res_md = views.run_timeline(self.root, person_id=PID)
        self.assertEqual(res_md.exit_code, EXIT_CLEAN)
        self.assertIsNone(open_index_db(self.root, ('persons',), strict=True))


class CleanSweepTests(_ViewsHtmlBase):
    def test_clean_sweeps_generated_views_marker_per_file(self):
        views.run_refresh(self.root, fmt='both')
        hand = self._gen_dir() / 'keep-me.html'
        hand.write_text('<p>mine</p>', encoding='utf-8')
        gen_before = sorted(p.name for p in self._gen_dir().iterdir())
        self.assertGreater(len(gen_before), 1)

        # Dry run: nothing deleted, both sweeps listed in the would-remove set.
        res = views.run_clean(self.root, dry_run=True)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertFalse(res.changed)
        self.assertEqual(sorted(p.name for p in self._gen_dir().iterdir()),
                         gen_before)
        # people/-tree .md + generated/views/ html were both counted.
        self.assertEqual(res.data.get('would_remove'),
                         len(gen_before) - 1 + 4)  # 3 companions + folder index

        # Real run: marker-owned files gone from both places; the hand-written
        # file survives (marker-per-file, never folder ownership).
        res = views.run_clean(self.root)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)  # people/-tree md removed
        self.assertEqual([p.name for p in self._gen_dir().iterdir()],
                         ['keep-me.html'])
        left = sorted(p.name for p in self.profile.parent.iterdir())
        self.assertEqual(left, [f'hartley__cur_{PID}.md'])

    def test_html_only_clean_exits_zero(self):
        # Removing only generated/views/ files leaves no stale index rows -
        # that sweep alone is a clean exit.
        views.run_refresh(self.root, fmt='html')
        res = views.run_clean(self.root)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertTrue(res.changed)
        self.assertEqual(list(self._gen_dir().iterdir()), [])


class RefreshFormatTests(_ViewsHtmlBase):
    def test_refresh_both_writes_both_sets(self):
        res = views.run_refresh(self.root, fmt='both')
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        # Per person: 3 md + 3 html; couple folder index: 1 md + 1 html.
        self.assertEqual(res.data.get('count'), 8)
        gen_names = sorted(p.name for p in self._gen_dir().iterdir())
        self.assertEqual(gen_names, [
            f'{COUPLE_DIR}_sources-index.html',
            f'hartley__cur_draft-queue_{PID}.html',
            f'hartley__cur_sources-index_{PID}.html',
            f'hartley__cur_timeline_{PID}.html',
        ])
        self.assertTrue((self.profile.parent / 'sources-index.md').exists())
        self.assertTrue(
            (self.profile.parent / f'hartley__cur_timeline_{PID}.md').exists())

    def test_refresh_html_writes_only_html(self):
        res = views.run_refresh(self.root, fmt='html')
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertEqual(res.data.get('count'), 4)
        self.assertFalse(
            (self.profile.parent / f'hartley__cur_timeline_{PID}.md').exists())
        self.assertFalse((self.profile.parent / 'sources-index.md').exists())
        self.assertEqual(len(list(self._gen_dir().iterdir())), 4)


class TreeHtmlRefusalTests(_ViewsHtmlBase):
    def test_tree_html_refuses_with_pointer_to_site(self):
        res = views.run_tree(self.root, PID, 'ancestors', None, 'html', None)
        self.assertEqual(res.exit_code, EXIT_FAILURE)

    def test_tree_html_accepted_by_argparse_refused_at_runtime(self):
        # argparse must accept the choice (so the refusal can explain itself)
        # and the CLI exit code must be 3, not an argparse usage error (2).
        code = views.main(['tree', PID, '--mode', 'ancestors',
                           '--format', 'html', '--root', str(self.root)])
        self.assertEqual(code, EXIT_FAILURE)


if __name__ == '__main__':
    unittest.main()
