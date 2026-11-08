"""Seen It's "already in your archive" marks stay right while getting fast.

The first page of every visit asks the Seen It helper (seen-it/host/
seenit_host.py) what the archive already holds. It used to read every source
record, inbox stub and site-data file, and to list a whole folder again for
every site-data file in it - about a minute on a real archive with 5,000 filed
site-data files. It now lists each folder once per scan (`_FolderFiles`) and
remembers what each file said between scans, keyed on that file's size and
modification time (`_KnownFiles`, known-files.json beside the settings).

What these tests hold the faster scan to:

  * **Same answers as the old full read**, on a fixture of many records and
    the awkward ones (two records sharing a page, a file in the wrong
    encoding, no frontmatter, a half-downloaded bundle, a processed/ folder),
    cold, warm, and after edits, moves and deletes - probing the changed
    records' own items. `_old_holds_saved_image`, `_old_holds_saved_file` and
    `_old_scan_known` below are the pre-change functions, copied verbatim, and
    serve as the reference.
  * **Never a stale "in archive"**: a deleted scan, photo or record, and a
    capture.json that stops listing an image, all lose their mark on the very
    next scan, though the site-data file beside them did not change.
  * **Never "not in archive" for what is there**: the 2-second settle window
    (a same-size edit with the very same mtime_ns), a file locked for a moment
    is not remembered as empty, a new helper version throws the cache away.
  * **Unchanged files are not read again** (a read spy). It fails on the old
    code, which read everything on every first page.
  * A damaged or unwritable cache costs speed, never a wrong answer; the
    archive itself is never written.

Fictional Hartley family / Fairview, Kansas data only.

Run: python -m unittest test_seen_it_known_cache -v   (from tests/)
"""

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HOST_PY = ROOT / 'seen-it' / 'host' / 'seenit_host.py'


