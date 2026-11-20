"""
test_obsidian_link_mode.py: with `links: obsidian` in fha.yaml, the links the
tools write name the record's file, so Obsidian can follow them (SPEC §18).

An owner who works in Obsidian turns the mode on, runs `fha normalize-links
--obsidian --write` once, and from then on every person, source and claim
link reads `[[hartley__thomas_edward_p-de957bcda1|Thomas Edward Hartley]]`
rather than `[[p-de957bcda1|Thomas Edward Hartley]]`. Covered here:

  * the setting itself, and an unknown value (lint W161 on fha.yaml);
  * the one formatter (`_lib.LinkWriter`) and the whole-record rewrite:
    displays kept, places, claims blocks, inline code, GENERATED regions,
    `id:` and `aliases:` left alone, CRLF and comments kept;
  * `fha normalize-links --obsidian`: dry run writes nothing and previews
    exactly what the write does; a second run changes nothing;
  * lint W161: an ID link, a stale file name, a claim link with its own
    words, and silence in `id` mode;
  * the round trip on the example archive: id, then obsidian, then lint
    clean apart from the archive's usual findings;
  * the writing verbs in both modes, and `id` mode unchanged;
  * privacy: the website, a packet, WikiTree and GEDCOM withhold the same
    things in both modes.

Temp copies throughout; the example archive itself is never written.
"""

import contextlib
import importlib.util
import io
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import claim  # noqa: E402
import confirm  # noqa: E402
import gedcom  # noqa: E402
import index as index_mod  # noqa: E402
import lint  # noqa: E402
import normalize_links  # noqa: E402
import packet  # noqa: E402
import person  # noqa: E402
import relate  # noqa: E402
import source  # noqa: E402
import views  # noqa: E402
import wikitree  # noqa: E402
from _lib import (  # noqa: E402
    LinkWriter,
    convert_written_links,
    extract_tokens,
    extract_wikilinks,
    link_entry,
    link_field_refs,
    link_list_lines,
    link_parts,
    links_to_id_form,
    link_mode,
    link_mode_setting,
    link_writer,
    load_fha_yaml,
    rewrite_record_links_to_stems,
)

_spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site)

TOM = 'p-de957bcda1'
TOM_STEM = 'hartley__thomas_edward_p-de957bcda1'
TOM_NAME = 'Thomas Edward Hartley'
CALVIN = 'p-fa7541e871'
CALVIN_STEM = 'hartley__calvin_george_p-fa7541e871'
CENSUS = 's-4f5f215e60'
CENSUS_STEM = '1880-census-fairview-hartley-household_s-4f5f215e60'
CLAIM = 'c-2e6b1f9c45'
CLAIM_STEM = 'thomas-hartley-parentage_s-3p8v6n0d5t'
PLACE = 'l-7c1a9f4e22'
TOM_PROFILE = ('people/040 Thomas Edward Hartley [Calvin + Ethel + Frances]/'
               'hartley__thomas_edward_p-de957bcda1.md')
JAMES = 'p-2b3c4d5e6f'
JAMES_PROFILE = 'people/002 James Robert Bradford [Alex]/bradford__james_robert_p-2b3c4d5e6f.md'
ROSTER = 'sources/other/hartley-lodge-roster_s-3m7k9n2p4q.md'
LIVING_SON = 'Alex James Bradford'
ALEX = 'p-0a1b2c3d4e'
BS = '\\'   # the backslash Obsidian writes before a link's | inside a table


def _quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def _copy_example(dest: Path) -> Path:
    shutil.copytree(ROOT / 'example-archive', dest,
                    ignore=shutil.ignore_patterns('generated', '.cache'))
    return dest


def _set_mode(root: Path, mode: str | None) -> None:
    path = root / 'fha.yaml'
    text = path.read_text(encoding='utf-8')
    text = re.sub(r'(?m)^links:.*\n', '', text)
    if mode is not None:
        text += f'\nlinks: {mode}\n'
    path.write_text(text, encoding='utf-8')


def _codes(root: Path, code: str) -> list:
    findings, _registry = _quiet(lint._run_lint_core, root, load_fha_yaml(root))
    return [f for f in findings if f.code == code]


def _lint_summary(root: Path) -> list:
    findings, _registry = _quiet(lint._run_lint_core, root, load_fha_yaml(root))
    return sorted((f.severity, f.code, Path(f.path).name if f.path else '') for f in findings)


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file() and '.cache' not in p.parts}


# ── The setting ──────────────────────────────────────────────────────────────

class LinkModeSettingTests(unittest.TestCase):

    def test_absent_is_id(self):
        self.assertEqual(link_mode_setting({}), ('id', None))
        self.assertEqual(link_mode(None), 'id')

    def test_the_two_values_read_forgivingly(self):
        self.assertEqual(link_mode({'links': 'obsidian'}), 'obsidian')
        self.assertEqual(link_mode({'links': ' Obsidian '}), 'obsidian')
        self.assertEqual(link_mode({'links': 'id'}), 'id')

    def test_a_typo_reads_as_id_and_says_how_to_fix_it(self):
        mode, problem = link_mode_setting({'links': 'obsidan'})
        self.assertEqual(mode, 'id')
        self.assertIn('links: obsidian', problem)
        self.assertIn("'obsidan'", problem)


# ── The one formatter ───────────────────────────────────────────────────────

