"""The browser companion's two lookups stay correct while getting fast.

`checkUrl` ("have I already saved this page?") runs on every record page the
human opens, and `suggestNames` on every keystroke in a name box. Both used to
parse every record in the archive per request - about eight seconds a page on a
3,000-source archive. They now remember what each record file said, keyed on
that file's size and modification time (`capture._RecordFacts`).

What these tests hold the faster lookups to:

  * **Same answers as the old full scan**, on a fixture with many records and
    the awkward ones (two records sharing a page, broken YAML, a file in the
    wrong encoding, no frontmatter, templates, id aliases, case-duplicate
    names). `_old_check_url` / `_old_suggest_names` below are the pre-change
    functions, copied verbatim, and serve as the reference.
  * **Every normalization `_normalize_source_url` performs** - host case, www,
    http/https, trailing slash, query, fragment, path parameters, the kept
    `clipping_id` - and what it deliberately does NOT fold (path case, ports,
    `www2.`), cold and warm.
  * **Never "not saved" for a saved page**: a stub filed a moment ago, an edit,
    a deletion are all seen on the very next request.
  * **Unrelated records are not parsed again** (a read_record spy). These spy
    tests fail against the old code, which parsed everything every time.
  * A damaged or unwritable cache costs speed, never a wrong answer.

Run: python -m unittest tests.test_capture_host_lookups -v   (from the repo root)
"""

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import capture
from _lib import load_fha_yaml, read_record, resolve_path

# Fixture files are back-dated past the cache's settle window so a warm request
# can use them; each edit moves the mtime to a new, still-settled value.
_PAST = time.time() - 3600


# ── The pre-change functions, verbatim: the reference answers ────────────────

def _old_suggest_names(archive_root, fha_config, q, limit):
    query = (q or '').strip().lower()
    people_dir = resolve_path('people', fha_config, archive_root)
    names, seen = [], set()
    if people_dir.is_dir():
        for md in sorted(people_dir.rglob('*.md')):
            if md.name.startswith('_'):
                continue
            try:
                meta = (read_record(md).get('meta') or {})
            except Exception:
                continue
            candidates = [str(meta['name'])] if meta.get('name') else []
            for alias in (meta.get('aliases') or []):
                alias = str(alias)
                if not re.match(r'^[PSLC]-[A-Za-z0-9]+$', alias):
                    candidates.append(alias)
            for name in candidates:
                key = name.lower()
                if key in seen or (query and query not in key):
                    continue
                seen.add(key)
                names.append(name)
    names.sort(key=lambda n: (not n.lower().startswith(query), n.lower()))
    return {'ok': True, 'names': names[:max(0, limit)]}


def _old_check_url(archive_root, fha_config, url):
    target = capture._normalize_source_url(url)
    if not target:
        return {'ok': True, 'known': False}
    for sub in ('sources', 'inbox'):
        root = resolve_path(sub, fha_config, archive_root)
        if not root.is_dir():
            continue
        for md in sorted(root.rglob('*.md')):
            try:
                meta = (read_record(md).get('meta') or {})
            except Exception:
                continue
            for link in (meta.get('external_links') or []):
                href = link.get('url') if isinstance(link, dict) else link
                if href and capture._normalize_source_url(href) == target:
                    return {'ok': True, 'known': True, 'source': meta.get('id'),
                            'date': meta.get('source_date') or meta.get('created')}
    return {'ok': True, 'known': False}


class _ArchiveCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive = Path(self._tmp.name) / 'archive'
        for d in ('people', 'sources', 'inbox'):
            (self.archive / d).mkdir(parents=True)
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        self.config = load_fha_yaml(self.archive, strict=True)
        self._tick = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, text: str, *, settled: bool = True, raw: bytes | None = None) -> Path:
        """Write a fixture file; `settled` back-dates it to a unique past mtime."""
        p = self.archive / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            p.write_bytes(raw)
        else:
            p.write_text(text, encoding='utf-8')
        if settled:
            self._tick += 1
            t = _PAST + self._tick
            os.utime(p, (t, t))
        return p

    def source(self, rel: str, sid: str, *urls: str, date: str = '1900') -> Path:
        links = ''.join(f'  - url: "{u}"\n    accessed: 2026-06-12\n' for u in urls)
        return self.write(f'sources/{rel}', f'---\nid: {sid}\ntitle: A record\nsource_date: {date}\n'
                               f'external_links:\n{links}---\n\nBody text.\n')

    def person(self, rel: str, name: str, *aliases: str) -> Path:
        alias_list = ', '.join(f'"{a}"' for a in aliases)
        return self.write(rel, f'---\nid: P-{abs(hash(rel)) % 10**10:010d}\nname: {name}\n'
                               f'aliases: [{alias_list}]\n---\n')

    def check(self, url: str) -> dict:
        return capture._host_check_url(self.archive, self.config, url)

    def names(self, q: str, limit: int = 1000) -> list[str]:
        return capture._host_suggest_names(self.archive, self.config, q, limit)['names']

    def count_parses(self, fn) -> int:
        """How many record files `fn` parses (a read_record spy)."""
        calls = []
        real = capture.read_record

        def spy(path, *a, **kw):
            calls.append(Path(path).name)
            return real(path, *a, **kw)

        with mock.patch.object(capture, 'read_record', spy):
            fn()
        return len(calls)


