"""
test_find_claim_citations.py: `fha find <s-id>` lists every place a source
is cited, including through its claims (TOOLING §4a).

In Obsidian link mode most citations point at a claim (`[[c-…]]`,
`[[s-…#Claims|c-…]]`, or the file-name form `[[letter_s-…#Claims|c-…]]`),
so a listing of `[[s-…]]` links alone missed most of them. A claim link cites
the source that holds the claim, by the same rule the exports use
(`_lib.cited_source_ids`), and each such site is marked with the claim's ID.
The plain listing, `--related` and `--json` (which the workbench reads)
report the same sites.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import find
import index

SID = 's-aaaaaaaaaa'
OTHER = 's-bbbbbbbbbb'
CLAIM = 'c-cccccccccc'
OTHER_CLAIM = 'c-dddddddddd'


def _source(sid, stem, cid):
    return ('---\n'
            f'id: {sid}\n'
            f'title: {stem}\n'
            'source_type: letter\n'
            '---\n\n## Claims\n\n```yaml\n'
            f'- id: {cid}\n'
            '  type: residence\n'
            '  value: "Easton"\n'
            '  persons: [p-eeeeeeeeee]\n'
            '  roles: {subject: [p-eeeeeeeeee]}\n'
            '  status: accepted\n'
            '  reviewed: 2026-11-20\n'
            '```\n')


class SourceCitationSitesTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                            encoding='utf-8')
        src = self.root / 'sources' / 'letters'
        src.mkdir(parents=True)
        (src / f'letter_{SID}.md').write_text(_source(SID, 'letter', CLAIM), encoding='utf-8')
        (src / f'diary_{OTHER}.md').write_text(_source(OTHER, 'diary', OTHER_CLAIM),
                                               encoding='utf-8')
        people = self.root / 'people' / 'stubs'
        people.mkdir(parents=True)
        (people / 'hartley__ada_p-eeeeeeeeee.md').write_text(
            '---\nid: p-eeeeeeeeee\nname: Ada Hartley\nliving: false\ntier: stub\n---\n\n'
            '# Ada Hartley\n\n## Biography\n\n'
            f'Ada wrote home [[{SID}]].\n'
            f'She lived in Easton [[{CLAIM}]].\n'
            f'Her letter says so [[{SID}#Claims|{CLAIM}]].\n'
            f'Obsidian writes it this way [[letter_{SID}#Claims|{CLAIM}]].\n'
            f'Her diary agrees [[{OTHER_CLAIM}]].\n', encoding='utf-8')
        index.build_index(self.root, {})

    def tearDown(self):
        self._tmp.cleanup()

    def _out(self, fn, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*args)
        return buf.getvalue()

    def _site_lines(self, out):
        lines = out.splitlines()
        start = next(i for i, l in enumerate(lines) if 'citation sites' in l)
        return lines[start], [l.strip() for l in lines[start + 1:] if l.startswith('    ')]

    def test_plain_find_lists_claim_citations_marked_with_the_claim(self):
        heading, sites = self._site_lines(self._out(find.find_by_id, SID, self.root, {}))
        self.assertIn('(4)', heading)
        self.assertEqual(sum(f'(claim {CLAIM})' in s for s in sites), 3)
        self.assertFalse(any(OTHER_CLAIM in s for s in sites))
        lines = sorted(int(s.split(':')[1].split()[0]) for s in sites)
        self.assertEqual(len(set(lines)), 4)

    def test_other_source_lists_only_its_own_claim(self):
        heading, sites = self._site_lines(self._out(find.find_by_id, OTHER, self.root, {}))
        self.assertIn('(1)', heading)
        self.assertIn(f'(claim {OTHER_CLAIM})', sites[0])

    def test_related_and_json_report_the_same_sites(self):
        _h, plain = self._site_lines(self._out(find.find_by_id, SID, self.root, {}))
        _h, related = self._site_lines(self._out(find.run_related, SID, None, self.root, {}))
        self.assertEqual(related, plain)
        hits = find.search_json(self.root, {}, SID)
        self.assertEqual(hits[0]['id'], SID)
        cites = hits[0]['citations']
        self.assertEqual(len(cites), len(plain))
        self.assertEqual(sorted(c['claim'] for c in cites if c['claim']), [CLAIM] * 3)
        self.assertEqual(sum(1 for c in cites if c['claim'] is None), 1)


if __name__ == '__main__':
    unittest.main()