class LinkWriterTests(unittest.TestCase):

    def setUp(self):
        self.obs = LinkWriter(ROOT / 'example-archive', 'obsidian')
        self.ids = LinkWriter(ROOT / 'example-archive', 'id')

    def test_id_mode_returns_text_unchanged(self):
        text = f'[[{TOM}]] [[{CENSUS}]] [[{CLAIM}]] [[{TOM}|Tom]]'
        self.assertIs(self.ids.convert(text), text)
        self.assertEqual(self.ids.link(TOM, 'Tom'), f'[[{TOM}|Tom]]')

    def test_person_source_and_claim_links_name_their_files(self):
        self.assertEqual(self.obs.convert(f'[[{TOM}]]'), f'[[{TOM_STEM}|{TOM_NAME}]]')
        self.assertEqual(self.obs.convert(f'[[{TOM}|Tom]]'), f'[[{TOM_STEM}|Tom]]')
        self.assertEqual(self.obs.convert(f'[[{CENSUS}]]'), f'[[{CENSUS_STEM}|{CENSUS}]]')
        self.assertEqual(self.obs.convert(f'[[{CENSUS}|the census]]'),
                         f'[[{CENSUS_STEM}|the census]]')
        self.assertEqual(self.obs.convert(f'[[{CLAIM}]]'), f'[[{CLAIM_STEM}#Claims|{CLAIM}]]')
        self.assertEqual(self.obs.convert(f'[[s-3p8v6n0d5t#Claims|{CLAIM}]]'),
                         f'[[{CLAIM_STEM}#Claims|{CLAIM}]]')

    def test_a_table_row_writes_the_display_the_way_obsidian_does(self):
        head = '| Year | Source |\n| --- | --- |\n'
        self.assertEqual(self.obs.convert(f'{head}| 1880 | [[{CENSUS}]] |\n\nSee [[{CENSUS}]].'),
                         f'{head}| 1880 | [[{CENSUS_STEM}{BS}|{CENSUS}]] |\n\n'
                         f'See [[{CENSUS_STEM}|{CENSUS}]].')

    def test_a_heading_is_kept(self):
        self.assertEqual(self.obs.convert(f'[[{TOM}#Biography|Tom]]'),
                         f'[[{TOM_STEM}#Biography|Tom]]')

    def test_a_stale_file_name_becomes_the_current_one(self):
        self.assertEqual(self.obs.convert(f'[[hartley__tom_old_{TOM}|Tom]]'),
                         f'[[{TOM_STEM}|Tom]]')

    def test_a_current_stem_is_left_alone(self):
        text = f'[[{TOM_STEM}|Tom]] [[{CLAIM_STEM}|{CLAIM}]]'
        self.assertEqual(self.obs.convert_counted(text), (text, 0, []))

    def test_places_embeds_unknown_ids_and_names_are_left(self):
        text = (f'[[{PLACE}|Fairview]] ![[{CENSUS}]] [[p-0000000000|Nobody]] '
                '[[Grandpa Joe]] [[h-0000000000]]')
        self.assertEqual(self.obs.convert_counted(text), (text, 0, []))

    def test_a_claim_link_with_its_own_words_is_left_and_reported(self):
        text = f'[[{CLAIM}|the parentage claim]]'
        self.assertEqual(self.obs.convert_counted(text), (text, 0, [text]))

    def test_link_entry_and_list_lines(self):
        self.assertEqual(link_entry(TOM, 'Tom', self.obs), f'[[{TOM_STEM}|Tom]]')
        self.assertEqual(link_entry(TOM, None, self.obs), f'[[{TOM_STEM}]]')
        self.assertEqual(link_entry(TOM, None, self.ids), TOM)
        self.assertEqual(link_entry('p-0000000000', None, self.obs), 'p-0000000000')
        self.assertEqual(link_list_lines('people', [TOM], {TOM: 'Tom'}, links=self.obs),
                         ['people:', f'  - "[[{TOM_STEM}|Tom]]"'])
        self.assertEqual(link_list_lines('people', [TOM], {TOM: 'Tom'}, links=self.ids),
                         link_list_lines('people', [TOM], {TOM: 'Tom'}))

    def test_link_writer_reads_the_mode_from_fha_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(link_writer(root).obsidian)
            (root / 'fha.yaml').write_text('links: obsidian\n', encoding='utf-8')
            self.assertTrue(link_writer(root).obsidian)


class ReadingBackTests(unittest.TestCase):

    def test_a_display_that_is_the_links_own_id_is_no_display(self):
        self.assertEqual(link_parts(CENSUS_STEM, CENSUS), (CENSUS, None))
        self.assertEqual(link_parts(TOM, TOM.upper()), (TOM, None))
        self.assertEqual(link_parts(CENSUS_STEM, 'the census'), (CENSUS, 'the census'))

    def test_links_to_id_form_reads_file_names_back(self):
        text = (f'[[{TOM_STEM}|Tom]] [[{CENSUS_STEM}|{CENSUS}]] [[{CENSUS_STEM}#Notes]] '
                f'[[{CLAIM_STEM}#Claims|{CLAIM}]] [[{TOM}|Tom]] [[{PLACE}|Fairview]] '
                f'![[{CENSUS_STEM}]] [[my_notes]]')
        self.assertEqual(links_to_id_form(text), (
            f'[[{TOM}|Tom]] [[{CENSUS}]] [[{CENSUS}#Notes]] [[{CLAIM}]] [[{TOM}|Tom]] '
            f'[[{PLACE}|Fairview]] ![[{CENSUS_STEM}]] [[my_notes]]'))