# ── Every case _normalize_source_url handles ─────────────────────────────────

class NormalizationCasesTestCase(_ArchiveCase):
    SAVED = 'https://www.ancestry.com/discoveryui-content/view/12345:7884'

    # (what the browser reports, should it read as the saved page?)
    CASES = [
        ('https://www.ancestry.com/discoveryui-content/view/12345:7884', True),    # identical
        ('HTTPS://WWW.ANCESTRY.COM/discoveryui-content/view/12345:7884', True),    # host case
        ('https://ancestry.com/discoveryui-content/view/12345:7884', True),        # www dropped
        ('http://www.ancestry.com/discoveryui-content/view/12345:7884', True),     # http vs https
        ('https://www.ancestry.com/discoveryui-content/view/12345:7884/', True),   # trailing slash
        ('https://www.ancestry.com/discoveryui-content/view/12345:7884///', True),  # several
        ('https://www.ancestry.com/discoveryui-content/view/12345:7884?_phsrc=x&pId=9', True),  # query
        ('https://www.ancestry.com/discoveryui-content/view/12345:7884#top', True),  # fragment
        ('https://www.ancestry.com/discoveryui-content/view/12345:7884;jsid=1', True),  # path params
        ('https://www.ancestry.com/Discoveryui-Content/View/12345:7884', False),    # path case kept
        ('https://www.ancestry.com/discoveryui-content/view/12345:7885', False),    # another record
        ('https://www.ancestry.com:8443/discoveryui-content/view/12345:7884', False),  # port kept
        ('https://www2.ancestry.com/discoveryui-content/view/12345:7884', False),   # only `www.`
        ('https://www.ancestry.co.uk/discoveryui-content/view/12345:7884', False),  # another host
        ('www.ancestry.com/discoveryui-content/view/12345:7884', False),           # no scheme: no host
        ('', False),
        (None, False),
    ]

    def setUp(self) -> None:
        super().setUp()
        self.source('census/a_S-aaaaaaaaaa.md', 'S-aaaaaaaaaa', self.SAVED)
        for i in range(30):                                  # unrelated neighbours
            self.source(f'census/n{i:02d}_S-n{i:09d}.md', f'S-n{i:09d}',
                        f'https://www.familysearch.org/ark:/61903/1:1:QQ{i:02d}')

    def test_every_case_cold_and_warm(self) -> None:
        for warm in (False, True):
            for url, saved in self.CASES:
                with self.subTest(url=url, warm=warm):
                    self.assertEqual(self.check(url), _old_check_url(self.archive, self.config, url))
                    self.assertEqual(self.check(url)['known'], saved)
                    if saved:
                        self.assertEqual(self.check(url)['source'], 'S-aaaaaaaaaa')

    def test_saved_url_spelled_differently_on_disk(self) -> None:
        # The stored side normalizes too: an upper-case host, http, a trailing
        # slash and throwaway params on the RECORD still match a clean page URL.
        self.source('census/b_S-bbbbbbbbbb.md', 'S-bbbbbbbbbb',
                    'http://WWW.FamilySearch.ORG/ark:/61903/3:1:ZZ99/?cc=1&lang=en#x')
        self.assertTrue(self.check('https://familysearch.org/ark:/61903/3:1:ZZ99')['known'])
        # urlparse drops a tab or newline inside a URL, so a record whose URL
        # YAML spelled across an escaped newline still reads as the same page.
        self.write('inbox/c.notes.md',
                   '---\ntitle: x\nexternal_links:\n  - url: "https://x.org/re\\ncord/77"\n---\n')
        self.assertEqual(self.check('https://x.org/record/77'),
                         _old_check_url(self.archive, self.config, 'https://x.org/record/77'))
        self.assertTrue(self.check('https://x.org/record/77')['known'])

    def test_clipping_id_is_the_identity(self) -> None:
        self.source('newspapers/clip_S-cccccccccc.md', 'S-cccccccccc',
                    'https://www.newspapers.com/image/123/?clipping_id=111')
        for warm in (False, True):
            with self.subTest(warm=warm):
                self.assertTrue(self.check(
                    'https://newspapers.com/image/123?_phsrc=x&clipping_id=111')['known'])
                self.assertFalse(self.check(
                    'https://www.newspapers.com/image/123/?clipping_id=222')['known'])
                self.assertFalse(self.check('https://www.newspapers.com/image/123/')['known'])


