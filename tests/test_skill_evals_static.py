"""
test_skill_evals_static.py - a skill edit that quietly drops one of the
rules that keep the assistant honest (a document's words are never orders,
a probate date is not a death date, a search that found nothing is not proof,
"(check this)" on anything unchecked) fails here, before it reaches anyone's
archive.

This is the offline layer of the skill evals (tests/skill_evals/README.md).
It calls no model and needs no credentials:

- every pinned phrase in tests/skill_evals/guardrails.yaml is still in every
  file the registry lists (matched after collapsing whitespace);
- every behavioural case under tests/skill_evals/cases/ is well formed:
  required keys, a skill that exists, guardrail ids that exist, only the
  archive checks the live runner implements, regexes that compile;
- every guardrail has at least one case, and every case's overlay exists and
  stays out of photos/;
- no case or overlay names a family outside the example archive, so a real
  surname cannot slip into the repo through a test;
- the live runner's checks behave as documented on a hand-made
  before/after snapshot (no agent involved).

The live layer, tests/skill_evals/run_skill_evals.py, drives a real agent and
is run by hand. It is never collected here (its filename has no test_ prefix).
"""

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / 'tests' / 'skill_evals'
SKILLS = ROOT / '.claude' / 'skills'


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        'run_skill_evals', EVALS / 'run_skill_evals.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collapse(text: str) -> str:
    return ' '.join(text.split())


# Capitalised words a case or overlay may use besides the example archive's
# own people and places: places the fictional family lived or wrote from,
# record and repository words, months and the like. A run of capitalised
# words ending in anything else reads as a person's name and is refused.
ALLOWED_PROPER_WORDS = {
    # places in the example's story
    'kansas', 'york', 'new', 'easton', 'washington', 'fairview', 'breton',
    'plains', 'junction', 'street', 'tennessee', 'county', 'territory',
    'states', 'united', 'state', 'america',
    # records, repositories and their parts
    'census', 'archives', 'national', 'office', 'records', 'record', 'vital',
    'historical', 'society', 'clerk', 'court', 'probate', 'surrogate',
    'familysearch', 'ancestry', 'gazette', 'index', 'register', 'lodge',
    'church', 'bible', 'will', 'estate', 'abstract', 'deed', 'book',
    'federal', 'schedule', 'death', 'tree', 'trees', 'online', 'family',
    'notes', 'letter', 'interview', 'transcript', 'tape',
    # calendar
    'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
    'september', 'october', 'november', 'december', 'monday', 'tuesday',
    # forms of address and ordinary words that start a sentence
    'mr', 'mrs', 'miss', 'dear', 'aunt', 'uncle', 'grandma', 'grandpa',
    'father', 'mother', 'son', 'daughter', 'brother', 'sister', 'cousin',
    'the', 'to', 'a', 'and', 'of', 'in', 'he', 'she', 'it', 'i', 'my', 'we',
    'q', 'dna', 'ai', 'gps', 'cm', 'yes', 'no',
}

# Two or more capitalised words in a row on one line (initials allowed):
# "Chastina A. Hartley", "Mr. Layng". The last word is the one checked.
_NAME_RUN = re.compile(
    r"(?:\b(?:Mr|Mrs|Dr)\.|\b[A-Z][a-z]+|\b[A-Z]\.)(?:[ \t]+(?:[A-Z]\.|[A-Z][a-z]+))+")


def _example_words() -> set[str]:
    words: set[str] = set()
    for path in (ROOT / 'example-archive' / 'people').rglob('*'):
        for part in re.split(r'[^A-Za-z]+', path.name.removesuffix('.md')):
            if len(part) > 1:
                words.add(part.lower())
    places = (ROOT / 'example-archive' / 'places' / 'places.yaml').read_text(
        encoding='utf-8')
    for word in re.findall(r'[A-Za-z]+', places):
        words.add(word.lower())
    return words


class GuardrailRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = _load_runner()
        self.registry = self.runner.load_registry()

    def test_every_pinned_phrase_is_in_every_file_it_names(self) -> None:
        for entry in self.registry:
            for rel in entry['files']:
                path = ROOT / rel
                self.assertTrue(path.is_file(), f"{entry['id']}: {rel} is missing")
                text = _collapse(path.read_text(encoding='utf-8'))
                self.assertTrue(
                    _collapse(entry['phrase']) in text,
                    f"{entry['id']} ({entry['rule']}): {rel} no longer says "
                    f"\"{entry['phrase']}\". If the rule was reworded on "
                    f"purpose, change tests/skill_evals/guardrails.yaml in the "
                    f"same change so the edit shows in review.")

    def test_registry_entries_are_complete_and_unique(self) -> None:
        ids = [entry.get('id') for entry in self.registry]
        self.assertEqual(len(ids), len(set(ids)), 'duplicate guardrail id')
        for entry in self.registry:
            self.assertEqual(sorted(entry), ['files', 'id', 'phrase', 'rule'],
                             entry.get('id'))
            self.assertTrue(entry['files'], entry['id'])

    def test_the_runner_finds_no_missing_phrase(self) -> None:
        self.assertEqual(self.runner.missing_phrases(self.registry), [])

    def test_a_dropped_phrase_is_caught(self) -> None:
        # The check must fail when a skill loses its rule: copy every pinned
        # file, delete one entry's phrase from one copy, and look again.
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp)
            for entry in self.registry:
                for rel in entry['files']:
                    dest = copy / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(_collapse((ROOT / rel).read_text(
                        encoding='utf-8')), encoding='utf-8')
            for entry in self.registry:
                rel = entry['files'][-1]
                original = (copy / rel).read_text(encoding='utf-8')
                (copy / rel).write_text(
                    original.replace(_collapse(entry['phrase']), ''),
                    encoding='utf-8')
                missing = self.runner.missing_phrases(self.registry, copy)
                self.assertIn((entry['id'], rel, entry['phrase']), missing)
                (copy / rel).write_text(original, encoding='utf-8')

    def test_every_skill_rule_file_named_is_a_shipped_skill_or_doc(self) -> None:
        for entry in self.registry:
            for rel in entry['files']:
                self.assertTrue(
                    rel in ('AGENTS.md', '.claude/skills/_STANDARD.md')
                    or re.fullmatch(r'\.claude/skills/[a-z-]+/SKILL\.md', rel),
                    f'{entry["id"]}: {rel} is not AGENTS.md or a skill')


class CaseFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = _load_runner()
        self.registry_ids = {e['id'] for e in self.runner.load_registry()}
        self.skills = {p.parent.name for p in SKILLS.glob('*/SKILL.md')}
        self.cases = self.runner.load_cases()

    def test_there_are_cases(self) -> None:
        self.assertGreaterEqual(len(self.cases), 10)

    def test_every_case_is_well_formed(self) -> None:
        for path, case in self.cases:
            problems = self.runner.validate_case(
                case, self.registry_ids, self.skills)
            self.assertEqual(problems, [], f'{path.name}: {problems}')
            self.assertEqual(case['id'], path.stem,
                             f'{path.name}: id should match the file name')

    def test_validate_case_rejects_unknown_checks_and_bad_regexes(self) -> None:
        case = {
            'id': 'x', 'skill': 'process-source', 'guardrails': ['AR-02'],
            'summary': 's', 'golden': 'g', 'turns': ['hello'],
            'expect': {'response': {'must': ['(unclosed']},
                       'archive': {'claim_count': 3}},
        }
        problems = self.runner.validate_case(case, {'AR-02'}, {'process-source'})
        self.assertTrue(any('claim_count' in p for p in problems), problems)
        self.assertTrue(any('(unclosed' in p for p in problems), problems)
        case['skill'] = 'no-such-skill'
        case['guardrails'] = ['ZZ-99']
        problems = self.runner.validate_case(case, {'AR-02'}, {'process-source'})
        self.assertTrue(any('no-such-skill' in p for p in problems), problems)
        self.assertTrue(any('ZZ-99' in p for p in problems), problems)

    def test_every_guardrail_has_a_case(self) -> None:
        covered = {g for _path, case in self.cases for g in case['guardrails']}
        self.assertEqual(sorted(self.registry_ids - covered), [],
                         'guardrails with no behavioural case')

    def test_overlays_exist_and_stay_out_of_photos(self) -> None:
        for path, case in self.cases:
            overlay = case.get('overlay')
            if not overlay:
                continue
            folder = EVALS / 'overlays' / overlay
            self.assertTrue(folder.is_dir(), f'{path.name}: no overlay {overlay}')
            for f in folder.rglob('*'):
                rel = f.relative_to(folder).as_posix()
                self.assertFalse(rel.startswith('photos/'),
                                 f'{overlay}: {rel} writes into the photo root')

    def test_every_overlay_is_used(self) -> None:
        used = {case.get('overlay') for _p, case in self.cases}
        for folder in (EVALS / 'overlays').iterdir():
            self.assertIn(folder.name, used, f'overlay {folder.name} is unused')

    def test_cases_and_overlays_name_only_example_archive_families(self) -> None:
        known = _example_words() | ALLOWED_PROPER_WORDS
        files = list((EVALS / 'cases').glob('*.yaml'))
        files += [f for f in (EVALS / 'overlays').rglob('*') if f.is_file()]
        for f in files:
            text = f.read_text(encoding='utf-8')
            for match in _NAME_RUN.finditer(text):
                last = match.group(0).split()[-1].rstrip('.').lower()
                self.assertIn(
                    last, known,
                    f'{f.relative_to(ROOT).as_posix()}: "{match.group(0)}" '
                    f'is not a name from example-archive. Use the Hartleys and '
                    f'their neighbours, or add a place or record word to '
                    f'ALLOWED_PROPER_WORDS.')

    def test_the_name_check_catches_an_outside_surname(self) -> None:
        known = _example_words() | ALLOWED_PROPER_WORDS
        last = _NAME_RUN.search('a letter from Thomas Smithfield').group(0)
        self.assertNotIn(last.split()[-1].lower(), known)
        self.assertIn('hartley', known)
        self.assertIn('layng', known)


