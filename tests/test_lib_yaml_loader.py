"""
test_lib_yaml_loader.py - `_lib.safe_load_yaml`, the one YAML reader every
tool uses for archive records (frontmatter, claims blocks, places.yaml,
fha.yaml).

What the user gets: every tool that reads the archive runs several times
faster, and nothing he sees changes. A good record reads to the same values
it always did; a broken one draws the same warning, word for word, with the
same line and column and the same quoted snippet pointing at the mistake.

How: the helper parses with libyaml's C parser (`yaml.CSafeLoader`) when
PyYAML has it, and on any failure parses again with the pure-Python
`yaml.SafeLoader`, whose answer - value or exception - stands. These tests
pin both halves of that promise:

  - identical values, type for type, for the YAML shapes the archive leans
    on (bare years and dates, `no`/`yes`, octal-looking years, quoted ids,
    block scalars, anchors, `!!str`), and for every block in the shipped
    example archive and the test fixtures;
  - identical errors, message for message, for malformed text - including
    text the C parser rejects with a non-YAML exception;
  - the same results with libyaml absent (the pure-Python path alone);
  - that the C parser really is what `read_record` uses when it is there
    (the speed-up is the point of the change, so it gets a guard too).
"""

import datetime
import io
import math
import re
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib
from _lib import read_record, safe_load_yaml

HAS_LIBYAML = getattr(yaml, 'CSafeLoader', None) is not None


def _python_load(text):
    return yaml.load(text, Loader=yaml.SafeLoader)


def _outcome(fn, text):
    """(True, value) or (False, (exception type, message)) for one parse."""
    try:
        return True, fn(text)
    except Exception as exc:  # noqa: BLE001 - comparing whatever is raised
        return False, (type(exc), str(exc))


