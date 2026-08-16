"""
test_atomic_record_writes.py - no verb destroys a record while refusing to edit it.

The defect these tests pin is one shape repeated across the suite. A verb reads
a record, computes the replacement, and writes it with a truncating writer -
`open(path, 'w')`, which empties the file before the first byte lands. When the
write dies partway (the disk fills, the process is killed, a network volume
drops), what is left on disk is a prefix of the replacement, and the verb's
`except OSError` hands the human a clean refusal saying nothing was changed.
The archive's truth is gone and the command that destroyed it reported success
at doing nothing. For a person record that is often the sole account of an
ancestor, and for `places/places.yaml` - the single registry for every place in
the archive - that is the worst outcome the tools can produce.

The cure is `_lib.write_text_exact_atomic`: write a sibling temp file, fsync it,
then `os.replace` it over the target. The record holds the old bytes or the new
bytes and nothing between, so the refusal message becomes true again.

WHAT EACH TEST ASSERTS. Every case here interrupts one verb mid-write and then
checks the same two things: the record's bytes are unchanged, and no stray temp
file is left in the folder. The refusal itself is already covered by each verb's
own test module - these tests exist for the file on disk behind the refusal.

RESETTING BETWEEN SUBTESTS. Any test that sweeps several verbs over one record
rebuilds the fixture before each one. Left shared, the first verb's torn write
would ruin the record and every later verb would then refuse for the wrong
reason - against a file that was already destroyed - and the sweep would pass
while proving nothing.

The last class is the durable guard: a grep-shaped assertion that no tool calls
the truncating writer at all, so this cannot drift back one call site at a time
the way it did the first time.

Fixtures only (AGENTS_TOOLING §5): temp trees, never the real archive.
"""

import contextlib
import errno
import os
import pathlib
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import confirm
import convert_mining
import lint
import normalize_links
import places
import process
import serve
import stubs


# ── The interruption harness ─────────────────────────────────────────────────

@contextlib.contextmanager
def write_dies_partway(target, keep: int = 12):
    """Make the next record write die after `keep` characters, as a full disk does.

    Lifted from `tests/test_person.py`, where it was written for the same
    defect in `fha person`. Kept as a copy rather than imported because a test
    module importing another test module makes the second one's collection
    order load-bearing; the helper is small and its contract is stated here.

    Both of `_lib`'s record writers end in one `handle.write(text)` on a
    text-mode file object, so intercepting that call reproduces the same
    interruption for either of them. What differs is WHICH file was open at the
    time: `write_text_exact` has the record itself open in truncating mode, so
    the wound lands on the record; `write_text_exact_atomic` has a sibling temp
    file open and the record is not touched until `os.replace`. That difference
    is the whole measurement, which is why the interception sits this low rather
    than stubbing out the writer function.

    `target` may be a file (interrupt writes to exactly that path) or a folder
    (interrupt writes to any file created directly inside it - the only way to
    aim at output whose filename is computed inside the call being interrupted).
    """
    real_path_open = pathlib.Path.open
    real_fdopen = os.fdopen
    target_key = os.path.abspath(str(target))
    by_parent = os.path.isdir(target_key)

    class _TornHandle:
        def __init__(self, fh):
            self._fh = fh

        def write(self, text):
            self._fh.write(text[:keep])
            raise OSError(errno.ENOSPC, 'No space left on device')

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            with contextlib.suppress(OSError):
                self._fh.close()
            return False

    def _aimed_at(path):
        here = os.path.abspath(str(path))
        return os.path.dirname(here) == target_key if by_parent else here == target_key

    def _patched_path_open(self, mode='r', *a, **kw):
        fh = real_path_open(self, mode, *a, **kw)
        if str(mode).startswith('w') and _aimed_at(self):
            return _TornHandle(fh)
        return fh

    def _patched_fdopen(fd, mode='r', *a, **kw):
        # The atomic writer's temp file is the only fd opened for writing
        # inside the calls these tests wrap.
        fh = real_fdopen(fd, mode, *a, **kw)
        if str(mode).startswith('w'):
            return _TornHandle(fh)
        return fh

    with mock.patch.object(pathlib.Path, 'open', _patched_path_open), \
            mock.patch.object(os, 'fdopen', _patched_fdopen):
        yield