class RecordRewriteTests(unittest.TestCase):

    def setUp(self):
        self.obs = LinkWriter(ROOT / 'example-archive', 'obsidian')

    def test_only_links_a_person_reads_are_rewritten(self):
        text = (
            '---\n'
            f'id: {TOM}\n'
            f'aliases: [{TOM}, "[[{TOM}]]"]\n'
            'people:\n'
            f'  - "[[{TOM}]]"   # the subject\n'
            f'  - [[{CALVIN}]]\n'
            '---\n'
            f'Prose [[{TOM}]] and `[[{TOM}]]` in code.\n'
            '```yaml\n'
            f'- persons: ["[[{TOM}|Tom]]"]\n'
            '```\n'
            '<!-- GENERATED-BEGIN sources-index by sources-index on 2026-01-01 -->\n'
            f'- [[{CENSUS}]]\n'
            '<!-- GENERATED-END sources-index -->\n'
            f'After [[{CENSUS}]].\n'
        )
        new, count, bad = rewrite_record_links_to_stems(text, self.obs)
        self.assertEqual(count, 4)
        self.assertEqual(bad, [])
        self.assertEqual(new, text
                         .replace(f'  - "[[{TOM}]]"', f'  - "[[{TOM_STEM}|{TOM_NAME}]]"')
                         .replace(f'  - [[{CALVIN}]]', f'  - [[{CALVIN_STEM}]]')
                         .replace(f'Prose [[{TOM}]]', f'Prose [[{TOM_STEM}|{TOM_NAME}]]')
                         .replace(f'After [[{CENSUS}]]', f'After [[{CENSUS_STEM}|{CENSUS}]]'))
        # Run twice: nothing more to do.
        self.assertEqual(rewrite_record_links_to_stems(new, self.obs), (new, 0, []))

    def test_crlf_is_kept(self):
        text = f'---\r\nid: {CALVIN}\r\n---\r\nSee [[{TOM}|Tom]].\r\n'
        new, count, _bad = rewrite_record_links_to_stems(text, self.obs)
        self.assertEqual(count, 1)
        self.assertEqual(new, text.replace(f'[[{TOM}|Tom]]', f'[[{TOM_STEM}|Tom]]'))

    def test_convert_written_links_touches_only_the_lines_written(self):
        before = f'---\r\nid: {CALVIN}\r\n---\r\nOld [[{TOM}|Tom]].\r\n'
        after = f'---\nid: {CALVIN}\n---\nOld [[{TOM}|Tom]].\nNew [[{TOM}|Tom]].\n'
        self.assertEqual(convert_written_links(before, after, self.obs),
                         after.replace(f'New [[{TOM}|Tom]]', f'New [[{TOM_STEM}|Tom]]'))
        self.assertIs(convert_written_links(before, after, LinkWriter(None, 'id')), after)
        self.assertIs(convert_written_links(before, after, None), after)


# ── normalize-links --obsidian and lint W161 ─────────────────────────────────

class NormalizeAndLintTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='fha-381p3-'))
        self.root = _copy_example(self.tmp / 'a')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_writes_nothing_and_previews_the_write(self):
        before = _tree(self.root)
        preview = normalize_links.run_normalize_links(self.root, {}, obsidian=True)
        self.assertEqual(_tree(self.root), before)
        self.assertGreater(preview.data['files_changed'], 20)
        self.assertIn('--obsidian --write', ' '.join(m.next_step or '' for m in preview.messages))
        twin = _copy_example(self.tmp / 'b')
        wrote = normalize_links.run_normalize_links(twin, {}, write=True, obsidian=True)
        self.assertEqual(wrote.data['diffs'], preview.data['diffs'])
        self.assertEqual(wrote.data['edits'], preview.data['edits'])
        again = normalize_links.run_normalize_links(twin, {}, write=True, obsidian=True)
        self.assertEqual(again.data['files_changed'], 0)
        self.assertIn(f'[[{TOM_STEM}|{TOM_NAME}]]',
                      (twin / 'people/connections/layng__charley_p-ajnng40q36.md')
                      .read_text(encoding='utf-8'))

    def test_the_mode_alone_turns_the_rewrite_on(self):
        _set_mode(self.root, 'obsidian')
        result = normalize_links.run_normalize_links(self.root, load_fha_yaml(self.root))
        self.assertTrue(result.data['obsidian'])
        self.assertGreater(result.data['files_changed'], 20)
        _set_mode(self.root, None)
        plain = normalize_links.run_normalize_links(self.root, load_fha_yaml(self.root))
        self.assertFalse(plain.data['obsidian'])
        self.assertEqual(plain.data['files_changed'], 0)

    def test_a_crlf_record_keeps_its_line_endings(self):
        path = self.root / TOM_PROFILE
        crlf = path.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        path.write_bytes(crlf)
        normalize_links.run_normalize_links(self.root, {}, write=True, obsidian=True)
        after = path.read_bytes()
        self.assertEqual(after.count(b'\r\n'), crlf.count(b'\r\n'))
        self.assertEqual(after.count(b'\n'), after.count(b'\r\n'))
        self.assertIn(f'[[{CENSUS_STEM}|{CENSUS}]]'.encode(), after)

    def test_round_trip_id_then_obsidian_then_lint_clean(self):
        baseline = _lint_summary(self.root)
        self.assertEqual(_codes(self.root, 'W161'), [], 'silent in id mode')
        _set_mode(self.root, 'obsidian')
        flagged = _codes(self.root, 'W161')
        self.assertGreater(len(flagged), 20)
        self.assertTrue(all('normalize-links --obsidian --write' in f.message for f in flagged))
        _quiet(normalize_links.run_normalize_links, self.root, load_fha_yaml(self.root),
               write=True)
        self.assertEqual(_codes(self.root, 'W161'), [])
        self.assertEqual(_lint_summary(self.root), baseline)

    def test_w161_names_a_stale_file_name(self):
        _set_mode(self.root, 'obsidian')
        _quiet(normalize_links.run_normalize_links, self.root, load_fha_yaml(self.root),
               write=True)
        layng = self.root / 'people/connections/layng__charley_p-ajnng40q36.md'
        text = layng.read_text(encoding='utf-8')
        layng.write_text(text.replace(f'[[{TOM_STEM}|', f'[[hartley__tom_{TOM}|', 1),
                         encoding='utf-8')
        flagged = _codes(self.root, 'W161')
        self.assertEqual([Path(f.path).name for f in flagged], [layng.name])
        self.assertIn('1 link(s)', flagged[0].message)

    def test_w161_names_a_claim_link_with_its_own_words(self):
        _set_mode(self.root, 'obsidian')
        _quiet(normalize_links.run_normalize_links, self.root, load_fha_yaml(self.root),
               write=True)
        notes = self.root / 'notes' / 'loose-ends.md'
        notes.write_text(f'# Loose ends\n\nSee [[{CLAIM}|the parentage claim]].\n',
                         encoding='utf-8')
        flagged = _codes(self.root, 'W161')
        self.assertEqual(len(flagged), 1)
        self.assertIn('#Claims|c-', flagged[0].message)
        result = normalize_links.run_normalize_links(self.root, load_fha_yaml(self.root))
        self.assertEqual(result.exit_code, 1)
        self.assertIn('loose-ends.md', ' '.join(m.text for m in result.messages))

    def test_w161_names_an_unknown_value(self):
        _set_mode(self.root, 'obsidan')
        flagged = _codes(self.root, 'W161')
        self.assertEqual([Path(f.path).name for f in flagged], ['fha.yaml'])

    def test_w161_is_silent_in_id_mode_over_stem_links(self):
        _quiet(normalize_links.run_normalize_links, self.root, {}, write=True, obsidian=True)
        _set_mode(self.root, 'id')
        self.assertEqual(_codes(self.root, 'W161'), [])