def _same(a, b):
    """Equal AND the same Python type at every level, keys included, in order.

    Plain `==` calls `1 == True` and `1880 == 1880.0` equal, and a value
    that came back as the wrong one of those would change what a tool
    prints (`living: True` vs `1`) without failing an equality test.
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return len(a) == len(b) and all(
            _same(ka, kb) and _same(va, vb)
            for (ka, va), (kb, vb) in zip(a.items(), b.items()))
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and math.isnan(a):
        return math.isnan(b)
    return a == b


# The shapes a hand-written record actually contains, and the YAML 1.1
# surprises the tools already defend against (`yaml_edtf`, `_coerce_yaml`).
TRICKY_DOCS = {
    'bare dates and datetimes': (
        'born: 1880-03-04\nseen: 2026-11-07 10:30:00\n'
        'stamp: 2026-11-07T10:30:00Z\nyear: 1880\nmonth: 1880-03\n'),
    'yes/no/on/off and null spellings': (
        'living: no\nflag: yes\nlight: on\ndark: off\nT: True\nf: false\n'
        'blank:\ntilde: ~\nword: null\n'),
    'numbers YAML 1.1 reads oddly': (
        'octal: 0750\nnot_octal: 0890\nhex: 0x1F\nsep: 1_000\n'
        'sexagesimal: 1:30\nfloat: 1.5e3\ninf: .inf\nnan: .nan\nneg: -07\n'),
    'quoted ids and quoted look-alikes': (
        "id: 'P-0123456789'\nsource: \"S-7q2x9c4m1a\"\n"
        "year: '1880'\nliving: 'no'\nempty: ''\n"),
    'EDTF strings, bare and quoted': (
        'a: 1880~\nb: 188X\nc: 1880/1890\nd: "[..1884-10-01]"\n'
        "e: '{1850,1860}'\nf: 1880-03-04?\n"),
    'block scalars, every chomping': (
        'lit: |\n  line one\n  line two\n\nkeep: |+\n  kept\n\n\n'
        'strip: >-\n  folded\n  text\n\nfold: >\n  a\n\n  b\n'),
    'anchors, aliases and merge keys': (
        'base: &b\n  place: Ohio\n  year: 1880\n'
        'copy: *b\nmerged:\n  <<: *b\n  year: 1881\n'),
    'explicit tags': (
        'a: !!str 1880\nb: !!str no\nc: !!int "42"\nd: !!float 1\n'
        'e: !!binary aGVsbG8=\nf: !!set {x, y}\n'),
    'non-string keys': (
        '1880: census\nno: false key\n2026-11-07: dated key\n'
        '? explicit key\n: value\n'),
    'unicode, comments, flow collections': (
        '# a comment\nname: Zoë Ł. Øster  # trailing\n'
        'places: [Kraków, "São Paulo", {city: Łódź}]\n'
        "quote: 'it''s'\nesc: \"tab\\there\\u00e9\"\n"),
    'multi-line plain and quoted scalars': (
        'note: first line\n  continues here\n'
        'q: "one\n  two"\nsq: \'a\n\n  b\'\n'),
    'a claims block': (
        '- id: C-0123456789\n  type: birth\n  persons: [P-0123456789]\n'
        '  value: born about 1880\n  date: 1880~\n  status: suggested\n'
        '  anchor: "line 3: \\"born\\""\n'
        '- id: C-0123456780\n  type: residence\n  persons:\n'
        '    - P-0123456789\n  place_text: Gdańsk\n  date: 1900-04\n'
        '  status: accepted\n  reviewed: 2026-11-07\n'),
    'empty and comment-only documents': '# only a comment\n',
    'CRLF line endings': 'id: P-0123456789\r\nliving: no\r\nyear: 1880\r\n',
    'document markers': '---\na: 1\n...\n',
}

# Malformed text the archive's warnings have to describe. Each one is a
# case where libyaml's own message differs from PyYAML's (checked below),
# so a helper that let the C error through would fail these tests.
MALFORMED_DOCS = {
    'unclosed quote': 'title: "Census page\nsource_type: census\n',
    'tab indentation': 'a:\n\tb: 1\n',
    'unclosed flow list': 'persons: [P-0123456789, P-0123456780\n',
    'list then mapping': '- id: C-0123456789\nb: c\n',
    'over-indented key': 'title: x\n  bad: y\n',
    'two documents': 'a: 1\n---\nb: 2\n',
    'undefined alias': 'a: *nope\n',
    'a list used as a key (fails after scanning, in the constructor)':
        '? [complex, key]\n: value\n',
    'control character': 'title: bad \x01 byte\n',
    'lone surrogate (C side fails to encode, not a YAMLError)': 'title: \udc80\n',
}


class HelperMatchesSafeLoaderTests(unittest.TestCase):
    """The helper's answer is the pure-Python SafeLoader's answer."""

    def test_tricky_values_identical(self):
        for label, text in TRICKY_DOCS.items():
            with self.subTest(label):
                expected = _python_load(text)
                got = safe_load_yaml(text)
                self.assertTrue(_same(got, expected),
                                f'{label}: {got!r} != {expected!r}')

    def test_spot_checks_on_types(self):
        """The values themselves, so a shared regression in both loaders
        (not just a difference between them) is caught too."""
        meta = safe_load_yaml(TRICKY_DOCS['bare dates and datetimes'])
        self.assertIs(type(meta['born']), datetime.date)
        self.assertIs(type(meta['seen']), datetime.datetime)
        self.assertEqual(meta['year'], 1880)
        self.assertEqual(meta['month'], '1880-03')
        flags = safe_load_yaml(TRICKY_DOCS['yes/no/on/off and null spellings'])
        self.assertIs(flags['living'], False)
        self.assertIs(flags['flag'], True)
        self.assertIsNone(flags['tilde'])
        nums = safe_load_yaml(TRICKY_DOCS['numbers YAML 1.1 reads oddly'])
        self.assertEqual(nums['octal'], 488)
        self.assertEqual(nums['sexagesimal'], 90)
        quoted = safe_load_yaml(TRICKY_DOCS['quoted ids and quoted look-alikes'])
        self.assertEqual(quoted['year'], '1880')
        self.assertEqual(quoted['living'], 'no')
        tags = safe_load_yaml(TRICKY_DOCS['explicit tags'])
        self.assertEqual(tags['a'], '1880')
        self.assertEqual(tags['b'], 'no')
        self.assertEqual(tags['e'], b'hello')
        self.assertIsNone(safe_load_yaml(TRICKY_DOCS['empty and comment-only documents']))

    def test_aliases_still_share_one_object(self):
        """An alias is the SAME Python object as its anchor in PyYAML; a
        caller that edits one in place sees it in both. Keep that."""
        doc = safe_load_yaml(TRICKY_DOCS['anchors, aliases and merge keys'])
        self.assertIs(doc['base'], doc['copy'])
        self.assertEqual(doc['merged'], {'place': 'Ohio', 'year': 1881})

    def test_malformed_raises_safeloader_error_exactly(self):
        for label, text in MALFORMED_DOCS.items():
            with self.subTest(label):
                ok_py, (py_type, py_msg) = _outcome(_python_load, text)
                self.assertFalse(ok_py, f'{label} should not parse')
                with self.assertRaises(Exception) as ctx:
                    safe_load_yaml(text)
                self.assertIs(type(ctx.exception), py_type)
                self.assertEqual(str(ctx.exception), py_msg)
                # The line/column the warning prints comes from the mark.
                self.assertEqual(_lib._yaml_problem_location(ctx.exception),
                                 _lib._yaml_problem_location(
                                     _outcome_exc(_python_load, text)))

    @unittest.skipUnless(HAS_LIBYAML, 'PyYAML installed without libyaml')
    def test_malformed_cases_are_ones_where_libyaml_words_it_differently(self):
        """Guard the guard: if libyaml ever worded these the same way, the
        test above would pass even with no fallback at all."""
        for label, text in MALFORMED_DOCS.items():
            with self.subTest(label):
                c = _outcome(lambda t: yaml.load(t, Loader=yaml.CSafeLoader), text)
                self.assertFalse(c[0])
                self.assertNotEqual(c[1], _outcome(_python_load, text)[1])

    def test_open_file_error_still_names_the_file(self):
        """`load_fha_yaml` and `fha index`'s places.yaml read hand the helper
        an open file; PyYAML's message then names that file, and the second
        (Python) parse must start from the top of it, not from wherever the
        C parser stopped."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fha.yaml'
            path.write_text('roots:\n  documents: "D:/docs\n', encoding='utf-8')
            with open(path, encoding='utf-8') as f:
                expected = _outcome(_python_load, f)
            with open(path, encoding='utf-8') as f:
                got = _outcome(safe_load_yaml, f)
            self.assertEqual(got, expected)
            self.assertIn('fha.yaml', got[1][1])
            path.write_text('roots:\n  documents: documents\n', encoding='utf-8')
            with open(path, encoding='utf-8') as f:
                self.assertEqual(safe_load_yaml(f), {'roots': {'documents': 'documents'}})

    def test_every_shipped_block_identical(self):
        """Every frontmatter, claims block and .yaml file in the example
        archive and the test fixtures reads the same both ways."""
        fence = re.compile(r'^```ya?ml[^\n]*\n(.*?)^```', re.S | re.M)
        blocks = []
        for tree in (ROOT / 'example-archive', ROOT / 'tests' / 'fixtures',
                     ROOT / 'archive-template'):
            for p in sorted(tree.rglob('*')):
                if not p.is_file() or p.suffix.lower() not in ('.md', '.yaml', '.yml'):
                    continue
                try:
                    text = p.read_text(encoding='utf-8')
                except (OSError, UnicodeDecodeError):
                    continue
                if p.suffix.lower() != '.md':
                    blocks.append((p, text))
                    continue
                fm = _lib.FRONT_RE.match(text)
                if fm:
                    blocks.append((p, fm.group(1)))
                body = text[fm.end():] if fm else text
                blocks.extend((p, m.group(1)) for m in fence.finditer(body))
        self.assertGreater(len(blocks), 50)
        for p, text in blocks:
            expected = _outcome(_python_load, text)
            got = _outcome(safe_load_yaml, text)
            same = got[0] == expected[0] and (
                _same(got[1], expected[1]) if got[0] else got[1] == expected[1])
            self.assertTrue(same, f'{p.relative_to(ROOT)} reads differently')