class SurvivesTornWriteMixin:
    """`assert_untouched`: the record still holds `before`, and no debris."""

    def assert_untouched(self, path: Path, before: bytes, listing: list[str]) -> None:
        self.assertEqual(
            path.read_bytes(), before,
            f'{path.name} was modified by a write that reported a refusal')
        # The atomic writer unlinks its temp file on the way out. A stray
        # `.places.yaml.abc123.tmp` is debris the human would have to recognise
        # as ours and delete himself.
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), listing)


# ── 1. places/places.yaml - the registry for every place in the archive ──────

_REGISTRY = (
    '# Place registry - a hand comment that must survive every edit\n'
    '- id: L-7c1a9f4e22\n'
    '  name: Fairview\n'
    '  coords: [39.8, -95.6]\n'
    '  hierarchy: Fairview, Breton County, Kansas, USA\n'
    '  alt_names: [Fairview City]\n'
    '  notes: fictional town\n'
    '- id: L-9999999999\n'
    '  name: Elsewhere\n'
    '  coords: [1.0, 2.0]\n'
    # The block-scalar form `places note` itself writes, so `edit-note` can
    # actually match an entry and reach its write instead of refusing early.
    '  notes: |\n'
    '    2026-01-01: an existing note to rewrite\n'
)


class PlacesRegistryTornWriteTests(SurvivesTornWriteMixin, unittest.TestCase):
    """`fha places set|note|edit-note` - one file holds every place there is.

    A torn write here is worse than a torn person record: every OTHER place in
    the archive is collateral, and nothing else in the tree carries a copy of
    the registry to rebuild it from.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'places').mkdir(parents=True)
        self.registry = self.root / 'places' / 'places.yaml'
        self._reset()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reset(self) -> None:
        self.registry.write_text(_REGISTRY, encoding='utf-8', newline='')

    def test_every_registry_verb_leaves_the_file_whole(self) -> None:
        verbs = {
            'set': lambda: places.run_place_set(
                self.root, 'L-7c1a9f4e22', coords='40.1, -95.0'),
            'note': lambda: places.run_place_note(
                self.root, 'L-7c1a9f4e22', 'A note that must not cost the registry.'),
            'edit-note': lambda: places.run_place_edit_note(
                self.root, 'L-9999999999',
                # The whole entry INCLUDING its date - that is the string
                # `split_log_entries` matches on. Without the date this refuses
                # before it ever writes, and the subtest proves nothing.
                old_text='2026-01-01: an existing note to rewrite',
                text='rewritten'),
        }
        for name, call in verbs.items():
            with self.subTest(verb=name):
                # Reset per verb: without this the first torn write ruins the
                # registry and the rest refuse against an already-destroyed
                # file, passing while proving nothing.
                self._reset()
                before = self.registry.read_bytes()
                listing = sorted(p.name for p in self.registry.parent.iterdir())
                with write_dies_partway(self.registry):
                    result = call()
                self.assertNotEqual(result.exit_code, 0, name)
                self.assert_untouched(self.registry, before, listing)

    def test_the_other_places_survive_a_failed_edit(self) -> None:
        # The point of the registry case: the block being edited is one of
        # many, and the others have no other home.
        before = self.registry.read_bytes()
        with write_dies_partway(self.registry):
            places.run_place_set(self.root, 'L-7c1a9f4e22', coords='40.1, -95.0')
        text = self.registry.read_text(encoding='utf-8')
        self.assertIn('L-9999999999', text)
        self.assertIn('# Place registry - a hand comment', text)
        self.assertEqual(self.registry.read_bytes(), before)


# ── 2. confirm: the question log, the discovery log, and a source record ─────

_SOURCE = (
    '---\n'
    'id: S-aaaaaaaaaa\n'
    'title: Breton County Census 1880\n'
    'source_type: census\n'
    '---\n'
    '\n'
    '## Claims\n'
    '\n'
    '```yaml\n'
    '- id: C-1111111111\n'
    '  type: residence\n'
    '  persons: [P-1111111111]\n'
    '  value: Fairview\n'
    '  status: accepted\n'
    '```\n'
    '\n'
    '## Notes\n'
    '\n'
    'Everything below the claims must survive a failed edit.\n'
)


class ConfirmLogTornWriteTests(SurvivesTornWriteMixin, unittest.TestCase):
    """`fha confirm discovery` - the append that rewrites the whole log.

    Appending one line rewrites the file, so a torn write trades every
    discovery the human has ever recorded for the one being added.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'notes').mkdir(parents=True)
        self.log = self.root / 'notes' / 'discoveries.md'
        self._reset()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reset(self) -> None:
        self.log.write_text(
            '# Discoveries Log\n\n- 2026-01-01: the first find, years of work\n',
            encoding='utf-8', newline='')

    def test_a_torn_append_keeps_every_earlier_discovery(self) -> None:
        before = self.log.read_bytes()
        listing = sorted(p.name for p in self.log.parent.iterdir())
        with write_dies_partway(self.log):
            result = confirm.run_add_discovery(
                self.root, text='Found the marriage notice at last.', refs=[])
        self.assertNotEqual(result.exit_code, 0)
        self.assert_untouched(self.log, before, listing)
        self.assertIn('the first find, years of work',
                      self.log.read_text(encoding='utf-8'))


