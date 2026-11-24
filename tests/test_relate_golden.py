"""
test_relate_golden.py - `fha relate` says the same thing, byte for byte, on
every pair of people in example-archive.

He asks "how is Ethel related to Thomas?" and the words must not shift under
him when the engine behind the command is reorganised. This test runs `fha
relate` (text and --json) for every ordered pair of people in a temp copy of
example-archive and compares each answer with the recorded one in
tests/fixtures/relate-golden/example-archive.json.

The text answer is stored whole, so a failure shows the words that changed.
The --json answer is stored as a SHA-256 of its exact bytes (the full JSON for
400 pairs would be half a megabyte).

When a change MEANS to alter an answer (a new word, a new line), regenerate
the file and say in the commit which answers changed and why:

    py -3.14 tests/test_relate_golden.py --write
"""

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod
import relate
from _lib import load_fha_yaml, open_index_db

EXAMPLE = ROOT / 'example-archive'
GOLDEN = ROOT / 'tests' / 'fixtures' / 'relate-golden' / 'example-archive.json'


def _relate_every_pair() -> dict[str, dict[str, str]]:
    """{"<a> <b>": {"text": ..., "json_sha256": ...}} for every ordered pair."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'arc'
        shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns('.cache', 'generated'))
        cfg = load_fha_yaml(root)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            index_mod.build_index(root, cfg)
        conn = open_index_db(root, ('persons',))
        try:
            pids = sorted(r['id'] for r in conn.execute('SELECT id FROM persons'))
        finally:
            conn.close()
        out: dict[str, dict[str, str]] = {}
        # Each pair, then each person's `--all` list (key "<a> --all"), so the
        # reunion list cannot drift from the pair answer's words either.
        asks = [(a, b, f'{a} {b}', {}) for a in pids for b in pids]
        asks += [(a, None, f'{a} --all', {'everyone': True}) for a in pids]
        for a, b, key, kw in asks:
            result = relate.run_relate(root, cfg, a, b, **kw)
            text, js = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(text):
                relate._cmd_relate(result, use_json=False)
            with contextlib.redirect_stdout(js):
                relate._cmd_relate(result, use_json=True)
            # The temp path never appears today; replace it anyway so a
            # future message naming the root cannot make the test flaky.
            js_bytes = js.getvalue().replace(str(root), '<ROOT>').encode('utf-8')
            out[key] = {
                'text': text.getvalue().replace(str(root), '<ROOT>'),
                'json_sha256': hashlib.sha256(js_bytes).hexdigest(),
            }
        return out


class RelateMatchesRecordedAnswersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not EXAMPLE.is_dir():
            raise unittest.SkipTest('example-archive not present')
        cls.expected = json.loads(GOLDEN.read_text(encoding='utf-8'))
        cls.actual = _relate_every_pair()

    def test_same_pairs(self):
        self.assertEqual(sorted(self.actual), sorted(self.expected))

    def test_text_output_unchanged_for_every_pair(self):
        self.assertGreaterEqual(len(self.expected), 100)
        for pair, want in self.expected.items():
            with self.subTest(pair=pair):
                self.assertEqual(self.actual.get(pair, {}).get('text'), want['text'])

    def test_json_output_unchanged_for_every_pair(self):
        for pair, want in self.expected.items():
            with self.subTest(pair=pair):
                self.assertEqual(self.actual.get(pair, {}).get('json_sha256'), want['json_sha256'])

    def test_golden_covers_blood_and_unrelated_pairs(self):
        # The recorded answers exercise aunts and nieces, lineal ties, spouses,
        # in-laws, a stepmother and strangers, so a broken naming rule cannot
        # hide behind a fixture of only "no blood". (example-archive has no
        # cousins; the word grid in test_kinship pins those.)
        texts = [v['text'] for v in self.expected.values()]
        for word in ('great-aunt', 'great-niece', 'great-grandfather', 'your wife',
                     'sister-in-law', 'stepmother', 'aunt by marriage',
                     'no blood relationship'):
            with self.subTest(word=word):
                self.assertTrue(any(word in t for t in texts))
        self.assertTrue(any('no blood relationship' in t for t in texts))


if __name__ == '__main__':
    if '--write' in sys.argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(_relate_every_pair(), indent=1, sort_keys=True,
                                     ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
        print(f'wrote {GOLDEN}')
    else:
        unittest.main()
