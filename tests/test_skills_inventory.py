"""
test_skills_inventory.py - the docs name every workflow skill that ships.

The skills directory is the inventory: each `.claude/skills/{name}/SKILL.md`
is one skill. Several docs list or count them (AGENTS.md's Playbooks
paragraph, CLAUDE.md, the skills README, TOOLING.md §16, TOOLING_INTERFACE.md
§2 and §3, _STANDARD.md). A new skill that one of those docs forgets leaves an
agent that never learns the skill exists, so the lists are checked against
the directory rather than against each other (AGENTS_TOOLING, "Inventory
changes are grep-driven").
"""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / '.claude' / 'skills'

_COUNT_WORDS = {
    10: 'ten', 11: 'eleven', 12: 'twelve', 13: 'thirteen', 14: 'fourteen',
    15: 'fifteen', 16: 'sixteen', 17: 'seventeen', 18: 'eighteen', 19: 'nineteen',
    20: 'twenty', 21: 'twenty-one', 22: 'twenty-two', 23: 'twenty-three',
}


def _skill_names() -> list[str]:
    return sorted(p.parent.name for p in SKILLS.glob('*/SKILL.md'))


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding='utf-8')


class SkillsInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.names = _skill_names()
        self.word = _COUNT_WORDS[len(self.names)]

    def test_every_skill_is_named_in_each_list(self) -> None:
        tooling_16 = _read('TOOLING.md').split('## 16.', 1)[1].split('\n## ', 1)[0]
        lists = {
            'CLAUDE.md': _read('CLAUDE.md'),
            '.claude/skills/README.md': _read('.claude/skills/README.md'),
            'TOOLING.md §16': tooling_16,
            'TOOLING_INTERFACE.md': _read('TOOLING_INTERFACE.md'),
        }
        for doc, text in lists.items():
            for name in self.names:
                self.assertRegex(text, rf'(?<![\w-]){re.escape(name)}(?![\w-])',
                                 f'{doc} does not name the {name} skill')

    def test_counts_match_the_directory(self) -> None:
        for rel, pattern in (
            ('AGENTS.md', rf'\b{self.word.capitalize()} workflow playbooks\b'),
            ('.claude/skills/_STANDARD.md', rf'\bso {self.word} skills\b'),
            ('TOOLING.md', rf'\ball {self.word} authored and shipped\b'),
            ('TOOLING_INTERFACE.md', rf'\ball {self.word} SKILL\.md files\b'),
        ):
            self.assertRegex(_read(rel), pattern,
                             f'{rel} should count {self.word} skills')

    def test_frontmatter_name_matches_folder(self) -> None:
        for name in self.names:
            text = (SKILLS / name / 'SKILL.md').read_text(encoding='utf-8')
            head = yaml.safe_load(text.split('---', 2)[1])
            self.assertEqual(head.get('name'), name)
            self.assertEqual(sorted(head), ['description', 'name'], name)


if __name__ == '__main__':
    unittest.main()
