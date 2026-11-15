"""
test_person_names.py - how a new person is named and minted.

Two issues, one surface (the minting verbs `fha stubs --from-names` and
`fha person new`, plus the workbench mint button that calls `person.run_new`):

  #279 - an accented letter in a name was DELETED from the filename slug
         instead of folded to its base letter ("Wiśniewska" filed as
         `winiewska`), and `fha index` read that slug back as the surname, so
         `fha gedcom` exported the misspelling. The fold now lives once in
         `_lib.fold_accents` (lifted from xref's town-name fold) and every
         person-filename slugger shares it.
  #282 - minting a name the archive already had made a second record without
         a word. Both verbs now refuse (exit 3, nothing written) and name the
         existing record, reading the person files on disk (a stub
         minted since the last `fha index` is only on disk), with `--force`
         for two people who really share a name.

Expected values come from the issues' own regression lists, not from the
implementation's output.
"""

import contextlib
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom
import gedcom_import
import index
import lint
import person
import serve
import stubs
import xref
from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    find_people_by_name,
    fold_accents,
    person_name_key,
    stub_filename,
)


# The issue #279 table: name -> the `{surname}__{given}` slots it must file as.
_FOLDED = {
    'Halina Wiśniewska': ('wisniewska', 'halina'),
    'Anna Gołębiowska': ('golebiowska', 'anna'),
    'Jan Ślęzak': ('slezak', 'jan'),
    'Józef Żółć': ('zolc', 'jozef'),
    'Hans Müller': ('muller', 'hans'),
}


