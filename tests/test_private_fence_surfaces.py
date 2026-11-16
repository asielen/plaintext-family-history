"""One profile, every shared surface: a note the genealogist fenced
`<!-- private -->` and an AI draft he has not reviewed must come out the same
way on the shared website, the WikiTree export and a family packet.

#305 fixed the fence on each surface separately, and each fix broke a
different combination of fence and draft (a fence around a draft's last
paragraph published the draft's first one; a fence opened above a heading
then dropped his own last Biography paragraph as if it were a draft). The
surfaces now share one pass, `_lib.withhold_private_and_drafts`; this test
pins that they agree, on a real indexed copy of the example archive.
"""

import contextlib
import importlib.util
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod  # noqa: E402
import packet  # noqa: E402
import wikitree  # noqa: E402

# tools/site.py shares its name with the stdlib `site` module; load it by path
# under a private name, as tests/test_site.py does.
_spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
site_mod = importlib.util.module_from_spec(_spec)
sys.modules['fha_site'] = site_mod
_spec.loader.exec_module(site_mod)

PID = 'p-de957bcda1'   # Thomas Hartley, deceased, in the fictional example archive

# What he wrote, section by section. Every marker is exactly as an author or
# the write-biography skill would leave it.
FIXTURE = (
    '## Biography\n'
    'HUMAN-BIO-ONE kept the books for the railroad.\n\n'
    'HUMAN-BIO-LAST he kept bees behind the house.\n\n'
    # A whole section fenced, opener ABOVE its heading, holding a draft.
    '<!-- private -->\n'
    '## Research Notes\n'
    'DRAFT-IN-FENCED-SECTION about a living cousin.\n'
    '<!-- AI-DRAFT 2026-07-01 claude-x - v1 -->\n'
    '<!-- /private -->\n\n'
    '## Stories\n'
    'HUMAN-STORY about the barn.\n'
    '<!-- AI-ACCEPTED 2026-06-01 claude-x - v0 (accepted 2026-06-20) -->\n\n'
    # A draft whose LAST paragraph (and marker) he fenced.
    'DRAFT-STORY-P1 he probably deserted.\n\n'
    '<!-- private -->\nDRAFT-STORY-P2.\n'
    '<!-- AI-DRAFT 2026-07-01 claude-x - v2 -->\n<!-- /private -->\n\n'
    'HUMAN-STORY-TAIL the barn was rebuilt.\n'
    '<!-- AI-ACCEPTED 2026-06-01 claude-x - v3 (accepted 2026-06-20) -->\n\n'
    # A nested fence.
    '<!-- private -->\nouter\n<!-- private -->\nINNER-SECRET\n<!-- /private -->\n'
    'NESTED-TAIL-SECRET\n<!-- /private -->\n\n'
    '## Friends & Family\n*(none yet)*\n'
)
SHOWN = ('HUMAN-BIO-ONE', 'HUMAN-BIO-LAST', 'HUMAN-STORY about', 'HUMAN-STORY-TAIL')
HIDDEN = ('DRAFT-IN-FENCED-SECTION', 'DRAFT-STORY-P1', 'DRAFT-STORY-P2',
          'INNER-SECRET', 'NESTED-TAIL-SECRET', 'private -->', 'AI-DRAFT', 'AI-ACCEPTED')


class OneProfileEverySurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / 'archive'
        shutil.copytree(ROOT / 'example-archive', cls.root,
                        ignore=shutil.ignore_patterns('generated', '.cache', 'out'))
        profile = next(cls.root.glob('people/**/hartley__thomas_edward_P-de957bcda1.md'))
        text = profile.read_text(encoding='utf-8')
        cut = text.index('## Biography')
        profile.write_text(text[:cut] + FIXTURE, encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            index_mod.build_index(cls.root, {})
            site_res = site_mod.run_site(cls.root, Path(cls._tmp.name) / 'site', linked=False)
            wt_res = wikitree.run_wikitree(cls.root, PID)
            pk_res = packet.run_packet(cls.root, PID, Path(cls._tmp.name) / 'out',
                                       no_photos=True)
        assert site_res['status'] == 'ok', site_res['messages']
        assert wt_res['status'] == 'ok', wt_res['messages']
        assert pk_res['status'] == 'ok', pk_res['messages']
        cls.surfaces = {
            'site': (Path(cls._tmp.name) / 'site' / 'persons' / f'{PID}.html').read_text(
                encoding='utf-8'),
            'wikitree': wt_res['text'],
            'packet': next((pk_res['packet_dir'] / 'profile').glob('*.md')).read_text(
                encoding='utf-8'),
        }
        cls.readme = (pk_res['packet_dir'] / 'README.txt').read_text(encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_his_own_prose_is_shown_everywhere(self):
        for surface, text in self.surfaces.items():
            for phrase in SHOWN:
                with self.subTest(surface=surface, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_fenced_notes_and_unreviewed_drafts_are_withheld_everywhere(self):
        for surface, text in self.surfaces.items():
            for phrase in HIDDEN:
                with self.subTest(surface=surface, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_packet_readme_counts_his_private_notes_and_the_one_draft_outside_them(self):
        # Two outer fences of his own plus the section fence = 3 notes; the
        # Stories draft's marker sits inside a fence, the fenced-section
        # draft too, so neither is miscounted as a visible draft paragraph.
        self.assertIn('3 notes you marked private were left out', self.readme)


if __name__ == '__main__':
    unittest.main()