def _outcome_exc(fn, text):
    try:
        fn(text)
    except Exception as exc:  # noqa: BLE001
        return exc
    return None


def _yaml_without_libyaml():
    """A stand-in `yaml` module with no CSafeLoader, the way PyYAML looks
    when it was built without libyaml. `load` records the loader asked for."""
    fake = types.ModuleType('yaml')
    fake.__dict__.update(
        {k: v for k, v in vars(yaml).items() if k != 'CSafeLoader'})
    fake.loaders_used = []

    def recording_load(stream, Loader):
        fake.loaders_used.append(Loader)
        return yaml.load(stream, Loader=Loader)
    fake.load = recording_load
    return fake


class WithoutLibyamlTests(unittest.TestCase):
    """PyYAML without libyaml: the helper is plain `yaml.safe_load`."""

    def test_values_and_errors_unchanged(self):
        fake = _yaml_without_libyaml()
        with unittest.mock.patch.object(_lib, 'yaml', fake):
            for label, text in {**TRICKY_DOCS, **MALFORMED_DOCS}.items():
                with self.subTest(label):
                    expected = _outcome(_python_load, text)
                    got = _outcome(safe_load_yaml, text)
                    self.assertEqual(got[0], expected[0])
                    if got[0]:
                        self.assertTrue(_same(got[1], expected[1]))
                    else:
                        self.assertEqual(got[1], expected[1])
        self.assertTrue(fake.loaders_used)
        self.assertEqual(set(fake.loaders_used), {yaml.SafeLoader})

    def test_read_record_works_without_libyaml(self):
        fake = _yaml_without_libyaml()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rec.md'
            path.write_text(_RECORD, encoding='utf-8')
            with unittest.mock.patch.object(_lib, 'yaml', fake):
                rec = read_record(path)
        self.assertEqual(rec['parse_errors'], [])
        self.assertEqual(rec['meta']['id'], 'S-0123456789')
        self.assertEqual(rec['claims'][0]['id'], 'C-0123456789')
        self.assertEqual(set(fake.loaders_used), {yaml.SafeLoader})