# ── Same answers as the old full scan, on many records ───────────────────────

class EquivalenceTestCase(_ArchiveCase):
    def setUp(self) -> None:
        super().setUp()
        self.urls = []
        for i in range(120):
            u = f'https://www.ancestry.com/discoveryui-content/view/{1000 + i}:7884?pid={i}'
            self.urls.append(u)
            self.source(f'census/{i // 40}/r{i:03d}_S-r{i:09d}.md', f'S-r{i:09d}', u,
                        date=str(1850 + i))
        for i in range(60):                                  # inbox stubs, some shared pages
            u = (self.urls[i] if i % 5 == 0
                 else f'https://www.familysearch.org/ark:/61903/1:1:M{i:03d}')
            self.urls.append(u)
            self.write(f'inbox/batch{i % 3}/stub{i:03d}.notes.md',
                       f'---\ntitle: stub {i}\ncreated: 2026-07-{1 + i % 28:02d}\n'
                       f'external_links:\n  - "{u}"\n---\n')
        # A page saved twice: the first in walk order is the one named.
        self.source('census/0/a-dup_S-dddddddddd.md', 'S-dddddddddd', self.urls[7])
        # Records the old scan skipped, or read oddly - they must read the same.
        self.write('sources/census/broken_S-eeeeeeeeee.md',
                   '---\nid: S-eeeeeeeeee\nexternal_links: [unclosed\n---\n')
        self.write('sources/census/cp1252_S-ffffffffff.md', '',
                   raw=('---\nid: S-ffffffffff\ntitle: M\xfcller\nexternal_links:\n'
                        f'  - url: "{self.urls[3]}x"\n---\n').encode('cp1252'))
        self.write('sources/census/nofront_S-gggggggggg.md', f'Just notes. {self.urls[4]}\n')
        self.write('sources/census/mapping_S-hhhhhhhhhh.md',
                   '---\nid: S-hhhhhhhhhh\nexternal_links:\n  "https://example.org/map/1": x\n---\n')
        self.write('sources/census/claims-bad_S-jjjjjjjjjj.md',
                   '---\nid: S-jjjjjjjjjj\nexternal_links:\n  - url: https://example.org/cb/2\n---\n'
                   '\n## Claims\n\n```yaml\n- [oops\n```\n')
        # People: names, aliases, id aliases, case duplicates, templates, folders.
        given = ['Anna', 'Anne', 'Johann', 'Mary', 'Marie', 'Ezra', 'Ida', 'Otto']
        for i in range(90):
            first = given[i % len(given)]
            aliases = [f'{first[0]}. Smith{i % 4}'] + (['P-0000000001'] if i % 7 == 0 else [])
            self.person(f'people/{i % 9:03d} Couple/{first.lower()}_{i:03d}.md',
                        f'{first} Smith{i % 4}', *aliases)
        self.person('people/connections/MARY SMITH1.md', 'MARY SMITH1')     # case duplicate
        self.write('people/_TEMPLATE.md', '---\nname: Template Person\n---\n')
        self.write('people/stubs/bad.md', '---\nname: [unclosed\n---\n')
        self.write('people/stubs/noname.md', '---\nid: P-1111111111\n---\n')

    def assert_same(self) -> None:
        # Every shared page and every 7th other one keeps the old reference
        # (a full parse per probe) affordable; the real-archive run in the PR
        # compared every saved URL.
        probes = [u for i, u in enumerate(self.urls) if i % 7 == 0 or i in (3, 4, 7, 90)]
        probes += [u.replace('https://www.', 'http://') + '/' for u in self.urls[::20]]
        probes += ['https://example.org/map/1', 'https://example.org/cb/2',
                   self.urls[3] + 'x', self.urls[4].split('?')[0] + 'zz', 'https://nowhere.test/a']
        for url in probes:
            with self.subTest(url=url):
                self.assertEqual(self.check(url), _old_check_url(self.archive, self.config, url))
        for q in ('', 'a', 'smith1', 'MARY', ' ida ', 'j. s', 'template', 'p-00', 'zzz'):
            for limit in (8, 1000):
                with self.subTest(q=q, limit=limit):
                    self.assertEqual(
                        capture._host_suggest_names(self.archive, self.config, q, limit),
                        _old_suggest_names(self.archive, self.config, q, limit))

    def test_matches_old_scan_cold_then_warm(self) -> None:
        self.assert_same()          # cold: the cache is being written
        self.assertTrue((self.archive / '.cache' / 'capture_links.json').is_file())
        self.assertTrue((self.archive / '.cache' / 'capture_names.json').is_file())
        self.assert_same()          # warm: answered from it

    def test_matches_old_scan_after_edits_moves_and_deletes(self) -> None:
        self.assert_same()
        # Edit, delete, add, and move records between requests.
        self.source('census/0/r005_S-r000000005.md', 'S-r000000005', 'https://new.example/x/5')
        (self.archive / 'sources/census/1/r050_S-r000000050.md').unlink()
        self.source('census/9/late_S-kkkkkkkkkk.md', 'S-kkkkkkkkkk', self.urls[90])
        (self.archive / 'inbox/batch1/stub001.notes.md').rename(
            self.archive / 'inbox/batch2/stub001.notes.md')
        self.person('people/000 Couple/anna_000.md', 'Annaliese Brandt', 'Lise')
        (self.archive / 'people/001 Couple/anne_001.md').unlink()
        self.assert_same()