def _new_archive() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'sources').mkdir()
    (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
    return tmp, root


def _stub_files(root: Path) -> list[str]:
    return sorted(p.name for p in (root / 'people' / 'stubs').glob('*.md'))


def _mint_from_names(root: Path, names: list[str], **kw) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = stubs.mint_named_stubs(root, names, **kw)
    return code, out.getvalue(), err.getvalue()


def _reindex(root: Path) -> None:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        index.build_index(root, {})
    # The index's freshness test is mtime-based: make sure a later mint is
    # strictly newer than the database it is compared with.
    time.sleep(0.02)


# ── #279: accented letters fold, they are not dropped ────────────────────────

class AccentFoldTests(unittest.TestCase):

    def test_fold_accents_examples(self) -> None:
        self.assertEqual(fold_accents('Wiśniewska'), 'Wisniewska')
        self.assertEqual(fold_accents('Gołębiowska'), 'Golebiowska')
        self.assertEqual(fold_accents('Straße Ærø Đorđe'), 'Strasse AEro Dorde')
        # Letters with no base form pass through for the caller's filter.
        self.assertEqual(fold_accents('Иван'), 'Иван')

    def test_stub_filename_folds_each_issue_name(self) -> None:
        for name, (surname, given) in _FOLDED.items():
            with self.subTest(name=name):
                self.assertEqual(stub_filename(name, 'p-0000000001'),
                                 f'{surname}__{given}_p-0000000001.md')

    def test_surname_override_folds_too(self) -> None:
        self.assertEqual(
            stub_filename('Halina Wiśniewska', 'p-0000000001', surname='Wiśniewska'),
            'wisniewska__halina_p-0000000001.md')

    def test_lint_fix_ids_slots_match(self) -> None:
        for name, slots in _FOLDED.items():
            with self.subTest(name=name):
                self.assertEqual(lint._person_filename_parts(name, 'fallback'), slots)

    def test_gedcom_import_slots_match(self) -> None:
        for name, (surname, given) in _FOLDED.items():
            with self.subTest(name=name):
                parts = name.split()
                self.assertEqual(gedcom_import._name_slot(parts[-1]), surname)
                self.assertEqual(gedcom_import._name_slot(' '.join(parts[:-1])), given)

    def test_xref_town_key_unchanged(self) -> None:
        # xref now calls the shared fold. Town keys built from the letters
        # its own table already folded (and from NFKD) must not move; the
        # letters added since (ð, þ, ı, œ, ħ, ŧ, ŋ) only extend it.
        self.assertEqual(xref._town_key('Suwałki, Poland'), 'suwalki')
        self.assertEqual(xref._town_key('München'), 'munchen')
        self.assertEqual(xref._town_key('Muenchen'), 'munchen')

    def test_stubs_from_names_writes_folded_file(self) -> None:
        tmp, root = _new_archive()
        self.addCleanup(tmp.cleanup)
        code, _out, _err = _mint_from_names(root, ['Halina Wiśniewska'])
        self.assertEqual(code, EXIT_CLEAN)
        [filename] = _stub_files(root)
        self.assertTrue(filename.startswith('wisniewska__halina_p-'), filename)

    def test_indexed_stub_exports_its_own_spelling(self) -> None:
        tmp, root = _new_archive()
        self.addCleanup(tmp.cleanup)
        _mint_from_names(root, ['Halina Wiśniewska'])
        _reindex(root)
        text = gedcom.run_gedcom(root, None, all_persons=True, include_living=True)['text']
        self.assertIn('1 NAME Halina /Wiśniewska/', text)

    def test_record_filed_with_the_old_broken_slug_exports_its_own_spelling(self) -> None:
        # A record minted before the fold is still filed `winiewska__halina_…`
        # and indexed with the surname "Winiewska"; the export must still
        # carry the name's own spelling, not the broken slug.
        self.assertEqual(gedcom._gedcom_name('Halina Wiśniewska', 'Winiewska'),
                         ('Halina /Wiśniewska/', None))
        self.assertEqual(gedcom._gedcom_name('Halina Wiśniewska', 'Wisniewska'),
                         ('Halina /Wiśniewska/', None))
        # A surname override the name does not end with still lands whole.
        self.assertEqual(gedcom._gedcom_name('Halina Wiśniewska', 'Kowalska'),
                         ('Halina Wiśniewska /Kowalska/', None))


# ── #282: a name that already has a record is not minted again ───────────────

class DuplicateNameTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp, self.root = _new_archive()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_name_key_ignores_case_accents_and_spacing(self) -> None:
        self.assertEqual(person_name_key('Ada  QUILLFEATHER.'), person_name_key('ada quillfeather'))
        self.assertEqual(person_name_key('Halina Wiśniewska'), person_name_key('halina wisniewska'))
        self.assertNotEqual(person_name_key('Quillfeather Ada'), person_name_key('Ada Quillfeather'))

    def _assert_refused_once(self, code: int, err: str, first: str) -> None:
        self.assertEqual(code, EXIT_FAILURE)
        pid = 'P-' + first[:-len('.md')].rsplit('_p-', 1)[1]
        self.assertIn(
            f'Ada Quillfeather already has a record: {pid} (people/stubs/{first}).', err)
        # `--force` is the human's call, not the agent's next step.
        self.assertIn('If you have confirmed this is a different person', err)
        self.assertIn('`--force`', err)
        self.assertEqual(_stub_files(self.root), [first])

    def test_from_names_twice_without_reindex_is_refused(self) -> None:
        _mint_from_names(self.root, ['Ada Quillfeather'])
        [first] = _stub_files(self.root)
        code, _out, err = _mint_from_names(self.root, ['Ada Quillfeather'])
        self._assert_refused_once(code, err, first)

    def test_from_names_twice_with_fresh_index_is_refused(self) -> None:
        _mint_from_names(self.root, ['Ada Quillfeather'])
        [first] = _stub_files(self.root)
        _reindex(self.root)
        code, _out, err = _mint_from_names(self.root, ['ada quillfeather'])
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn(f'(people/stubs/{first})', err)
        self.assertEqual(_stub_files(self.root), [first])

    def test_alternate_name_is_matched(self) -> None:
        # An alternate name (name_variants) counts too, index or not.
        path = self.root / 'people' / 'stubs' / 'quillfeather__ada_p-0000000001.md'
        path.write_text('---\nid: p-0000000001\nname: Ada Quillfeather\n'
                        'name_variants: [Addie Quillfeather]\nliving: unknown\n'
                        'tier: stub\n---\n', encoding='utf-8')
        _reindex(self.root)
        found = find_people_by_name(self.root, ['Addie Quillfeather'])
        self.assertEqual([m['id'] for m in found['Addie Quillfeather']], ['P-0000000001'])

    def test_dry_run_shows_the_same_refusal(self) -> None:
        _mint_from_names(self.root, ['Ada Quillfeather'])
        [first] = _stub_files(self.root)
        code, _out, err = _mint_from_names(self.root, ['Ada Quillfeather'], dry_run=True)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn('[dry-run] REFUSED: Ada Quillfeather already has a record', err)
        self.assertEqual(_stub_files(self.root), [first])

    def test_same_name_twice_in_one_batch_is_refused(self) -> None:
        code, _out, err = _mint_from_names(self.root, ['Ada Quillfeather', 'ADA Quillfeather'])
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn('is in this list twice', err)
        self.assertEqual(_stub_files(self.root), [])

    def test_refusal_writes_nothing_for_the_other_names_either(self) -> None:
        _mint_from_names(self.root, ['Ada Quillfeather'])
        code, _out, _err = _mint_from_names(self.root, ['Bea Newname', 'Ada Quillfeather'])
        self.assertEqual(code, EXIT_FAILURE)
        self.assertEqual(len(_stub_files(self.root)), 1)

    def test_from_names_force_creates_the_second_record(self) -> None:
        _mint_from_names(self.root, ['Ada Quillfeather'])
        code, _out, _err = _mint_from_names(self.root, ['Ada Quillfeather'], force=True)
        self.assertEqual(code, EXIT_CLEAN)
        self.assertEqual(len(_stub_files(self.root)), 2)

    def test_accented_spelling_matches_the_plain_one(self) -> None:
        _mint_from_names(self.root, ['Halina Wiśniewska'])
        code, _out, err = _mint_from_names(self.root, ['Halina Wisniewska'])
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn('Halina Wisniewska already has a record', err)

    def _hand_named_profile(self) -> Path:
        # A hand-authored profile (SPEC §10's pre-rename state): a valid id
        # in the frontmatter, but a filename with no `__` and no P-id. The
        # indexer identifies it from its frontmatter, so this check must too.
        path = self.root / 'people' / 'stubs' / 'Mary Smith.md'
        path.write_text('---\nid: p-0000000011\nname: Mary Smith\nliving: unknown\n---\n',
                        encoding='utf-8')
        return path

    def test_hand_named_profile_counts_without_an_index(self) -> None:
        self._hand_named_profile()
        result = person.run_new(self.root, 'Mary Smith')
        self.assertEqual(result.data['status'], 'duplicate-name')
        self.assertEqual([m['id'] for m in result.data['duplicates']], ['P-0000000011'])
        self.assertEqual(len(_stub_files(self.root)), 1)

    def test_hand_named_profile_counts_with_an_index(self) -> None:
        self._hand_named_profile()
        _reindex(self.root)
        result = person.run_new(self.root, 'Mary Smith')
        self.assertEqual(result.data['status'], 'duplicate-name')
        self.assertEqual([m['path'] for m in result.data['duplicates']],
                         ['people/stubs/Mary Smith.md'])

    def test_name_edited_then_source_upsert_still_reads_the_new_name(self) -> None:
        # Edit a profile after `fha index`, then let an incremental source
        # upsert bump the database's mtime without refreshing `persons`: the
        # profile now looks older than the index, but its indexed row is
        # stale. The check must still see the new name.
        path = self.root / 'people' / 'stubs' / 'smith__ada_p-0000000021.md'
        path.write_text('---\nid: p-0000000021\nname: Ada Smith\nliving: unknown\n'
                        'tier: stub\n---\n', encoding='utf-8')
        source = self.root / 'sources' / 'note_S-0000000031.md'
        source.write_text('---\nid: S-0000000031\ntitle: Note\nsource_type: letter\n---\n',
                          encoding='utf-8')
        _reindex(self.root)
        path.write_text('---\nid: p-0000000021\nname: Ada Jones\nliving: unknown\n'
                        'tier: stub\n---\n', encoding='utf-8')
        time.sleep(0.02)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(index.upsert_source(self.root, {}, 'S-0000000031'), 'indexed')
        self.assertGreater((self.root / '.cache' / 'index.sqlite').stat().st_mtime,
                           path.stat().st_mtime)
        found = find_people_by_name(self.root, ['Ada Jones', 'Ada Smith'])
        self.assertEqual([m['id'] for m in found.get('Ada Jones', [])], ['P-0000000021'])
        self.assertNotIn('Ada Smith', found)

    def test_merged_tombstone_does_not_count(self) -> None:
        path = self.root / 'people' / 'stubs' / 'quillfeather__ada_p-0000000001.md'
        path.write_text('---\nid: p-0000000001\nname: Ada Quillfeather\nstatus: merged\n'
                        'merged_into: p-0000000002\n---\n', encoding='utf-8')
        self.assertEqual(find_people_by_name(self.root, ['Ada Quillfeather']), {})

    def test_placeholder_names_never_match(self) -> None:
        _mint_from_names(self.root, ['unknown'])
        self.assertEqual(find_people_by_name(self.root, ['unknown']), {})

    def test_person_new_after_from_names_is_refused(self) -> None:
        _mint_from_names(self.root, ['Ada Quillfeather'])
        [first] = _stub_files(self.root)
        result = person.run_new(self.root, 'Ada Quillfeather')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'duplicate-name')
        self.assertEqual([m['path'] for m in result.data['duplicates']],
                         [f'people/stubs/{first}'])
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('Ada Quillfeather already has a record', text)
        self.assertIn('`--force`', text)
        self.assertEqual(_stub_files(self.root), [first])

    def test_person_new_dry_run_refuses_identically(self) -> None:
        person.run_new(self.root, 'Ada Quillfeather')
        result = person.run_new(self.root, 'Ada Quillfeather', dry_run=True)
        self.assertEqual(result.data['status'], 'duplicate-name')
        self.assertEqual(len(_stub_files(self.root)), 1)

    def test_person_new_force_creates_the_second_record(self) -> None:
        person.run_new(self.root, 'Ada Quillfeather')
        result = person.run_new(self.root, 'Ada Quillfeather', force=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(len(_stub_files(self.root)), 2)

    def test_person_new_cli_force_flag(self) -> None:
        person.run_new(self.root, 'Ada Quillfeather')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            refused = person._standalone_main(['new', 'Ada Quillfeather', '--root', str(self.root)])
            forced = person._standalone_main(
                ['new', 'Ada Quillfeather', '--force', '--root', str(self.root)])
        self.assertEqual((refused, forced), (EXIT_FAILURE, EXIT_CLEAN))
        self.assertEqual(len(_stub_files(self.root)), 2)

    def test_stubs_cli_force_flag(self) -> None:
        _mint_from_names(self.root, ['Ada Quillfeather'])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            refused = stubs._standalone_main(
                ['--from-names', 'Ada Quillfeather', '--root', str(self.root)])
            forced = stubs._standalone_main(
                ['--from-names', 'Ada Quillfeather', '--force', '--root', str(self.root)])
        self.assertEqual((refused, forced), (EXIT_FAILURE, EXIT_CLEAN))
        self.assertEqual(len(_stub_files(self.root)), 2)

    def test_workbench_verb_passes_force_and_echoes_it(self) -> None:
        person.run_new(self.root, 'Ada Quillfeather')
        state = type('State', (), {'archive_root': self.root})()
        refused = serve._verb_person_new(state, {'name': 'Ada Quillfeather'}, True)
        self.assertEqual(refused.data['status'], 'duplicate-name')
        forced = serve._verb_person_new(state, {'name': 'Ada Quillfeather', 'force': True}, False)
        self.assertTrue(forced.ok)
        self.assertEqual(len(_stub_files(self.root)), 2)
        self.assertTrue(serve._echo_person_new(
            {'name': 'Ada Quillfeather', 'force': True}).endswith('--force'))
        self.assertEqual(serve.VERBS['person.new']['schema'].get('force'), 'bool')
        self.assertEqual(serve.VERBS['person.add_family']['schema'].get('force'), 'bool')

    def test_workbench_add_family_mint_asks_too(self) -> None:
        subject = person.run_new(self.root, 'Subject Person').data['person_id']
        person.run_new(self.root, 'Ada Quillfeather')
        state = type('State', (), {'archive_root': self.root})()
        kw = {'person_id': subject, 'relation_type': 'sibling', 'name': 'Ada Quillfeather'}
        refused = serve._verb_add_family(state, dict(kw), True)
        self.assertEqual(refused.data['status'], 'duplicate-name')
        self.assertEqual(len(_stub_files(self.root)), 2)
        forced = serve._verb_add_family(state, dict(kw, force=True), False)
        self.assertTrue(forced.ok, [m.text for m in forced.messages])
        self.assertEqual(len(_stub_files(self.root)), 3)

    def test_claim_reference_mint_with_explicit_id_is_checked(self) -> None:
        # The workbench's mint '+' beside a claim-named P-id passes that id;
        # a same-name record already on file is still surfaced first.
        person.run_new(self.root, 'Ada Quillfeather')
        result = person.run_new(self.root, 'Ada Quillfeather', person_id='P-0000000009')
        self.assertEqual(result.data['status'], 'duplicate-name')
        forced = person.run_new(self.root, 'Ada Quillfeather', person_id='P-0000000009', force=True)
        self.assertTrue(forced.ok)
        self.assertTrue(any(n.endswith('_p-0000000009.md') for n in _stub_files(self.root)))
        self.assertEqual(serve.VERBS['person.add_family']['schema'].get('force'), 'bool')


# ── Review follow-ups (PR #285) ───────────────────────────────────────────────

class MoreLettersFoldTests(unittest.TestCase):
    """Letters NFKD leaves whole beyond xref's original table were still
    dropped (P2-1): "Guðrún Þórðardóttir" filed as `orardottir__gurun`."""

    def test_icelandic_name_files_and_exports_its_own_spelling(self) -> None:
        self.assertEqual(stub_filename('Guðrún Þórðardóttir', 'p-0000000001'),
                         'thordardottir__gudrun_p-0000000001.md')
        tmp, root = _new_archive()
        self.addCleanup(tmp.cleanup)
        _mint_from_names(root, ['Guðrún Þórðardóttir'])
        _reindex(root)
        text = gedcom.run_gedcom(root, None, all_persons=True, include_living=True)['text']
        self.assertIn('1 NAME Guðrún /Þórðardóttir/', text)

    def test_old_lossy_slug_still_exports_its_own_spelling(self) -> None:
        self.assertEqual(gedcom._gedcom_name('Guðrún Þórðardóttir', 'Orardottir'),
                         ('Guðrún /Þórðardóttir/', None))

    def test_turkish_and_french_letters(self) -> None:
        self.assertEqual(stub_filename('Ayşe Yıldız', 'p-0000000001'),
                         'yildiz__ayse_p-0000000001.md')
        self.assertEqual(stub_filename('Œdipe', 'p-0000000001'), '__oedipe_p-0000000001.md')
        self.assertEqual(fold_accents('ħŧŋĦŦŊ'), 'htnHTN')

    def test_fix_ids_fallback_slug_folds(self) -> None:
        # No usable name: lint falls back to the hand filename, folded too.
        self.assertEqual(lint._person_filename_parts('', 'Wiśniewska'),
                         ('wisniewska', 'unknown'))


class DuplicateNameReviewTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp, self.root = _new_archive()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, filename: str, frontmatter: str) -> Path:
        path = self.root / 'people' / 'stubs' / filename
        path.write_text(f'---\n{frontmatter}---\n', encoding='utf-8')
        return path

    def test_stem_alias_is_not_a_name(self) -> None:
        # `aliases:` carries filename stems; `quillfeather__ada` is a link
        # handle, not the surname-first name "Quillfeather Ada".
        self._write('quillfeather__ada_p-0000000001.md',
                    'id: p-0000000001\nname: Ada Quillfeather\n'
                    'aliases: [p-0000000001, quillfeather__ada]\nliving: unknown\n')
        self.assertEqual(find_people_by_name(self.root, ['Quillfeather Ada']), {})

    def test_refusal_names_the_alias_it_matched(self) -> None:
        self._write('hartley__edith_p-0000000001.md',
                    'id: p-0000000001\nname: Edith Hartley\n'
                    'name_variants: [Edith Bradford]\nliving: false\n')
        result = person.run_new(self.root, 'Edith Bradford')
        self.assertEqual(result.data['status'], 'duplicate-name')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('P-0000000001 Edith Hartley (also known as "Edith Bradford")', text)

    def test_id_less_hand_profile_counts(self) -> None:
        # A hand-made record nobody has minted an id for yet (SPEC §10) is
        # still that person.
        self._write('Mary Smith.md', 'name: Mary Smith\nliving: unknown\n')
        result = person.run_new(self.root, 'Mary Smith')
        self.assertEqual(result.data['status'], 'duplicate-name')
        [match] = result.data['duplicates']
        self.assertIsNone(match['id'])
        self.assertEqual(match['path'], 'people/stubs/Mary Smith.md')
        self.assertIn('people/stubs/Mary Smith.md, no ID yet',
                      ' '.join(m.text for m in result.messages))

    def test_unreadable_file_is_reported_not_silent(self) -> None:
        (self.root / 'people' / 'stubs' / 'broken__one_p-0000000001.md').write_bytes(
            b'---\nname: Caf\xe9 Owner\n---\n')
        unreadable: list = []
        find_people_by_name(self.root, ['Ada Quillfeather'], unreadable=unreadable)
        self.assertEqual(unreadable, ['people/stubs/broken__one_p-0000000001.md'])
        result = person.run_new(self.root, 'Ada Quillfeather', dry_run=True)
        self.assertTrue(any(m.level == 'warning' and 'could not be read' in m.text
                            for m in result.messages))
        code, _out, err = _mint_from_names(self.root, ['Bea Newname'], dry_run=True)
        self.assertEqual(code, EXIT_CLEAN)
        self.assertIn('could not be read', err)

    def test_reads_frontmatter_only(self) -> None:
        # The check parses frontmatter, never the claims block: a profile
        # whose body YAML is broken is still found by its name.
        self._write('quillfeather__ada_p-0000000001.md',
                    'id: p-0000000001\nname: Ada Quillfeather\nliving: unknown\n')
        path = self.root / 'people' / 'stubs' / 'quillfeather__ada_p-0000000001.md'
        path.write_text(path.read_text(encoding='utf-8')
                        + '\n## Claims\n```yaml\n- id: [unclosed\n```\n', encoding='utf-8')
        import _lib
        original = _lib.read_record
        _lib.read_record = lambda *a, **k: (_ for _ in ()).throw(AssertionError('full parse'))
        try:
            found = find_people_by_name(self.root, ['Ada Quillfeather'])
        finally:
            _lib.read_record = original
        self.assertEqual([m['id'] for m in found['Ada Quillfeather']], ['P-0000000001'])

    def test_stubs_force_without_from_names_is_refused(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = stubs._standalone_main(['--force', '--root', str(self.root)])
        self.assertEqual(code, EXIT_ERRORS)
        self.assertIn('--from-names', err.getvalue())


class RenameRepairsLossySlugTests(unittest.TestCase):
    """`fha person rename P-x "<same name>"` re-files a record whose file
    name was written by the old, letter-dropping slugger (L6) - the repair
    path for existing `winiewska__halina_…` files."""

    def setUp(self) -> None:
        self._tmp, self.root = _new_archive()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, filename: str, name: str, pid: str) -> Path:
        path = self.root / 'people' / 'stubs' / filename
        path.write_text(f'---\nid: {pid}\nname: {name}\nliving: unknown\n---\n',
                        encoding='utf-8')
        return path

    def test_same_name_rename_refiles_a_lossy_slug(self) -> None:
        old = self._write('winiewska__halina_p-0000000041.md', 'Halina Wiśniewska',
                          'p-0000000041')
        result = person.run_rename(self.root, 'P-0000000041', 'Halina Wiśniewska')
        self.assertEqual(result.data['status'], 'ok', [m.text for m in result.messages])
        self.assertFalse(old.exists())
        self.assertTrue((self.root / 'people' / 'stubs'
                         / 'wisniewska__halina_p-0000000041.md').is_file())

    def test_same_name_rename_leaves_a_hand_chosen_name_alone(self) -> None:
        # Filed under a birth surname the name: no longer carries: not a
        # lossy slug, so an unchanged name still changes nothing.
        path = self._write('smith__mary_p-0000000042.md', 'Mary Jones', 'p-0000000042')
        result = person.run_rename(self.root, 'P-0000000042', 'Mary Jones')
        self.assertEqual(result.data['status'], 'already')
        self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