def _load_host():
    spec = importlib.util.spec_from_file_location('seenit_host_known_cache', HOST_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


host = _load_host()

# What the verbatim reference below uses from the helper (all unchanged by the
# cache work).
SID_RE = host.SID_RE
IMAGE_EXTS = host.IMAGE_EXTS
FAG_MEMORIAL_RE = host.FAG_MEMORIAL_RE
SOURCE_ID_RE = host.SOURCE_ID_RE
_expand = host._expand
_globbed = host._globbed
_frontmatter = host._frontmatter
_frontmatter_urls = host._frontmatter_urls
_keys_from_url = lambda url: host._keys_from_url(url)   # noqa: E731 - patchable, like the helper's
_keys_from_site_data = host._keys_from_site_data

# Fixture files are back-dated past the settle window so a warm scan can use
# them; each edit moves the mtime to a new, still-settled value.
_PAST = time.time() - 3600


# ── The pre-change functions, verbatim: the reference answers ────────────────

def _old_holds_saved_image(path, roles):
    """Does the bundle or source folder beside this site-data file hold a saved
    image - not merely the id of one?

    A staged bundle's capture.json lists its files: a listed, present,
    non-provisional asset with one of `roles` counts (a screen capture is a
    stand-in, not the scan). Without one (an ingested inbox bundle, a filed
    source), an image file in the same folder counts - for a filed source,
    one carrying the same S-id as the site-data file.
    """
    folder = path.parent
    cap = folder / 'capture.json'
    if cap.is_file():
        try:
            assets = json.loads(cap.read_text(encoding='utf-8')).get('assets')
        except (OSError, ValueError, AttributeError):
            assets = None
        if isinstance(assets, list):
            return any(
                isinstance(a, dict) and a.get('role') in roles and not a.get('provisional')
                and a.get('file') and (folder / str(a['file'])).is_file()
                for a in assets)
    m = SID_RE.search(path.name)
    sid = m.group(1).lower() if m else None
    try:
        files = list(folder.iterdir())
    except OSError:
        return False
    return any(f.suffix.lower() in IMAGE_EXTS and f.is_file() and (sid is None or sid in f.name.lower())
               for f in files)


def _old_holds_saved_file(path, name):
    """Does the bundle or source folder beside this site-data file hold the
    file its data names (a memorial photo the capture saved as `name`)?

    A staged bundle's capture.json must list it, present and not provisional.
    Without one, the file itself counts: by its own name in an ingested inbox
    bundle, and in a filed source by the name `fha process` gives it there -
    its old name, a role suffix and the source's S-id
    (`grave-1-grave-1_S-….jpg`), the S-id being the one in this file's name.
    """
    name = Path(str(name or '')).name
    if not name:
        return False
    folder = path.parent
    cap = folder / 'capture.json'
    if cap.is_file():
        try:
            assets = json.loads(cap.read_text(encoding='utf-8')).get('assets')
        except (OSError, ValueError, AttributeError):
            assets = None
        if isinstance(assets, list):
            return any(isinstance(a, dict) and str(a.get('file') or '') == name and not a.get('provisional')
                       and (folder / name).is_file() for a in assets)
    m = SID_RE.search(path.name)
    if not m:
        # an ingested inbox bundle: its files keep their names
        return (folder / name).is_file()
    # A filed source: its files carry its S-id. A bare `grave-1.jpg` in the
    # same shared folder is someone else's (or nobody's) - PR #221 review.
    sid = m.group(1).lower()
    stem, suffix = Path(name).stem.lower(), Path(name).suffix.lower()
    try:
        files = list(folder.iterdir())
    except OSError:
        return False
    return any(f.is_file() and sid in f.name.lower() and f.suffix.lower() == suffix
               and (f.stem.lower() == stem or f.stem.lower().startswith((stem + '-', stem + '_')))
               for f in files)


def _old_scan_known(folders):
    """{key: {state, sid?, where}} for everything the folders hold.

    'filed' beats 'processed' beats 'captured': an item that is both waiting in
    the inbox and already filed shows as filed. `where` is only the name of the
    folder the file sits in, never a full path. Also returns the folders that
    could not be reached, so the add-on can say so.
    """
    out = {}
    missing = []
    rank = {'captured': 1, 'processed': 2, 'filed': 3}

    def put(key, state, where, sid=None):
        old = out.get(key)
        if old and rank[old['state']] >= rank[state]:
            return
        entry = {'state': state, 'where': where}
        if sid:
            entry['sid'] = sid
        out[key] = entry

    for root in folders.get('filed', []):
        root = _expand(root)
        if not root.is_dir():
            missing.append(str(root))
            continue
        for p in root.rglob('*site-data*.json'):
            m = SID_RE.search(p.name)
            try:
                data = json.loads(p.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            for k in _keys_from_site_data(data, lambda roles, p=p: _old_holds_saved_image(p, roles),
                                          lambda name, p=p: _old_holds_saved_file(p, name)):
                put(k, 'filed', p.parent.name, m.group(1) if m else None)

    # Source records. The page address a capture came from is kept in the
    # record's frontmatter (`external_links:`, written by `fha capture` and
    # `fha process`; sometimes in `citation:` too), so a record filed with the
    # archive's own tools is recognized even with no site-data file beside it.
    # A Find a Grave memorial is the exception: one a record cites or links is
    # fagidx: (its photos are not captured yet), never fag:, so the capture
    # add-on's Find a Grave batch still fetches it.
    for root in folders.get('sources', []):
        root = _expand(root)
        if not root.is_dir():
            missing.append(str(root))
            continue
        for p in root.rglob('*.md'):
            try:
                text = p.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError):
                continue
            sm = SOURCE_ID_RE.search(_frontmatter(text) or text)
            sid = sm.group(1) if sm else None
            for url in _frontmatter_urls(text):
                for k in _keys_from_url(url):
                    if not k.startswith('fag:'):
                        put(k, 'filed', p.parent.name, sid)
            for mid in set(FAG_MEMORIAL_RE.findall(text)):
                put('fagidx:%s' % mid, 'filed', p.parent.name, sid)

    for root in folders.get('required', []):
        if not _expand(root).is_dir():
            missing.append(str(_expand(root)))
    for root in _globbed(folders.get('captured', [])):
        root = Path(root)
        if not root.is_dir():
            continue  # no download folder yet is normal: nothing captured on this computer
        for p in root.rglob('*'):
            name = p.name.lower()
            is_stub = name.endswith('.notes.md') or name == 'notes.md'
            is_data = name.endswith('.json') and (
                name == 'capture.json' or 'site-data' in name
                or name.startswith(('clipping-data', 'record-data')))
            if not (is_stub or is_data) or not p.is_file():
                continue
            # A bundle's data file counts once the bundle is whole: the
            # companion writes capture.json last, and an ingested inbox
            # bundle has its notes.md. A download that stopped part-way has
            # neither, and nothing in it is captured yet.
            if is_data and name != 'capture.json' and not (
                    (p.parent / 'capture.json').is_file() or (p.parent / 'notes.md').is_file()):
                continue
            # an item in the inbox's processed/ holding folder has been filed
            # (or set aside as a duplicate). Only folders INSIDE the inbox
            # count: an inbox that itself lives under a folder called
            # "processed" is not all done.
            inside = [x.lower() for x in p.relative_to(root).parts[:-1]]
            state = 'processed' if 'processed' in inside else 'captured'
            try:
                raw = p.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError):
                continue
            if is_stub:
                # `fha capture --ingest` turns a bundle into an inbox stub
                # whose frontmatter carries the page address.
                keys = [k for url in _frontmatter_urls(raw) for k in _keys_from_url(url)]
            else:
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                keys = (_keys_from_url(data.get('url')) if name == 'capture.json'
                        else _keys_from_site_data(data, lambda roles, p=p: _old_holds_saved_image(p, roles),
                                                  lambda name, p=p: _old_holds_saved_file(p, name)))
            for k in keys:
                put(k, state, p.parent.name)
    return out, missing

