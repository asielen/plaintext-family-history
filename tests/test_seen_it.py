"""test_seen_it.py - the Seen It browser add-on (seen-it/) and its local helper.

Seen It is a separate, optional add-on: it remembers which newspaper pages and
records a researcher has already looked at, and - only in its "Advanced mode" -
asks a small local helper (seen-it/host/seenit_host.py) which of them the
family archive already holds. These tests cover the helper on synthetic
archives built in temp folders (never a real one), plus the guards that keep a
public copy of the add-on free of anyone's own paths and browsing data:

  HelperSettings        where settings and the data folder live; the data
                        folder is refused inside an archive or a git checkout
  ArchiveConfig         fha.yaml roots -> the folders read (PyYAML and the
                        built-in fallback reader agree)
  ScanKnown             site-data / capture.json / source records -> keys
  Messages              the stdin/stdout requests Chrome sends
  Installer             the launcher and Chrome's helper file
  AddonFiles            manifest, extension ID, node syntax + unit tests
  PrivacyGuard          no owner-specific paths or ids anywhere in seen-it/
  NotVendoredYet        seen-it/ does not ship into archives (a later PR)

Fictional Hartley family / Fairview, Kansas data only.
"""

import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SEEN_IT = ROOT / 'seen-it'
HOST_PY = SEEN_IT / 'host' / 'seenit_host.py'
sys.path.insert(0, str(ROOT / 'tools'))


