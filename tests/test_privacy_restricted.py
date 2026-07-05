"""
test_privacy_restricted.py - the generalized `restricted` marker (datamodel-gaps
chunk 04, SPEC §8.4/§9/§18/§19/§21).

`restricted` may sit on a source, a claim, a person, or a name (a `name_variants`
entry). These tests prove the export contract every path shares:

  - by-request never leaks under any flag combination, on any path;
  - dna needs its own --include-dna (--include-restricted never opens it);
  - a restricted claim / person / name is excluded from public output;
  - a deadname variant resolves internally (no E004) but redacts on export.

Each tool reads the claim/person/name-level marker from the record .md files
(the index carries none of them), so the fixtures here write real records with
`restricted:` frontmatter / claim fields and `{value:, restricted: true}` name
variants, alongside the synthetic index rows the tools query - the same
synthetic-index pattern as test_packet.py / test_site.py / test_gedcom.py.

IDs use only the Crockford alphabet (no i, l, o, u) because gedcom/wikitree
validate them with `is_valid_id`.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import packet
import gedcom
import wikitree
import lint
from _lib import read_record
from index import _DDL as INDEX_DDL

# site.py's module stem collides with the stdlib `site`; load it by path.
_spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site'] = site
_spec.loader.exec_module(site)

# Crockford-valid 10-char ids (no i/l/o/u).
P_SUBJECT = 'p-aaaaaaaaaa'
P_SECRET = 'p-bbbbbbbbbb'
P_PUBLIC = 'p-cccccccccc'
P_JANE = 'p-dddddddddd'
S_BYREQUEST = 's-aaaaaaaaaa'
S_DNA = 's-bbbbbbbbbb'
S_PLAIN = 's-dddddddddd'
S_MIXED = 's-eeeeeeeeee'


# ── Shared fixture builder ─────────────────────────────────────────────────────

class _Archive:
    """A throwaway archive: real record files plus a synthetic index built from
    the live DDL. Records are written to disk because the restriction markers at
    the claim/person/name level live only in the files."""

    def __init__(self, root: Path):
        self.root = root
        cache = root / '.cache'
        cache.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(cache / 'index.sqlite'))
        self.conn.executescript(INDEX_DDL)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    def fresh(self):
        """Commit and stamp the index newer than every record so freshness
        checks pass (the exporters refuse a stale index)."""
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.root / '.cache' / 'index.sqlite', (future, future))

    def person(self, pid, name='Test Person', *, living='false', tier='curated',
               surname='Person', sex='M', restricted=None, name_variants=None,
               aliases=None, body=None):
        rel = f'people/{surname.lower()}__test_{pid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = [f'id: {pid}', f'name: {name}', f'sex: {sex}', f'living: {living}']
        if restricted is not None:
            fm.append(f'restricted: {restricted}')
        if aliases:
            fm.append('aliases:')
            fm.extend(f'  - {a}' for a in aliases)
        if name_variants:
            fm.append('name_variants:')
            for v in name_variants:
                if isinstance(v, dict):
                    inner = ', '.join(f'{k}: {val}' for k, val in v.items())
                    fm.append(f'  - {{{inner}}}')
                else:
                    fm.append(f'  - {v}')
        text = '---\n' + '\n'.join(fm) + '\n---\n' + (body or f'# {name}\n')
        path.write_text(text, encoding='utf-8')
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, name, surname, sex, living, tier, 'active', rel),
        )
        # Mirror name + variant values into the aliases table so name-links
        # resolve, exactly as fha index would (variant mappings contribute their
        # `value`).
        self._alias(name, pid, 'name')
        for v in (name_variants or []):
            value = v.get('value') if isinstance(v, dict) else v
            if value:
                self._alias(str(value), pid, 'variant')
        return path

    def source(self, sid, title='A Source', *, source_type='census', restricted=None,
               index_restricted=0, citation='A citation.', people=(), claims=()):
        rel = f'sources/{source_type}/src_{sid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = [f'id: {sid}', f'title: {title}', f'source_type: {source_type}',
              f'citation: "{citation}"']
        if restricted is not None:
            fm.append(f'restricted: {restricted}')
        lines = ['---', *fm, '---', '', '## Claims', '```yaml']
        for c in claims:
            # `id:` is optional on hand-written claims (the quickstart teaches
            # id-less claims): a claim dict without one writes a valid id-less
            # entry - the exact shape round-2 finding 1 leaked through.
            entry = [f'id: {c["id"]}'] if c.get('id') else []
            entry.append(f'type: {c["type"]}')
            entry.append(f'value: "{c["value"]}"')
            entry.append(f'persons: [{c["person"]}]')
            entry.append(f'status: {c.get("status", "accepted")}')
            entry.append('confidence: high')
            if c.get('date'):
                entry.append(f'date: {c["date"]}')
            if c.get('restricted') is not None:
                entry.append(f'restricted: {c["restricted"]}')
            lines.append(f'- {entry[0]}')
            lines.extend(f'  {e}' for e in entry[1:])
        lines.append('```')
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO sources(id, title, source_type, restricted, status, path) '
            'VALUES (?,?,?,?,?,?)',
            (sid, title, source_type, index_restricted, 'active', rel),
        )
        for pid in people:
            self.conn.execute(
                'INSERT INTO source_people(source_id, person_id) VALUES (?,?)', (sid, pid))
        for c in claims:
            if not c.get('id'):
                # fha index drops any claim without a valid C-id (index.py's
                # claims loop), so the synthetic index mirrors that: the
                # record file is an id-less claim's ONLY carrier.
                continue
            mn = (c['date'] or '')[:4] + '-01-01' if c.get('date') else None
            self.conn.execute(
                'INSERT INTO claims(id, source_id, type, value, status, date_edtf, date_min) '
                'VALUES (?,?,?,?,?,?,?)',
                (c['id'], sid, c['type'], c['value'], c.get('status', 'accepted'),
                 c.get('date'), mn),
            )
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position) VALUES (?,?,?)',
                (c['id'], c['person'], 0))
        return path

    def rel(self, a, r, b, claim_id='c-eeeeeeeeee'):
        self.conn.execute(
            'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
            (a, r, b, claim_id))

    def _alias(self, text, cid, kind):
        self.conn.execute(
            'INSERT INTO aliases(alias, canonical_id, kind) VALUES (?,?,?)',
            (str(text).strip().lower(), cid, kind))


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.a = _Archive(Path(self._tmp.name))
        self.root = self.a.root

    def tearDown(self):
        self.a.close()
        self._tmp.cleanup()


# ── packet ─────────────────────────────────────────────────────────────────────

class PacketRestrictedTests(_Base):
    def _build(self, pid=P_SUBJECT, **kw):
        self.a.fresh()
        return packet.run_packet(self.root, pid, self.root / 'out',
                                 no_photos=True, **kw)

    def _copied_source(self, res, sid=S_MIXED):
        return (res['packet_dir'] / 'sources' / f'src_{sid}.md').read_text(encoding='utf-8')

    def test_by_request_source_never_included_even_with_all_flags(self):
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.source(S_BYREQUEST, 'By-request source', restricted='by-request',
                      people=(P_SUBJECT,))
        res = self._build(include_restricted=True, include_dna=True)
        self.assertEqual(res['status'], 'ok')
        # The source record is never copied; it stays on the excluded list.
        self.assertFalse((res['packet_dir'] / 'sources').exists())

    def test_dna_needs_its_own_flag(self):
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.source(S_DNA, 'DNA source', source_type='dna',
                      restricted='dna', index_restricted=1, people=(P_SUBJECT,))
        # --include-restricted alone does NOT open a DNA source.
        res = self._build(include_restricted=True)
        self.assertFalse((res['packet_dir'] / 'sources').exists())
        # --include-dna does.
        res2 = self._build(include_dna=True, overwrite=True)
        self.assertTrue((res2['packet_dir'] / 'sources').exists())

    def test_plain_restricted_source_opens_with_include_restricted(self):
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.source(S_PLAIN, 'Plain restricted', restricted='true',
                      index_restricted=1, people=(P_SUBJECT,))
        excluded = self._build()
        self.assertFalse((excluded['packet_dir'] / 'sources').exists())
        included = self._build(include_restricted=True, overwrite=True)
        self.assertTrue((included['packet_dir'] / 'sources').exists())

    def test_restricted_claim_dropped_from_timeline(self):
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.source(
            S_MIXED, 'Mixed source', people=(P_SUBJECT,),
            claims=[
                {'id': 'c-aaaaaaaaaa', 'type': 'residence', 'value': 'lived in Kansas',
                 'person': P_SUBJECT, 'date': '1900'},
                {'id': 'c-bbbbbbbbbb', 'type': 'death', 'value': 'cause of death suicide',
                 'person': P_SUBJECT, 'date': '1950', 'restricted': 'true'},
            ])
        res = self._build()
        timeline = (res['packet_dir'] / 'timeline.md').read_text(encoding='utf-8')
        self.assertIn('lived in Kansas', timeline)
        self.assertNotIn('cause of death suicide', timeline)

    def test_by_request_claim_never_ships_in_copied_source(self):
        # The timeline filter alone is not enough: the copied source record
        # itself must not carry the by-request claim's YAML under ANY flag
        # combination (TOOLING §8: by-request is never included by any flag).
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.source(
            S_MIXED, 'Mixed source', people=(P_SUBJECT,),
            claims=[
                {'id': 'c-aaaaaaaaaa', 'type': 'residence', 'value': 'lived in Kansas',
                 'person': P_SUBJECT, 'date': '1900'},
                {'id': 'c-bbbbbbbbbb', 'type': 'note', 'value': 'asked to be left out',
                 'person': P_SUBJECT, 'restricted': 'by-request'},
            ])
        for flags in ({}, {'include_restricted': True},
                      {'include_restricted': True, 'include_dna': True}):
            res = self._build(overwrite=True, **flags)
            self.assertEqual(res['status'], 'ok', flags)
            copied = self._copied_source(res)
            self.assertIn('lived in Kansas', copied, flags)
            self.assertNotIn('asked to be left out', copied, flags)
            self.assertNotIn('c-bbbbbbbbbb', copied, flags)

    def test_plain_restricted_claim_ships_only_with_include_restricted(self):
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.source(
            S_MIXED, 'Mixed source', people=(P_SUBJECT,),
            claims=[
                {'id': 'c-aaaaaaaaaa', 'type': 'residence', 'value': 'lived in Kansas',
                 'person': P_SUBJECT},
                {'id': 'c-bbbbbbbbbb', 'type': 'death', 'value': 'cause of death suicide',
                 'person': P_SUBJECT, 'restricted': 'true'},
            ])
        withheld = self._build()
        self.assertNotIn('cause of death suicide', self._copied_source(withheld))
        opened = self._build(include_restricted=True, overwrite=True)
        self.assertIn('cause of death suicide', self._copied_source(opened))

    def test_redacted_source_copy_stays_a_valid_record(self):
        # The cut must leave a parseable record: the surviving claim is still
        # read back by the shared reader, the withheld one is gone.
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.source(
            S_MIXED, 'Mixed source', people=(P_SUBJECT,),
            claims=[
                {'id': 'c-aaaaaaaaaa', 'type': 'residence', 'value': 'lived in Kansas',
                 'person': P_SUBJECT},
                {'id': 'c-bbbbbbbbbb', 'type': 'note', 'value': 'kept private',
                 'person': P_SUBJECT, 'restricted': 'by-request'},
            ])
        res = self._build()
        rec = read_record(res['packet_dir'] / 'sources' / f'src_{S_MIXED}.md')
        self.assertEqual(rec['parse_errors'], [])
        self.assertEqual([c.get('id') for c in rec['claims']], ['c-aaaaaaaaaa'])

    def test_readme_counts_withheld_claims_in_plain_words(self):
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.source(
            S_MIXED, 'Mixed source', people=(P_SUBJECT,),
            claims=[
                {'id': 'c-aaaaaaaaaa', 'type': 'residence', 'value': 'lived in Kansas',
                 'person': P_SUBJECT},
                {'id': 'c-bbbbbbbbbb', 'type': 'note', 'value': 'kept private',
                 'person': P_SUBJECT, 'restricted': 'true'},
                {'id': 'c-cccccccccc', 'type': 'note', 'value': 'also private',
                 'person': P_SUBJECT, 'restricted': 'by-request'},
            ])
        res = self._build()
        readme = (res['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn(
            f'2 private facts were left out of src_{S_MIXED}.md; '
            'they stay in your archive.', readme)

    def test_unseparable_restricted_claim_fails_closed(self):
        # A hand-unfenced Claims section still parses through the forgiving
        # reader (so the restricted marker IS seen), but its lines cannot be
        # cut safely - the record must be left out entirely, never shipped
        # verbatim with the private claim inside.
        self.a.person(P_SUBJECT, 'Subject Person')
        path = self.a.source(
            S_MIXED, 'Mixed source', people=(P_SUBJECT,),
            claims=[
                {'id': 'c-aaaaaaaaaa', 'type': 'residence', 'value': 'lived in Kansas',
                 'person': P_SUBJECT},
                {'id': 'c-bbbbbbbbbb', 'type': 'note', 'value': 'kept private',
                 'person': P_SUBJECT, 'restricted': 'by-request'},
            ])
        text = path.read_text(encoding='utf-8').replace('```yaml\n', '').replace('```', '')
        path.write_text(text, encoding='utf-8')
        res = self._build()
        self.assertEqual(res['status'], 'ok')
        self.assertFalse((res['packet_dir'] / 'sources' / f'src_{S_MIXED}.md').exists())
        self.assertTrue(any('left out of sources/' in m for m in res['messages']))

    def test_idless_by_request_claim_never_ships_in_copied_source(self):
        # Round-2 finding 1: `id:` is optional on hand-written claims, and the
        # old withheld set was keyed by C-id - an id-less restricted claim
        # never entered it and the record was byte-copied, README silent. The
        # withhold must not require an id, under ANY flag combination for
        # by-request, and the README must count the id-less cut too.
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.source(
            S_MIXED, 'Mixed source', people=(P_SUBJECT,),
            claims=[
                {'id': 'c-aaaaaaaaaa', 'type': 'residence', 'value': 'lived in Kansas',
                 'person': P_SUBJECT},
                {'type': 'note', 'value': 'asked to be left out',
                 'person': P_SUBJECT, 'restricted': 'by-request'},
            ])
        for flags in ({}, {'include_restricted': True},
                      {'include_restricted': True, 'include_dna': True}):
            res = self._build(overwrite=True, **flags)
            self.assertEqual(res['status'], 'ok', flags)
            copied = self._copied_source(res)
            self.assertIn('lived in Kansas', copied, flags)
            self.assertNotIn('asked to be left out', copied, flags)
            readme = (res['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
            self.assertIn(
                f'1 private fact was left out of src_{S_MIXED}.md; '
                'it stays in your archive.', readme, flags)

    def test_idless_plain_restricted_claim_opens_with_flag(self):
        # The id-less withhold follows the same flag logic as everything
        # else: plain `restricted: true` is withheld by default and opens
        # with --include-restricted.
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.source(
            S_MIXED, 'Mixed source', people=(P_SUBJECT,),
            claims=[
                {'id': 'c-aaaaaaaaaa', 'type': 'residence', 'value': 'lived in Kansas',
                 'person': P_SUBJECT},
                {'type': 'death', 'value': 'cause of death suicide',
                 'person': P_SUBJECT, 'restricted': 'true'},
            ])
        withheld = self._build()
        self.assertNotIn('cause of death suicide', self._copied_source(withheld))
        opened = self._build(include_restricted=True, overwrite=True)
        self.assertIn('cause of death suicide', self._copied_source(opened))

    def test_idless_restricted_claim_stays_out_of_timeline(self):
        # Pin the asymmetry the copy fix leans on: the timeline reads the
        # index, and fha index drops id-less claims entirely (the fixture
        # mirrors that), so the copied record file is the ONLY surface an
        # id-less claim can leak through. If the index ever started keeping
        # id-less claims, this pin breaks and the timeline filter must learn
        # about them too.
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.source(
            S_MIXED, 'Mixed source', people=(P_SUBJECT,),
            claims=[
                {'id': 'c-aaaaaaaaaa', 'type': 'residence', 'value': 'lived in Kansas',
                 'person': P_SUBJECT, 'date': '1900'},
                {'type': 'death', 'value': 'cause of death suicide',
                 'person': P_SUBJECT, 'date': '1950', 'restricted': 'true'},
            ])
        res = self._build()
        timeline = (res['packet_dir'] / 'timeline.md').read_text(encoding='utf-8')
        self.assertIn('lived in Kansas', timeline)
        self.assertNotIn('cause of death suicide', timeline)

    def test_malformed_claims_source_not_copied(self):
        # read_record reports parse_errors (claims read as []) for a claims
        # block that will not parse - the old code saw "nothing to withhold"
        # and byte-copied the record, private text and all. Fail closed: no
        # copy, a warning naming the fix, a README count, and none of the
        # record's indexed claims in the timeline.
        self.a.person(P_SUBJECT, 'Subject Person')
        path = self.a.source(
            S_MIXED, 'Mixed source', people=(P_SUBJECT,),
            claims=[
                {'id': 'c-aaaaaaaaaa', 'type': 'residence', 'value': 'lived in Kansas',
                 'person': P_SUBJECT, 'date': '1900'},
                {'id': 'c-bbbbbbbbbb', 'type': 'death', 'value': 'cause of death suicide',
                 'person': P_SUBJECT, 'date': '1950', 'restricted': 'true'},
            ])
        text = path.read_text(encoding='utf-8').replace(
            '```yaml\n', '```yaml\n- {broken: [\n', 1)
        path.write_text(text, encoding='utf-8')
        res = self._build()
        self.assertEqual(res['status'], 'ok')
        self.assertFalse((res['packet_dir'] / 'sources' / f'src_{S_MIXED}.md').exists())
        self.assertTrue(any('left out of sources/' in m and 'fha lint' in m
                            for m in res['messages']))
        readme = (res['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('Left out for privacy', readme)
        self.assertIn('could not be read', readme)
        timeline = (res['packet_dir'] / 'timeline.md').read_text(encoding='utf-8')
        self.assertNotIn('cause of death suicide', timeline)
        self.assertNotIn('lived in Kansas', timeline)

    def test_stray_nonclaim_entry_fails_closed(self):
        # A bullet that parses as a plain string is not a claim mapping - its
        # restricted-ness cannot even be checked - so the record is left out
        # (fail closed) rather than byte-copied around the doubt.
        self.a.person(P_SUBJECT, 'Subject Person')
        path = self.a.source(
            S_MIXED, 'Mixed source', people=(P_SUBJECT,),
            claims=[{'id': 'c-aaaaaaaaaa', 'type': 'residence',
                     'value': 'lived in Kansas', 'person': P_SUBJECT}])
        text = path.read_text(encoding='utf-8').replace(
            '\n```\n', '\n- a stray sentence, not a claim\n```\n')
        path.write_text(text, encoding='utf-8')
        res = self._build()
        self.assertEqual(res['status'], 'ok')
        self.assertFalse((res['packet_dir'] / 'sources' / f'src_{S_MIXED}.md').exists())
        self.assertTrue(any('left out of sources/' in m for m in res['messages']))

    def test_restricted_name_variant_stripped_from_profile_copy(self):
        # A deadname recorded as {value:, restricted: true} - and its mirror in
        # aliases: - must not ship in the copied profile; the public name and
        # unrestricted variants survive.
        self.a.person(P_JANE, 'Jane Hartley', surname='Hartley',
                      aliases=['John Hartley'],
                      name_variants=[{'value': 'John Hartley', 'restricted': 'true'},
                                     'Janie'])
        res = self._build(pid=P_JANE)
        self.assertEqual(res['status'], 'ok')
        copied = next((res['packet_dir'] / 'profile').glob('*.md')).read_text(encoding='utf-8')
        self.assertNotIn('John Hartley', copied)
        self.assertIn('Janie', copied)
        self.assertIn('Jane Hartley', copied)
        readme = (res['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('2 private names were left out of', readme)
        self.assertIn('they stay in your archive', readme)

    def test_wrapped_and_nested_alias_mirrors_stripped(self):
        # Round-2 finding 5: an alias mirroring a hidden name may be authored
        # as a quoted wikilink ("[[John Hartley]]") or an unquoted
        # [[John Hartley]] (which YAML parses to a nested list). Both resolve
        # as live aliases everywhere else, so both must be stripped when the
        # variant they mirror is withheld - and the README count must include
        # them (it used to say "1 private name" while two carriers of the
        # name still printed it).
        self.a.person(P_JANE, 'Jane Hartley', surname='Hartley',
                      aliases=['"[[John Hartley]]"', '[[John Hartley]]'],
                      name_variants=[{'value': 'John Hartley', 'restricted': 'true'},
                                     'Janie'])
        res = self._build(pid=P_JANE)
        self.assertEqual(res['status'], 'ok')
        copied = next((res['packet_dir'] / 'profile').glob('*.md')).read_text(encoding='utf-8')
        self.assertNotIn('John Hartley', copied)
        self.assertIn('Janie', copied)
        self.assertIn('Jane Hartley', copied)
        readme = (res['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('3 private names were left out of', readme)

    def test_by_request_name_variant_never_ships(self):
        self.a.person(P_JANE, 'Jane Hartley', surname='Hartley',
                      name_variants=[{'value': 'Secret Name', 'restricted': 'by-request'}])
        res = self._build(pid=P_JANE, include_restricted=True, include_dna=True)
        self.assertEqual(res['status'], 'ok')
        copied = next((res['packet_dir'] / 'profile').glob('*.md')).read_text(encoding='utf-8')
        self.assertNotIn('Secret Name', copied)

    def test_plain_restricted_name_variant_opens_with_flag(self):
        # TOOLING §8: --include-restricted "opens plain restrictions on a
        # source, claim, person, or name" - the plain-restricted prior name
        # ships only under the flag.
        self.a.person(P_JANE, 'Jane Hartley', surname='Hartley',
                      name_variants=[{'value': 'John Hartley', 'restricted': 'true'}])
        withheld = self._build(pid=P_JANE)
        copied = next((withheld['packet_dir'] / 'profile').glob('*.md')).read_text(encoding='utf-8')
        self.assertNotIn('John Hartley', copied)
        opened = self._build(pid=P_JANE, include_restricted=True, overwrite=True)
        copied = next((opened['packet_dir'] / 'profile').glob('*.md')).read_text(encoding='utf-8')
        self.assertIn('John Hartley', copied)

    def test_by_request_subject_refused(self):
        self.a.person(P_SUBJECT, 'Private Person', restricted='by-request')
        res = self._build(include_restricted=True, include_dna=True)
        self.assertEqual(res['status'], 'restricted-subject')
        self.assertFalse((self.root / 'out').exists())

    def test_plain_restricted_subject_opens_with_flag(self):
        self.a.person(P_SUBJECT, 'Private Person', restricted='true')
        refused = self._build()
        self.assertEqual(refused['status'], 'restricted-subject')
        ok = self._build(include_restricted=True)
        self.assertEqual(ok['status'], 'ok')


# ── site (public output) ───────────────────────────────────────────────────────

class SiteRestrictedTests(_Base):
    def _run(self):
        self.a.fresh()
        return site.run_site(self.root, self.root / '.cache' / 'site', linked=False)

    def _page(self, rel):
        return (self.root / '.cache' / 'site' / rel).read_text(encoding='utf-8')

    def test_restricted_person_gets_no_page(self):
        self.a.person(P_PUBLIC, 'Public Person')
        self.a.person(P_SECRET, 'Secret Person', restricted='by-request')
        res = self._run()
        self.assertEqual(res['status'], 'ok')
        self.assertTrue((self.root / '.cache' / 'site' / 'persons' / f'{P_PUBLIC}.html').exists())
        self.assertFalse((self.root / '.cache' / 'site' / 'persons' / f'{P_SECRET}.html').exists())

    def test_restricted_source_gets_no_page_via_free_text_type(self):
        # index_restricted stays 0 - the marker is a free-text type only in the file.
        self.a.person(P_PUBLIC, 'Public Person')
        self.a.source(S_BYREQUEST, 'Private source', restricted='by-request',
                      people=(P_PUBLIC,))
        self._run()
        self.assertFalse((self.root / '.cache' / 'site' / 'sources' / f'{S_BYREQUEST}.html').exists())

    def test_restricted_claim_withheld_from_source_page(self):
        self.a.person(P_PUBLIC, 'Public Person')
        self.a.source(
            S_MIXED, 'Mixed source', people=(P_PUBLIC,),
            claims=[
                {'id': 'c-aaaaaaaaaa', 'type': 'residence', 'value': 'lived in Kansas',
                 'person': P_PUBLIC},
                {'id': 'c-bbbbbbbbbb', 'type': 'death', 'value': 'cause of death suicide',
                 'person': P_PUBLIC, 'restricted': 'true'},
            ])
        self._run()
        html = self._page(f'sources/{S_MIXED}.html')
        self.assertIn('lived in Kansas', html)
        self.assertNotIn('cause of death suicide', html)

    def test_deadname_resolves_internally_and_redacts_on_export(self):
        # The deadname is a restricted name variant. A prose link to it must
        # resolve (so the link works) yet render the current display name.
        body = '# Jane Hartley\n\n## Biography\nFormerly known as [[John Hartley]] in the records.\n'
        self.a.person(P_JANE, 'Jane Hartley', surname='Hartley', body=body,
                      name_variants=[{'value': 'John Hartley', 'restricted': 'true'}])
        self._run()
        html = self._page(f'persons/{P_JANE}.html')
        # The deadname text never appears in the rendered output...
        self.assertNotIn('John Hartley', html)
        # ...but the link resolved to the person's current name (not a dangling cite).
        self.assertIn('Jane Hartley', html)
        self.assertNotIn('<mark>', html)   # no unresolved-token marker


# ── wikitree (public output) ───────────────────────────────────────────────────

class WikitreeRestrictedTests(_Base):
    def _run(self, pid=P_SUBJECT):
        self.a.fresh()
        return wikitree.run_wikitree(self.root, pid)

    def test_restricted_subject_refused_any_value(self):
        self.a.person(P_SUBJECT, 'Private Person', restricted='by-request')
        res = self._run()
        self.assertEqual(res['status'], 'restricted-subject')

    def test_profile_linking_restricted_person_refused(self):
        body = f'# Subject\n\n## Biography\nKnew [[{P_SECRET}]] well.\n'
        self.a.person(P_SUBJECT, 'Subject Person', body=body)
        self.a.person(P_SECRET, 'Secret Person', restricted='true', surname='Secret')
        res = self._run()
        self.assertEqual(res['status'], 'restricted-people')

    def test_profile_citing_by_request_source_refused(self):
        body = f'# Subject\n\n## Biography\nBorn in 1900. [[{S_BYREQUEST}]]\n'
        self.a.person(P_SUBJECT, 'Subject Person', body=body)
        self.a.source(S_BYREQUEST, 'Private source', restricted='by-request',
                      people=(P_SUBJECT,))
        res = self._run()
        self.assertEqual(res['status'], 'restricted-sources')


# ── gedcom (public output) ─────────────────────────────────────────────────────

class GedcomRestrictedTests(_Base):
    def _run(self, **kw):
        self.a.fresh()
        return gedcom.run_gedcom(self.root, P_SUBJECT, mode='connected', **kw)

    def test_restricted_person_name_withheld_no_override(self):
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.person(P_SECRET, 'Secret Person', restricted='by-request', surname='Secret')
        self.a.rel(P_SUBJECT, 'spouse', P_SECRET)
        self.a.rel(P_SECRET, 'spouse', P_SUBJECT)
        # --include-living must NOT lift a restriction.
        res = self._run(include_living=True)
        self.assertEqual(res['status'], 'ok')
        text = res['text']
        self.assertIn('Subject /Person/', text)     # GEDCOM Given /Surname/ form
        self.assertNotIn('Secret', text)            # restricted name fully withheld
        self.assertIn('/Restricted/', text)
        # The structural family link survives (tree shape intact).
        self.assertIn('0 @F1@ FAM', text)

    def test_free_text_restricted_source_not_a_fact_source(self):
        self.a.person(P_SUBJECT, 'Subject Person')
        self.a.source(S_BYREQUEST, 'Private source', restricted='by-request',
                      claims=[{'id': 'c-cccccccccc', 'type': 'birth', 'value': 'born',
                               'person': P_SUBJECT, 'date': '1900'}])
        # Index restricted stays 0; the marker is free-text in the file.
        res = self._run()
        # No BIRT event is emitted from a restricted source.
        self.assertNotIn('BIRT', res['text'])


# ── lint (recognition) ─────────────────────────────────────────────────────────

class LintRecognitionTests(_Base):
    """lint runs from on-disk records (no index), so these write `fha.yaml` and
    call `_run_lint_core` directly for the full findings list."""

    def _findings(self):
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        return findings

    def test_free_text_restricted_value_is_not_an_error(self):
        # A free-text restricted type on a source/claim/person is valid, never an
        # error (the marker is open like subtype).
        self.a.person(P_SUBJECT, 'Subject Person', restricted='by-request')
        self.a.source(S_PLAIN, 'Plain', restricted='by-request',
                      claims=[{'id': 'c-dddddddddd', 'type': 'note', 'value': 'a note',
                               'person': P_SUBJECT, 'restricted': 'true'}])
        codes = {f.code for f in self._findings()}
        self.assertNotIn('E019', codes)   # restricted is not a vocabulary failure
        self.assertNotIn('E010', codes)   # not a schema failure

    def test_dna_satisfied_by_free_text_restricted_type(self):
        # E017 requires a DNA source to be restricted; `restricted: dna` (a
        # free-text value, not the plain boolean) must satisfy it.
        rec_path = self.root / 'sources' / 'dna' / 'src_s-ffffffffff.md'
        rec_path.parent.mkdir(parents=True, exist_ok=True)
        rec_path.write_text(
            '---\nid: s-ffffffffff\ntitle: DNA test\nsource_type: dna\nrestricted: dna\n'
            'files:\n  - file: documents/dna/raw_s-ffffffffff.txt\n    role: primary\n---\n',
            encoding='utf-8')
        (self.root / 'documents' / 'dna').mkdir(parents=True, exist_ok=True)
        (self.root / 'documents' / 'dna' / 'raw_s-ffffffffff.txt').write_text('x', encoding='utf-8')
        e017 = [f for f in self._findings() if f.code == 'E017']
        self.assertEqual(e017, [], f'E017 should not fire for restricted: dna; got {e017}')

    def test_dna_without_restricted_still_fails_e017(self):
        # The guard rail still holds: a DNA source with no restricted flag fails.
        rec_path = self.root / 'sources' / 'dna' / 'src_s-ggggggggg0.md'
        rec_path.parent.mkdir(parents=True, exist_ok=True)
        rec_path.write_text(
            '---\nid: s-ggggggggg0\ntitle: DNA test\nsource_type: dna\n'
            'files:\n  - file: documents/dna/raw_s-ggggggggg0.txt\n    role: primary\n---\n',
            encoding='utf-8')
        (self.root / 'documents' / 'dna').mkdir(parents=True, exist_ok=True)
        (self.root / 'documents' / 'dna' / 'raw_s-ggggggggg0.txt').write_text('x', encoding='utf-8')
        e017 = [f for f in self._findings() if f.code == 'E017']
        self.assertTrue(e017, 'a DNA source with no restricted flag must still fail E017')

    def test_deadname_variant_resolves_no_orphan(self):
        # A `[[prior name]]` link to a restricted name variant must resolve
        # through the alias surface - E004 must NOT fire.
        body = '# Jane Hartley\n\n## Biography\nOnce known as [[John Hartley]].\n'
        self.a.person(P_JANE, 'Jane Hartley', surname='Hartley', body=body,
                      aliases=[P_JANE],
                      name_variants=[{'value': 'John Hartley', 'restricted': 'true'}])
        e004 = [f for f in self._findings() if f.code == 'E004']
        self.assertEqual(e004, [], f'a deadname link should resolve, no E004; got {e004}')


if __name__ == '__main__':
    unittest.main()
