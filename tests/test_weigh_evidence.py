"""
test_weigh_evidence.py - the weigh-evidence skill's machine-checkable parts.

When two records disagree, the skill drafts a written argument he rules on.
Until he accepts it, none of that argument may reach the shared site as if it
were his. (A family packet ships source records whole and its README names
any that still hold draft text; test_packet covers that.) A marker covers only the text back to the previous
marker or `##` heading (SPEC §16c), so the skill ends each of the four
sections it writes with its own marker; these tests show that four markers
withhold all four sections and that one marker at the end would not.

The skill is also the record of how the tools behave (_STANDARD.md §9): every
`fha` command it shows must exist with the flags it names.
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from _lib import strip_unaccepted_drafts  # noqa: E402

SKILL = ROOT / '.claude' / 'skills' / 'weigh-evidence' / 'SKILL.md'
FHA = ROOT / 'tools' / 'fha.py'

SECTIONS = {
    'The question': 'QUESTION-TEXT When was Thomas Edward Hartley born?',
    'The evidence': 'EVIDENCE-TEXT 1. The 1850 census gives him as 10.',
    'The reasoning': 'REASONING-TEXT The two census ages agree.',
    'Conclusion': 'CONCLUSION-TEXT The evidence points to about 1840.',
}


def _marker(section: str) -> str:
    return (f'<!-- AI-DRAFT 2026-11-24 test-model - proof argument ({section.lower()}) '
            'drafted by weigh-evidence -->')


def _proof(markers: str) -> str:
    parts = ['## Claims\n```yaml\n```\n']
    for heading, text in SECTIONS.items():
        parts.append(f'## {heading}\n\n{text}\n')
        if markers == 'each':
            parts.append(_marker(heading) + '\n')
    if markers == 'end':
        parts.append(_marker('conclusion') + '\n')
    parts.append('## Notes\n\nHis own note stays.\n')
    return '\n'.join(parts)


class ProofDraftMarkersTests(unittest.TestCase):
    def test_a_marker_per_section_withholds_the_whole_argument(self) -> None:
        out, problem = strip_unaccepted_drafts(_proof('each'))
        self.assertIsNone(problem)
        for text in SECTIONS.values():
            self.assertNotIn(text.split()[0], out)
        self.assertIn('His own note stays.', out)

    def test_one_marker_at_the_end_would_leak_the_first_sections(self) -> None:
        # Why the skill writes four markers, not one.
        out, problem = strip_unaccepted_drafts(_proof('end'))
        self.assertIsNone(problem)
        self.assertIn('QUESTION-TEXT', out)
        self.assertNotIn('CONCLUSION-TEXT', out)

    def test_the_skill_says_a_marker_per_section(self) -> None:
        text = SKILL.read_text(encoding='utf-8')
        self.assertIn('End each of the four sections with its own marker', text)
        self.assertIn('drafted by weigh-evidence -->', text)


def _fha_commands(text: str) -> list[list[str]]:
    """Every `fha …` line inside the skill's fenced blocks, as words, with
    `<placeholder>` arguments and trailing comments dropped and `\\` line
    continuations joined."""
    commands = []
    for block in re.findall(r'```[a-z]*\n(.*?)```', text, flags=re.S):
        block = re.sub(r'\\\n\s*', ' ', block)
        for line in block.splitlines():
            line = line.split('  #', 1)[0].strip()
            if not line.startswith('fha '):
                continue
            line = re.sub(r'<[^>]*>', 'X', line)
            commands.append(re.findall(r'"[^"]*"|\S+', line)[1:])
    return commands


class SkillCommandsExistTests(unittest.TestCase):
    def test_every_command_and_flag_in_the_skill_exists(self) -> None:
        commands = _fha_commands(SKILL.read_text(encoding='utf-8'))
        self.assertGreater(len(commands), 8)
        seen = set()
        for words in commands:
            verbs = []
            for word in words:
                if word.startswith('-') or word.startswith('"') or word == 'X':
                    break
                if re.match(r'^[a-z][a-z-]*$', word) and len(verbs) < 2:
                    verbs.append(word)
                else:
                    break
            flags = sorted({w for w in words if w.startswith('--')})
            key = (tuple(verbs), tuple(flags))
            if key in seen:
                continue
            seen.add(key)
            with self.subTest(command=' '.join(['fha', *words])):
                run = subprocess.run(
                    [sys.executable, str(FHA), *verbs, '--help'],
                    capture_output=True, text=True, encoding='utf-8', cwd=ROOT)
                self.assertEqual(run.returncode, 0, run.stderr)
                for flag in flags:
                    self.assertRegex(run.stdout, rf'(?<![\w-]){re.escape(flag)}(?![\w-])',
                                     f'fha {" ".join(verbs)} has no {flag}')


if __name__ == '__main__':
    unittest.main()