# ── The writing verbs ─────────────────────────────────────────────────────────

class WritersInBothModesTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='fha-381p3w-'))
        cls.roots = {}
        for mode in ('id', 'obsidian', 'absent'):
            root = _copy_example(cls.tmp / mode)
            _set_mode(root, None if mode == 'absent' else mode)
            _quiet(index_mod.build_index, root, load_fha_yaml(root))
            cls.roots[mode] = root

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _both(self):
        return self.roots['id'], self.roots['obsidian']

    def test_person_note_writes_the_archive_form(self):
        text = f'Calvin wrote to his father [[{TOM}|Thomas]] that spring [[{CENSUS}]].'
        for mode, root in zip(('id', 'obsidian'), self._both()):
            res = person.run_note(root, CALVIN, 'stories', text)
            self.assertEqual(res.exit_code, 0, [m.text for m in res.messages])
        calvin = 'people/stubs/hartley__calvin_george_p-fa7541e871.md'
        self.assertIn(text, (self.roots['id'] / calvin).read_text(encoding='utf-8'))
        written = (self.roots['obsidian'] / calvin).read_text(encoding='utf-8')
        self.assertIn(f'[[{TOM_STEM}|Thomas]] that spring [[{CENSUS_STEM}|{CENSUS}]].', written)
        # The lines the verb did not write keep their ID form.
        self.assertIn('[[s-fa1234567b]]', written)

    def test_source_note_and_discovery_write_the_archive_form(self):
        text = f'The household includes [[{CALVIN}|Calvin]].'
        for root in self._both():
            self.assertEqual(source.run_source_note(root, CENSUS, text=text).exit_code, 0)
            res = confirm.run_add_discovery(root, text=f'Found [[{TOM}]] in 1880.')
            self.assertEqual(res.exit_code, 0, [m.text for m in res.messages])
        census = f'sources/census/{CENSUS_STEM}.md'
        self.assertIn(text, (self.roots['id'] / census).read_text(encoding='utf-8'))
        self.assertIn(f'[[{CALVIN_STEM}|Calvin]]',
                      (self.roots['obsidian'] / census).read_text(encoding='utf-8'))
        self.assertIn(f'Found [[{TOM}]] in 1880.',
                      (self.roots['id'] / 'notes/discoveries.md').read_text(encoding='utf-8'))
        self.assertIn(f'Found [[{TOM_STEM}|{TOM_NAME}]] in 1880.',
                      (self.roots['obsidian'] / 'notes/discoveries.md').read_text(encoding='utf-8'))

    def test_person_relate_writes_the_archive_form(self):
        susan = 'p-s5a8g87at3'
        for root in self._both():
            res = person.run_relate(root, 'p-ajnng40q36', 'sibling', susan)
            self.assertIn(res.exit_code, (0, 1), [m.text for m in res.messages])
        layng = 'people/connections/layng__charley_p-ajnng40q36.md'
        self.assertIn(f'"[[{susan}|Susan Cole]]"',
                      (self.roots['id'] / layng).read_text(encoding='utf-8'))
        self.assertIn('"[[cole__susan_p-s5a8g87at3|Susan Cole]]"',
                      (self.roots['obsidian'] / layng).read_text(encoding='utf-8'))

    def test_views_write_the_archive_form_and_id_mode_matches_no_setting(self):
        outputs = {}
        for mode in ('id', 'obsidian', 'absent'):
            root = _copy_example(self.tmp / f'views-{mode}')
            _set_mode(root, None if mode == 'absent' else mode)
            _quiet(index_mod.build_index, root, load_fha_yaml(root))
            _quiet(views.run_refresh, root)
            outputs[mode] = {k: re.sub(rb'on \d{4}-\d{2}-\d{2}', b'', v)
                             for k, v in _tree(root / 'people').items()}
        self.assertEqual(outputs['id'], outputs['absent'])
        timeline = next(k for k in outputs['obsidian'] if 'view_timeline' in k and TOM in k)
        self.assertIn(f'[[{CENSUS_STEM}|{CENSUS}]]'.encode(), outputs['obsidian'][timeline])
        self.assertNotIn(f'[[{CENSUS}]]'.encode(), outputs['obsidian'][timeline])
        self.assertIn(f'[[{CENSUS}]]'.encode(), outputs['id'][timeline])

    def test_relate_output(self):
        for mode, root in zip(('id', 'obsidian'), self._both()):
            res = _quiet(relate.run_relate, root, load_fha_yaml(root), TOM, CALVIN)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                relate._cmd_relate(res)
            if mode == 'id':
                self.assertNotIn('links', res.data)
                self.assertIn(f'[[{TOM}|{TOM_NAME}]]', out.getvalue())
            else:
                self.assertIn(f'[[{TOM_STEM}|{TOM_NAME}]]', out.getvalue())


