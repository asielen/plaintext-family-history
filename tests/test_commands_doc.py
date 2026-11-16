"""
test_commands_doc.py - the documents a rebuilder reads cannot drift from the tools.

- docs/COMMANDS.md is exactly what build_commands_doc.py writes from the
  commands' own definitions, so the one command reference names every flag
  that exists and no flag that does not.
- TOOLING.md's lint catalogue lists exactly the codes the linter can emit.
- The core design documents are written in the present tense: no issue
  numbers, decision logs or "earlier revision" history, which belongs in git
  and the issue tracker (AGENTS_TOOLING.md, "Docs are present tense").

Run: py -3.14 -m pytest tests/test_commands_doc.py -q   (from the repo root)
"""

import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CORE_DOCS = ('SPEC.md', 'TOOLING.md', 'TOOLING_INGESTION.md', 'TOOLING_INTERFACE.md',
             'BUILD.md', 'BUILD_INGESTION.md', 'BUILD_INTERFACE.md',
             'AGENTS.md', 'AGENTS_TOOLING.md', 'CHEATSHEET.md')

# '#' plus digits that is not an issue number: an Ahnentafel position in a
# folder bracket example.
NOT_ISSUE_REFS = ('also #128',)


def _load_generator():
    spec = importlib.util.spec_from_file_location('build_commands_doc',
                                                  ROOT / 'build_commands_doc.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CommandReferenceTests(unittest.TestCase):

    def test_commands_doc_matches_the_cli(self) -> None:
        committed = (ROOT / 'docs' / 'COMMANDS.md').read_text(encoding='utf-8')
        self.assertEqual(
            committed, _load_generator().render(),
            'docs/COMMANDS.md is stale: run `py -3.14 build_commands_doc.py` and commit it')

    def test_every_top_level_command_is_documented(self) -> None:
        import sys
        sys.path.insert(0, str(ROOT / 'tools'))
        import fha
        text = (ROOT / 'docs' / 'COMMANDS.md').read_text(encoding='utf-8')
        for command in fha.COMMANDS:
            if command == 'check':        # `fha check` is a hidden alias of lint
                continue
            with self.subTest(command=command):
                self.assertIn(f'## `fha {command}`', text)


class LintCatalogueTests(unittest.TestCase):

    def test_tooling_lint_table_lists_exactly_the_emitted_codes(self) -> None:
        code_re = re.compile(r"""['"]((?:E|W|PL)\d{3})['"]""")
        emitted = set(code_re.findall((ROOT / 'tools' / 'lint.py').read_text(encoding='utf-8')))
        emitted |= set(code_re.findall((ROOT / 'tools' / 'places.py').read_text(encoding='utf-8')))
        tooling = (ROOT / 'TOOLING.md').read_text(encoding='utf-8')
        tabled = set(re.findall(r'^\|\s*`?((?:E|W|PL)\d{3})`?\s*\|', tooling, re.M))
        # A row may keep a code that is reserved but never emitted, saying so.
        reserved = set(re.findall(r'^\|\s*`?((?:E|W|PL)\d{3})`?\s*\|.*(?:no-op|retired)', tooling, re.M))
        self.assertTrue(tabled, 'TOOLING.md has no lint table rows')
        self.assertEqual(sorted(emitted - tabled), [], 'codes the linter emits but TOOLING does not list')
        self.assertEqual(sorted(tabled - emitted - reserved), [], 'codes TOOLING lists but the linter never emits')


class PresentTenseTests(unittest.TestCase):

    def test_core_docs_carry_no_history(self) -> None:
        issue_re = re.compile(r'(?<![\w&/])#\d{2,}\b')
        for name in CORE_DOCS:
            text = (ROOT / name).read_text(encoding='utf-8')
            with self.subTest(doc=name):
                stray = [line.strip()[:120] for line in text.splitlines()
                         if issue_re.search(line)
                         and not any(ok in line for ok in NOT_ISSUE_REFS)]
                self.assertEqual(stray, [], 'issue numbers belong in commits and PRs')
                for phrase in ('Decision log', 'earlier revision'):
                    self.assertNotIn(phrase, text)


if __name__ == '__main__':
    unittest.main()