def _load_host():
    spec = importlib.util.spec_from_file_location('seenit_host', HOST_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


host = _load_host()

SID_A = 'S-0a1b2c3d4e'
SID_B = 'S-5f6g7h8j9k'


def _write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def _json(path, data):
    return _write(path, json.dumps(data))


class _TempHome(unittest.TestCase):
    """Every test gets its own settings folder and per-user data location."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.conf = self.tmp / 'settings'
        # `~` too: the default capture download folder is ~/Downloads/fha-inbox,
        # and a test must never read the real one on the machine running it.
        env = {
            'HOME': str(self.tmp / 'home'),
            'USERPROFILE': str(self.tmp / 'home'),
            'SEENIT_CONFIG_DIR': str(self.conf),
            'LOCALAPPDATA': str(self.tmp / 'localappdata'),
            'XDG_DATA_HOME': str(self.tmp / 'xdg-data'),
            'XDG_CONFIG_HOME': str(self.tmp / 'xdg-config'),
        }
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def make_archive(self, fha_yaml='roots:\n  photos: photos\n  documents: documents\n'):
        archive = self.tmp / 'Hartley Family Archive'
        archive.mkdir(parents=True, exist_ok=True)
        _write(archive / 'fha.yaml', fha_yaml)
        (archive / 'sources').mkdir(exist_ok=True)
        (archive / 'inbox').mkdir(exist_ok=True)
        return archive


# ── settings and the data folder ────────────────────────────────────────────

class HelperSettings(_TempHome):

    def test_default_data_dir_is_per_user_and_outside_archive_and_addon(self):
        archive = self.make_archive()
        d = host.default_data_dir().resolve()
        if os.name == 'nt':
            self.assertEqual(d, (self.tmp / 'localappdata' / 'SeenIt' / 'data').resolve())
        else:
            self.assertEqual(d, (self.tmp / 'xdg-data' / 'seen-it').resolve())
        for forbidden in (archive, SEEN_IT, ROOT):
            self.assertNotEqual(d, forbidden)
            self.assertNotIn(forbidden.resolve(), d.parents)
        settings = {'archive': str(archive)}
        self.assertEqual(host.resolve_data_dir(settings).resolve(), d)

    def test_without_a_setting_the_data_dir_is_never_next_to_the_addon(self):
        # The old helper put the data at ../../<name>-data beside the add-on,
        # which in a clone of this repo is inside the git checkout.
        d = host.resolve_data_dir({})
        self.assertNotIn(SEEN_IT.resolve(), d.resolve().parents)
        self.assertNotIn(ROOT.resolve(), d.resolve().parents)

    def test_data_dir_inside_the_archive_is_refused(self):
        archive = self.make_archive()
        with self.assertRaises(host.SettingsError) as cm:
            host.resolve_data_dir({'archive': str(archive), 'data_dir': str(archive / 'browsing-data')})
        self.assertIn('inside your family archive', str(cm.exception))

    def test_data_dir_inside_any_archive_or_git_checkout_is_refused(self):
        other = self.tmp / 'Another Archive'
        _write(other / 'fha.yaml', 'roots: {}\n')
        self.assertIn('family archive', host.data_dir_problem(other / 'data'))
        repo = self.tmp / 'some-clone'
        (repo / '.git').mkdir(parents=True)
        self.assertIn('git folder', host.data_dir_problem(repo / 'seen-it' / 'data'))
        # this very repo, too: a data folder in seen-it/ would be committed
        self.assertIsNotNone(host.data_dir_problem(SEEN_IT / 'data'))

    def test_a_hand_written_relative_folder_is_refused(self):
        # relative to wherever the browser starts the helper: maybe the add-on folder
        with self.assertRaises(host.SettingsError) as cm:
            host.resolve_data_dir({'data_dir': 'data'})
        self.assertIn('full folder path', str(cm.exception))
        self.assertIn('full path', host.archive_problem('Hartley Family Archive'))

    def test_a_folder_outside_is_accepted_and_saved(self):
        archive = self.make_archive()
        synced = self.tmp / 'Synced' / 'Seen It data'
        info = host._set_config({'archive': str(archive), 'dataDir': str(synced)}, {})
        self.assertEqual(Path(info['dataDir']), synced)
        self.assertTrue(synced.is_dir())
        saved = json.loads(host.config_path().read_text(encoding='utf-8'))
        self.assertEqual(Path(saved['archive']), archive)
        self.assertEqual(Path(saved['data_dir']), synced)
        self.assertEqual(host.config_path(), self.conf / 'config.json')

    def test_an_unusable_setting_saves_nothing(self):
        not_archive = self.tmp / 'Just a folder'
        not_archive.mkdir()
        with self.assertRaises(host.SettingsError) as cm:
            host._set_config({'archive': str(not_archive)}, {})
        self.assertIn('fha.yaml', str(cm.exception))
        archive = self.make_archive()
        with self.assertRaises(host.SettingsError):
            host._set_config({'archive': str(archive), 'dataDir': str(archive / 'data')}, {})
        self.assertFalse(host.config_path().exists())

    def test_empty_value_clears_back_to_default(self):
        synced = self.tmp / 'Synced'
        host._set_config({'dataDir': str(synced)}, {})
        info = host._set_config({'dataDir': ''}, host.load_settings())
        self.assertFalse(info['dataDirSet'])
        self.assertEqual(Path(info['dataDir']).resolve(), host.default_data_dir().resolve())

    def test_unreadable_settings_file_names_the_fix(self):
        _write(host.config_path(), '{not json')
        with self.assertRaises(host.SettingsError) as cm:
            host.load_settings()
        self.assertIn('run the installer again', str(cm.exception))

    def test_setup_keeps_the_other_setting(self):
        archive = self.make_archive()
        out = io.StringIO()
        self.assertEqual(host.setup(archive=str(archive), out=out), 0)
        synced = self.tmp / 'Synced'
        self.assertEqual(host.setup(data_dir=str(synced), out=out), 0)
        saved = host.load_settings()
        self.assertEqual(Path(saved['archive']), archive)
        self.assertEqual(Path(saved['data_dir']), synced)
        bad = io.StringIO()
        self.assertEqual(host.setup(archive=str(self.tmp / 'nowhere'), out=bad), 2)
        self.assertIn('was not found', bad.getvalue())


# ── the archive's fha.yaml ──────────────────────────────────────────────────

class ArchiveConfig(_TempHome):

    def test_roots_from_fha_yaml_external_and_relative(self):
        docs = self.tmp / 'Drive' / 'Hartley Documents'
        archive = self.make_archive(
            'site:\n  archive_name: The Hartley Family Archive\n'
            'roots:\n'
            '  photos: %s/Photos   # the photo drive\n'
            '  documents: "%s"\n'
            '  inbox: incoming\n'
            'root_person: P-0a1b2c3d4e\n' % (self.tmp.as_posix(), docs.as_posix()))
        folders = host.scan_folders({'archive': str(archive)})
        self.assertEqual([Path(p) for p in folders['filed']], [docs])
        self.assertEqual([Path(p) for p in folders['sources']], [archive / 'sources'])
        self.assertEqual(Path(folders['captured'][0]), archive / 'incoming')

    def test_missing_roots_mean_the_folders_inside_the_archive(self):
        archive = self.make_archive('root_person: P-0a1b2c3d4e\n')
        folders = host.scan_folders({'archive': str(archive)})
        self.assertEqual(Path(folders['filed'][0]), archive / 'documents')
        self.assertEqual(Path(folders['captured'][0]), archive / 'inbox')

    def test_capture_download_folder_default_fha_yaml_and_override(self):
        archive = self.make_archive()
        home = self.tmp / 'home'
        folders = host.scan_folders({'archive': str(archive)})
        dl = str(home / 'Downloads' / 'fha-inbox')
        self.assertEqual(folders['captured'][1:], [dl, dl + '-*'])
        archive = self.make_archive('roots:\n  documents: documents\ncapture_staging: ~/Captures/fha\n')
        cap = str(home / 'Captures' / 'fha')
        self.assertEqual(host.scan_folders({'archive': str(archive)})['captured'][1:], [cap, cap + '-*'])
        over = host.scan_folders({'archive': str(archive), 'capture_staging': str(self.tmp / 'dl')})
        self.assertEqual(over['captured'][1:], [str(self.tmp / 'dl'), str(self.tmp / 'dl') + '-*'])

    def test_known_config_adds_folders(self):
        archive = self.make_archive()
        data = self.tmp / 'data'
        extra = str(self.tmp / 'Old Scans')
        _json(data / 'known-config.json', {'filed': [extra], 'captured': str(self.tmp / 'more-*')})
        folders = host.scan_folders({'archive': str(archive)}, data)
        self.assertEqual(Path(folders['filed'][0]), archive / 'documents')
        self.assertIn(extra, folders['filed'])
        self.assertIn(str(self.tmp / 'more-*'), folders['captured'])
        # known-config alone (no archive) is enough to scan
        alone = host.scan_folders({}, data)
        self.assertEqual(alone['filed'], [extra])

    def test_no_archive_and_nothing_else_names_the_fix(self):
        with self.assertRaises(host.SettingsError) as cm:
            host.scan_folders({})
        self.assertIn('Archive folder', str(cm.exception))

    def test_archive_folder_problems_are_plain(self):
        self.assertIn('was not found', host.archive_problem(str(self.tmp / 'gone')))
        plain = self.tmp / 'Hartley letters'
        plain.mkdir()
        self.assertIn('fha.yaml', host.archive_problem(str(plain)))
        with self.assertRaises(host.SettingsError):
            host.scan_folders({'archive': str(plain)})

    def test_fallback_reader_matches_pyyaml_on_the_shipped_files(self):
        import yaml
        for f in (ROOT / 'archive-template' / 'fha.yaml', ROOT / 'example-archive' / 'fha.yaml'):
            text = f.read_text(encoding='utf-8')
            full = yaml.safe_load(text)
            mini = host._parse_fha_yaml_minimal(text)
            self.assertEqual(mini.get('roots'), {k: str(v) for k, v in full['roots'].items()}, f)

    def test_fallback_reader_forms(self):
        import yaml
        cases = [
            'roots:\n  documents: E:/Hartley Docs   # comment\n  inbox: \'in box\'\n',
            'roots:\n  # a comment line\n  documents: "C:\\\\Users\\\\you\\\\Docs"\n\nbiography:\n  style: chronicle\n',
            'roots: {documents: docs, inbox: inbox}\ncapture_staging: ~/Downloads/fha-inbox\n',
            "roots:\n  documents: 'it''s here'\nphotos_ignore:\n  - a\n  - b\n",
            'site:\n  documents: not-a-root\nroots:\n  documents: real\n',
        ]
        for text in cases:
            full = yaml.safe_load(text)
            mini = host._parse_fha_yaml_minimal(text)
            self.assertEqual(mini['roots'], {k: str(v) for k, v in full['roots'].items()}, text)
            if 'capture_staging' in full:
                self.assertEqual(mini['capture_staging'], full['capture_staging'])

    def test_both_readers_give_the_same_folders(self):
        docs = self.tmp / 'External Docs'
        archive = self.make_archive('\ufeffroots:\n  documents: %s\n  inbox: in\n' % docs.as_posix())
        with_yaml = host.read_fha_yaml(archive, use_yaml=True)
        without = host.read_fha_yaml(archive, use_yaml=False)
        for alias in ('documents', 'inbox', 'photos'):
            self.assertEqual(host.resolve_root(alias, with_yaml, archive),
                             host.resolve_root(alias, without, archive))

    def test_resolve_root_agrees_with_the_archive_tools(self):
        import _lib
        cfg = {'roots': {'documents': (self.tmp / 'abs').as_posix(), 'inbox': 'rel/in'}}
        archive = self.tmp / 'arch'
        for alias in ('documents', 'inbox', 'photos'):
            self.assertEqual(host.resolve_root(alias, cfg, archive),
                             _lib.resolve_path(alias, cfg, archive))

    def test_broken_fha_yaml_is_an_error_not_an_empty_archive(self):
        archive = self.make_archive('roots:\n  documents: [unclosed\n')
        with self.assertRaises(host.SettingsError) as cm:
            host.scan_folders({'archive': str(archive)})
        self.assertIn('fha doctor', str(cm.exception))


# ── scanning what the archive holds ─────────────────────────────────────────

class ScanKnown(_TempHome):

    def build(self):
        ext_docs = self.tmp / 'Drive' / 'Docs'
        archive = self.make_archive('roots:\n  documents: %s\n' % ext_docs.as_posix())
        # filed: site-data files beside processed documents carry the S-id
        _json(ext_docs / 'newspapers' / 'hartley-wedding' / ('clipping-site-data_%s.json' % SID_A),
              {'clipping': {'id': 1101}, 'page': {'id': 2202}})
        _write(ext_docs / 'newspapers' / 'hartley-wedding' / ('hartley-wedding-page_%s.jpg' % SID_A), 'jpg')
        # a list of records matched to a source after the fact speaks for its
        # images only when it says imageSaved (one file cannot say whose it is)
        _json(ext_docs / 'census' / '1900' / ('ancestry-records-site-data_%s.json' % SID_B),
              {'records': [{'dbId': 7602, 'recordId': 3303, 'imageId': 4404, 'imageSaved': True}, 'junk']})
        _json(ext_docs / 'vital' / 'fs' / 'record-site-data.json',
              {'record': {'fsId': 'abcd-123', 'imageId': 'wxyz-9'}})
        _write(ext_docs / 'vital' / 'fs' / 'record.jpg', 'jpg')
        _json(ext_docs / 'burial' / 'x' / 'memorial-site-data.json',
              {'record': {'memorialId': 5505}, 'photos': [{'photoId': 77, 'file': 'grave-1.jpg'}], 'photosTotal': 1})
        _write(ext_docs / 'burial' / 'x' / 'grave-1.jpg', 'jpg')
        _write(ext_docs / 'broken' / 'bad-site-data.json', '{nope')
        # sources: a record that cites a memorial it has not captured yet
        _write(archive / 'sources' / 'burial' / ('thomas-hartley-grave_%s.md' % SID_A),
               '---\nid: %s\ntitle: Thomas Hartley grave\n---\n'
               'See https://www.findagrave.com/memorial/6606/thomas-hartley\n' % SID_A)
        # captured: inbox bundle, and one the inbox set aside as processed
        _json(archive / 'inbox' / 'hartley-obit' / 'capture.json',
              {'url': 'https://www.newspapers.com/article/the-fairview-courier-hartley/7707/'})
        _json(archive / 'inbox' / 'processed' / 'dup' / 'capture.json',
              {'url': 'https://www.familysearch.org/ark:/61903/1:1:efgh-456'})
        # captured AND filed: filed wins
        _json(archive / 'inbox' / 'again' / 'capture.json',
              {'url': 'https://www.newspapers.com/clip/1101/hartley-wedding/'})
        staging = self.tmp / 'Downloads' / 'fha-inbox'
        _json(staging / 'b1-20260101-abc123' / 'capture.json',
              {'url': 'https://www.ancestry.com/discoveryui-content/view/8808:7602'})
        _json(self.tmp / 'Downloads' / 'fha-inbox-anc01' / 'b2' / 'capture.json',
              {'url': 'https://www.ancestry.com/search/collections/7602/records/9909'})
        _write(staging / 'b1-20260101-abc123' / 'page.json', '{"url": "ignored, not a bundle data file"}')
        return archive, {'archive': str(archive), 'capture_staging': str(staging)}

    def test_every_kind_of_file_becomes_a_key(self):
        _archive, settings = self.build()
        items, missing = host.scan_known(host.scan_folders(settings))
        self.assertEqual(missing, [])
        self.assertEqual(items['clip:1101'], {'state': 'filed', 'where': 'hartley-wedding', 'sid': SID_A})
        self.assertEqual(items['page:2202']['state'], 'filed')
        self.assertEqual(items['rec:7602:3303']['sid'], SID_B)
        self.assertEqual(items['img:7602:4404']['state'], 'filed')
        self.assertEqual(items['fsrec:ABCD-123']['state'], 'filed')
        self.assertNotIn('sid', items['fsrec:ABCD-123'])
        self.assertEqual(items['fsimg:WXYZ-9']['state'], 'filed')
        self.assertEqual(items['fag:5505']['state'], 'filed')
        self.assertEqual(items['fagphoto:77']['state'], 'filed')
        self.assertEqual(items['fagidx:6606'], {'state': 'filed', 'where': 'burial', 'sid': SID_A})
        self.assertEqual(items['clip:7707']['state'], 'captured')
        self.assertEqual(items['fsrec:EFGH-456']['state'], 'processed')
        self.assertEqual(items['rec:7602:8808']['state'], 'captured')
        self.assertEqual(items['rec:7602:9909']['state'], 'captured')  # a -* chunk folder
        self.assertEqual(len(items), 13)

    def test_where_is_a_folder_name_never_a_path(self):
        _archive, settings = self.build()
        items, _ = host.scan_known(host.scan_folders(settings))
        for entry in items.values():
            self.assertNotIn('/', entry['where'])
            self.assertNotIn('\\', entry['where'])

    def test_an_unreachable_documents_drive_is_reported(self):
        archive = self.make_archive('roots:\n  documents: %s/Unplugged\n' % self.tmp.as_posix())
        items, missing = host.scan_known(host.scan_folders({'archive': str(archive)}))
        self.assertEqual(items, {})
        self.assertEqual([Path(m) for m in missing], [self.tmp / 'Unplugged'])

    def test_known_answers_in_pages_from_one_scan(self):
        _archive, settings = self.build()
        data = host.resolve_data_dir(settings)
        with mock.patch.object(host, 'KNOWN_PAGE', 5):
            first = host.handle({'op': 'known', 'offset': 0}, settings)
            self.assertEqual(first['total'], 13)
            self.assertEqual(len(first['items']), 5)
            # later pages reuse the cached scan: a file added now is not seen
            _json(Path(settings['capture_staging']) / 'late' / 'capture.json',
                  {'url': 'https://www.findagrave.com/memorial/1234/x'})
            got = dict(first['items'])
            offset = first['next']
            while offset < first['total']:
                page = host.handle({'op': 'known', 'offset': offset}, settings)
                got.update(page['items'])
                offset = page['next']
        self.assertEqual(len(got), 13)
        self.assertTrue((self.conf / 'known-cache.json').is_file())
        self.assertFalse((data / 'known-cache.json').exists(), 'the cache stays out of the synced data folder')


# ── the messages Chrome sends ───────────────────────────────────────────────

class Messages(_TempHome):

    def test_framing_round_trip(self):
        buf = io.BytesIO()
        host.send({'op': 'ping', 'name': 'Hartley – Fairview'}, buf)
        buf.seek(0)
        self.assertEqual(host.read_message(buf), {'op': 'ping', 'name': 'Hartley – Fairview'})
        self.assertIsNone(host.read_message(io.BytesIO(b'')))

    def test_write_list_read_in_the_data_folder(self):
        settings = {'data_dir': str(self.tmp / 'Synced')}
        text = json.dumps({'app': 'newspapers-seen-it', 'views': {'101': {'t': [1]}}}) + 'x' * 500
        w = host.handle({'op': 'write', 'name': 'seen-it-abc123.json', 'text': text}, settings)
        self.assertEqual(Path(w['path']), self.tmp / 'Synced' / 'seen-it-abc123.json')
        listing = host.handle({'op': 'list'}, settings)
        self.assertEqual([f['name'] for f in listing['files']], ['seen-it-abc123.json'])
        with mock.patch.object(host, 'CHUNK_CHARS', 100):
            got, offset = '', 0
            while True:
                r = host.handle({'op': 'read', 'name': 'seen-it-abc123.json', 'offset': offset}, settings)
                got += r['text']
                if r['next'] >= r['total']:
                    break
                offset = r['next']
        self.assertEqual(got, text)
        self.assertEqual(sorted(p.name for p in (self.tmp / 'Synced').iterdir()), ['seen-it-abc123.json'])

    def test_only_seen_it_files_can_be_touched(self):
        settings = {'data_dir': str(self.tmp / 'd')}
        for bad in ('../fha.yaml', 'notes.md', 'seen-it-ABC.json', 'seen-it-x/../../y.json', None):
            with self.assertRaises(ValueError):
                host.handle({'op': 'write', 'name': bad, 'text': '{}'}, settings)
            with self.assertRaises(ValueError):
                host.handle({'op': 'read', 'name': bad}, settings)

    def test_config_and_set_config(self):
        archive = self.make_archive()
        info = host.handle({'op': 'config'}, {})
        self.assertEqual(info['archive'], '')
        self.assertFalse(info['dataDirSet'])
        self.assertEqual(Path(info['configFile']), self.conf / 'config.json')
        info = host.handle({'op': 'setConfig', 'archive': '"%s"' % archive}, {})
        self.assertEqual(Path(info['archive']), archive)
        self.assertIsNone(info['archiveProblem'])
        self.assertEqual(Path(host.load_settings()['archive']), archive)

    def test_main_reports_errors_instead_of_crashing(self):
        stdin = io.BytesIO()
        host.send({'op': 'nonsense'}, stdin)
        host.send({'op': 'known'}, stdin)  # no archive set
        stdin.seek(0)
        stdout = io.BytesIO()
        with mock.patch.object(host.sys, 'stdin', mock.Mock(buffer=stdin)), \
                mock.patch.object(host.sys, 'stdout', mock.Mock(buffer=stdout)):
            host.main()
        stdout.seek(0)
        first, second = host.read_message(stdout), host.read_message(stdout)
        self.assertFalse(first['ok'])
        self.assertIn('unknown request', first['error'])
        self.assertFalse(second['ok'])
        self.assertIn('Archive folder', second['error'])

    def test_started_the_way_chrome_starts_it(self):
        # Chrome runs the helper with the add-on's origin (and on Windows a
        # --parent-window handle) as arguments and talks over stdin/stdout.
        archive = self.make_archive()
        _json(archive / 'inbox' / 'b' / 'capture.json', {'url': 'https://www.findagrave.com/memorial/42/x'})
        stdin = io.BytesIO()
        host.send({'op': 'setConfig', 'archive': str(archive)}, stdin)
        host.send({'op': 'known', 'offset': 0}, stdin)
        host.send({'op': 'ping'}, stdin)
        r = subprocess.run(
            [sys.executable, str(HOST_PY), 'chrome-extension://cennkaddolfbhiabacldomegncpdphed/',
             '--parent-window=0'],
            input=stdin.getvalue(), capture_output=True, env=dict(os.environ), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = io.BytesIO(r.stdout)
        config, known, ping = host.read_message(out), host.read_message(out), host.read_message(out)
        self.assertTrue(config['ok'], config)
        self.assertEqual(known['items'], {'fagidx:42': {'state': 'captured', 'where': 'b'}})
        self.assertEqual(Path(ping['dir']).resolve(), host.default_data_dir().resolve())

    def test_setup_from_the_command_line(self):
        archive = self.make_archive()
        r = subprocess.run([sys.executable, str(HOST_PY), '--setup', '--archive', str(archive)],
                           capture_output=True, text=True, env=dict(os.environ), timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('Family archive: %s' % archive, r.stdout)
        self.assertEqual(Path(host.load_settings()['archive']), archive)


# ── installer pieces ────────────────────────────────────────────────────────

class Installer(_TempHome):

    def test_host_manifest_allows_only_seen_it(self):
        m = host.host_manifest(self.tmp / 'seenit_host')
        self.assertEqual(m['name'], 'com.genealogy.seenit')
        self.assertEqual(m['type'], 'stdio')
        self.assertEqual(m['allowed_origins'], ['chrome-extension://cennkaddolfbhiabacldomegncpdphed/'])
        self.assertTrue(Path(m['path']).is_absolute())

    def test_launchers_name_python_and_survive_odd_folder_names(self):
        odd = self.tmp / "Bob's 100% & co"
        py = odd / 'python.exe'
        script = odd / 'seenit_host.py'
        cmd = host.write_launcher(self.tmp / 'w', python=py, script=script, windows=True)
        body = cmd.read_bytes().decode('utf-8')
        self.assertTrue(body.startswith('@echo off\r\n'))
        self.assertIn('100%% & co', body)
        self.assertIn('" -u "', body)
        self.assertTrue(body.rstrip().endswith('%*'))
        sh = host.write_launcher(self.tmp / 'p', python=py, script=script, windows=False)
        body = sh.read_bytes().decode('utf-8')
        self.assertTrue(body.startswith('#!/bin/sh\nexec '))
        self.assertIn("Bob'\\''s", body)
        self.assertNotIn('\r', body)
        if os.name != 'nt' and shutil.which('sh'):
            fake_py = self.tmp / "Bob's py"
            _write(fake_py, '#!/bin/sh\nprintf "%s|" "$@"\n')
            fake_py.chmod(0o755)
            launcher = host.write_launcher(self.tmp / 'q', python=fake_py, script=script, windows=False)
            out = subprocess.run([str(launcher), 'chrome-extension://x/'], capture_output=True, text=True)
            self.assertEqual(out.stdout, '-u|%s|chrome-extension://x/|' % script.resolve())

    def test_browser_folders_mac_and_linux(self):
        home = self.tmp / 'home'
        mac = host.browser_manifest_dirs(home, 'darwin')
        self.assertEqual(mac, [home / 'Library/Application Support/Google/Chrome/NativeMessagingHosts'])
        (home / 'Library/Application Support/Microsoft Edge').mkdir(parents=True)
        self.assertEqual(host.browser_manifest_dirs(home, 'darwin')[1],
                         home / 'Library/Application Support/Microsoft Edge/NativeMessagingHosts')
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': ''}):
            linux = host.browser_manifest_dirs(home, 'linux')
        self.assertEqual(linux, [home / '.config/google-chrome/NativeMessagingHosts'])

    def test_install_on_mac_or_linux_writes_settings_launcher_and_browser_file(self):
        archive = self.make_archive()
        home = self.tmp / 'home'
        out = io.StringIO()
        real_write_launcher = host.write_launcher
        # Never touch the real registry or browser folders: both are stubbed,
        # and the launcher is the POSIX one on every platform.
        with mock.patch.object(host, 'browser_manifest_dirs', lambda: [home / 'NativeMessagingHosts']), \
                mock.patch.object(host, 'register_windows', lambda path: []), \
                mock.patch.object(host, 'write_launcher',
                                  lambda folder: real_write_launcher(folder, windows=False)):
            code = host.install(str(archive), None, out)
        if os.name == 'nt':
            files = [self.conf / 'com.genealogy.seenit.json']
        else:
            files = [home / 'NativeMessagingHosts' / 'com.genealogy.seenit.json']
        self.assertEqual(code, 0, out.getvalue())
        for f in files:
            m = json.loads(f.read_text(encoding='utf-8'))
            self.assertEqual(Path(m['path']), (self.conf / 'seenit_host').resolve())
        self.assertEqual(Path(host.load_settings()['archive']), archive)
        self.assertIn('reload', out.getvalue())

    @unittest.skipUnless(shutil.which('sh'), 'no POSIX shell here')
    def test_install_sh_parses(self):
        r = subprocess.run(['sh', '-n', str(SEEN_IT / 'host' / 'install.sh')], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_install_cmd_hands_everything_to_the_helper(self):
        text = (SEEN_IT / 'host' / 'install.cmd').read_text(encoding='utf-8')
        self.assertIn('seenit_host.py" --install', text)
        self.assertNotIn('reg add', text)  # the registry is written by register_windows
        self.assertIn('Python was not found', text)

    def test_install_refuses_before_registering_anything(self):
        out = io.StringIO()
        with mock.patch.object(host, 'register_windows') as reg, \
                mock.patch.object(host, 'browser_manifest_dirs') as dirs:
            code = host.install(str(self.tmp / 'no such archive'), None, out)
        self.assertEqual(code, 2)
        reg.assert_not_called()
        dirs.assert_not_called()
        self.assertFalse(self.conf.exists() and any(self.conf.iterdir()))


# ── the add-on's own files ──────────────────────────────────────────────────

def _extension_id(key_b64):
    """Chrome's ID for an unpacked add-on with a manifest `key`: the first 32
    hex digits of the key's SHA-256, written with the letters a-p for 0-f."""
    digest = hashlib.sha256(base64.b64decode(key_b64)).hexdigest()[:32]
    return ''.join(chr(ord('a') + int(c, 16)) for c in digest)


class AddonFiles(unittest.TestCase):

    def setUp(self):
        self.manifest = json.loads((SEEN_IT / 'manifest.json').read_text(encoding='utf-8'))

    def test_manifest_key_pins_the_id_the_helper_allows(self):
        self.assertEqual(_extension_id(self.manifest['key']), host.EXTENSION_ID)
        readme = (SEEN_IT / 'README.md').read_text(encoding='utf-8')
        self.assertIn(host.EXTENSION_ID, readme)

    def test_every_file_the_manifest_and_popup_name_exists(self):
        names = [self.manifest['background']['service_worker'], self.manifest['action']['default_popup']]
        for cs in self.manifest['content_scripts']:
            names += cs.get('js', []) + cs.get('css', [])
        popup = (SEEN_IT / 'popup.html').read_text(encoding='utf-8')
        names += re.findall(r'<script src="([^"]+)"', popup)
        for name in names:
            self.assertTrue((SEEN_IT / name).is_file(), name)

    def test_least_privilege_shape(self):
        self.assertEqual(self.manifest['manifest_version'], 3)
        self.assertEqual(self.manifest['optional_permissions'], ['nativeMessaging'])
        self.assertNotIn('nativeMessaging', self.manifest['permissions'])
        self.assertNotIn('<all_urls>', json.dumps(self.manifest))
        self.assertEqual(json.loads((SEEN_IT / 'package.json').read_text(encoding='utf-8'))['version'],
                         self.manifest['version'])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_every_js_file_parses(self):
        for f in sorted(SEEN_IT.rglob('*.js')):
            r = subprocess.run(['node', '--check', str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, '%s: %s' % (f.name, r.stderr))

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_node_unit_tests_pass(self):
        tests = sorted(str(p) for p in (SEEN_IT / 'tests').glob('test-*.js'))
        self.assertTrue(tests)
        r = subprocess.run(['node', '--test', *tests], capture_output=True, text=True, cwd=SEEN_IT)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-2000:])

    def test_line_endings(self):
        for f in SEEN_IT.rglob('*'):
            if not f.is_file() or '__pycache__' in f.parts:
                continue
            data = f.read_bytes()
            if f.suffix == '.cmd':
                self.assertNotIn(b'\n', data.replace(b'\r\n', b''), f.name)
            else:
                self.assertNotIn(b'\r', data, f.name)