class ClaimAcceptWritesTheArchiveFormTests(unittest.TestCase):
    """Accepting a kin claim writes `relationships:` entries whose `to:` and
    `claims:` links name files in obsidian mode."""

    def _archive(self, mode: str) -> Path:
        root = Path(tempfile.mkdtemp(prefix='fha-381p3c-'))
        self.addCleanup(shutil.rmtree, root, True)
        (root / 'fha.yaml').write_text(f'links: {mode}\n', encoding='utf-8')
        files = {
            'people/stubs/hartley__ann_p-aaaaaaaaaa.md':
                '---\nid: p-aaaaaaaaaa\nname: Ann Hartley\nliving: false\ntier: stub\n'
                'relationships: []\n---\n\n# Ann Hartley\n',
            'people/stubs/cole__mary_p-bbbbbbbbbb.md':
                '---\nid: p-bbbbbbbbbb\nname: Mary Cole\nliving: false\ntier: stub\n'
                'relationships: []\n---\n\n# Mary Cole\n',
            'sources/other/register_s-gggggggggg.md':
                '---\nid: s-gggggggggg\ntitle: Family register\nsource_type: other\n---\n\n'
                '## Claims\n\n```yaml\n- value: "Ann is the daughter of Mary"\n'
                '  id: c-dddddddddd\n  type: relationship\n'
                '  persons: [p-aaaaaaaaaa, p-bbbbbbbbbb]\n  roles:\n'
                '    child: p-aaaaaaaaaa\n    parent: p-bbbbbbbbbb\n  status: suggested\n```\n',
        }
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode('utf-8'))
        return root

    def test_both_modes(self):
        for mode, to, cites in (
                ('id', '"[[p-bbbbbbbbbb|Mary Cole]]"', '["[[c-dddddddddd]]"]'),
                ('obsidian', '"[[cole__mary_p-bbbbbbbbbb|Mary Cole]]"',
                 '["[[register_s-gggggggggg#Claims|c-dddddddddd]]"]')):
            root = self._archive(mode)
            res = _quiet(claim.run_claim, root, claim_id='c-dddddddddd', status='accepted')
            self.assertEqual(res.exit_code, 0, [m.text for m in res.messages])
            ann = (root / 'people/stubs/hartley__ann_p-aaaaaaaaaa.md').read_text(encoding='utf-8')
            self.assertIn(f'to: {to}', ann, mode)
            self.assertIn(f'claims: {cites}', ann, mode)
            # The claims block keeps its ID form in both modes.
            src = (root / 'sources/other/register_s-gggggggggg.md').read_text(encoding='utf-8')
            self.assertIn('persons: [p-aaaaaaaaaa, p-bbbbbbbbbb]', src)
            self.assertEqual([f for f in _codes(root, 'W115')], [], mode)
            self.assertEqual([f for f in _codes(root, 'W161')], [], mode)


# ── Privacy: switching modes changes nothing an export withholds ─────────────

def _private_example(dest: Path) -> Path:
    """The example archive with a living son linked from James's biography
    and the lodge roster restricted, so every export has something to hold
    back."""
    _copy_example(dest)
    profile = dest / JAMES_PROFILE
    text = profile.read_text(encoding='utf-8')
    profile.write_text(text.replace('## Stories', (
        f'His son [[p-0a1b2c3d4e|{LIVING_SON}]] still lives in Fairview.\n\n## Stories'), 1),
        encoding='utf-8', newline='')
    roster = dest / ROSTER
    text = roster.read_text(encoding='utf-8')
    roster.write_text(text.replace('source_type:', 'restricted: true\nsource_type:', 1),
                      encoding='utf-8', newline='')
    # Bare links (no display) to the living son and the restricted roster in
    # a public source's notes and people:, a stub's stories and the packet
    # subject's own stories: the stem form fills in the son's name.
    line = (f'Kin [[{ALEX}]] and [[{ALEX}|Alex]] cite [[s-3m7k9n2p4q]] '
            '[[s-3m7k9n2p4q|the lodge roster]] [[s-3m7k9n2p4q#Page 2]].\n\n')
    for rel, old, new in (
            (f'sources/census/{CENSUS_STEM}.md', '## Notes\n', '## Notes\n' + line),
            (f'sources/census/{CENSUS_STEM}.md', 'people:\n', f'people:\n  - "[[{ALEX}]]"\n'),
            ('people/stubs/hartley__calvin_george_p-fa7541e871.md', '## Stories\n',
             '## Stories\n' + line),
            (TOM_PROFILE, '## Stories\n', '## Stories\n' + line),
            ('people/stubs/bradford__alex_james_p-0a1b2c3d4e.md', 'living: unknown',
             'living: true')):
        path = dest / rel
        text = path.read_text(encoding='utf-8')
        assert old in text, (rel, old)
        path.write_text(text.replace(old, new, 1), encoding='utf-8', newline='')
    return dest