class RunnerChecksTests(unittest.TestCase):
    """The live runner's hard checks, on hand-made snapshots."""

    def setUp(self) -> None:
        self.runner = _load_runner()
        self.before = {
            'files': {'people/a.md': 'h1', 'notes/research-log.md': 'h2'},
            'claims': {
                'c-1': {'id': 'c-1', 'status': 'accepted', 'type': 'birth',
                        'date_edtf': '1840~', 'negated': 0, 'source_id': 's-1',
                        'value': 'born', 'persons': ['p-1']},
            },
            'research_log': [{'date': '2026-06-12', 'result': 'nil'}],
        }

    def _after(self, **changes):
        after = {
            'files': dict(self.before['files']),
            'claims': {k: dict(v) for k, v in self.before['claims'].items()},
            'research_log': list(self.before['research_log']),
        }
        for key, value in changes.items():
            after[key] = value
        return after

    def test_no_new_accepted(self) -> None:
        after = self._after()
        after['claims']['c-2'] = {
            'id': 'c-2', 'status': 'accepted', 'type': 'death',
            'date_edtf': '1873-09-15', 'negated': 0, 'source_id': 's-2',
            'value': 'died', 'persons': ['p-1']}
        ok, _ = self.runner.check_archive(
            'no_new_accepted', True, self.before, after, None)
        self.assertFalse(ok)
        after['claims']['c-2']['status'] = 'suggested'
        ok, _ = self.runner.check_archive(
            'no_new_accepted', True, self.before, after, None)
        self.assertTrue(ok)

    def test_claim_and_no_claim(self) -> None:
        after = self._after()
        after['claims']['c-2'] = {
            'id': 'c-2', 'status': 'suggested', 'type': 'death',
            'date_edtf': '1871-04-02/1873-09-15', 'negated': 0,
            'source_id': 's-2', 'value': 'Caleb died', 'persons': ['p-1']}
        spec = {'type': 'death', 'persons': 'p-1', 'date_not': '1873-09-15',
                'date_any_of': ['[..1873-09-15]', '1871-04-02/1873-09-15'],
                'value': '(?i)caleb'}
        self.assertTrue(self.runner.check_archive(
            'claim', spec, self.before, after, None)[0])
        self.assertFalse(self.runner.check_archive(
            'no_claim', {'type': 'death'}, self.before, after, None)[0])
        self.assertTrue(self.runner.check_archive(
            'no_claim', {'negated': True}, self.before, after, None)[0])
        after['claims']['c-2']['date_edtf'] = '1873-09-15'
        self.assertFalse(self.runner.check_archive(
            'claim', spec, self.before, after, None)[0])

    def test_unchanged_and_files_added(self) -> None:
        after = self._after()
        self.assertTrue(self.runner.check_archive(
            'unchanged', ['people/**'], self.before, after, None)[0])
        after['files'] = dict(after['files'])
        del after['files']['people/a.md']
        self.assertFalse(self.runner.check_archive(
            'unchanged', ['people/**'], self.before, after, None)[0])
        after['files']['sources/proofs/x_s-1.md'] = 'h9'
        self.assertTrue(self.runner.check_archive(
            'files_added', ['sources/proofs/*'], self.before, after, None)[0])
        self.assertFalse(self.runner.check_archive(
            'files_added', ['letters/*'], self.before, after, None)[0])

    def test_research_log_added(self) -> None:
        after = self._after(research_log=self.before['research_log'] + [
            {'date': '2026-11-24', 'result': 'requested (letter to the clerk)',
             'years': '1911/1925'}])
        self.assertTrue(self.runner.check_archive(
            'research_log_added', {'result': '^requested'},
            self.before, after, None)[0])
        self.assertFalse(self.runner.check_archive(
            'research_log_added', {'result': '^nil'},
            self.before, after, None)[0])

    def test_file_regex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp)
            (archive / 'letters').mkdir()
            (archive / 'letters' / 'one.md').write_text(
                'Dear clerk, Thomas Hartley (check this)', encoding='utf-8')
            spec = {'path': 'letters/*.md', 'must': [r'\(check this\)'],
                    'must_not': ['Carol']}
            self.assertTrue(self.runner.check_archive(
                'file_regex', spec, self.before, self.before, archive)[0])
            spec['must_not'] = ['Thomas']
            self.assertFalse(self.runner.check_archive(
                'file_regex', spec, self.before, self.before, archive)[0])

    def test_response_checks(self) -> None:
        results = self.runner.check_response(
            {'must': ['(?i)probate'], 'must_not': [r'\d+\s*%']},
            'The probate date is when the court acted.')
        self.assertTrue(all(ok for _name, ok, _d in results))
        results = self.runner.check_response(
            {'must_not': [r'\d+\s*%']}, 'About 70 % likely.')
        self.assertFalse(all(ok for _name, ok, _d in results))

    def test_every_allowed_check_is_implemented(self) -> None:
        for name in self.runner.ALLOWED_ARCHIVE_CHECKS:
            self.assertIn(name, self.runner.ARCHIVE_CHECKS, name)

    def test_lint_codes_are_read_from_lint_output(self) -> None:
        out = ('W101 people/x.md: no birth or death\n'
               'W119 people/stubs/y.md: lead\nE005 sources/z.md: bad\n')
        self.assertEqual(self.runner.lint_codes(out), {'W101', 'W119', 'E005'})


if __name__ == '__main__':
    unittest.main()