_RECORD = (
    '---\nid: S-0123456789\ntitle: 1880 census page\nsource_type: census\n'
    'date: 1880\nliving: no\n---\n\n# 1880 census page\n\n## Claims\n\n'
    '```yaml\n- id: C-0123456789\n  type: birth\n  persons: [P-0123456789]\n'
    '  value: born about 1880\n  status: suggested\n```\n'
)


@unittest.skipUnless(HAS_LIBYAML, 'PyYAML installed without libyaml')
class ReadRecordUsesLibyamlTests(unittest.TestCase):
    """The speed-up is real only if the record reader takes the fast path."""

    def _loaders_used_by(self, fn):
        used = []
        real_load = yaml.load

        def spy(stream, Loader):
            used.append(Loader)
            return real_load(stream, Loader=Loader)
        # yaml.safe_load calls the module-level `load`, so the spy sees the
        # old code path too - which is what makes this fail before the change.
        with unittest.mock.patch.object(yaml, 'load', spy):
            result = fn()
        return used, result

    def test_read_record_parses_with_csafeloader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rec.md'
            path.write_text(_RECORD, encoding='utf-8')
            used, rec = self._loaders_used_by(lambda: read_record(path))
        self.assertEqual(rec['parse_errors'], [])
        self.assertEqual(used, [yaml.CSafeLoader, yaml.CSafeLoader],
                         'frontmatter and claims should both take the C parser')

    def test_broken_record_warning_is_the_safeloader_wording(self):
        """A malformed claims block falls back, and the E010 text the human
        reads is built from the pure-Python error: snippet, line, column."""
        broken = _RECORD.replace('persons: [P-0123456789]', 'persons: [P-0123456789')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rec.md'
            path.write_text(broken, encoding='utf-8')
            used, rec = self._loaders_used_by(lambda: read_record(path))
            block = _lib.CLAIMS_RE.search(broken).group(1)
            py_exc = _outcome_exc(_python_load, block)
            expected = ('E010', 'Claims YAML error: ' + _lib.format_record_yaml_error(
                path, py_exc, section='claims'))
        self.assertIn(expected, rec['parse_errors'])
        self.assertIn('^', expected[1])   # PyYAML's snippet pointer survives
        self.assertEqual(used, [yaml.CSafeLoader, yaml.CSafeLoader, yaml.SafeLoader])


# Text libyaml READS but PyYAML's own parser refuses, or reads to other
# values (#228 audit, F1). Taking libyaml's word on any of these would turn
# a warning the human gets today into silence, and only on machines that
# have libyaml. The first is the one a person really types: Notepad's Tab
# key used to line values up.
LIBYAML_LENIENT_DOCS = {
    'Tab after a colon (Notepad alignment)': 'id: P-0123456789\nname:\tMary Ann\n',
    'Tab inside a value (pasted from a spreadsheet)': 'place_text: daly\tCity\n',
    'trailing Tab': 'title: census page\t\n',
    '? inside a flow list (a name)': 'persons: [Mary Ann?]\n',
    '? inside a flow list (a date)': 'dates: [1880?]\n',
    'BOM at the start of an indented line': 'a:\n\ufeff  b: 1\n',
    'BOM on a blank line': 'x: 1\n\ufeff\ny: 2\n',
    '# straight after |': 'note: |#\n  x\n',
    '# straight after >-': 'note: >-#\n  x\n',
    '# straight after |2': 'note: |2#\n    x\n',
}