# ── privacy: nobody's own paths, names or ids in the public copy ────────────

# The project's own address is the one place the owner's GitHub name may appear.
_ALLOWED = ('github.com/asielen/plaintext-family-history',)
_FORBIDDEN = [
    (re.compile(r'Genealogy'), 'a folder name from a real machine'),
    (re.compile(r'Andrew', re.I), "a person's name"),
    (re.compile(r'asielen', re.I), 'a GitHub username outside the project URL'),
    # (?![\\/]) makes the slashes all-or-nothing, so the placeholder lookahead
    # cannot be dodged by backtracking one slash of a doubled JSON "C:\\Users".
    (re.compile(r'(?<![A-Za-z])[A-Za-z]:[\\/]+(?![\\/])(?!Users[\\/]+you\b|path[\\/]+to\b)'),
     'a drive-letter path'),
    (re.compile(r'/(?:Users|home)/(?!you\b)[A-Za-z]'), 'a home folder that is not the placeholder "you"'),
    (re.compile(r'(?<![0-9A-Za-z#.])[0-9]{7,}(?![0-9])'), 'a long number that could be a real record id'),
]
_LONG_NUMBERS_OK = {'2147483000'}  # a CSS z-index


def privacy_findings(root):
    found = []
    for f in sorted(Path(root).rglob('*')):
        if not f.is_file() or '__pycache__' in f.parts or 'node_modules' in f.parts:
            continue
        try:
            text = f.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        if f.name == 'manifest.json':
            text = re.sub(r'"key":\s*"[^"]*"', '"key": ""', text)  # the public key is base64, not data
        for allowed in _ALLOWED:
            text = text.replace(allowed, '')
        for n, line in enumerate(text.splitlines(), 1):
            for pattern, why in _FORBIDDEN:
                for m in pattern.finditer(line):
                    if m.group(0) in _LONG_NUMBERS_OK:
                        continue
                    found.append('%s:%d: %s (%r)' % (f.relative_to(root).as_posix(), n, why, m.group(0)))
    return found


