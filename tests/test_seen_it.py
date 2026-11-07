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
        folders = host.scan_folders({'archive': str(archive)})
        self.assertEqual(folders['captured'][1:], ['~/Downloads/fha-inbox', '~/Downloads/fha-inbox-*'])
        archive = self.make_archive('roots:\n  documents: documents\ncapture_staging: ~/Captures/fha\n')
        self.assertEqual(host.scan_folders({'archive': str(archive)})['captured'][1:],
                         ['~/Captures/fha', '~/Captures/fha-*'])
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
        _json(ext_docs / 'census' / '1900' / ('ancestry-records-site-data_%s.json' % SID_B),
              {'records': [{'dbId': 7602, 'recordId': 3303, 'imageId': 4404}, 'junk']})
        _json(ext_docs / 'vital' / 'fs' / 'record-site-data.json',
              {'record': {'fsId': 'abcd-123', 'imageId': 'wxyz-9'}})
        _json(ext_docs / 'burial' / 'x' / 'memorial-site-data.json', {'record': {'memorialId': 5505}})
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
        self.assertEqual(items['fagidx:6606'], {'state': 'filed', 'where': 'burial', 'sid': SID_A})
        self.assertEqual(items['clip:7707']['state'], 'captured')
        self.assertEqual(items['fsrec:EFGH-456']['state'], 'processed')
        self.assertEqual(items['rec:7602:8808']['state'], 'captured')
        self.assertEqual(items['rec:7602:9909']['state'], 'captured')  # a -* chunk folder
        self.assertEqual(len(items), 12)

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
            self.assertEqual(first['total'], 12)
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
        self.assertEqual(len(got), 12)
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
        self.assertEqual(known['items'], {'fag:42': {'state': 'captured', 'where': 'b'}})
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