# ── Fresh the moment a record changes: never a false "not saved" ─────────────

class FreshnessTestCase(_ArchiveCase):
    def setUp(self) -> None:
        super().setUp()
        for i in range(20):
            self.source(f'census/r{i:02d}_S-r{i:09d}.md', f'S-r{i:09d}', f'https://a.example/rec/{i}')
            self.person(f'people/p{i:02d}.md', f'Person Number{i}')
        self.check('https://a.example/warm-up')
        self.names('')

    def test_a_page_saved_a_moment_ago_reads_as_saved(self) -> None:
        self.assertFalse(self.check('https://b.example/new/1')['known'])
        # Just written - inside the settle window, mtime "now".
        self.write('inbox/new.notes.md',
                   '---\ntitle: new\nexternal_links:\n  - url: https://b.example/new/1\n---\n',
                   settled=False)
        self.assertTrue(self.check('https://b.example/new/1')['known'])
        self.assertTrue(self.check('https://b.example/new/1')['known'])

    def test_an_edit_that_adds_or_removes_a_link_is_seen(self) -> None:
        self.assertFalse(self.check('https://c.example/z/9')['known'])
        p = self.source('census/r03_S-r000000003.md', 'S-r000000003', 'https://c.example/z/9')
        self.assertTrue(self.check('https://c.example/z/9')['known'])
        self.assertFalse(self.check('https://a.example/rec/3')['known'])
        p.unlink()
        self.assertFalse(self.check('https://c.example/z/9')['known'])

    def test_same_size_edit_with_new_mtime_is_seen(self) -> None:
        p = self.archive / 'people/p04.md'
        before = p.stat().st_size
        self.write('people/p04.md', p.read_text(encoding='utf-8').replace('Number4', 'Numbe44'))
        self.assertEqual(p.stat().st_size, before)
        self.assertIn('Person Numbe44', self.names('numbe44'))
        self.assertNotIn('Person Number4', self.names('number4'))

    def test_a_just_saved_person_is_offered_and_rechecked(self) -> None:
        self.write('people/new.md', '---\nname: Zelda Quist\n---\n', settled=False)
        self.assertEqual(self.names('zelda'), ['Zelda Quist'])
        # Rewritten inside the settle window - possibly the same mtime tick.
        self.write('people/new.md', '---\nname: Zelda Quinn\n---\n', settled=False)
        self.assertEqual(self.names('zelda'), ['Zelda Quinn'])

    def test_a_deleted_person_is_no_longer_offered(self) -> None:
        (self.archive / 'people/p07.md').unlink()
        self.assertNotIn('Person Number7', self.names('number7'))