class PrivacyGuard(unittest.TestCase):

    def test_seen_it_carries_no_owner_specific_paths_or_ids(self):
        self.assertEqual(privacy_findings(SEEN_IT), [])

    def test_the_guard_catches_what_it_is_for(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d) / 'a.py', "DATA = r'Q:/Family Genealogy/Inbox'\n")
            _write(Path(d) / 'b.js', "// see https://www.findagrave.com/memorial/123456789/x\n")
            _write(Path(d) / 'c.md', 'Load from /Users/margaret/Downloads, or C:\\Users\\you\\Docs.\n')
            found = privacy_findings(d)
        self.assertEqual(len([x for x in found if x.startswith('a.py')]), 2, found)
        self.assertTrue(any(x.startswith('b.js') and 'long number' in x for x in found), found)
        self.assertEqual(len([x for x in found if x.startswith('c.md')]), 1, found)

    def test_helper_defaults_name_no_machine(self):
        src = HOST_PY.read_text(encoding='utf-8')
        self.assertNotIn('KNOWN_DEFAULTS', src)
        self.assertEqual(host.DEFAULT_STAGING, '~/Downloads/fha-inbox')

    @unittest.skipUnless((ROOT / '.git').exists(), 'requires a git checkout')
    def test_a_data_folder_made_in_seen_it_is_never_committed(self):
        for rel in ('seen-it/data/seen-it-abc123.json', 'seen-it/seen-it-abc123.json',
                    'seen-it/host/seen-it-abc123.json', 'seen-it/known-config.json',
                    'seen-it/config.json', 'seen-it/old-data/x.json',
                    'seen-it/my-browsing-data/x.json', 'seen-it/seen-it-backup-2026-01-15.json',
                    'seen-it/.known-cache.json', 'seen-it/known-cache.json'):
            r = subprocess.run(['git', 'check-ignore', '-q', rel], cwd=ROOT)
            self.assertEqual(r.returncode, 0, '%s must be gitignored' % rel)
        for rel in ('seen-it/manifest.json', 'seen-it/package.json', 'seen-it/host/seenit_host.py'):
            r = subprocess.run(['git', 'check-ignore', '-q', rel], cwd=ROOT)
            self.assertEqual(r.returncode, 1, '%s must stay tracked' % rel)


