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

import views
import index as index_mod
from _lib import EXIT_WARNINGS, load_fha_yaml

PID = 'P-aaaaaaaaaa'


def _person(pid: str, name: str) -> str:
    return (f'---\nid: {pid}\nname: {name}\nliving: false\n'
            f'tier: curated\n---\n\n# {name}\n\n## Biography\n\nx\n')


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
            / f'hartley__ann_draft-queue_{PID}.md')
        self.assertFalse(companion.exists())


if __name__ == '__main__':
    unittest.main()
