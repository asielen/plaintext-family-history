"""
test_samples_conformance.py - the shipped samples use only keys SPEC.md names.

`example-archive/` is what a new user studies to learn the format, and
`archive-template/` is what they copy to start by hand. A key in either that
SPEC.md never mentions teaches a format that does not exist: a reader holding
SPEC.md and a sample record could not say what the key means. This walks every
record in both (frontmatter, the dicts inside its lists, each claim, each
claim's roles, each place in the registry) and fails on any key SPEC.md does
not name.

A key counts as named when SPEC.md writes it as `key` in a code span or as
`key:` (a YAML example line).
"""

import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from _lib import read_record  # noqa: E402

SAMPLES = (ROOT / 'example-archive', ROOT / 'archive-template')
SKIP_DIRS = {'generated', '.cache', '.fha', 'docs'}


def _spec_names(key: str, spec: str) -> bool:
    k = re.escape(key)
    return bool(re.search(rf'`{k}`|`{k}:|\b{k}:', spec))


def _record_keys(path: Path) -> list[tuple[str, str]]:
    """(where, key) for every key a record uses."""
    rec = read_record(path)
    found: list[tuple[str, str]] = []

    def walk(where: str, value) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                found.append((where, str(k)))
                if isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            walk(f'{where}.{k}[]', item)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    walk(where, item)

    walk('frontmatter', rec['meta'] or {})
    for claim in rec['claims'] or []:
        if not isinstance(claim, dict):
            continue
        for k, v in claim.items():
            found.append(('claim', str(k)))
            if k == 'roles' and isinstance(v, dict):
                found.extend(('claim.roles', str(role)) for role in v)
    return found


def _place_keys(path: Path) -> list[tuple[str, str]]:
    places = yaml.safe_load(path.read_text(encoding='utf-8')) or []
    found = []
    for place in places:
        if isinstance(place, dict):
            for k, v in place.items():
                found.append(('place', str(k)))
                if isinstance(v, list):
                    found.extend((f'place.{k}[]', str(kk))
                                 for item in v if isinstance(item, dict) for kk in item)
    return found


def _sample_files():
    for sample in SAMPLES:
        for path in sorted(sample.rglob('*')):
            rel = path.relative_to(sample)
            if not path.is_file() or SKIP_DIRS & set(rel.parts):
                continue
            yield sample, rel, path


class SamplesUseOnlySpecKeysTests(unittest.TestCase):

    def test_every_key_in_the_samples_is_named_in_spec(self) -> None:
        spec = (ROOT / 'SPEC.md').read_text(encoding='utf-8')
        unknown: dict[str, set[str]] = {}
        checked = 0
        for sample, rel, path in _sample_files():
            if path.suffix == '.md':
                keys = _record_keys(path)
            elif rel.as_posix() == 'places/places.yaml':
                keys = _place_keys(path)
            else:
                continue
            checked += len(keys)
            for where, key in keys:
                if not _spec_names(key, spec):
                    unknown.setdefault(f'{where}: {key}', set()).add(
                        f'{sample.name}/{rel.as_posix()}')
        self.assertGreater(checked, 500, 'the walk found almost nothing - is the example archive there?')
        self.assertEqual(
            {k: sorted(v)[:3] for k, v in unknown.items()}, {},
            'keys SPEC.md does not name (fix the record, or add the key to SPEC.md '
            'if it is genuinely part of the format)')

    def test_the_check_catches_an_unnamed_key(self) -> None:
        spec = 'A claim carries `value` and `confidence:`; see `persons`.'
        self.assertTrue(_spec_names('value', spec))
        self.assertTrue(_spec_names('confidence', spec))
        self.assertFalse(_spec_names('human_reviewed', spec))
        self.assertFalse(_spec_names('conf', spec))


if __name__ == '__main__':
    unittest.main()