# ── PR #212 review findings (each test fails on 20dcf78) ────────────────────

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)  # a hidden console of its own, as Chrome gives


class InstallerInput(_TempHome):
    """Audit #1 and #6: a folder as people actually paste or drag it."""

    def _fake_install_cmd(self):
        """install.cmd beside a stand-in helper that only records its arguments."""
        folder = self.tmp / 'Seen It host'
        folder.mkdir()
        shutil.copy(SEEN_IT / 'host' / 'install.cmd', folder / 'install.cmd')
        _write(folder / 'seenit_host.py',
               'import json, os, sys\n'
               'open(os.environ["SEENIT_FAKE_OUT"], "w", encoding="utf-8").write(json.dumps(sys.argv[1:]))\n')
        return folder / 'install.cmd'

    def _run_cmd(self, script, typed=None, args=()):
        out = self.tmp / 'args.json'
        env = dict(os.environ, SEENIT_FAKE_OUT=str(out))
        # The whole line in one more pair of quotes, the way Explorer runs a
        # .cmd a folder was dropped onto (cmd /c strips the outer pair).
        line = 'cmd /c ""%s"%s"' % (script, ''.join(' "%s"' % a for a in args))
        r = subprocess.run(line, input=(typed or '') + '\r\n\r\n',
                           capture_output=True, text=True, env=env, timeout=60,
                           creationflags=_NO_WINDOW)
        return r, (json.loads(out.read_text(encoding='utf-8')) if out.is_file() else None)

    @unittest.skipUnless(os.name == 'nt', 'install.cmd is the Windows installer')
    def test_install_cmd_takes_a_folder_pasted_with_quotes_and_spaces(self):
        # Explorer's Ctrl+Shift+C gives "C:\...\Family Archive" - quotes and a space.
        script = self._fake_install_cmd()
        archive = str(self.tmp / 'Hartley Family Archive')
        r, got = self._run_cmd(script, typed='"%s"' % archive)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(got, ['--install', '--archive', archive])
        for typed, want in ((archive, ['--install', '--archive', archive]),  # typed plainly
                            ('', ['--install'])):                            # Enter: skip
            (self.tmp / 'args.json').unlink(missing_ok=True)
            r, got = self._run_cmd(script, typed=typed)
            self.assertEqual((r.returncode, got), (0, want), r.stdout + r.stderr)
        (self.tmp / 'args.json').unlink(missing_ok=True)
        r, got = self._run_cmd(script, args=[archive])  # dropped onto install.cmd
        self.assertEqual((r.returncode, got), (0, ['--install', '--archive', archive]), r.stdout + r.stderr)

    def test_dragged_and_pasted_folders_come_back_as_the_folder(self):
        clean = host.clean_typed_path
        # GNOME Terminal: single quotes, a trailing space; an apostrophe as '\''
        self.assertEqual(clean("'/home/you/Hartley Family Archive' ", posix=True), '/home/you/Hartley Family Archive')
        self.assertEqual(clean("'/home/you/Mom'\\''s Archive' ", posix=True), "/home/you/Mom's Archive")
        # macOS Terminal: every space and apostrophe backslash-escaped, a trailing space
        self.assertEqual(clean("/Users/you/Mom\\'s\\ Archive ", posix=True), "/Users/you/Mom's Archive")
        self.assertEqual(clean('"/Users/you/Hartley Archive"', posix=True), '/Users/you/Hartley Archive')
        # typed plainly, no shell quoting at all
        self.assertEqual(clean('/home/you/Hartley Family Archive', posix=True), '/home/you/Hartley Family Archive')
        self.assertEqual(clean("/home/you/Mom's Archive", posix=True), "/home/you/Mom's Archive")
        # Windows: quotes go, backslashes stay - and a "...\" ending reaches us as ...\"
        self.assertEqual(clean('  "C:\\Users\\you\\Hartley Archive"  ', posix=False), 'C:\\Users\\you\\Hartley Archive')
        self.assertEqual(clean('C:\\Users\\you\\Hartley Archive"', posix=False), 'C:\\Users\\you\\Hartley Archive')
        self.assertEqual(clean('   ', posix=True), '')

    def test_the_helper_cleans_what_the_installer_hands_it(self):
        archive = self.make_archive()
        if os.name == 'nt':
            typed = '"%s"  ' % archive
        else:
            typed = "'%s' " % archive
        info = host._set_config({'archive': typed}, {})
        self.assertEqual(Path(info['archive']), archive)
        # a blank answer to the installer's question keeps what is set
        self.assertEqual(host.setup(archive='   ', out=io.StringIO()), 0)
        self.assertEqual(Path(host.load_settings()['archive']), archive)