class ConfirmDraftTornWriteTests(SurvivesTornWriteMixin, unittest.TestCase):
    """`fha confirm draft` - accepting a biography must not cost the biography."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people').mkdir(parents=True)
        self.profile = self.root / 'people' / 'hartley__thomas_P-2222222222.md'
        self._reset()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reset(self) -> None:
        self.profile.write_text(
            '---\nid: P-2222222222\nname: Thomas Hartley\nliving: false\n'
            'tier: curated\n---\n\n# Thomas Hartley\n\n## Biography\n\n'
            'Years of human research live in this file.\n\n'
            '<!-- AI-DRAFT 2026-01-01 -->\nHe farmed near Fairview [[S-aaaaaaaaaa]].\n'
            '<!-- /AI-DRAFT -->\n',
            encoding='utf-8', newline='')

    def test_a_torn_accept_keeps_the_whole_profile(self) -> None:
        before = self.profile.read_bytes()
        listing = sorted(p.name for p in self.profile.parent.iterdir())
        with write_dies_partway(self.profile):
            result = confirm.run_accept_draft(self.root, person_id='P-2222222222')
        self.assertNotEqual(result.exit_code, 0)
        self.assert_untouched(self.profile, before, listing)
        self.assertIn('Years of human research',
                      self.profile.read_text(encoding='utf-8'))


# ── 3. lint --fix: bulk record rewrites with nobody watching ─────────────────

class LintFixTornWriteTests(SurvivesTornWriteMixin, unittest.TestCase):
    """`fha lint --fix*` rewrites person and source records in bulk, unattended.

    lint kept a PRIVATE copy of the byte-exact writer, so it never received the
    atomic upgrade the shared one got. These pin the two halves that a human is
    least likely to be watching when they go wrong.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'notes').mkdir(parents=True)
        (self.root / 'people' / 'stubs').mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_spawn_questions_keeps_the_existing_question_log(self) -> None:
        qlog = self.root / 'notes' / 'questions.md'
        qlog.write_text(
            '# Open Questions\n\n## Q: Which Thomas married Rose?\n'
            '- origin: human\n- status: open\n',
            encoding='utf-8', newline='')
        before = qlog.read_bytes()
        listing = sorted(p.name for p in qlog.parent.iterdir())
        findings = [lint.Finding('E', 'E009', self.root / 'sources' / 'x.md',
                                 'C-1111111111 contradicts C-2222222222')]
        progress: list[str] = []
        changed: list[str] = []
        with write_dies_partway(qlog):
            with self.assertRaises(OSError):
                lint._fix_spawn_questions(
                    None, findings, self.root, progress, changed)
        self.assert_untouched(qlog, before, listing)
        self.assertIn('Which Thomas married Rose?',
                      qlog.read_text(encoding='utf-8'))

    def test_format_fix_keeps_the_record_it_is_tidying(self) -> None:
        # --fix's formatting pass touches every record in the archive, so it
        # is the widest-blast-radius writer lint has.
        rec = self.root / 'people' / 'stubs' / 'hartley__rose_P-3333333333.md'
        rec.write_text(
            '---\r\nid: P-3333333333\r\nname: Rose Hartley\r\n---\r\n\r\n'
            '# Rose Hartley\r\n\r\nHer whole record, in CRLF.',
            encoding='utf-8', newline='')
        before = rec.read_bytes()
        listing = sorted(p.name for p in rec.parent.iterdir())
        progress: list[str] = []
        changed: list[str] = []
        with write_dies_partway(rec):
            with self.assertRaises(OSError):
                lint._fix_format(rec, progress, changed)
        self.assert_untouched(rec, before, listing)

    def test_format_fix_actually_converts_crlf(self) -> None:
        # The CRLF half of the W109 fix only ever worked by accident, through
        # the default write translating back to os.linesep - so on Windows it
        # converted a clean LF archive TO CRLF, and on a CRLF file that already
        # ended in a newline it did nothing at all. Byte-exact IO makes the fix
        # match its own docstring on every platform.
        rec = self.root / 'people' / 'stubs' / 'hartley__ann_P-4444444444.md'
        rec.write_text('---\r\nid: P-4444444444\r\n---\r\n', encoding='utf-8',
                       newline='')
        progress: list[str] = []
        changed: list[str] = []
        lint._fix_format(rec, progress, changed)
        self.assertEqual(rec.read_bytes(), b'---\nid: P-4444444444\n---\n')
        self.assertEqual(changed, [str(rec)])


