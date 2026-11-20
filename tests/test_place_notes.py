"""
test_place_notes.py: with `links: obsidian`, every registered place gets a
generated note in places/, so a place link has a file Obsidian can follow
(SPEC §15).

An owner who works in Obsidian runs `fha views places` (or `fha views
refresh`) and finds `places/fairview_l-7c1a9f4e22.md`: the place's
hierarchy, coordinates, history and notes, with its names in `aliases:`.
A place link then reads `[[fairview_l-7c1a9f4e22|Fairview]]`. Covered here:

  * the note itself: frontmatter first, then the GENERATED header and the
    purpose block, and a body built only from places.yaml;
  * regeneration: a second run, or a run on a new day, rewrites nothing;
  * a rename in places.yaml: a new note, the old one gone, and links to the
    old file name re-pointed; a removed place's note is removed;
  * hand-written files in places/ are never overwritten or deleted;
  * `--dry-run` writes nothing; `fha views clean` removes the notes;
  * link writing and reading for places in both modes, and lint W161;
  * `id` mode: nothing written, and `fha views refresh` output unchanged;
  * the index reads the same whether the notes exist;
  * privacy: the website and a packet hold the same things with the notes
    present, and neither ships a place note.

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

import index as index_mod  # noqa: E402
import lint  # noqa: E402
import normalize_links  # noqa: E402
import packet  # noqa: E402
import views  # noqa: E402
from _lib import (  # noqa: E402
    LinkWriter,
    extract_tokens,
    is_generated_text,
    link_parts,
    links_to_id_form,
    load_fha_yaml,
    place_note_slug,
    place_note_stem,
    rewrite_record_links_to_stems,
    rewrite_stale_stem_links,
)

_spec = importlib.util.spec_from_file_location('fha_site_385', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site)

PLACE = 'l-7c1a9f4e22'
STEM = f'fairview_{PLACE}'
NOTE = f'places/{STEM}.md'
TOM = 'p-de957bcda1'
TOM_PROFILE = ('people/040 Thomas Edward Hartley [Calvin + Ethel + Frances]/'
               'hartley__thomas_edward_p-de957bcda1.md')
SUSAN = 'people/connections/cole__susan_p-s5a8g87at3.md'
CENSUS = 'sources/census/1880-census-fairview-hartley-household_s-4f5f215e60.md'
HOME = 'l-9e2210ab44'
BS = '\\'


def _quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def _loud(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = fn(*args, **kwargs)
    return result, out.getvalue(), err.getvalue()


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


def _index(root: Path) -> None:
    _quiet(index_mod.build_index, root, load_fha_yaml(root))


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file() and '.cache' not in p.parts}


def _rename_place(root: Path, old: str, new: str) -> None:
    path = root / 'places' / 'places.yaml'
    text = path.read_text(encoding='utf-8')
    assert f'  name: {old}\n' in text
    path.write_text(text.replace(f'  name: {old}\n', f'  name: {new}\n', 1), encoding='utf-8')


def _add_home(root: Path) -> None:
    """A micro-place inside Fairview, so a note has an `Inside:` line."""
    path = root / 'places' / 'places.yaml'
    path.write_text(path.read_text(encoding='utf-8') + (
        f'- id: {HOME}\n'
        '  name: Hartley family home, 214 N 5th St\n'
        f'  within: {PLACE}\n'
        '  coords: [39.5644, -95.1209]\n'), encoding='utf-8')


def _obsidian_archive(tmp: Path, name: str = 'a') -> Path:
    root = _copy_example(tmp / name)
    _set_mode(root, 'obsidian')
    _index(root)
    return root


def _codes(root: Path, code: str) -> list:
    findings, _registry = _quiet(lint._run_lint_core, root, load_fha_yaml(root))
    return [f for f in findings if f.code == code]


def _lint_summary(root: Path) -> list:
    findings, _registry = _quiet(lint._run_lint_core, root, load_fha_yaml(root))
    return sorted((f.severity, f.code, Path(f.path).name if f.path else '') for f in findings)


# ── The note ─────────────────────────────────────────────────────────────────

class PlaceNoteTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='fha-385n-'))
        self.root = _obsidian_archive(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_note_mirrors_the_registry(self):
        _add_home(self.root)
        _index(self.root)
        res = _quiet(views.run_places, self.root)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.data['count'], 2)
        text = (self.root / NOTE).read_text(encoding='utf-8')
        # Frontmatter first (Obsidian reads aliases: only there), then the
        # GENERATED header, then the title and the purpose block.
        self.assertTrue(text.startswith(
            f'---\naliases:\n  - {PLACE}\n  - "Fairview"\n  - "Fairview City"\n---\n'
            '<!-- GENERATED by fha views places on '))
        self.assertTrue(is_generated_text(text, '<!-- GENERATED by fha views',
                                          after_frontmatter=True))
        self.assertFalse(is_generated_text(text, '<!-- GENERATED by fha views'))
        self.assertIn('\n# Fairview\n\n> **Generated place note. Do not edit.**', text)
        for line in ('- **Place:** Fairview, Breton County, Kansas, USA',
                     '- **Coordinates:** 39.8, -95.6',
                     '- **Also called:** Fairview City',
                     '## History\n\n- 1858 to 1861: Fairview, Breton Co., Kansas Territory, USA',
                     '## Notes\n\n2026-06-12: A fictional town'):
            self.assertIn(line, text)
        # No people or sources list: Obsidian's backlinks show those.
        self.assertNotIn('p-', text)
        self.assertNotIn('s-', text.split('---', 2)[2].replace('fha views places', ''))
        home = (self.root / 'places' /
                f'hartley-family-home-214-n-5th-st_{HOME}.md').read_text(encoding='utf-8')
        # The containing place is linked by its own note's file name.
        self.assertIn(f'- **Inside:** [[{STEM}|Fairview]]', home)
        self.assertNotIn('## History', home)
        self.assertNotIn('## Notes', home)

    def test_the_name_is_a_plain_slug(self):
        self.assertEqual(place_note_slug('Kraków, St. Mary\'s'), 'krakow-st-mary-s')
        self.assertEqual(place_note_slug('  '), 'place')
        self.assertEqual(place_note_slug(None), 'place')
        self.assertEqual(place_note_stem('L-7C1A9F4E22', 'Fairview'), STEM)

    def test_a_second_run_and_a_new_day_rewrite_nothing(self):
        _quiet(views.run_places, self.root)
        note = self.root / NOTE
        # Yesterday's note: only the header date differs from today's.
        old = re.sub(r'on \d{4}-\d{2}-\d{2}', 'on 2026-01-02', note.read_text(encoding='utf-8'))
        note.write_bytes(old.encode('utf-8'))
        before = _tree(self.root)
        res = _quiet(views.run_places, self.root)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.data['count'], 0)
        self.assertEqual(res.data['unchanged'], 1)
        self.assertEqual(_tree(self.root), before)
        self.assertIn('on 2026-01-02', note.read_text(encoding='utf-8'))
        # A real change in places.yaml is written.
        path = self.root / 'places' / 'places.yaml'
        path.write_text(path.read_text(encoding='utf-8').replace(
            'coords: [39.8, -95.6]', 'coords: [39.81, -95.6]'), encoding='utf-8')
        _index(self.root)
        res = _quiet(views.run_places, self.root)
        self.assertEqual(res.data['count'], 1)
        self.assertIn('- **Coordinates:** 39.81, -95.6', note.read_text(encoding='utf-8'))

    def test_a_rename_writes_a_new_note_removes_the_old_and_repoints_links(self):
        self.assertEqual(_quiet(views.run_places, self.root).exit_code, 0)
        _quiet(normalize_links.run_normalize_links, self.root, load_fha_yaml(self.root),
               write=True)
        susan = (self.root / SUSAN).read_text(encoding='utf-8')
        self.assertIn(f'[[{STEM}|Fairview]]', susan)
        census = (self.root / CENSUS).read_text(encoding='utf-8')
        self.assertIn(f'"[[{STEM}|Fairview]]"', census)

        _rename_place(self.root, 'Fairview', 'Fair View')
        _index(self.root)
        res, out, _err = _loud(views.run_places, self.root)
        self.assertEqual(res.exit_code, 0)
        new_stem = f'fair-view_{PLACE}'
        self.assertFalse((self.root / NOTE).exists())
        self.assertTrue((self.root / 'places' / f'{new_stem}.md').exists())
        self.assertIn('the place is now called Fair View', out)
        self.assertIn('Run `fha index`', out)
        # The links name the new file and keep the display a person wrote.
        self.assertEqual((self.root / SUSAN).read_text(encoding='utf-8'),
                         susan.replace(f'[[{STEM}|', f'[[{new_stem}|'))
        self.assertIn(f'"[[{new_stem}|Fairview]]"', (self.root / CENSUS).read_text(encoding='utf-8'))
        _index(self.root)
        self.assertEqual(_codes(self.root, 'W161'), [])
        # Nothing left to do.
        before = _tree(self.root)
        res = _quiet(views.run_places, self.root)
        self.assertEqual((res.data['count'], res.data['removed'], res.data['links']), (0, 0, 0))
        self.assertEqual(_tree(self.root), before)

    def test_a_removed_place_loses_its_note(self):
        _quiet(views.run_places, self.root)
        (self.root / 'places' / 'places.yaml').write_text('[]\n', encoding='utf-8')
        _index(self.root)
        res, out, _err = _loud(views.run_places, self.root)
        self.assertEqual(res.exit_code, 0)
        self.assertFalse((self.root / NOTE).exists())
        self.assertIn('no longer in places.yaml', out)

    def test_hand_written_files_are_never_overwritten_or_deleted(self):
        mine = self.root / NOTE
        mine.write_text('# My Fairview notes\n', encoding='utf-8')
        stray = self.root / 'places' / f'old-name_{PLACE}.md'
        stray.write_text('---\naliases: [Fairview]\n---\nMine too.\n', encoding='utf-8')
        res, _out, err = _loud(views.run_places, self.root)
        self.assertEqual(res.exit_code, 1)
        self.assertEqual(res.data['skipped'], 2)
        self.assertEqual(mine.read_text(encoding='utf-8'), '# My Fairview notes\n')
        self.assertTrue(stray.exists())
        self.assertIn('no GENERATED header', err)
        self.assertIn('fha did not write it', err)
        _quiet(views.run_clean, self.root)
        self.assertTrue(mine.exists())
        self.assertTrue(stray.exists())

    def test_dry_run_writes_nothing_and_says_what_it_would_do(self):
        _quiet(views.run_places, self.root)
        _rename_place(self.root, 'Fairview', 'Fair View')
        susan = self.root / SUSAN
        susan.write_text(susan.read_text(encoding='utf-8') + f'\nSee [[{STEM}|Fairview]].\n',
                         encoding='utf-8', newline='')
        _index(self.root)
        before = _tree(self.root)
        res, out, _err = _loud(views.run_places, self.root, dry_run=True)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(_tree(self.root), before)
        self.assertEqual(res.changed, [])
        self.assertIn('would remove places', out)
        self.assertIn('would write place note -> places', out)
        self.assertIn('would re-point 1 place link(s)', out)
        live = _quiet(views.run_places, self.root)
        self.assertEqual((live.data['count'], live.data['removed'], live.data['links']),
                         (res.data['count'], res.data['removed'], res.data['links']))

    def test_views_clean_removes_place_notes_and_keeps_the_registry(self):
        _quiet(views.run_places, self.root)
        preview = _quiet(views.run_clean, self.root, dry_run=True)
        self.assertTrue((self.root / NOTE).exists())
        self.assertGreaterEqual(preview.data['would_remove'], 1)
        res = _quiet(views.run_clean, self.root)
        self.assertEqual(res.exit_code, 0)
        self.assertFalse((self.root / NOTE).exists())
        self.assertTrue((self.root / 'places' / 'places.yaml').exists())
        self.assertIn(str(self.root / NOTE), res.changed)

    def test_refresh_writes_the_notes_first_so_views_link_them(self):
        res = _quiet(views.run_refresh, self.root)
        self.assertEqual(res.exit_code, 0)
        self.assertTrue((self.root / NOTE).exists())
        timeline = next((self.root / 'people').rglob('*view_timeline_p-2b3c4d5e6f.md'))
        text = timeline.read_text(encoding='utf-8')
        self.assertIn(f'[[{STEM}|Fairview, Breton County, Kansas]]', text)
        self.assertNotIn(f'[[{PLACE}|', text)


# ── id mode ──────────────────────────────────────────────────────────────────

class IdModeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='fha-385i-'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_views_places_writes_nothing(self):
        root = _copy_example(self.tmp / 'places-id')
        _index(root)
        before = _tree(root)
        res, out, _err = _loud(views.run_places, root)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(_tree(root), before)
        self.assertIn('only when fha.yaml says `links: obsidian`', out)

    def test_refresh_output_is_the_same_as_with_no_setting(self):
        outputs = {}
        for mode in ('id', 'absent'):
            root = _copy_example(self.tmp / f'refresh-{mode}')
            _set_mode(root, None if mode == 'absent' else mode)
            _index(root)
            _quiet(views.run_refresh, root)
            outputs[mode] = {k: re.sub(rb'on \d{4}-\d{2}-\d{2}', b'', v)
                             for k, v in _tree(root).items() if k != 'fha.yaml'}
        self.assertEqual(outputs['id'], outputs['absent'])
        self.assertEqual([k for k in outputs['id'] if k.startswith('places/')],
                         ['places/places.yaml'])

    def test_id_mode_place_links_stay_ids_even_with_a_note(self):
        root = _obsidian_archive(self.tmp, 'id-with-note')
        _quiet(views.run_places, root)
        ids = LinkWriter(root, 'id')
        text = f'[[{PLACE}|Fairview]]'
        self.assertIs(ids.convert(text), text)


# ── Writing and reading place links ─────────────────────────────────────────

class PlaceLinkTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='fha-385l-'))
        cls.root = _obsidian_archive(cls.tmp)
        _quiet(views.run_places, cls.root)
        cls.bare = _copy_example(cls.tmp / 'no-notes')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_place_link_names_the_note_once_it_exists(self):
        obs = LinkWriter(self.root, 'obsidian')
        self.assertEqual(obs.convert(f'[[{PLACE}|the town]]'), f'[[{STEM}|the town]]')
        self.assertEqual(obs.convert(f'[[{PLACE}]]'), f'[[{STEM}|Fairview]]')
        self.assertEqual(obs.link(PLACE, 'Fairview'), f'[[{STEM}|Fairview]]')
        self.assertEqual(obs.convert(f'[[old-name_{PLACE}|Fairview]]'), f'[[{STEM}|Fairview]]')
        head = '| Year | Place |\n| --- | --- |\n'
        self.assertEqual(obs.convert(f'{head}| 1880 | [[{PLACE}|Fairview]] |\n'),
                         f'{head}| 1880 | [[{STEM}{BS}|Fairview]] |\n')
        current = f'[[{STEM}|Fairview]]'
        self.assertEqual(obs.convert_counted(current), (current, 0, []))

    def test_without_a_note_the_place_link_keeps_its_id(self):
        obs = LinkWriter(self.bare, 'obsidian')
        text = f'[[{PLACE}|Fairview]] [[{PLACE}]]'
        self.assertEqual(obs.convert_counted(text), (text, 0, []))

    def test_frontmatter_places_list_takes_the_note_name(self):
        obs = LinkWriter(self.root, 'obsidian')
        text = f'---\nid: s-4f5f215e60\nplaces:\n  - "[[{PLACE}|Fairview]]"\n---\nBody.\n'
        new, count, _bad = rewrite_record_links_to_stems(text, obs)
        self.assertEqual(count, 1)
        self.assertIn(f'  - "[[{STEM}|Fairview]]"', new)

    def test_readers_resolve_the_note_name_to_the_place(self):
        self.assertEqual(link_parts(STEM, 'Fairview'), (PLACE, 'Fairview'))
        self.assertEqual([t[0] for t in extract_tokens(f'In [[{STEM}|Fairview]].')], [PLACE])
        self.assertEqual(links_to_id_form(f'[[{STEM}|Fairview]] [[{STEM}{BS}|Fairview]]'),
                         f'[[{PLACE}|Fairview]] [[{PLACE}{BS}|Fairview]]')

    def test_rewrite_stale_stem_links_follows_a_rename_only(self):
        stems = {PLACE: f'fair-view_{PLACE}'}
        text = (f'---\naliases: [{STEM}]\nplaces:\n  - "[[{STEM}|Fairview]]"\n---\n'
                f'In [[{STEM}|Fairview]] and [[{STEM}#History]] and `[[{STEM}]]`.\n'
                f'ID form [[{PLACE}|Fairview]] stays.\n'
                '```yaml\n'
                f'- place: "[[{STEM}]]"\n'
                '```\n'
                '| a | b |\n| - | - |\n'
                f'| x | [[{STEM}{BS}|Fairview]] |\n')
        new, count = rewrite_stale_stem_links(text, stems)
        self.assertEqual(count, 4)
        self.assertEqual(new, text
                         .replace(f'  - "[[{STEM}|', f'  - "[[fair-view_{PLACE}|')
                         .replace(f'In [[{STEM}|', f'In [[fair-view_{PLACE}|')
                         .replace(f'[[{STEM}#History]]', f'[[fair-view_{PLACE}#History]]')
                         .replace(f'[[{STEM}{BS}|', f'[[fair-view_{PLACE}{BS}|'))
        self.assertEqual(rewrite_stale_stem_links(new, stems), (new, 0))

    def test_lint_w161_names_an_id_form_place_link_once_the_note_exists(self):
        tmp = Path(tempfile.mkdtemp(prefix='fha-385w-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = _obsidian_archive(tmp)
        _quiet(normalize_links.run_normalize_links, root, load_fha_yaml(root), write=True)
        # No notes yet: the place links keep their ID form, and that is fine.
        self.assertEqual(_codes(root, 'W161'), [])
        self.assertIn(f'[[{PLACE}|Fairview]]', (root / SUSAN).read_text(encoding='utf-8'))
        _index(root)
        self.assertEqual(_quiet(views.run_places, root).exit_code, 0)
        flagged = {Path(f.path).name for f in _codes(root, 'W161')}
        self.assertIn(Path(SUSAN).name, flagged)
        self.assertIn(Path(CENSUS).name, flagged)
        _quiet(normalize_links.run_normalize_links, root, load_fha_yaml(root), write=True)
        self.assertEqual(_codes(root, 'W161'), [])
        self.assertIn(f'[[{STEM}|Fairview]]', (root / SUSAN).read_text(encoding='utf-8'))
        # Nothing else in lint notices the notes: the findings are the
        # example archive's usual ones.
        baseline = _copy_example(tmp / 'baseline')
        self.assertEqual(_lint_summary(root), _lint_summary(baseline))

    def test_a_note_that_could_not_be_written_draws_no_links(self):
        tmp = Path(tempfile.mkdtemp(prefix='fha-385f-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = _obsidian_archive(tmp)
        susan = root / SUSAN
        text = susan.read_text(encoding='utf-8')
        susan.write_text(text + f'\nSee [[old-name_{PLACE}|Fairview]].\n',
                         encoding='utf-8', newline='')
        _index(root)
        mine = root / NOTE
        mine.write_text('# My own Fairview page\n', encoding='utf-8')
        res = _quiet(views.run_places, root)
        self.assertEqual(res.exit_code, 1)
        self.assertEqual(res.data['links'], 0)
        self.assertIn(f'[[old-name_{PLACE}|Fairview]]', susan.read_text(encoding='utf-8'))


# ── The index ────────────────────────────────────────────────────────────────

class IndexTests(unittest.TestCase):

    def test_the_index_reads_the_same_with_place_notes_and_stem_links(self):
        tmp = Path(tempfile.mkdtemp(prefix='fha-385x-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        plain = _copy_example(tmp / 'plain')
        _index(plain)
        obs = _obsidian_archive(tmp, 'obs')
        _quiet(views.run_places, obs)
        _quiet(normalize_links.run_normalize_links, obs, load_fha_yaml(obs), write=True)
        _index(obs)

        def rows(root, sql):
            conn = sqlite3.connect(str(root / '.cache' / 'index.sqlite'))
            try:
                return sorted(conn.execute(sql).fetchall())
            finally:
                conn.close()

        for sql in ('SELECT source_id, place_id FROM source_places',
                    'SELECT token, kind, path FROM citations WHERE kind = \'L\'',
                    'SELECT id, name FROM places'):
            self.assertEqual(rows(obs, sql), rows(plain, sql), sql)
        self.assertEqual(rows(obs, "SELECT path FROM citations WHERE path LIKE 'places%'"), [])


# ── Privacy: the notes ship nowhere and change nothing withheld ──────────────

class PrivacyTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='fha-385p-'))
        cls.out = {}
        for mode in ('id', 'obsidian'):
            root = _copy_example(cls.tmp / mode)
            _set_mode(root, mode)
            _index(root)
            if mode == 'obsidian':
                _quiet(views.run_places, root)
                _quiet(normalize_links.run_normalize_links, root, load_fha_yaml(root),
                       write=True)
                cls.obs_root = root
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                index_mod.build_index(root, load_fha_yaml(root))
                views.run_refresh(root)
                index_mod.build_index(root, load_fha_yaml(root))
                site_res = site.run_site(root, cls.tmp / f'site-{mode}', linked=False)
                pk = packet.run_packet(root, TOM, cls.tmp / f'packet-{mode}', no_photos=True)
            cls.out[mode] = {
                'site_status': site_res['status'],
                'site': _tree(cls.tmp / f'site-{mode}'),
                'packet_exit': pk.exit_code,
                'packet': _tree(cls.tmp / f'packet-{mode}'),
            }

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_archive_really_links_places_by_note(self):
        self.assertTrue((self.obs_root / NOTE).exists())
        self.assertIn(f'[[{STEM}|Fairview]]',
                      (self.obs_root / TOM_PROFILE).read_text(encoding='utf-8'))

    def test_the_website_is_the_same(self):
        ids, obs = self.out['id'], self.out['obsidian']
        self.assertEqual(obs['site_status'], 'ok')
        self.assertEqual(sorted(obs['site']), sorted(ids['site']))
        differ = [k for k in ids['site'] if obs['site'][k] != ids['site'][k]]
        self.assertEqual(differ, [])

    def test_the_packet_is_the_same_and_holds_no_place_note(self):
        ids, obs = self.out['id'], self.out['obsidian']
        self.assertEqual(obs['packet_exit'], ids['packet_exit'])
        self.assertEqual(sorted(obs['packet']), sorted(ids['packet']))
        for name, body in obs['packet'].items():
            self.assertNotIn(STEM.encode(), body, name)
            self.assertNotIn(b'Generated place note', body, name)
            self.assertFalse(name.endswith(f'{STEM}.md'), name)


if __name__ == '__main__':
    unittest.main()