class LibyamlLenientTextTests(unittest.TestCase):
    """Where libyaml is more forgiving than PyYAML, PyYAML still decides."""

    @unittest.skipUnless(HAS_LIBYAML, 'PyYAML installed without libyaml')
    def test_these_really_are_cases_libyaml_reads_differently(self):
        """Guard the guard: each case must be one where libyaml alone would
        give a different answer, or the test below proves nothing."""
        for label, text in LIBYAML_LENIENT_DOCS.items():
            with self.subTest(label):
                c = _outcome(lambda t: yaml.load(t, Loader=yaml.CSafeLoader), text)
                py = _outcome(_python_load, text)
                differ = c[0] != py[0] or (
                    not _same(c[1], py[1]) if c[0] else c[1] != py[1])
                self.assertTrue(differ, f'{label}: libyaml agrees here')

    def test_helper_gives_safeloaders_answer(self):
        for label, text in LIBYAML_LENIENT_DOCS.items():
            with self.subTest(label):
                expected = _outcome(_python_load, text)
                got = _outcome(safe_load_yaml, text)
                self.assertEqual(got[0], expected[0], label)
                if got[0]:
                    self.assertTrue(_same(got[1], expected[1]), label)
                else:
                    self.assertEqual(got[1], expected[1])

    def test_tab_aligned_fha_yaml_file_still_refused_by_name(self):
        """The stream path (fha.yaml, places.yaml): the open file is read for
        the check, then rewound and handed to PyYAML, so the error names it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fha.yaml'
            path.write_text('roots:\n  documents:\tdocuments\n', encoding='utf-8')
            with open(path, encoding='utf-8') as f:
                expected = _outcome(_python_load, f)
            with open(path, encoding='utf-8') as f:
                got = _outcome(safe_load_yaml, f)
        self.assertFalse(expected[0])
        self.assertEqual(got, expected)
        self.assertIn('fha.yaml', got[1][1])

    def test_yaml_edtf_quotes_a_value_with_a_tab(self):
        """F3: `yaml_edtf` writes a date bare only when reading it back gives
        the same text. A raw Tab does not read back under PyYAML, so the
        value must be written quoted - on every machine."""
        written = _lib.yaml_edtf('abt\t1880')
        self.assertNotEqual(written, 'abt\t1880')
        self.assertEqual(_python_load(f'date: {written}\n'), {'date': 'abt\t1880'})


class TabEditStillWarnedEndToEndTests(unittest.TestCase):
    """The user-visible half of F1: a hand edit lint flagged before this
    change is still flagged, and a Tab in fha.yaml still stops doctor."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'archive'
        shutil.copytree(ROOT / 'example-archive', self.root,
                        ignore=shutil.ignore_patterns('.cache'))

    def tearDown(self):
        self._tmp.cleanup()

    def test_lint_flags_a_tab_aligned_name(self):
        import lint
        person = next(p for p in sorted((self.root / 'people').rglob('*.md'))
                      if re.search(r'^name: ', p.read_text(encoding='utf-8'), re.M))
        text = person.read_text(encoding='utf-8')
        person.write_text(re.sub(r'^name: ', 'name:\t', text, count=1, flags=re.M),
                          encoding='utf-8', newline='')
        findings, _ = lint._run_lint_core(self.root, {})
        e010 = [f for f in findings if f.code == 'E010' and person.name in str(f.path)]
        self.assertTrue(e010, 'a Tab after name: must still draw E010')
        self.assertIn('Frontmatter YAML error', e010[0].message)

    def test_doctor_reports_a_tab_in_fha_yaml(self):
        """F2: the seen-it host and the import-recordings duplicate finder
        read fha.yaml with plain yaml.safe_load, so they refuse a Tab there.
        doctor must refuse it too, with PyYAML's own words, or the human is
        told the file is fine by one tool and broken by another."""
        import contextlib
        import doctor
        fy = self.root / 'fha.yaml'
        text = fy.read_text(encoding='utf-8')
        bad = re.sub(r'^(\s+documents:) ', r'\1\t', text, count=1, flags=re.M)
        self.assertNotEqual(bad, text)
        fy.write_text(bad, encoding='utf-8', newline='')
        with self.assertRaises(yaml.YAMLError):
            yaml.safe_load(bad)   # what the two sibling readers do
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = doctor._standalone_main(['--root', str(self.root)])
        self.assertEqual(code, _lib.EXIT_ERRORS)
        with open(fy, encoding='utf-8') as f:
            py_exc = _outcome_exc(_python_load, f)
        self.assertIn(_lib.format_fha_config_error(fy, py_exc), err.getvalue())


if __name__ == '__main__':
    unittest.main()