def _exports(root: Path, out: Path) -> dict:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        index_mod.build_index(root, load_fha_yaml(root))
        # The generated regions, rebuilt in the archive's link form first.
        views.run_refresh(root)
        index_mod.build_index(root, load_fha_yaml(root))
        site_res = site.run_site(root, out / 'site', linked=False)
        wt = {pid: wikitree.run_wikitree(root, pid).data for pid in (TOM, JAMES, CALVIN)}
        ged = gedcom.run_gedcom(root, None, all_persons=True)['text']
        pk = packet.run_packet(root, TOM, out / 'packet', no_photos=True)
    return {
        'site_status': site_res['status'],
        'site': {p.relative_to(out / 'site').as_posix(): p.read_bytes()
                 for p in (out / 'site').rglob('*') if p.is_file()},
        'wikitree': wt,
        'gedcom': ged.split('0 @', 1)[1],
        'packet_exit': pk.exit_code,
        'packet': {p.relative_to(out / 'packet').as_posix(): p.read_bytes()
                   for p in (out / 'packet').rglob('*') if p.is_file()},
    }


class PrivacyInBothModesTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='fha-381p3p-'))
        ids = _private_example(cls.tmp / 'ids')
        cls.ids = _exports(ids, cls.tmp / 'out-ids')
        obs = _private_example(cls.tmp / 'obs')
        _set_mode(obs, 'obsidian')
        _quiet(normalize_links.run_normalize_links, obs, load_fha_yaml(obs), write=True)
        cls.obs_text = (obs / JAMES_PROFILE).read_text(encoding='utf-8')
        cls.obs = _exports(obs, cls.tmp / 'out-obs')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_obsidian_copy_really_links_by_file_name(self):
        self.assertIn(f'[[bradford__alex_james_p-0a1b2c3d4e|{LIVING_SON}]]', self.obs_text)

    def test_the_website_has_the_same_pages_and_withholds_the_same(self):
        self.assertEqual(self.ids['site_status'], 'ok')
        self.assertEqual(self.obs['site_status'], 'ok')
        self.assertEqual(sorted(self.obs['site']), sorted(self.ids['site']))
        label = site._LIVING_LABEL.encode()
        for page, body in self.ids['site'].items():
            other = self.obs['site'][page]
            self.assertEqual(other.count(label), body.count(label), page)
            self.assertEqual(LIVING_SON.encode() in other, LIVING_SON.encode() in body, page)
            self.assertEqual(b'Lodge' in other, b'Lodge' in body, page)

    def test_the_website_is_byte_for_byte_the_same(self):
        # A citation written [[source_file|s-…]] is the same footnote as [[s-…]].
        differ = [k for k in self.ids['site'] if self.obs['site'][k] != self.ids['site'][k]]
        self.assertEqual(differ, [])

    def test_wikitree_and_gedcom_are_the_same(self):
        self.assertEqual(self.obs['wikitree'], self.ids['wikitree'])
        self.assertEqual(self.obs['gedcom'], self.ids['gedcom'])

    def test_the_packet_holds_the_same_files_and_leaves_out_the_same(self):
        self.assertEqual(self.obs['packet_exit'], self.ids['packet_exit'])
        self.assertEqual(sorted(self.obs['packet']), sorted(self.ids['packet']))
        for name, body in self.ids['packet'].items():
            other = self.obs['packet'][name]
            for secret in (LIVING_SON.encode(), b'Alex', b'hartley-lodge-roster', b'Masonic Lodge',
                           b'bradford__alex_james'):
                self.assertEqual(other.count(secret), body.count(secret), (name, secret))


