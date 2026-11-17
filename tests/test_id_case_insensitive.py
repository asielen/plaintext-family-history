"""IDs are matched without regard to case (SPEC §10).

A genealogist who types `P-DE957BCDA1`, or an archive whose asset files and
older records still carry `_S-` / `P-` from an earlier tool, must read exactly
as the all-lowercase canon does: same people, same sources, same claims, same
family ties, same lint findings. This test rewrites every ID in a copy of the
example archive - record text, links, file names - to uppercase and requires
the index and lint to come out identical to the lowercase original.
"""
import contextlib
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod  # noqa: E402
import lint  # noqa: E402
from _lib import load_fha_yaml  # noqa: E402

_ID = re.compile(r'(?<![0-9A-Za-z])([psclh])-([0-9a-hjkmnp-tv-z]{10})(?![0-9a-z])')
_TABLES = ('persons', 'sources', 'claims', 'claim_persons', 'relationships',
           'aliases', 'source_files', 'source_people', 'places')


def _uppercase_ids(root: Path) -> None:
    def up(m: re.Match) -> str:
        return f'{m.group(1).upper()}-{m.group(2).upper()}'
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = Path(dirpath) / name
            if path.suffix in ('.md', '.yaml', '.yml', '.txt', '.jsonl', '.csv'):
                text = path.read_text(encoding='utf-8', errors='surrogateescape')
                path.write_text(_ID.sub(up, text), encoding='utf-8', errors='surrogateescape')
            new = _ID.sub(up, name)
            if new != name:
                path.rename(path.with_name(new))


def _index_and_lint(root: Path) -> tuple[dict, list[str]]:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        index_mod.build_index(root, load_fha_yaml(root))
    conn = sqlite3.connect(root / '.cache' / 'index.sqlite')
    try:
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in _TABLES}
        ids = sorted(r[0].lower() for r in conn.execute(
            'SELECT id FROM persons UNION ALL SELECT id FROM sources UNION ALL SELECT id FROM claims'))
        counts['ids'] = ids
    finally:
        conn.close()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        found, _registry = lint._run_lint_core(root, load_fha_yaml(root))
    prefix = str(root).lower().replace('\\', '/') + '/'
    findings = sorted(str(f).lower().replace('\\', '/').replace(prefix, '') for f in found)
    return counts, findings


class IdsMatchWithoutRegardToCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        for name in ('lower', 'upper'):
            shutil.copytree(ROOT / 'example-archive', self.tmp / name,
                            ignore=shutil.ignore_patterns('.cache', 'generated'))
        _uppercase_ids(self.tmp / 'upper')

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_all_uppercase_archive_reads_exactly_like_the_lowercase_one(self) -> None:
        upper_ids = list((self.tmp / 'upper' / 'people').rglob('*_P-*.md'))
        self.assertTrue(upper_ids, 'the rewrite produced no uppercase record names')
        lower_counts, lower_lint = _index_and_lint(self.tmp / 'lower')
        upper_counts, upper_lint = _index_and_lint(self.tmp / 'upper')
        self.assertEqual(upper_counts, lower_counts)
        self.assertEqual(upper_lint, lower_lint)


if __name__ == '__main__':
    unittest.main()