# ── 4. normalize-links --write: every record in the archive, one pass ────────

class NormalizeLinksTornWriteTests(SurvivesTornWriteMixin, unittest.TestCase):
    """`fha normalize-links --write` had two defects in one line.

    It wrote through `Path.write_text`, which (a) truncates, and (b) translates
    newlines - so tidying one citation in a CRLF-authored record rewrote every
    line ending in it. The verb walks every record in the archive, so both
    defects are archive-wide.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.profile = self.root / 'people' / 'hartley__thomas_P-2222222222.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_profile(self, newline: str) -> bytes:
        body = (
            '---\n'
            'id: P-2222222222\n'
            'name: Thomas Hartley\n'
            'living: false\n'
            '---\n'
            '\n'
            '# Thomas Hartley\n'
            '\n'
            '## Biography\n'
            '\n'
            'He farmed near Fairview [S-aaaaaaaaaa].\n'
            'A second line of hard-won prose.\n'
        ).replace('\n', newline)
        self.profile.write_text(body, encoding='utf-8', newline='')
        return self.profile.read_bytes()

    def test_a_torn_normalize_keeps_the_record(self) -> None:
        before = self._write_profile('\n')
        listing = sorted(p.name for p in self.profile.parent.iterdir())
        with write_dies_partway(self.profile):
            with self.assertRaises(OSError):
                normalize_links.run_normalize_links(self.root, {}, write=True)
        self.assert_untouched(self.profile, before, listing)

    def test_a_crlf_record_keeps_its_line_endings(self) -> None:
        self._write_profile('\r\n')
        result = normalize_links.run_normalize_links(self.root, {}, write=True)
        self.assertEqual(result.exit_code, 0)
        after = self.profile.read_bytes()
        # The one intended edit landed...
        self.assertIn(b'[[S-aaaaaaaaaa]]', after)
        # ...and it is the ONLY change: every line still ends CRLF, and no
        # lone LF was introduced.
        self.assertNotIn(b'\n', after.replace(b'\r\n', b''))


# ── 5. serve: the workbench homepage editor ─────────────────────────────────

class ServeHomeEditTornWriteTests(SurvivesTornWriteMixin, unittest.TestCase):
    """`notes/home.md` via the workbench form - one line of feedback, so the
    refusal has to be true."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'notes').mkdir(parents=True)
        self.home = self.root / 'notes' / 'home.md'
        self.home.write_text('# Our Family\n\nThe intro the human wrote.\n',
                             encoding='utf-8', newline='')
        self.state = mock.Mock(archive_root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_torn_home_edit_keeps_the_old_intro(self) -> None:
        before = self.home.read_bytes()
        listing = sorted(p.name for p in self.home.parent.iterdir())
        with write_dies_partway(self.home):
            result = serve._verb_home_edit(
                self.state, {'text': '# Our Family\n\nA replacement intro.\n'},
                dry_run=False)
        self.assertFalse(result.ok)
        self.assert_untouched(self.home, before, listing)
        self.assertIn('The intro the human wrote',
                      self.home.read_text(encoding='utf-8'))


# ── 6. stubs: new person records, in bulk ───────────────────────────────────

class StubsTornWriteTests(unittest.TestCase):
    """`fha stubs` creates person records. The `exists()` guard means a torn
    stub is never repaired - the next run skips it - so a half-written stub is
    a permanently broken record nobody is told about."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.stubs_dir = self.root / 'people' / 'stubs'
        self.stubs_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_torn_stub_is_not_left_behind(self) -> None:
        with write_dies_partway(self.stubs_dir):
            with self.assertRaises(OSError):
                stubs.create_stubs(self.root, {'p-5555555555': 'Ann Kid'})
        # Nothing half-written in the folder, and no temp debris either: the
        # verb either created a whole stub or created nothing.
        self.assertEqual(list(self.stubs_dir.iterdir()), [])

    def test_stub_writing_does_not_go_through_the_translating_writer(self) -> None:
        # The other half of the stubs fix: `Path.write_text` translates '\n' to
        # os.linesep, so the stored form of a person record depended on which
        # machine minted it (CRLF on Windows, LF everywhere else). Asserting on
        # the bytes could only ever fail on Windows, which makes it no guard at
        # all on this platform - so assert on the route instead: nothing in the
        # stub path may reach the translating writer.
        with mock.patch.object(
                pathlib.Path, 'write_text',
                side_effect=AssertionError(
                    'stub writing went through Path.write_text, which '
                    'translates newlines - use _lib.write_text_exact_atomic')):
            stubs.create_stubs(self.root, {'p-5555555555': 'Ann Kid'})
        written = list(self.stubs_dir.iterdir())
        self.assertEqual(len(written), 1)
        self.assertNotIn(b'\r\n', written[0].read_bytes())


# ── 7. process --more: appending a files: entry to an existing record ───────

class ProcessAttachMoreTornWriteTests(SurvivesTornWriteMixin, unittest.TestCase):
    """The sweep find the audit missed.

    Most of `process.py`'s record writes CREATE a source record and register an
    undo that unlinks the partial - defensible, since there is no earlier
    complete version to lose. `--more` is the exception: it appends a `files:`
    entry to a record that already exists, so the same truncating write costs a
    complete record, and the rollback beside it (`write_text(old_text)`) used
    the truncating writer too - a failed restore destroying what the failure
    was supposed to preserve.

    Driven through the documents-root branch of `attach_more`, which needs only
    a rename and the record write - no exiftool on the test machine.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'sources' / 'census').mkdir(parents=True)
        (self.root / 'documents' / 'census').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.record = self.root / 'sources' / 'census' / 'census-1880_S-aaaaaaaaaa.md'
        self.record.write_text(_SOURCE, encoding='utf-8', newline='')
        self.primary = (self.root / 'documents' / 'census'
                        / 'census-1880_S-aaaaaaaaaa.jpg')
        self.primary.write_bytes(b'primary page')
        self.more = self.root / 'documents' / 'census' / 'page2.jpg'
        self.more.write_bytes(b'second page')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_torn_files_append_keeps_the_source_record(self) -> None:
        before = self.record.read_bytes()
        listing = sorted(p.name for p in self.record.parent.iterdir())
        with write_dies_partway(self.record):
            code = process.attach_more(
                self.root, {}, self.primary, self.more,
                role='page2', copy=None, dry_run=False)
        self.assertNotEqual(code, 0)
        self.assert_untouched(self.record, before, listing)
        self.assertIn('Everything below the claims must survive',
                      self.record.read_text(encoding='utf-8'))