class LauncherEncoding(_TempHome):
    """Audit #2: cmd.exe reads a batch file in the console code page."""

    @unittest.skipUnless(os.name == 'nt', 'the .cmd launcher is the Windows one')
    def test_launcher_runs_from_a_folder_with_accented_letters(self):
        folder = self.tmp / 'Müller Ünïcode'
        script = _write(folder / 'fake_host.py', 'import sys\nprint("ok", sys.argv[1])\n')
        launcher = host.write_launcher(self.tmp / 'Ärchiv launcher', python=sys.executable, script=script,
                                       windows=True)
        r = subprocess.run([str(launcher), 'chrome-extension://x/'], capture_output=True, timeout=60,
                           creationflags=_NO_WINDOW)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.decode('utf-8', 'replace').strip(), 'ok chrome-extension://x/')


class DataFolderNotInArchiveRoots(_TempHome):
    """Audit #4: an archive's photos/documents/inbox often live on another drive."""

    def test_data_folder_under_any_archive_root_or_the_download_folder_is_refused(self):
        drive = self.tmp / 'Drive'
        archive = self.make_archive(
            'roots:\n  photos: %s\n  documents: %s\n  inbox: %s\n'
            % ((drive / 'Photos').as_posix(), (drive / 'Docs').as_posix(), (drive / 'Inbox').as_posix()))
        settings = {'archive': str(archive)}
        for root, word in (('Photos', 'photos'), ('Docs', 'documents'), ('Inbox', 'inbox')):
            with self.assertRaises(host.SettingsError, msg=root) as cm:
                host.resolve_data_dir(dict(settings, data_dir=str(drive / root / 'seen-it')))
            self.assertIn("archive's %s folder" % word, str(cm.exception))
        with self.assertRaises(host.SettingsError) as cm:
            host.resolve_data_dir(dict(settings, data_dir=str(self.tmp / 'home' / 'Downloads' / 'fha-inbox' / 'x')))
        self.assertIn('capture download folder', str(cm.exception))
        # beside them is fine
        self.assertEqual(host.resolve_data_dir(dict(settings, data_dir=str(drive / 'Seen It data'))),
                         drive / 'Seen It data')
        with self.assertRaises(host.SettingsError):
            host._set_config({'archive': str(archive), 'dataDir': str(drive / 'Inbox' / 'data')}, {})
        self.assertFalse(host.config_path().exists())