# ── Unrelated records are not parsed again (fails on the old full scan) ──────

class ParseSpyTestCase(_ArchiveCase):
    def setUp(self) -> None:
        super().setUp()
        for i in range(40):
            self.source(f'census/r{i:02d}_S-r{i:09d}.md', f'S-r{i:09d}', f'https://a.example/rec/{i}')
            self.person(f'people/p{i:02d}.md', f'Person Number{i}')

    def test_check_url_parses_only_what_changed(self) -> None:
        self.assertEqual(self.count_parses(lambda: self.check('https://a.example/none')), 40)
        self.assertEqual(self.count_parses(lambda: self.check('https://a.example/none')), 0)
        self.assertEqual(self.count_parses(lambda: self.check('https://a.example/rec/39')), 0)
        self.source('census/r12_S-r000000012.md', 'S-r000000012', 'https://a.example/rec/x12')
        self.assertEqual(self.count_parses(lambda: self.check('https://a.example/none')), 1)

    def test_suggest_names_parses_only_what_changed(self) -> None:
        self.assertEqual(self.count_parses(lambda: self.names('p')), 40)
        for q in ('pe', 'per', 'pers', 'perso'):            # a burst of keystrokes
            self.assertEqual(self.count_parses(lambda: self.names(q)), 0)
        self.person('people/p05.md', 'Renamed Person')
        self.assertEqual(self.count_parses(lambda: self.names('ren')), 1)
        self.assertEqual(self.names('ren'), ['Renamed Person'])


# ── A damaged or unwritable cache costs speed, never a wrong answer ──────────

class DamagedCacheTestCase(_ArchiveCase):
    def setUp(self) -> None:
        super().setUp()
        self.source('census/a_S-aaaaaaaaaa.md', 'S-aaaaaaaaaa', 'https://a.example/rec/1')
        self.person('people/a.md', 'Ada Lovelace', 'Countess Ada')
        self.cache = self.archive / '.cache'

    def assert_answers(self) -> None:
        self.assertEqual(self.check('https://a.example/rec/1')['source'], 'S-aaaaaaaaaa')
        self.assertFalse(self.check('https://a.example/rec/2')['known'])
        self.assertEqual(self.names('ada'), ['Ada Lovelace', 'Countess Ada'])

    def test_garbage_wrong_version_and_wrong_shape(self) -> None:
        self.assert_answers()
        links, names = self.cache / 'capture_links.json', self.cache / 'capture_names.json'
        key_s = os.path.abspath(self.archive / 'sources/census/a_S-aaaaaaaaaa.md')
        key_p = os.path.abspath(self.archive / 'people/a.md')
        st_s, st_p = Path(key_s).stat(), Path(key_p).stat()
        for bad_links, bad_names in [
            ('not json', '{'),
            (json.dumps({'version': 999, 'files': {}}), json.dumps([1, 2])),
            # Right size and mtime but a value of the wrong shape.
            (json.dumps({'version': 1, 'files': {key_s: [st_s.st_mtime_ns, st_s.st_size, 5]}}),
             json.dumps({'version': 1, 'files': {key_p: [st_p.st_mtime_ns, st_p.st_size, [1]]}})),
        ]:
            with self.subTest(bad_links=bad_links[:20]):
                links.write_text(bad_links, encoding='utf-8')
                names.write_text(bad_names, encoding='utf-8')
                self.assert_answers()

    def test_stale_entry_for_a_changed_file_is_not_trusted(self) -> None:
        self.assert_answers()
        links = self.cache / 'capture_links.json'
        data = json.loads(links.read_text(encoding='utf-8'))
        # Pretend the cache remembers a link the file no longer has: the stat
        # no longer matches, so the record is read again.
        for entry in data['files'].values():
            entry[2]['hrefs'] = ['https://a.example/rec/2']
            entry[0] -= 1
        links.write_text(json.dumps(data), encoding='utf-8')
        self.assert_answers()

    def test_unwritable_cache_still_answers(self) -> None:
        self.cache.write_text('a file where the folder should be', encoding='utf-8')
        self.assert_answers()
        self.assertTrue(self.cache.is_file())


if __name__ == '__main__':
    unittest.main()
