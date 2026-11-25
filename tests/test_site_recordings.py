"""
test_site_recordings.py - a family recording plays on its source page.

On the shared site (SPEC §19, Recordings) a recording plays only when the
owner said yes (`rights: recording_ok: true`), everyone heard on it is listed
in `speakers:` and has died and is not restricted, nobody else the source
names is living, unknown, restricted or unresolved, and the source itself may
be shared. Any other recording stays in the archive, and the shared site does
not even give its file name, which often names the person interviewed. The
owner's own linked preview plays every recording.

Expected answers come from SPEC §19 and A.10, not from the code. Real records
are written and indexed with `fha index` into a temp archive. `ffmpeg` is
stood in for (the stripped copy is a marker file), except in the one test
that runs the real binary when it is installed.
"""

import contextlib
import importlib.util
import io
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod                     # noqa: E402
from _lib import anchor_seconds, load_fha_yaml  # noqa: E402

_spec = importlib.util.spec_from_file_location('fha_site_recordings', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site_recordings'] = site
_spec.loader.exec_module(site)

GRANDPA = 'p-1111111111'    # deceased: the man interviewed
ASKER = 'p-2222222222'      # deceased: the grandson asking the questions
LIVE = 'p-3333333333'       # living
UNK = 'p-4444444444'        # living: unknown
QUIET = 'p-5555555555'      # deceased, restricted: by-request
OLDID = 'p-6666666666'      # a duplicate merged into GRANDPA, marked living: true

SID = 's-aaaaaaaaaa'
SID2 = 's-bbbbbbbbbb'
SESSION = 'hartley-1998-05-02'
STRIPPED = b'STRIPPED COPY'
ORIGINAL = b'ORIGINAL RECORDING BYTES with a phone location tag'

_real_which = shutil.which


def _which_with_ffmpeg(name, *a, **kw):
    return '/fake/bin/ffmpeg' if name == 'ffmpeg' else _real_which(name, *a, **kw)


def _which_without_ffmpeg(name, *a, **kw):
    return None if name == 'ffmpeg' else _real_which(name, *a, **kw)


def _fake_remux(self, src: Path, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(STRIPPED)
    return True


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'archive'
        self.root.mkdir()
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        self.out = Path(self._tmp.name) / 'site'
        self.person(GRANDPA, 'Walter Hartley')
        self.person(ASKER, 'Thomas Hartley')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # - records -

    def person(self, pid, name, *, living='false', extra=''):
        path = self.root / 'people' / f'{name.split()[-1].lower()}__{name.split()[0].lower()}_{pid}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nid: {pid}\nname: {name}\nliving: {living}\ntier: curated\n'
                        f'{extra}---\n# {name}\n', encoding='utf-8')

    def recording(self, rel: str, data: bytes = ORIGINAL) -> str:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return rel

    def source(self, sid=SID, *, speakers=(GRANDPA, ASKER), recording_ok='true',
               people=(GRANDPA,), files=None, claims=None, extra='',
               source_type='interview'):
        """An interview source. `files` is a list of (path, original_filename)."""
        if files is None:
            files = [(self.recording(f'documents/interviews/{SESSION}/{SESSION}_{sid}.m4a'),
                      'grandpa-farm-stories.m4a')]
        lines = ['---', f'id: {sid}', 'title: Interview with Walter Hartley, 1998',
                 f'source_type: {source_type}', 'citation: "Walter Hartley, interviewed 1998."']
        if speakers is not None:
            lines.append('speakers: [' + ', '.join(speakers) + ']')
        if people:
            lines.append('people:')
            lines += [f'  - "{p}"' for p in people]
        if recording_ok is not None:
            lines += ['rights:', f'  recording_ok: {recording_ok}']
        lines.append('files:')
        for rel, original in files:
            lines.append(f'  - file: {rel}')
            if original:
                lines.append(f'    original_filename: {original}')
        if extra:
            lines.append(extra.rstrip('\n'))
        lines += ['---', '', '## Claims', '```yaml']
        for c in claims if claims is not None else [
                {'id': 'c-aaaaaaaa01', 'persons': [GRANDPA], 'anchor': '00:14:32'}]:
            lines += [f'- id: {c["id"]}', f'  type: {c.get("type", "residence")}',
                      f'  value: "{c.get("value", "lived on the farm")}"',
                      f'  persons: [{", ".join(c["persons"])}]',
                      '  status: accepted', '  confidence: low']
            if c.get('anchor'):
                lines.append(f'  anchor: "{c["anchor"]}"')
            if c.get('roles'):
                lines.append(f'  roles: {c["roles"]}')
            if c.get('restricted'):
                lines.append('  restricted: true')
        lines.append('```')
        path = self.root / 'sources' / source_type / f'interview_{sid}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    # - building -

    def build(self, *, linked=False, ffmpeg=True, out=None):
        out = out or self.out
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            index_mod.build_index(self.root, load_fha_yaml(self.root))
            with unittest.mock.patch.object(site.shutil, 'which',
                                            _which_with_ffmpeg if ffmpeg else _which_without_ffmpeg), \
                    unittest.mock.patch.object(site._SiteBuilder, '_remux_audio', _fake_remux):
                res = site.run_site(self.root, out, linked=linked)
        self.assertEqual(res['status'], 'ok', res['messages'])
        return res

    def page(self, sid=SID) -> str:
        return (self.out / 'sources' / f'{sid}.html').read_text(encoding='utf-8')

    def media(self, out=None) -> list[Path]:
        media = (out or self.out) / 'media'
        return sorted(p for p in media.rglob('*') if p.is_file()) if media.is_dir() else []

    def all_output(self) -> str:
        return '\n'.join(p.read_text(encoding='utf-8', errors='replace')
                         for p in self.out.rglob('*')
                         if p.is_file() and p.suffix in ('.html', '.json', '.js'))

    def assert_withheld(self, sid=SID, stem=SESSION):
        """Plays nothing, copies nothing, and never names the file."""
        html = self.page(sid)
        self.assertNotIn('<audio', html)
        self.assertNotIn('play-from', html)
        self.assertIn('a recording, kept in the archive', html)
        self.assertNotIn(stem, self.all_output())
        self.assertNotIn('grandpa-farm-stories', self.all_output())
        self.assertEqual([p for p in self.media() if p.suffix != '.jpg'], [])


class SharedSiteTests(_Base):
    def test_deceased_speakers_and_owner_yes_play_a_stripped_copy(self):
        self.source()
        self.build()
        html = self.page()
        self.assertIn('<audio id="rec-1" controls preload="metadata">', html)
        copies = self.media()
        self.assertEqual(len(copies), 1, copies)
        copy = copies[0]
        self.assertEqual(copy.parent.name, SID)
        self.assertEqual(copy.suffix, '.m4a')
        # The site's copy, never the original file.
        self.assertEqual(copy.read_bytes(), STRIPPED)
        self.assertIn(f'src="../media/{SID}/{copy.name}"', html)
        self.assertIn('type="audio/mp4"', html)
        self.assertNotIn('documents/interviews', html)

    def test_a_living_speaker_keeps_the_recording_home(self):
        self.person(LIVE, 'Carol Hartley', living='true')
        self.source(speakers=(GRANDPA, LIVE))
        self.build()
        self.assert_withheld()

    def test_a_speaker_of_unknown_living_status_keeps_the_recording_home(self):
        self.person(UNK, 'Dora Hartley', living='unknown')
        self.source(speakers=(GRANDPA, UNK))
        self.build()
        self.assert_withheld()

    def test_a_restricted_speaker_keeps_the_recording_home(self):
        self.person(QUIET, 'Ruth Hartley', extra='restricted: by-request\n')
        self.source(speakers=(GRANDPA, QUIET))
        self.build()
        self.assert_withheld()

    def test_no_speakers_list_keeps_the_recording_home(self):
        self.source(speakers=None)
        self.build()
        self.assert_withheld()

    def test_an_empty_speakers_list_keeps_the_recording_home(self):
        self.source(speakers=())
        self.build()
        self.assert_withheld()

    def test_without_the_owners_yes_the_recording_stays_home(self):
        self.source(recording_ok=None)
        self.build()
        self.assert_withheld()

    def test_only_a_real_yes_counts(self):
        # "yes" in quotes is text, not YAML true; A.6 says absent or
        # anything else keeps it in the archive.
        self.source(recording_ok='"yes"')
        self.build()
        self.assert_withheld()

    def test_a_speaker_not_in_the_index_counts_as_living_and_is_reported_once(self):
        self.source(speakers=(GRANDPA, 'p-9999999999'))
        res = self.build()
        self.assert_withheld()
        hits = [m for m in res['messages'] if 'p-9999999999' in m]
        self.assertEqual(len(hits), 1, res['messages'])
        self.assertIn('fha find', hits[0])

    def test_a_merged_record_marked_living_holds_the_recording_back(self):
        self.person(OLDID, 'Walt Hartley', living='true', extra=f'merged_into: {GRANDPA}\n')
        self.source(speakers=(OLDID, ASKER))
        self.build()
        self.assert_withheld()

    def test_an_unresolved_name_on_people_counts_as_living(self):
        self.source(people=(GRANDPA, '[[Aunt Somebody]]'))
        self.build()
        self.assert_withheld()

    def test_an_unresolved_name_in_a_claim_counts_as_living(self):
        self.source(claims=[{'id': 'c-aaaaaaaa01', 'persons': [GRANDPA, '"[[Cousin Nobody]]"'],
                             'anchor': '00:14:32'}])
        self.build()
        self.assert_withheld()

    def test_a_living_person_named_by_a_kept_claim_holds_the_recording_back(self):
        # The source keeps its page (a record naming a living witness is
        # still part of Walter's story), but the voice could name her.
        self.person(LIVE, 'Carol Hartley', living='true')
        self.source(people=(GRANDPA,), claims=[
            {'id': 'c-aaaaaaaa01', 'type': 'marriage', 'persons': [GRANDPA, LIVE],
             'roles': f'{{spouse: [{GRANDPA}], witness: [{LIVE}]}}', 'anchor': '00:14:32'}])
        self.build()
        self.assertIn('Walter Hartley', self.page())   # the page itself stays
        self.assert_withheld()

    def test_a_claim_marked_private_holds_the_recording_back(self):
        # The recording is the evidence for that private fact: playing it
        # would publish what the claim's marker keeps home.
        self.source(claims=[
            {'id': 'c-aaaaaaaa01', 'persons': [GRANDPA], 'anchor': '00:14:32'},
            {'id': 'c-aaaaaaaa02', 'persons': [GRANDPA], 'restricted': True}])
        self.build()
        self.assert_withheld()

    def test_a_source_not_cleared_for_publication_has_no_page(self):
        self.source()
        path = next((self.root / 'sources').rglob(f'*{SID}.md'))
        path.write_text(path.read_text(encoding='utf-8').replace(
            '  recording_ok: true', '  recording_ok: true\n  publication_ok: false'), encoding='utf-8')
        self.build()
        self.assertFalse((self.out / 'sources' / f'{SID}.html').exists())
        self.assertNotIn(SESSION, self.all_output())
        self.assertEqual(self.media(), [])

    def test_a_withheld_video_is_not_named_either(self):
        self.person(LIVE, 'Carol Hartley', living='true')
        self.source(speakers=(LIVE,), files=[
            (self.recording(f'documents/interviews/{SESSION}/{SESSION}_{SID}.mp4'), None)])
        self.build()
        self.assert_withheld()

    def test_without_ffmpeg_nothing_is_copied_and_the_build_says_so_once(self):
        self.source()
        self.source(SID2, files=[(self.recording(f'documents/interviews/x/x_{SID2}.mp3'), None)])
        res = self.build(ffmpeg=False)
        for sid in (SID, SID2):
            html = self.page(sid)
            self.assertNotIn('<audio', html)
            self.assertNotIn('play-from', html)
            self.assertIn('install ffmpeg', html)
        self.assertEqual(self.media(), [])
        notes = [m for m in res['messages'] if 'ffmpeg' in m]
        self.assertEqual(len(notes), 1, res['messages'])
        self.assertTrue(notes[0].startswith('NOTE: 2 recording(s)'), notes[0])

    def test_a_failed_copy_is_not_played(self):
        self.source()
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            index_mod.build_index(self.root, load_fha_yaml(self.root))
            with unittest.mock.patch.object(site.shutil, 'which', _which_with_ffmpeg), \
                    unittest.mock.patch.object(site._SiteBuilder, '_remux_audio',
                                               lambda self, s, d: False):
                res = site.run_site(self.root, self.out, linked=False)
        self.assertNotIn('<audio', self.page())
        self.assertEqual(self.media(), [])
        self.assertTrue(any('could not make' in m for m in res['messages']), res['messages'])

    def test_the_site_is_identical_build_to_build(self):
        self.source()
        other = Path(self._tmp.name) / 'site2'
        self.build()
        self.build(out=other)
        first = {p.relative_to(self.out).as_posix(): p.read_bytes()
                 for p in self.out.rglob('*') if p.is_file() and p.name != '.fha-site'}
        second = {p.relative_to(other).as_posix(): p.read_bytes()
                  for p in other.rglob('*') if p.is_file() and p.name != '.fha-site'}
        self.assertEqual(sorted(first), sorted(second))
        self.assertEqual([k for k in first if first[k] != second[k]], [])


class LinkedPreviewTests(_Base):
    def test_the_owners_preview_plays_every_recording(self):
        self.person(LIVE, 'Carol Hartley', living='true')
        self.source(speakers=(GRANDPA, LIVE), recording_ok=None)
        self.build(linked=True)
        html = self.page()
        self.assertIn('<audio id="rec-1"', html)
        self.assertIn(f'documents/interviews/{SESSION}/{SESSION}_{SID}.m4a', html)
        self.assertEqual(self.media(), [])
        self.assertIn('play from 14:32', html)

    def test_the_workbench_plays_the_real_file_through_fha_serve(self):
        self.person(LIVE, 'Carol Hartley', living='true')
        self.source(speakers=(LIVE,), recording_ok=None)
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            index_mod.build_index(self.root, load_fha_yaml(self.root))
            res = site.run_site(self.root, self.out, linked=True, workbench=True)
        self.assertEqual(res['status'], 'ok', res['messages'])
        html = self.page()
        self.assertIn(f'<source src="/root/documents/interviews/{SESSION}/{SESSION}_{SID}.m4a" '
                      'type="audio/mp4">', html)
        self.assertIn('data-wb-open-file=', html)


class PlayFromTests(_Base):
    def test_a_claim_with_a_time_links_to_that_minute(self):
        self.source()
        self.build()
        html = self.page()
        self.assertIn('<th>Listen</th>', html)
        m = re.search(r'<a class="play-from" href="([^"]+)" data-audio="rec-1" data-t="872">'
                      r'play from 14:32</a>', html)
        self.assertIsNotNone(m, html)
        self.assertTrue(m.group(1).endswith('.m4a#t=872'), m.group(1))
        self.assertIn('id="fha-recording-script"', html)

    def test_a_page_anchor_gets_no_link(self):
        self.source(claims=[{'id': 'c-aaaaaaaa01', 'persons': [GRANDPA], 'anchor': 'page 3'}])
        self.build()
        html = self.page()
        self.assertIn('<audio', html)
        self.assertNotIn('play from', html)
        self.assertNotIn('<th>Listen</th>', html)

    def test_two_recordings_and_an_unlabelled_time_get_no_link(self):
        files = [(self.recording(f'documents/interviews/{SESSION}/{SESSION}_{SID}.m4a'), 'a.m4a'),
                 (self.recording(f'documents/interviews/{SESSION}/{SESSION}-cars-audio_{SID}.m4a'), None)]
        self.source(files=files)
        self.build()
        html = self.page()
        self.assertEqual(html.count('<audio'), 2)
        self.assertNotIn('play from', html)

    def test_a_labelled_time_finds_its_recording_by_its_first_name(self):
        files = [(self.recording(f'documents/interviews/{SESSION}/{SESSION}_{SID}.m4a'),
                  'grandpa-1998-wedding.m4a'),
                 (self.recording(f'documents/interviews/{SESSION}/{SESSION}-cars-audio_{SID}.m4a'), None)]
        self.source(files=files, claims=[
            {'id': 'c-aaaaaaaa01', 'persons': [GRANDPA], 'anchor': 'wedding-whisper, 00:14:32'},
            {'id': 'c-aaaaaaaa02', 'persons': [GRANDPA], 'anchor': 'cars-whisper, 1:02:03'}])
        self.build()
        html = self.page()
        self.assertIn('data-audio="rec-1" data-t="872">play from 14:32<', html)
        self.assertIn('data-audio="rec-2" data-t="3723">play from 1:02:03<', html)

    def test_a_withheld_recording_gets_no_link(self):
        self.person(LIVE, 'Carol Hartley', living='true')
        self.source(speakers=(LIVE,))
        self.build()
        self.assertNotIn('play from', self.page())
        self.assertNotIn('fha-recording-script', self.page())


HELEN = 'p-7777777777'      # deceased: talked about on the tape, never heard on it
QUOTE = 'We never locked the door, not once.'


def _quotes(*entries) -> str:
    """A source's `quotes:` block (SPEC A.10), each (speaker, text, anchor)."""
    lines = ['quotes:']
    for speaker, text, anchor in entries:
        item = f'  - {{speaker: {speaker}, text: "{text}"'
        lines.append(item + (f', anchor: "{anchor}"}}' if anchor else '}'))
    return '\n'.join(lines)


class PersonPageTests(_Base):
    """Grandpa's own page has his voice: the recordings he is heard on, and a
    short quote in his own words, under the same SPEC §19 rule as the
    source page (TOOLING §12)."""

    def person_page(self, pid=GRANDPA, out=None) -> str:
        return ((out or self.out) / 'persons' / f'{pid}.html').read_text(encoding='utf-8')

    def test_a_speakers_page_plays_the_same_copy_as_the_source_page(self):
        self.source()
        self.build()
        html = self.person_page()
        self.assertIn('<h2>Recordings</h2>', html)
        self.assertIn('Walter Hartley can be heard on this recording.', html)
        copy = self.media()[0]
        href = f'../media/{SID}/{copy.name}'
        self.assertIn(f'src="{href}"', self.page())
        self.assertIn(f'<audio id="rec-{SID}-1" controls preload="none"><source src="{href}" '
                      'type="audio/mp4">', html)
        self.assertIn(f'href="../sources/{SID}.html">Interview with Walter Hartley, 1998</a>', html)
        self.assertNotIn('documents/interviews', html)
        # The other speaker is heard on it too.
        self.assertIn('<h2>Recordings</h2>', self.person_page(ASKER))

    def test_someone_only_talked_about_gets_no_recordings_section(self):
        self.person(HELEN, 'Helen Hartley')
        self.source(people=(GRANDPA, HELEN))
        self.build()
        self.assertIn('<audio', self.page())
        self.assertNotIn('<h2>Recordings</h2>', self.person_page(HELEN))

    def test_a_living_speaker_keeps_both_the_recording_and_the_quote_home(self):
        self.person(LIVE, 'Carol Hartley', living='true')
        self.source(speakers=(GRANDPA, LIVE), extra=_quotes((GRANDPA, QUOTE, '00:14:32')))
        self.build()
        html = self.person_page()
        self.assertNotIn('<h2>Recordings</h2>', html)
        self.assertNotIn('<audio', html)
        self.assertNotIn('locked the door', html)
        self.assertFalse((self.out / 'persons' / f'{LIVE}.html').exists())

    def test_a_quote_needs_no_yes_for_the_recording(self):
        self.source(recording_ok=None, extra=_quotes((GRANDPA, QUOTE, '00:14:32')))
        self.build()
        html = self.person_page()
        self.assertNotIn('<h2>Recordings</h2>', html)
        m = re.search(r'<blockquote class="voice-quote">(.*?)</blockquote>', html, re.S)
        self.assertIsNotNone(m, html)
        self.assertIn(f'"{QUOTE}"', m.group(1))
        self.assertIn('class="fn-ref"', m.group(1))     # its footnote
        self.assertNotIn('hear it', m.group(1))          # nothing to play

    def test_a_quote_on_a_shared_recording_can_be_heard(self):
        self.source(extra=_quotes((GRANDPA, QUOTE, '00:14:32')))
        self.build()
        html = self.person_page()
        self.assertIn(f'<a class="play-from" href="../sources/{SID}.html#play-rec-1-872" '
                      f'data-audio="rec-{SID}-1" data-t="872">hear it</a>', html)
        self.assertIn('id="fha-recording-script"', html)

    def test_a_quote_from_someone_not_on_speakers_is_not_shown(self):
        self.person(HELEN, 'Helen Hartley')
        self.source(people=(GRANDPA, HELEN), extra=_quotes((HELEN, QUOTE, None)))
        self.build()
        self.assertNotIn('locked the door', self.all_output())

    def test_a_long_quote_is_still_shown(self):
        # Lint is the place to warn about length, not the site.
        long_text = ' '.join(['word'] * 60)
        self.source(extra=_quotes((GRANDPA, long_text, None)))
        self.build()
        self.assertIn(long_text, self.person_page())

    def test_the_first_quote_in_source_order_is_the_one_shown(self):
        self.source(extra=_quotes((ASKER, 'Tell me about the farm.', None),
                                  (GRANDPA, QUOTE, None),
                                  (GRANDPA, 'A second thing he said.', None)))
        self.build()
        html = self.person_page()
        self.assertIn(QUOTE, html)
        self.assertNotIn('A second thing he said.', html)
        self.assertNotIn('Tell me about the farm.', html)
        self.assertIn('Tell me about the farm.', self.person_page(ASKER))

    def test_a_timeline_fact_from_the_recording_has_a_listen_link(self):
        self.source()
        self.build()
        html = self.person_page()
        self.assertIn(f'<a class="play-from" href="../sources/{SID}.html#play-rec-1-872" '
                      f'data-audio="rec-{SID}-1" data-t="872">listen</a>', html)

    def test_a_timeline_fact_from_a_withheld_recording_has_no_listen_link(self):
        self.source(recording_ok=None)
        self.build()
        html = self.person_page()
        self.assertNotIn('listen</a>', html)
        self.assertNotIn('play-from', html)
        self.assertNotIn('fha-recording-script', html)

    def test_the_owners_preview_plays_a_living_speakers_recording(self):
        self.person(LIVE, 'Carol Hartley', living='true')
        self.source(speakers=(GRANDPA, LIVE), recording_ok=None,
                    extra=_quotes((LIVE, QUOTE, '00:14:32')))
        self.build(linked=True)
        html = self.person_page(LIVE)
        self.assertIn('<h2>Recordings</h2>', html)
        self.assertIn(f'documents/interviews/{SESSION}/{SESSION}_{SID}.m4a', html)
        self.assertIn(QUOTE, html)

    def test_person_pages_are_identical_build_to_build(self):
        self.source(extra=_quotes((GRANDPA, QUOTE, '00:14:32')))
        other = Path(self._tmp.name) / 'site2'
        self.build()
        self.build(out=other)
        self.assertEqual(self.person_page(), self.person_page(out=other))


class AnchorSecondsTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(anchor_seconds('00:14:32'), (None, 872))
        self.assertEqual(anchor_seconds('14:32'), (None, 872))
        self.assertEqual(anchor_seconds('cars-whisper, 00:14:32'), ('cars-whisper', 872))
        self.assertEqual(anchor_seconds('1:02:03'), (None, 3723))
        self.assertIsNone(anchor_seconds('page 3'))
        self.assertIsNone(anchor_seconds('line 40'))
        self.assertIsNone(anchor_seconds('14:75'))
        self.assertIsNone(anchor_seconds(''))
        self.assertIsNone(anchor_seconds(None))


class RecordingSuffixTests(unittest.TestCase):
    def test_the_site_and_fha_media_agree_on_what_a_recording_is(self):
        # SPEC §19: any audio or video file is a recording. The site keeps
        # its own copy of the list (tools never import tools).
        import media
        self.assertEqual(site._RECORDING_SUFFIXES, media.MEDIA_EXTENSIONS)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg not installed')
class RealFfmpegTests(unittest.TestCase):
    def test_the_copy_drops_every_tag_and_is_the_same_each_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / 'in.m4a'
            subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-f', 'lavfi',
                            '-i', 'sine=frequency=440:duration=1', '-c:a', 'aac',
                            '-metadata', 'title=Grandpa at the farm',
                            '-metadata', 'location=+40.7128-074.0060/',
                            '-metadata', 'comment=recorded on a phone', str(src)],
                           check=True)
            builder = site._SiteBuilder.__new__(site._SiteBuilder)
            builder._ffmpeg = shutil.which('ffmpeg')
            a, b = tmp / 'a.m4a', tmp / 'b.m4a'
            self.assertTrue(builder._remux_audio(src, a))
            self.assertTrue(builder._remux_audio(src, b))
            self.assertEqual(a.read_bytes(), b.read_bytes())
            def tags(path):
                out = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                                      str(path)], capture_output=True, text=True, check=True).stdout
                return dict(line[4:].split('=', 1) for line in out.splitlines()
                            if line.startswith('TAG:'))
            self.assertEqual(tags(src).get('title'), 'Grandpa at the farm')
            # Only what the container itself needs is left: no title,
            # place, comment, encoder or date.
            structural = {'language', 'handler_name', 'vendor_id', 'major_brand',
                          'minor_version', 'compatible_brands'}
            self.assertLessEqual(set(tags(a)), structural, tags(a))


if __name__ == '__main__':
    unittest.main()