class ImagesCountOnlyWhenSaved(_TempHome):
    """PR #213 review (Codex 4500176561, audit #1, sweep #6).

    A record's file names the scan it LINKS to whether or not the capture saved
    it. Counted as saved, a refused scan or a screen capture made the next
    capture of the same page skip its download and say "already saved".
    """

    def staged(self, name, record, assets, files=('record-data.json',)):
        staging = self.tmp / 'Downloads' / 'fha-inbox'
        bundle = staging / name
        _json(bundle / 'record-data.json', {'record': record})
        for f in files:
            if f != 'record-data.json':
                _write(bundle / f, 'x')
        _json(bundle / 'capture.json', {'url': 'https://www.ancestry.com/search/collections/%s/records/%s'
                                        % (record['dbId'], record['recordId']), 'assets': assets})
        return staging

    def keys(self, staging):
        items, _ = host.scan_known({'captured': [str(staging)]})
        return items

    REC = {'dbId': 90001, 'recordId': 1000001, 'imageId': '4000123_00045'}
    IMG = 'img:90001:4000123_00045'

    def test_a_refused_scan_is_not_a_saved_page(self):
        st = self.staged('refused', dict(self.REC, imageSaved=False),
                         [{'file': 'page-snapshot.html', 'role': 'webpage'},
                          {'file': 'record-data.json', 'role': 'site-data'}],
                         files=('page-snapshot.html',))
        items = self.keys(st)
        self.assertEqual(items['rec:90001:1000001']['state'], 'captured')
        self.assertNotIn(self.IMG, items)

    def test_a_screen_capture_is_not_a_saved_page(self):
        st = self.staged('shot', dict(self.REC, imageSaved=False),
                         [{'file': 'record.png', 'role': 'record', 'provisional': True},
                          {'file': 'record-data.json', 'role': 'site-data'}], files=('record.png',))
        self.assertNotIn(self.IMG, self.keys(st))

    def test_a_saved_scan_is(self):
        st = self.staged('good', dict(self.REC, imageSaved=True),
                         [{'file': 'record.jpg', 'role': 'record'},
                          {'file': 'record-data.json', 'role': 'site-data'}], files=('record.jpg',))
        self.assertEqual(self.keys(st)[self.IMG]['state'], 'captured')

    def test_a_household_member_pointing_at_another_bundle_holds_no_page(self):
        st = self.staged('harriet', {'dbId': 90001, 'recordId': 1000002, 'imageId': '4000123_00045',
                                     'imageSaved': False, 'same_image_as': {'record': 'rec:90001:1000001'}},
                         [{'file': 'record-data.json', 'role': 'site-data'}])
        items = self.keys(st)
        self.assertIn('rec:90001:1000002', items)
        self.assertNotIn(self.IMG, items)

    def test_files_made_before_imageSaved_are_judged_by_what_is_beside_them(self):
        # The archive's earlier captures carry no imageSaved: a listed,
        # present, non-provisional record asset counts; an imageId alone, or a
        # same_image_as pointer, does not.
        old = {'dbId': 90001, 'recordId': 1000001, 'imageId': 'with-scan'}
        st = self.staged('old-good', old, [{'file': 'record.jpg', 'role': 'record'}], files=('record.jpg',))
        self.staged('old-refused', dict(old, recordId=1000005, imageId='no-scan'),
                    [{'file': 'page-snapshot.html', 'role': 'webpage'}], files=('page-snapshot.html',))
        self.staged('old-listed-missing', dict(old, recordId=1000006, imageId='listed-not-there'),
                    [{'file': 'record.jpg', 'role': 'record'}])
        self.staged('old-sibling', dict(old, recordId=1000007, imageId='sibling', same_image_as='the capture x'),
                    [], files=('record.jpg',))
        items = self.keys(st)
        self.assertIn('img:90001:with-scan', items)
        for gone in ('no-scan', 'listed-not-there', 'sibling'):
            self.assertNotIn('img:90001:%s' % gone, items)
        # FamilySearch image keys follow the same rule
        fs = st / 'fs-old'
        _json(fs / 'record-data.json', {'record': {'fsId': 'abcd-123', 'imageId': 'wxyz-9'}})
        _json(fs / 'capture.json', {'url': 'https://www.familysearch.org/ark:/61903/1:1:ABCD-123', 'assets': []})
        items = self.keys(st)
        self.assertIn('fsrec:ABCD-123', items)
        self.assertNotIn('fsimg:WXYZ-9', items)

    def test_a_filed_source_needs_its_own_image(self):
        archive = self.make_archive()
        docs = archive / 'documents' / 'census' / 'hartley-1880'
        _json(docs / ('hartley-1880-site-data_%s.json' % SID_A),
              {'record': {'dbId': 90001, 'recordId': 1000001, 'imageId': 'kept'}})
        _write(docs / ('hartley-1880_%s.jpg' % SID_A), 'jpg')
        _json(docs / ('hartley-1880-b-site-data_%s.json' % SID_B),
              {'record': {'dbId': 90001, 'recordId': 1000009, 'imageId': 'not-kept'}})
        items, _ = host.scan_known(host.scan_folders({'archive': str(archive)}))
        self.assertEqual(items['img:90001:kept'], {'state': 'filed', 'where': 'hartley-1880', 'sid': SID_A})
        self.assertNotIn('img:90001:not-kept', items)   # SID_B has no image of its own
        self.assertEqual(items['rec:90001:1000009']['sid'], SID_B)

    def test_a_download_that_stopped_part_way_is_not_captured(self):
        # The companion writes capture.json last: a folder holding a data file
        # but no capture.json (or, once ingested, no notes.md) is incomplete.
        staging = self.tmp / 'Downloads' / 'fha-inbox'
        _json(staging / 'half' / 'record-data.json', {'record': dict(self.REC, imageSaved=True)})
        _write(staging / 'half' / 'record.jpg', 'jpg')
        _json(staging / 'ingested' / 'record-data.json', {'record': dict(self.REC, recordId=7)})
        _write(staging / 'ingested' / 'notes.md', '---\ntitle: x\n---\n')
        items = self.keys(staging)
        self.assertNotIn('rec:90001:1000001', items)
        self.assertNotIn(self.IMG, items)
        self.assertIn('rec:90001:7', items)

    def test_a_memorial_whose_photos_were_not_saved_is_not_fully_captured(self):
        # Sweep (#6), Find a Grave: the memorial's facts were saved but a photo
        # failed, was over the limit, or the gallery could not be read - its
        # photos are still to come, so it is fagidx: (known), not fag:
        # (captured). Only the memorial's facts and no photo list (an older
        # capture) is never fag: either: no saved file says the photos are.
        all_saved = {'record': {'memorialId': 5504}, 'photosTotal': 2,
                     'photos': [{'photoId': 1, 'file': 'grave-1.jpg'},
                                {'photoId': 9, 'file': None, 'same_image_as': {'where': 'archive', 'source': SID_A}}]}
        over = {'record': {'memorialId': 5505}, 'photosTotal': 2,
                'photos': [{'photoId': 1, 'file': 'grave-1.jpg'}, {'photoId': 2, 'file': None, 'skipped': 'over the limit'}]}
        failed = {'record': {'memorialId': 5506}, 'photosTotal': 1, 'photos': [{'photoId': 3, 'file': None}]}
        unread = {'record': {'memorialId': 5507}, 'photosTotal': None, 'photos': []}
        none = {'record': {'memorialId': 5508}, 'photosTotal': 0, 'photos': []}
        older = {'record': {'memorialId': 5509}}
        short = {'record': {'memorialId': 5510}, 'photosTotal': 3, 'photos': [{'photoId': 4, 'file': 'grave-1.jpg'}]}
        self.assertEqual(host._keys_from_site_data(all_saved), ['fag:5504', 'fagphoto:1'])
        self.assertEqual(host._keys_from_site_data(over), ['fagidx:5505', 'fagphoto:1'])
        self.assertEqual(host._keys_from_site_data(failed), ['fagidx:5506'])
        self.assertEqual(host._keys_from_site_data(unread), ['fagidx:5507'])
        self.assertEqual(host._keys_from_site_data(none), ['fag:5508'])
        self.assertEqual(host._keys_from_site_data(older), ['fagidx:5509'])
        self.assertEqual(host._keys_from_site_data(short), ['fagidx:5510', 'fagphoto:4'],
                         'a gallery listing fewer photos than the memorial shows is not all of them')
        # a photo the data names but no saved file backs is not saved
        self.assertEqual(host._keys_from_site_data(all_saved, None, lambda name: False), ['fagidx:5504'])

    def test_a_memorial_photo_counts_only_from_its_saved_file(self):
        # The capture companion's memorial bundle, staged: capture.json lists
        # every file; a photo is saved only when it is listed and present.
        archive = self.make_archive()
        staging = self.tmp / 'Downloads' / 'fha-inbox'
        b = staging / 'calvin-george-hartley-20261106-abc123'
        data = {'record': {'memorialId': 900000001}, 'photosTotal': 2,
                'photos': [{'photoId': 990000101, 'file': 'grave-1.jpg'}, {'photoId': 990000102, 'file': 'person-1.jpg'}]}
        _json(b / 'record-data.json', data)
        _write(b / 'grave-1.jpg', 'jpg')
        _json(b / 'capture.json', {'url': 'https://www.findagrave.com/memorial/900000001/calvin-george-hartley',
                                   'assets': [{'file': 'grave-1.jpg', 'role': 'grave-1'},
                                              {'file': 'person-1.jpg', 'role': 'person-1'},
                                              {'file': 'record-data.json', 'role': 'site-data'}]})
        items, _ = host.scan_known(host.scan_folders({'archive': str(archive), 'capture_staging': str(staging)}))
        self.assertEqual(sorted(items), ['fagidx:900000001', 'fagphoto:990000101'],
                         'person-1.jpg never arrived: the memorial is not captured, and that photo is not saved')
        _write(b / 'person-1.jpg', 'jpg')
        items, _ = host.scan_known(host.scan_folders({'archive': str(archive), 'capture_staging': str(staging)}))
        self.assertEqual(sorted(items), ['fag:900000001', 'fagidx:900000001', 'fagphoto:990000101', 'fagphoto:990000102'])
        self.assertEqual(items['fag:900000001'], {'state': 'captured', 'where': b.name})

    def test_a_filed_memorial_photo_is_found_by_the_name_filing_gave_it(self):
        # `fha process` renames a bundle's files {old name}-{role}_{S-id}: the
        # photo saved as grave-1.jpg is grave-1-grave-1_S-….jpg in the source's
        # folder, which other sources share.
        archive = self.make_archive()
        docs = archive / 'documents' / 'website'
        _json(docs / ('record-data-site-data_%s.json' % SID_A),
              {'record': {'memorialId': 900000001}, 'photosTotal': 2,
               'photos': [{'photoId': 990000101, 'file': 'grave-1.jpg'}, {'photoId': 990000102, 'file': 'grave-10.jpg'}]})
        _write(docs / ('grave-1-grave-1_%s.jpg' % SID_A), 'jpg')
        _write(docs / ('grave-10-grave-10_%s.jpg' % SID_B), 'jpg')  # another source's photo
        items, _ = host.scan_known(host.scan_folders({'archive': str(archive)}))
        self.assertEqual(items['fagphoto:990000101'], {'state': 'filed', 'where': 'website', 'sid': SID_A})
        self.assertNotIn('fagphoto:990000102', items)
        self.assertNotIn('fag:900000001', items)
        self.assertEqual(items['fagidx:900000001']['sid'], SID_A)

    def test_without_a_folder_check_only_imageSaved_counts(self):
        self.assertEqual(host._keys_from_site_data({'record': self.REC}), ['rec:90001:1000001'])
        self.assertEqual(host._keys_from_site_data({'record': dict(self.REC, imageSaved=True)}),
                         ['rec:90001:1000001', self.IMG])
        self.assertEqual(host._keys_from_site_data({'clipping': {'id': 1}, 'page': {'id': 2}}), ['clip:1'])
        self.assertEqual(host._keys_from_site_data({'record': dict(self.REC, imageSaved='yes')}),
                         ['rec:90001:1000001'])