# ── 7b. convert-mining: the one apply write that touches an existing file ───

class ConvertMiningQuestionLogTests(SurvivesTornWriteMixin, unittest.TestCase):
    """`apply_plan`'s `write_new` is defensible; the questions.md branch is not.

    Every `write_new` target is a path `_preflight_apply` has already proven
    does not exist, and its undo unlinks the partial - so a torn write there
    destroys nothing, because there were no bytes to lose. That is why the
    audit's "new files with an undo journal" reading is right about `write_new`
    and wrong about the branch beside it: appending the imported questions
    rewrites an existing `notes/questions.md`, and unlinking a partial there
    would delete the human's whole question log rather than rescue it.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'notes').mkdir(parents=True)
        self.qlog = self.root / 'notes' / 'questions.md'
        self.qlog.write_text(
            '# Open Questions\n\n## Q: Which Thomas married Rose?\n'
            '- origin: human\n- status: open\n',
            encoding='utf-8', newline='')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _plan(self):
        return convert_mining.ConversionPlan(
            archive_root=self.root, sources=[], stub_people=[],
            questions=[{'question': 'Where was Ann born?', 'refs': []}],
            mapping_rows=[], warnings=[],
        )

    def test_a_torn_question_append_keeps_the_log(self) -> None:
        before = self.qlog.read_bytes()
        listing = sorted(p.name for p in self.qlog.parent.iterdir())
        with write_dies_partway(self.qlog):
            with self.assertRaises(OSError):
                convert_mining.apply_plan(self._plan(), {})
        self.assert_untouched(self.qlog, before, listing)
        self.assertIn('Which Thomas married Rose?',
                      self.qlog.read_text(encoding='utf-8'))

    def test_a_clean_append_keeps_the_existing_questions(self) -> None:
        convert_mining.apply_plan(self._plan(), {})
        text = self.qlog.read_text(encoding='utf-8')
        self.assertIn('Which Thomas married Rose?', text)
        self.assertIn('Where was Ann born?', text)


# ── 8. The durable guard ────────────────────────────────────────────────────

class NoToolWritesRecordsNonAtomicallyTests(unittest.TestCase):
    """No tool may call the truncating writer.

    This is the test that keeps the fix from drifting back. Eight writers went
    wrong the first time for one structural reason: two writers with nearly the
    same name sit next to each other in `_lib.py`, and picking the wrong one
    looks exactly like picking the right one at the call site. Reviewers cannot
    reliably catch that; a grep can.

    The assertion is deliberately absolute rather than a curated allow-list of
    "records" versus "generated output". An allow-list needs a judgement call
    per call site, and judgement is what failed here. `write_text_exact_atomic`
    is correct everywhere the plain one is correct - it costs one rename - so
    "never the truncating one" is a rule that needs no judgement to apply and
    no maintenance when a new tool lands.

    If a future call site genuinely wants the plain writer, this test failing
    is the conversation about why, which is exactly the review this defect
    never got.
    """

    # `write_text_exact(` but not `write_text_exact_atomic(`.
    CALL_RE = re.compile(r'\bwrite_text_exact\s*\((?!\s*\))')

    def test_no_tool_calls_the_non_atomic_writer(self) -> None:
        offenders = []
        for path in sorted((ROOT / 'tools').glob('*.py')):
            if path.name == '_lib.py':
                continue          # where both writers are defined
            for n, line in enumerate(
                    path.read_text(encoding='utf-8').splitlines(), start=1):
                code = line.split('#', 1)[0]
                if self.CALL_RE.search(code):
                    offenders.append(f'{path.name}:{n}: {line.strip()}')
        self.assertEqual(offenders, [], (
            'These call the truncating `write_text_exact`, which empties the '
            'target before writing - a failure partway leaves the record as a '
            'fragment while the verb reports a clean refusal. Use '
            '`_lib.write_text_exact_atomic`:\n  ' + '\n  '.join(offenders)))

    def test_no_tool_keeps_a_private_copy_of_the_writer(self) -> None:
        # lint.py drifted because it held `_write_text_exact` privately and so
        # never received the shared one's atomic upgrade. A private copy of
        # shared code does not get the shared code's fixes.
        private = re.compile(r'^def _(?:read|write)_text_exact\b', re.M)
        offenders = [
            path.name for path in sorted((ROOT / 'tools').glob('*.py'))
            if private.search(path.read_text(encoding='utf-8'))
        ]
        self.assertEqual(offenders, [], (
            'These define a private copy of _lib\'s byte-exact IO helpers. '
            'Import `read_text_exact` / `write_text_exact_atomic` from _lib '
            'instead, so the next fix to them reaches every caller: '
            + ', '.join(offenders)))


if __name__ == '__main__':
    unittest.main()