class PacketWithheldPeopleTests(unittest.TestCase):
    """A bare link to anyone the packet withholds (restricted, or living
    through a merge) never gains that person's name in an Obsidian-mode
    packet: the copy says what an `id`-mode packet says."""

    ADA = 'p-htyn3mpg9g'
    CALEB = 'p-075114a0f8'
    ADA_FILE = 'people/stubs/hartley__ada_jane_p-htyn3mpg9g.md'
    CALEB_FILE = 'people/stubs/hartley__caleb_comstock_p-075114a0f8.md'

    def _packet(self, tmp: Path, mode: str | None, setup) -> str:
        root = _copy_example(tmp / (mode or 'id'))
        profile = root / TOM_PROFILE
        text = profile.read_text(encoding='utf-8')
        profile.write_text(text.replace(
            '## Stories', f'Kin [[{self.ADA}]] and [[{self.CALEB}]].\n\n## Stories', 1),
            encoding='utf-8', newline='')
        setup(root)
        if mode:
            _set_mode(root, mode)
            _quiet(normalize_links.run_normalize_links, root, load_fha_yaml(root), write=True)
        _quiet(index_mod.build_index, root, load_fha_yaml(root))
        out = tmp / f'out-{mode or "id"}'
        res = _quiet(packet.run_packet, root, TOM, out, no_photos=True)
        self.assertEqual(res.data['status'], 'ok', res.data.get('messages'))
        return '\n'.join(p.read_text(encoding='utf-8') for p in out.rglob('*.md'))

    def _both(self, setup) -> tuple[str, str]:
        tmp = Path(tempfile.mkdtemp(prefix='fha-381p3pk-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        return self._packet(tmp, None, setup), self._packet(tmp, 'obsidian', setup)

    @staticmethod
    def _edit(root: Path, rel: str, old: str, new: str) -> None:
        path = root / rel
        text = path.read_text(encoding='utf-8')
        assert old in text, (rel, old)
        path.write_text(text.replace(old, new, 1), encoding='utf-8', newline='')

    def test_a_restricted_person_who_is_not_living(self):
        def setup(root):
            self._edit(root, self.ADA_FILE, 'living: false', 'living: false\nrestricted: true')
        ids, obs = self._both(setup)
        self.assertNotIn('Ada Jane', ids)
        self.assertNotIn('Ada Jane', obs)
        self.assertIn(f'[[{self.ADA}]]', obs)

    def test_a_person_living_only_through_a_merge(self):
        def setup(root):
            (root / 'people/stubs/MERGED-INTO-hartley__ada_p-mmmmmmmmm1.md').write_text(
                '---\nid: p-mmmmmmmmm1\nname: Ada Hartley\nstatus: merged\n'
                f'merged_into: {self.ADA}\nliving: true\ntier: stub\n---\n', encoding='utf-8')
        ids, obs = self._both(setup)
        self.assertNotIn('Ada Jane', ids)
        self.assertNotIn('Ada Jane', obs)

    def test_a_restricted_name_variant_stays_out_and_the_name_is_kept(self):
        def setup(root):
            self._edit(root, self.CALEB_FILE, 'name: Caleb Comstock Hartley',
                       'name: Caleb Comstock Hartley\nname_variants:\n'
                       '  - value: Secretname Hartley\n    restricted: true')
        ids, obs = self._both(setup)
        self.assertNotIn('Secretname', ids)
        self.assertNotIn('Secretname', obs)
        # Caleb is neither living nor restricted: the stem form's filled-in
        # name is an ordinary display and reads back with the link.
        self.assertIn(f'[[{self.CALEB}|Caleb Comstock Hartley]]', obs)


# ── Review round: tables, code blocks, merged records, people: shapes ────────

class TableLinkTests(unittest.TestCase):
    """Obsidian writes a link inside a table as `[[stem\\|Name]]`, since a bare
    `|` would end the cell. The tools write it that way and read it back."""

    def setUp(self):
        self.obs = LinkWriter(ROOT / 'example-archive', 'obsidian')

    def test_a_table_without_edge_pipes_is_a_table(self):
        text = f'Name | Link\n--- | ---\nTom | [[{TOM}]]\n\nAfter [[{TOM}]].\n'
        new, count, _bad = rewrite_record_links_to_stems(text, self.obs)
        self.assertEqual(count, 2)
        self.assertEqual(new, (f'Name | Link\n--- | ---\nTom | [[{TOM_STEM}{BS}|{TOM_NAME}]]\n\n'
                               f'After [[{TOM_STEM}|{TOM_NAME}]].\n'))

    def test_every_row_of_a_table_with_edge_pipes(self):
        text = (f'| Name | Link |\n|:---|---:|\n| Tom | [[{TOM}]] |\n'
                f'| Tom | [[{TOM}|Tom]] |\n| Tom | [[{TOM}{BS}|Tom]] |\n')
        new, count, _bad = rewrite_record_links_to_stems(text, self.obs)
        self.assertEqual(count, 3)
        self.assertEqual(new.splitlines()[2:], [
            f'| Tom | [[{TOM_STEM}{BS}|{TOM_NAME}]] |',
            f'| Tom | [[{TOM_STEM}{BS}|Tom]] |',
            f'| Tom | [[{TOM_STEM}{BS}|Tom]] |'])
        self.assertEqual(rewrite_record_links_to_stems(new, self.obs), (new, 0, []))

    def test_readers_take_the_escaped_pipe(self):
        for written in (f'[[{TOM_STEM}{BS}|Tom]]', f'[[{TOM}{BS}|Tom]]'):
            row = f'| Tom | {written} |'
            self.assertEqual([t[:2] for t in extract_tokens(row)], [(TOM, 'Tom')], written)
            self.assertEqual([t[:2] for t in extract_wikilinks(row)], [(TOM, 'Tom')], written)
        self.assertEqual(link_parts(TOM_STEM + BS, 'Tom'), (TOM, 'Tom'))
        self.assertEqual([t[:3] for t in extract_tokens(f'[[{CLAIM_STEM}#Claims{BS}|{CLAIM}]]')],
                         [(CLAIM, None, 'Claims')])
        self.assertEqual(links_to_id_form(f'| [[{TOM_STEM}{BS}|Tom]] |'), f'| [[{TOM}{BS}|Tom]] |')


class CodeBlockTests(unittest.TestCase):
    """A link inside code is an example, never rewritten."""

    def setUp(self):
        self.obs = LinkWriter(ROOT / 'example-archive', 'obsidian')

    def test_fences_close_only_on_their_own_kind_and_length(self):
        text = (f'~~~\nexample:\n```\ninside tilde fence [[{TOM}]]\n~~~\n\n'
                f'````\n```\ninside 4-tick fence [[{TOM}]]\n````\n\n'
                f'After [[{TOM}]].\n')
        new, count, _bad = rewrite_record_links_to_stems(text, self.obs)
        self.assertEqual(count, 1)
        self.assertEqual(new, text.replace(f'After [[{TOM}]]', f'After [[{TOM_STEM}|{TOM_NAME}]]'))

    def test_an_indented_code_block_is_code_but_a_nested_list_is_not(self):
        text = (f'Para.\n\n    indented code [[{TOM}]]\n\n'
                f'- a list\n\n    - nested item [[{TOM}]]\n')
        new, count, _bad = rewrite_record_links_to_stems(text, self.obs)
        self.assertEqual(count, 1)
        self.assertIn(f'    indented code [[{TOM}]]', new)
        self.assertIn(f'    - nested item [[{TOM_STEM}|{TOM_NAME}]]', new)

    def test_normalize_links_reads_code_blocks_the_same_way(self):
        tmp = Path(tempfile.mkdtemp(prefix='fha-381p3k-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = _copy_example(tmp / 'a')
        note = root / 'notes' / 'code.md'
        text = '~~~\n```\n[s-4f5f215e60]\n~~~\n\n    [s-4f5f215e60]\n\nSee [s-4f5f215e60].\n'
        note.write_text(text, encoding='utf-8')
        normalize_links.run_normalize_links(root, {}, write=True)
        self.assertEqual(note.read_text(encoding='utf-8'),
                         text.replace('See [s-4f5f215e60]', 'See [[s-4f5f215e60]]'))


class MergedRecordTests(unittest.TestCase):
    """In Obsidian mode a link to a merged record points at the record it was
    merged into, not at the tombstone (or nothing, for a source)."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix='fha-381p3m-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.root = _copy_example(tmp / 'a')
        _set_mode(self.root, 'obsidian')
        (self.root / 'people/stubs/MERGED-INTO-hartley__tom_dup_p-zzzzzzzzz2.md').write_text(
            '---\nid: p-zzzzzzzzz2\naliases: [p-zzzzzzzzz2]\nname: Tom Dup\nstatus: merged\n'
            f'merged_into: {TOM}\nliving: false\ntier: stub\n---\n\n# Tom Dup\n',
            encoding='utf-8')
        census = self.root / f'sources/census/{CENSUS_STEM}.md'
        text = census.read_text(encoding='utf-8')
        census.write_text(text.replace(f'aliases: [{CENSUS}', f'aliases: [{CENSUS}, s-zzzzzzzzz3', 1),
                          encoding='utf-8')
        self.assertIn('s-zzzzzzzzz3', census.read_text(encoding='utf-8'))
        self.note = self.root / 'notes' / 'merged.md'
        self.note.write_text('# Merged\n\nSee [[p-zzzzzzzzz2]], [[p-zzzzzzzzz2|Tommy]] and '
                             '[[s-zzzzzzzzz3]].\n', encoding='utf-8')

    def test_normalize_points_at_the_survivor(self):
        _quiet(normalize_links.run_normalize_links, self.root, load_fha_yaml(self.root),
               write=True)
        self.assertEqual(self.note.read_text(encoding='utf-8'), (
            f'# Merged\n\nSee [[{TOM_STEM}|{TOM_NAME}]], [[{TOM_STEM}|Tommy]] and '
            f'[[{CENSUS_STEM}|{CENSUS}]].\n'))
        self.assertEqual([f for f in _codes(self.root, 'W161')
                          if Path(f.path).name == 'merged.md'], [])

    def test_lint_names_a_link_to_a_merged_record(self):
        flagged = [f for f in _codes(self.root, 'W161') if Path(f.path).name == 'merged.md']
        self.assertEqual(len(flagged), 1)
        self.assertIn('3 link(s)', flagged[0].message)


class PeopleListShapesTests(unittest.TestCase):
    """normalize-links keeps every person in a `people:` list, whatever YAML
    shape it was written in, in both link modes."""

    def _run(self, mode):
        tmp = Path(tempfile.mkdtemp(prefix='fha-381p3l-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = _copy_example(tmp / 'a')
        _set_mode(root, mode)
        src = root / 'sources/other/zz-test_s-zzzzzzzzz1.md'
        src.write_text('---\nid: s-zzzzzzzzz1\ntitle: T\nsource_type: other\npeople:\n'
                       f'  - [[{TOM}]]\n  - {CALVIN}\n  - "[[p-c4b26bb4bc|Ethel]]"\n'
                       f'  - "[[{JAMES}]]"\n---\n\nNotes.\n', encoding='utf-8')
        flow = root / 'sources/other/zz-flow_s-zzzzzzzzz4.md'
        flow.write_text(f'---\nid: s-zzzzzzzzz4\ntitle: F\nsource_type: other\n'
                        f'people: [[[{TOM}]], "[[{CALVIN}]]"]\n---\n\nNotes.\n',
                        encoding='utf-8')
        _quiet(normalize_links.run_normalize_links, root, load_fha_yaml(root), write=True)
        again = _quiet(normalize_links.run_normalize_links, root, load_fha_yaml(root))
        self.assertEqual(again.data['files_changed'], 0, mode)
        _quiet(index_mod.build_index, root, load_fha_yaml(root))
        con = sqlite3.connect(root / '.cache' / 'index.sqlite')
        try:
            got = {sid: sorted(r[0] for r in con.execute(
                'SELECT person_id FROM source_people WHERE source_id = ?', (sid,)))
                for sid in ('s-zzzzzzzzz1', 's-zzzzzzzzz4')}
        finally:
            con.close()
        return src.read_text(encoding='utf-8'), flow.read_text(encoding='utf-8'), got

    def test_both_modes_keep_everyone(self):
        for mode, tom, cal in (
                ('id', f'"[[{TOM}|{TOM_NAME}]]"', f'"[[{CALVIN}|Calvin George Hartley]]"'),
                ('obsidian', f'"[[{TOM_STEM}|{TOM_NAME}]]"',
                 f'"[[{CALVIN_STEM}|Calvin George Hartley]]"')):
            src, flow, got = self._run(mode)
            self.assertIn(f'  - {tom}\n', src, mode)
            self.assertIn(f'  - {cal}\n', src, mode)
            self.assertIn(f'  - {tom}\n', flow, mode)
            self.assertEqual(got['s-zzzzzzzzz1'], sorted([TOM, CALVIN, 'p-c4b26bb4bc', JAMES]), mode)
            self.assertEqual(got['s-zzzzzzzzz4'], sorted([TOM, CALVIN]), mode)

    def test_an_unquoted_link_item_reads_as_the_link(self):
        self.assertEqual(link_field_refs([[[TOM]]]), [TOM])
        self.assertEqual(link_field_refs([[TOM]]), [TOM])


if __name__ == '__main__':
    unittest.main()