class OddFolderNames(_TempHome):
    """Audit #5 (+ sweep): brackets, `$` and `%` are ordinary letters in a folder name."""

    def test_brackets_in_the_archive_and_download_folder(self):
        archive = self.tmp / 'Hartley Family Archive [2024]'
        _write(archive / 'fha.yaml', 'roots:\n  documents: documents\n')
        (archive / 'sources').mkdir()
        (archive / 'documents').mkdir()
        _json(archive / 'inbox' / 'b' / 'capture.json', {'url': 'https://www.findagrave.com/memorial/42/x'})
        staging = self.tmp / 'Downloads [old]' / 'fha-inbox'
        _json(staging / 'b2' / 'capture.json', {'url': 'https://www.newspapers.com/article/x/77/'})
        _json(self.tmp / 'Downloads [old]' / 'fha-inbox-anc01' / 'b3' / 'capture.json',
              {'url': 'https://www.ancestry.com/search/collections/76/records/88'})
        items, missing = host.scan_known(host.scan_folders({'archive': str(archive), 'capture_staging': str(staging)}))
        self.assertEqual(missing, [])
        # a memorial's page address alone is known, not captured: its photos may be missing
        self.assertEqual(sorted(items), ['clip:77', 'fagidx:42', 'rec:76:88'])

    def test_dollar_and_percent_are_not_environment_variables(self):
        with mock.patch.dict(os.environ, {'SEENIT_T': 'elsewhere'}):
            archive = self.tmp / 'Hartley $SEENIT_T 100%SEENIT_T% Archive'
            _write(archive / 'fha.yaml', 'roots:\n  documents: documents\n')
            (archive / 'sources').mkdir()
            _json(archive / 'inbox' / 'b' / 'capture.json', {'url': 'https://www.findagrave.com/memorial/42/x'})
            info = host._set_config({'archive': str(archive)}, {})
            self.assertEqual(Path(info['archive']), archive)
            items, _ = host.scan_known(host.scan_folders(host.load_settings()))
        self.assertIn('fagidx:42', items)


class RecordsFiledByTheArchiveTools(_TempHome):
    """PR #212 review (Codex) (+ sweep): records filed by fha capture / fha process."""

    def test_source_records_external_links_mark_the_record_filed(self):
        archive = self.make_archive()
        # the shape process.py _scaffold_text writes for a captured page
        _write(archive / 'sources' / 'census' / ('1900-census-hartley_%s.md' % SID_A),
               '---\nid: %s\ntitle: 1900 census, Thomas Hartley household\nsource_type: census\n'
               'external_links:\n'
               '  - url: https://www.ancestry.com/discoveryui-content/view/3303:7602\n'
               '    accessed: 2026-01-15\n'
               '  - url: "https://www.familysearch.org/ark:/61903/1:1:ABCD-123"\n'
               '---\n\n## Notes\nHer sister is at https://www.ancestry.com/search/collections/7602/records/9999\n'
               % SID_A)
        _write(archive / 'sources' / 'newspapers' / ('hartley-wedding_%s.md' % SID_B),
               '---\nid: %s\ncitation: >\n  "Hartley wedding", The Fairview Courier,\n'
               '  https://www.newspapers.com/article/the-fairview-courier/1101/\n'
               'external_links:\n  - url: https://www.findagrave.com/memorial/5505/x\n---\n' % SID_B)
        items, _ = host.scan_known(host.scan_folders({'archive': str(archive)}))
        self.assertEqual(items['rec:7602:3303'], {'state': 'filed', 'where': 'census', 'sid': SID_A})
        self.assertEqual(items['fsrec:ABCD-123']['sid'], SID_A)
        self.assertEqual(items['clip:1101'], {'state': 'filed', 'where': 'newspapers', 'sid': SID_B})
        # a memorial a record links is still fagidx (photos not captured), never fag
        self.assertEqual(items['fagidx:5505']['sid'], SID_B)
        self.assertNotIn('fag:5505', items)
        # a record merely mentioned in the notes is not filed
        self.assertNotIn('rec:7602:9999', items)

    def test_inbox_stubs_from_capture_ingest_count_as_captured(self):
        archive = self.make_archive()
        _write(archive / 'inbox' / 'hartley-obit.notes.md',
               '---\ntitle: Hartley obituary\nsource_type: newspaper\nexternal_links:\n'
               '  - url: https://www.newspapers.com/article/the-fairview-courier-hartley/7707/\n'
               '    accessed: 2026-01-15\n---\n\nGrandma\'s obituary.\n')
        _write(archive / 'inbox' / 'census-bundle' / 'notes.md',
               '---\nexternal_links:\n  - url: https://www.ancestry.com/search/collections/7602/records/8808\n---\n')
        items, _ = host.scan_known(host.scan_folders({'archive': str(archive)}))
        self.assertEqual(items['clip:7707'], {'state': 'captured', 'where': 'inbox'})
        self.assertEqual(items['rec:7602:8808']['state'], 'captured')

    def test_a_folder_named_processed_above_the_inbox_does_not_mark_everything_done(self):
        archive = self.tmp / 'processed' / 'Hartley Archive'
        _write(archive / 'fha.yaml', 'roots:\n  documents: documents\n')
        (archive / 'sources').mkdir()
        _json(archive / 'inbox' / 'b' / 'capture.json', {'url': 'https://www.findagrave.com/memorial/42/x'})
        _json(archive / 'inbox' / 'processed' / 'c' / 'capture.json', {'url': 'https://www.findagrave.com/memorial/43/x'})
        items, _ = host.scan_known(host.scan_folders({'archive': str(archive)}))
        self.assertEqual(items['fagidx:42']['state'], 'captured')
        self.assertEqual(items['fagidx:43']['state'], 'processed')

    def test_an_inbox_on_a_drive_not_connected_is_reported(self):
        archive = self.make_archive('roots:\n  documents: documents\n  inbox: %s/Unplugged Inbox\n'
                                    % self.tmp.as_posix())
        (archive / 'documents').mkdir()
        _, missing = host.scan_known(host.scan_folders({'archive': str(archive)}))
        self.assertEqual([Path(m) for m in missing], [self.tmp / 'Unplugged Inbox'])


# ── not shipped into archives yet ───────────────────────────────────────────

class NotVendoredYet(unittest.TestCase):

    def test_seen_it_is_not_in_the_archive_manifest(self):
        # Shipping Seen It into archives (fha install / update-tools) is a
        # separate, later change; until then nothing under seen-it/ is vendored.
        import scaffold
        paths = [e['path'] for e in scaffold.generate_manifest(ROOT)['files']]
        srcs = [e.get('src', '') for e in scaffold.generate_manifest(ROOT)['files']]
        self.assertFalse([p for p in paths + srcs if 'seen-it' in p])


if __name__ == '__main__':
    unittest.main()