# ── the fixture ─────────────────────────────────────────────────────────────

def _sid(i, letter='a'):
    return 'S-%s%09d' % (letter, i)


def _write(path, text, encoding='utf-8'):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


def _json(path, data):
    return _write(path, json.dumps(data))


def _stamp(path, t):
    os.utime(path, ns=(int(t * 1e9), int(t * 1e9)))


class _KnownCacheCase(unittest.TestCase):
    """A synthetic archive with many records, its own settings folder, and a
    cache file per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.conf = self.tmp / 'settings'
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
        self.cache = self.conf / 'known-files.json'
        self.tick = 0
        self.build()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def build(self):
        self.docs = self.tmp / 'Drive' / 'Docs'
        self.archive = self.tmp / 'Hartley Family Archive'
        _write(self.archive / 'fha.yaml', 'roots:\n  documents: %s\n' % self.docs.as_posix())
        self.sources = self.archive / 'sources'
        self.inbox = self.archive / 'inbox'
        self.staging = self.tmp / 'Downloads' / 'fha-inbox'
        self.settings = {'archive': str(self.archive), 'capture_staging': str(self.staging)}

        # source records: one Ancestry record address each; every fifth also
        # cites a Find a Grave memorial in its notes
        self.records = {}
        for i in range(40):
            sid = _sid(i)
            body = ('See https://www.findagrave.com/memorial/%d/hartley\n' % (60000 + i)) if i % 5 == 0 else ''
            self.records[i] = _write(
                self.sources / 'census' / ('hartley-census-%02d_%s.md' % (i, sid)),
                '---\nid: %s\ntitle: Hartley census %d\nexternal_links:\n'
                '  - url: https://www.ancestry.com/search/collections/7602/records/%d\n---\n%s'
                % (sid, i, 1000 + i, body))
        # two records saved from the same page: the first in walk order names it
        for folder, i in (('a-first', 90), ('b-second', 91)):
            _write(self.sources / folder / ('shared_%s.md' % _sid(i)),
                   '---\nid: %s\nexternal_links:\n  - https://www.newspapers.com/article/x/777001/\n---\n'
                   % _sid(i))
        # the awkward ones
        (self.sources / 'odd').mkdir(parents=True)
        (self.sources / 'odd' / 'latin1.md').write_bytes(
            b'---\nid: S-z000000001\nexternal_links:\n  - https://www.newspapers.com/article/x/777002/\n'
            b'title: Caf\xe9\n---\n')
        _write(self.sources / 'odd' / 'no-frontmatter.md',
               'id: S-z000000002\nhttps://www.newspapers.com/article/x/777003/\n')
        _write(self.sources / 'odd' / 'bom.md',
               '﻿---\nid: S-z000000003\nexternal_links:\n  - https://www.newspapers.com/article/x/777004/\n---\n')

        # filed site-data in one big census folder: an image beside every even
        # one; some say imageSaved outright either way
        census = self.docs / 'census' / '1900'
        self.site = {}
        for i in range(30):
            rec = {'dbId': 7602, 'recordId': 5000 + i, 'imageId': 8000 + i}
            if i % 3 == 0:
                rec['imageSaved'] = True
            elif i % 7 == 0:
                rec['imageSaved'] = False
            self.site[i] = _json(census / ('ancestry-record-site-data_%s.json' % _sid(i, 'b')), {'record': rec})
            if i % 2 == 0:
                _write(census / ('hartley-census-1900_%s.jpg' % _sid(i, 'b')), 'jpg')
        _write(census / 'notes.txt', 'not an image')
        _write(self.docs / 'broken' / 'bad-site-data.json', '{nope')
        # a filed memorial with two photos, saved by the name filing gives them
        self.memorial = self.docs / 'burial' / 'thomas'
        msid = _sid(1, 'c')
        _json(self.memorial / ('memorial-site-data_%s.json' % msid),
              {'record': {'memorialId': 5505},
               'photos': [{'photoId': 71, 'file': 'grave-1.jpg'}, {'photoId': 72, 'file': 'grave-2.jpg'}],
               'photosTotal': 2})
        self.photo1 = _write(self.memorial / ('grave-1-grave-1_%s.jpg' % msid), 'jpg')
        _write(self.memorial / ('grave-2_%s.jpg' % msid), 'jpg')
        _json(self.docs / 'newspapers' / 'wedding' / ('clipping-site-data_%s.json' % _sid(2, 'c')),
              {'clipping': {'id': 1101}, 'page': {'id': 2202}})
        _write(self.docs / 'newspapers' / 'wedding' / ('wedding-page_%s.jpg' % _sid(2, 'c')), 'jpg')

        # inbox: ingested stubs, staged bundles with capture.json, a processed one
        self.stubs = {}
        for i in range(15):
            self.stubs[i] = _write(
                self.inbox / ('hartley-%02d.notes.md' % i),
                '---\nexternal_links:\n  - https://www.familysearch.org/ark:/61903/1:1:AB%02d-CD\n---\nnotes\n' % i)
        self.bundles = {}
        for i in range(5):
            b = self.inbox / ('bundle-%d' % i)
            _json(b / 'record-data.json', {'record': {'dbId': 9100, 'recordId': 100 + i, 'imageId': 200 + i}})
            _write(b / 'scan.jpg', 'jpg')
            _json(b / 'capture.json', {'url': 'https://www.ancestry.com/discoveryui-content/view/%d:9100' % (100 + i),
                                       'assets': [{'file': 'scan.jpg', 'role': 'record'}]})
            self.bundles[i] = b
        _json(self.inbox / 'processed' / 'dup' / 'capture.json',
              {'url': 'https://www.familysearch.org/ark:/61903/1:1:EF99-GH'})
        _json(self.inbox / 'half-downloaded' / 'record-data.json', {'record': {'dbId': 9100, 'recordId': 999}})
        # the capture add-on's download folder: one ingested-style bundle
        _json(self.staging / 'b1' / 'record-site-data.json',
              {'record': {'fsId': 'zz11-22', 'imageId': 'qq-1'}})
        _write(self.staging / 'b1' / 'notes.md', '---\n---\n')
        _write(self.staging / 'b1' / 'page.jpg', 'jpg')
        self.settle_all()

    def settle_all(self):
        for root in (self.archive, self.docs, self.staging):
            for p in root.rglob('*'):
                if p.is_file():
                    _stamp(p, _PAST)

    def edit(self, path, text):
        """Rewrite a fixture file and give it a new, settled mtime."""
        self.tick += 1
        _write(path, text)
        _stamp(path, _PAST + self.tick)
        return path

    def folders(self):
        return host.scan_folders(self.settings)

    def scan(self):
        return host.scan_known(self.folders(), host._KnownFiles(self.cache))

    def assert_same_as_old(self):
        """The cached scan's answer, after checking it equals the old full read."""
        old = _old_scan_known(self.folders())
        new = host.scan_known(self.folders(), host._KnownFiles(self.cache))
        self.assertEqual(new, old)
        return new[0]

    @contextlib.contextmanager
    def reads(self, fail=None):
        """Every fixture file read while inside; `fail` (a path) refuses its first read."""
        seen = []
        real = Path.read_text
        state = {'failed': False}

        def spy(p, *args, **kwargs):
            if str(p).startswith(str(self.tmp)) and self.conf not in Path(p).parents:
                seen.append(Path(p))
                if fail is not None and Path(p) == Path(fail) and not state['failed']:
                    state['failed'] = True
                    raise PermissionError(13, 'The process cannot access the file', str(p))
            return real(p, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', spy):
            yield seen



# ── same answers as the old full read ───────────────────────────────────────

class SameAnswers(_KnownCacheCase):

    def test_matches_old_scan_cold_and_warm(self):
        cold = self.assert_same_as_old()
        self.assertTrue(self.cache.is_file())
        warm = self.assert_same_as_old()
        self.assertEqual(warm, cold)
        # the fixture really exercises what it claims to
        self.assertEqual(cold['img:7602:8002']['state'], 'filed')       # image beside it
        self.assertNotIn('img:7602:8001', cold)                         # none beside it
        self.assertIn('img:7602:8003', cold)                            # imageSaved: true
        self.assertNotIn('img:7602:8007', cold)                         # imageSaved: false
        self.assertEqual(cold['fag:5505']['state'], 'filed')
        self.assertIn('fagphoto:72', cold)
        self.assertEqual(cold['fagidx:60005']['sid'], _sid(5))
        self.assertEqual(cold['rec:7602:1039'], {'state': 'filed', 'where': 'census', 'sid': _sid(39)})
        self.assertEqual(cold['fsrec:AB14-CD']['state'], 'captured')
        self.assertEqual(cold['img:9100:204']['state'], 'captured')
        self.assertEqual(cold['fsrec:EF99-GH']['state'], 'processed')
        self.assertIn('fsimg:QQ-1', cold)
        self.assertNotIn('rec:9100:999', cold)                          # a half-finished download
        self.assertNotIn('clip:777002', cold)                           # not UTF-8: skipped, as ever
        self.assertNotIn('clip:777003', cold)                           # no frontmatter
        self.assertIn('clip:777004', cold)                              # a byte-order mark

    def test_every_page_of_the_known_answer_matches(self):
        old, old_missing = _old_scan_known(self.folders())
        for _visit in range(2):                       # cold, then a first page with a warm cache
            got, offset, total = {}, 0, 1
            with mock.patch.object(host, 'KNOWN_PAGE', 7):
                while offset < total:
                    page = host.handle({'op': 'known', 'offset': offset}, self.settings)
                    got.update(page['items'])
                    total, offset = page['total'], page['next']
                    self.assertEqual(page['missing'], old_missing)
            self.assertEqual(got, old)

    def test_matches_old_scan_after_edits_moves_and_deletes(self):
        before = self.assert_same_as_old()
        shared_first = before['clip:777001']['sid']
        # an edit of the same size (a new record number) and one that grows
        self.edit(self.records[3], self.records[3].read_text(encoding='utf-8').replace('1003', '9003'))
        self.edit(self.records[4], self.records[4].read_text(encoding='utf-8').replace(
            'records/1004', 'records/1004\n  - https://www.familysearch.org/ark:/61903/1:1:NEW4-44'))
        # a record deleted, one moved to another folder, and the record that
        # named a shared page deleted so the other one names it
        self.records[5].unlink()
        moved = self.sources / 'moved' / self.records[7].name
        moved.parent.mkdir()
        self.records[7].rename(moved)
        for p in self.sources.rglob('shared_*.md'):
            if shared_first in p.name:
                p.unlink()
        # a stub moved into processed/, a bundle that lost its capture.json
        (self.inbox / 'processed' / self.stubs[2].name).parent.mkdir(exist_ok=True)
        self.stubs[2].rename(self.inbox / 'processed' / self.stubs[2].name)
        (self.bundles[0] / 'capture.json').unlink()
        after = self.assert_same_as_old()

        self.assertNotIn('rec:7602:1003', after)
        self.assertEqual(after['rec:7602:9003']['sid'], _sid(3))
        self.assertEqual(after['fsrec:NEW4-44']['sid'], _sid(4))
        self.assertIn('rec:7602:1004', after)
        self.assertNotIn('rec:7602:1005', after)
        self.assertNotIn('fagidx:60005', after)
        self.assertEqual(after['rec:7602:1007']['where'], 'moved')
        self.assertNotEqual(after['clip:777001']['sid'], shared_first)
        self.assertEqual(after['fsrec:AB02-CD']['state'], 'processed')
        self.assertNotIn('rec:9100:100', after)      # capture.json gone: neither it nor its data counts
        self.assertNotIn('img:9100:200', after)
        # the deleted record is forgotten, not just skipped
        stored = json.loads(self.cache.read_text(encoding='utf-8'))['files']
        self.assertNotIn(str(self.records[5]), stored)
        self.assertNotIn(str(self.records[7]), stored)
        self.assertIn(str(moved), stored)


# ── never a stale "in archive" ──────────────────────────────────────────────

class NoStaleMarks(_KnownCacheCase):
    """What a site-data file means depends on the files beside it. Those are
    asked again on every scan even when the site-data file itself is
    unchanged - otherwise a deleted scan would stay "in archive" and he would
    skip a record he never saved."""

    def test_a_deleted_scan_stops_counting_as_saved(self):
        self.assertEqual(self.assert_same_as_old()['img:7602:8002']['state'], 'filed')
        next(self.site[2].parent.glob('*_%s.jpg' % _sid(2, 'b'))).unlink()
        after = self.assert_same_as_old()
        self.assertNotIn('img:7602:8002', after)
        self.assertIn('rec:7602:5002', after)

    def test_a_scan_saved_later_counts_at_once(self):
        self.assertNotIn('img:7602:8001', self.assert_same_as_old())
        _write(self.site[1].parent / ('hartley-census-1900_%s.png' % _sid(1, 'b')), 'png')
        self.assertIn('img:7602:8001', self.assert_same_as_old())

    def test_a_deleted_memorial_photo_makes_it_known_not_captured(self):
        self.assertIn('fag:5505', self.assert_same_as_old())
        self.photo1.unlink()
        after = self.assert_same_as_old()
        self.assertNotIn('fag:5505', after)
        self.assertIn('fagidx:5505', after)
        self.assertNotIn('fagphoto:71', after)
        self.assertIn('fagphoto:72', after)

    def test_a_capture_json_that_stops_listing_the_scan(self):
        self.assertIn('img:9100:201', self.assert_same_as_old())
        cap = self.bundles[1] / 'capture.json'
        data = json.loads(cap.read_text(encoding='utf-8'))
        data['assets'][0]['provisional'] = True
        self.edit(cap, json.dumps(data))
        after = self.assert_same_as_old()
        self.assertNotIn('img:9100:201', after)
        self.assertIn('rec:9100:101', after)


# ── never "not in archive" for what is there ────────────────────────────────

class Freshness(_KnownCacheCase):

    def _inside_window_then_same_size_edit(self, path, old, new):
        """Write `path` half a second ago, scan, then make a same-size edit
        carrying the very same mtime_ns and scan again."""
        text = path.read_text(encoding='utf-8')
        self.assertEqual(len(old), len(new))
        stamp = time.time() - 0.5
        _write(path, text)
        _stamp(path, stamp)
        mtime_ns = path.stat().st_mtime_ns
        self.assert_same_as_old()
        _write(path, text.replace(old, new))
        os.utime(path, ns=(mtime_ns, mtime_ns))
        self.assertEqual(path.stat().st_mtime_ns, mtime_ns)
        return self.assert_same_as_old()

    def test_settle_window_source_record(self):
        after = self._inside_window_then_same_size_edit(self.records[9], 'records/1009', 'records/7009')
        self.assertEqual(after['rec:7602:7009']['sid'], _sid(9))
        self.assertNotIn('rec:7602:1009', after)

    def test_settle_window_site_data(self):
        after = self._inside_window_then_same_size_edit(self.site[3], '5003', '6003')
        self.assertIn('rec:7602:6003', after)
        self.assertNotIn('rec:7602:5003', after)

    def test_a_record_locked_for_a_moment_is_not_remembered_as_empty(self):
        # a backup, a sync client or an antivirus scan holds the file for a
        # moment: that scan cannot see it (as ever), the next one must
        target = self.records[11]
        with self.reads(fail=target):
            first = host.scan_known(self.folders(), host._KnownFiles(self.cache))[0]
        self.assertNotIn('rec:7602:1011', first)
        stored = json.loads(self.cache.read_text(encoding='utf-8'))['files']
        self.assertNotIn(str(target), stored, 'a read that failed is not remembered')
        self.assertEqual(self.assert_same_as_old()['rec:7602:1011']['sid'], _sid(11))

    def test_a_site_data_file_locked_for_a_moment(self):
        target = self.site[4]
        with self.reads(fail=target):
            host.scan_known(self.folders(), host._KnownFiles(self.cache))
        stored = json.loads(self.cache.read_text(encoding='utf-8'))['files']
        self.assertNotIn(str(target), stored)
        self.assertIn('rec:7602:5004', self.assert_same_as_old())

    def test_new_helper_code_throws_the_cache_away(self):
        with mock.patch.object(host, '_reader_fingerprint', lambda: 'reader-A'):
            self.assert_same_as_old()
        with mock.patch.object(host, '_reader_fingerprint', lambda: 'reader-B'):
            with self.reads() as seen:
                host.scan_known(self.folders(), host._KnownFiles(self.cache))
        self.assertIn(self.records[0], seen)
        self.assertIn(self.site[0], seen)
        self.assertEqual(json.loads(self.cache.read_text(encoding='utf-8'))['reader'], 'reader-B')

    def test_a_record_the_new_code_reads_is_found_after_an_update(self):
        # an older helper that did not know Newspapers.com addresses, then an
        # update that does: the shared page must read as filed at once
        real = host._keys_from_url
        older = lambda url: [k for k in real(url) if not k.startswith('clip:')]  # noqa: E731
        with mock.patch.object(host, '_reader_fingerprint', lambda: 'reader-old'), \
                mock.patch.object(host, '_keys_from_url', older):
            self.assertNotIn('clip:777001', self.scan()[0])
        with mock.patch.object(host, '_reader_fingerprint', lambda: 'reader-new'):
            self.assertIn('clip:777001', self.assert_same_as_old())

    def test_the_fingerprint_covers_this_file(self):
        stamp = host._reader_fingerprint()
        self.assertIsInstance(stamp, str)
        host._reader_fingerprint.cache_clear()
        with mock.patch.object(Path, 'read_bytes', lambda p: b'other helper code'):
            other = host._reader_fingerprint()
        host._reader_fingerprint.cache_clear()
        self.assertNotEqual(stamp, other)
        self.assertEqual(host._reader_fingerprint(), stamp)


# ── unchanged files are not read again ──────────────────────────────────────

class ReadsOnlyWhatChanged(_KnownCacheCase):
    """Through the add-on's own request, so it runs unchanged on the old code -
    where every first page read every file - and fails there."""

    def first_page(self):
        """The record files one first page reads. fha.yaml is read every time,
        and so are the two files that never read cleanly (broken JSON, not
        UTF-8): neither is remembered, by design."""
        never_clean = {self.docs / 'broken' / 'bad-site-data.json', self.sources / 'odd' / 'latin1.md'}
        with self.reads() as seen:
            host.handle({'op': 'known', 'offset': 0}, self.settings)
        return [p for p in seen if p.name != 'fha.yaml' and p not in never_clean]

    def test_a_warm_first_page_reads_no_record_again(self):
        self.first_page()
        seen = self.first_page()
        # only a bundle's capture.json is read, once a folder, to re-check a
        # scan it lists; no source record, stub or site-data file
        self.assertEqual([p for p in seen if p.name != 'capture.json'], [])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertLessEqual(len(seen), len(self.bundles))

    def test_an_edit_reads_just_that_file(self):
        self.first_page()
        self.edit(self.records[12], self.records[12].read_text(encoding='utf-8') + 'more notes\n')
        self.edit(self.stubs[3], self.stubs[3].read_text(encoding='utf-8') + 'more\n')
        seen = [p for p in self.first_page() if p.name != 'capture.json']
        self.assertEqual(sorted(seen), sorted([self.records[12], self.stubs[3]]))

    def test_a_drive_not_plugged_in_is_not_a_full_read_when_it_comes_back(self):
        self.first_page()
        away = self.tmp / 'Drive-away'
        self.docs.parent.rename(away)
        host.handle({'op': 'known', 'offset': 0}, self.settings)
        away.rename(self.docs.parent)
        seen = self.first_page()
        self.assertEqual([p for p in seen if self.docs in p.parents], [])


# ── a damaged cache, and the folders it may and may not write ───────────────

class DamagedCache(_KnownCacheCase):

    def test_garbage_costs_only_a_re_read(self):
        self.assert_same_as_old()
        for junk in ('{nope', '[]', '{"version": 1}', json.dumps({'version': 99, 'files': {}})):
            self.cache.write_text(junk, encoding='utf-8')
            self.assert_same_as_old()

    def test_entries_of_the_wrong_shape_are_read_again(self):
        self.assert_same_as_old()
        raw = json.loads(self.cache.read_text(encoding='utf-8'))
        bad_values = ['junk', None, {'keys': 'rec:1:1'}, {'keys': [1]}, {'sid': 7, 'keys': []}, {'keys': []},
                      {'sid': None, 'keys': [], 'asked': []},
                      {'keys': [], 'asked': [['image', 'record', True]]},
                      {'keys': [], 'asked': [['mystery', 'x', True]]},
                      {'keys': [], 'asked': [['file', 'x', 'yes']]},
                      {'keys': [], 'asked': 'nope'}]
        for value in bad_values:
            for name, entry in raw['files'].items():
                if isinstance(entry, list) and len(entry) == 3:
                    raw['files'][name] = [entry[0], entry[1], value]
            self.cache.write_text(json.dumps(raw), encoding='utf-8')
            self.assert_same_as_old()
            raw = json.loads(self.cache.read_text(encoding='utf-8'))

    def test_a_cache_that_cannot_be_written(self):
        self.cache.mkdir(parents=True)           # a folder where the file should be
        self.assert_same_as_old()
        self.assert_same_as_old()
        self.assertEqual([p.name for p in self.conf.iterdir() if p.name.startswith('.writing-')], [])

    def test_the_archive_is_never_written(self):
        def snapshot():
            return sorted((str(p), p.stat().st_mtime_ns) for root in (self.archive, self.docs, self.staging)
                          for p in root.rglob('*'))
        before = snapshot()
        host.handle({'op': 'known', 'offset': 0}, self.settings)
        host.handle({'op': 'known', 'offset': 0}, self.settings)
        self.assertEqual(snapshot(), before)
        self.assertTrue((self.conf / 'known-files.json').is_file())
        self.assertFalse((host.resolve_data_dir(self.settings) / 'known-files.json').exists())


# ── one listing per folder, same answers ────────────────────────────────────

class OneListingPerFolder(_KnownCacheCase):
    """The speed fix itself: a folder full of site-data files and scans is
    listed once per scan, not once per site-data file in it. Listing the big
    census folders again for every file was most of the minute a first page
    took on a real archive."""

    def listings(self):
        """How often each folder is listed for "is the scan saved?" in one scan."""
        counts = {}
        real = os.scandir
        listing_code = host._FolderFiles.files.__code__

        def spy(path='.', *args, **kwargs):
            if sys._getframe(1).f_code is listing_code:
                key = str(Path(path))
                counts[key] = counts.get(key, 0) + 1
            return real(path, *args, **kwargs)

        with mock.patch.object(os, 'scandir', spy):
            host.scan_known(self.folders(), host._KnownFiles(self.cache))
        return counts

    def test_each_folder_is_listed_once_per_scan(self):
        census = str(self.site[0].parent)
        for label in ('cold', 'warm'):
            counts = self.listings()
            self.assertEqual(counts.get(census), 1, label)
            self.assertEqual({k: v for k, v in counts.items() if v > 1}, {}, label)


class FolderListing(unittest.TestCase):
    """_holds_saved_image / _holds_saved_file answer from one listing per
    folder, indexed by S-id; the answers must be the old ones exactly."""

    def test_matches_the_old_answers_on_awkward_names(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d).resolve()
            sid = 'S-ab12cd34ef'
            names = ['x_%s.jpg' % sid, 'x_%s.JPG' % sid.upper(), '.jpg', 'plain.jpg', 'other_S-zz99yy88xx.png',
                     's-s-ab12cd34ef.tif', 'grave-1-grave-1_%s.jpg' % sid, 'grave-1_%s.jpeg' % sid,
                     'grave-10_%s.jpg' % sid, 'grave-1.jpg', 'notes_%s.txt' % sid, 'S-AB12CD34EF',
                     'x%s.webp' % sid[:-1]]
            for n in names:
                (folder / n).write_text('x', encoding='utf-8')
            (folder / ('dir_%s.jpg' % sid)).mkdir()
            probes = ['grave-1.jpg', 'grave-1.jpeg', 'grave-10.jpg', 'grave-2.jpg', '.jpg', 'notes.txt', '', None, 7]
            cases = [('a-site-data_%s.json' % sid), 'a-site-data_S-zz99yy88xx.json',
                     'a-site-data_S-0000000000.json', 'record-site-data.json']
            for subset in (names, names[3:], names[:1], []):
                for f in folder.iterdir():
                    if f.is_file():
                        f.unlink()
                for n in subset:
                    (folder / n).write_text('x', encoding='utf-8')
                for case in cases:
                    p = folder / case
                    listing = host._FolderFiles()
                    for roles in (('record',), ('page',)):
                        self.assertEqual(host._holds_saved_image(p, roles, listing),
                                         _old_holds_saved_image(p, roles), (subset, case))
                    for probe in probes:
                        self.assertEqual(host._holds_saved_file(p, probe, listing),
                                         _old_holds_saved_file(p, probe), (subset, case, probe))


if __name__ == '__main__':
    unittest.main()
