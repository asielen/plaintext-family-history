import argparse
import contextlib
import io
import os
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import xref
from index import _DDL
from _lib import index_manifest_path, record_path_manifest, write_path_manifest


def _make_index(archive_root: Path) -> sqlite3.Connection:
    """Build a synthetic .cache/index.sqlite with the real schema."""
    cache = archive_root / '.cache'
    cache.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache / 'index.sqlite'))
    conn.executescript(_DDL)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_claim(conn, cid, source_id, ctype, value, *, date_edtf=None,
                   place_text=None, place_id=None, subtype=None, negated=0,
                   status='accepted', persons=(), roles=None):
    conn.execute(
        '''INSERT INTO claims(id, source_id, type, subtype, date_edtf, place_id,
                               place_text, value, status, negated)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (cid, source_id, ctype, subtype, date_edtf, place_id, place_text, value,
         status, negated),
    )
    roles = roles or {}
    for pos, pid in enumerate(persons):
        conn.execute(
            'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
            (cid, pid, pos, roles.get(pid)),
        )


class XrefTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _seed_persons_sources(self):
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-aaaaaaaaaa','Test Person','false','curated','x.md')")
        self.conn.execute("INSERT INTO sources(id, title, path) VALUES "
                           "('s-1111111111','Source One','a.md')")
        self.conn.execute("INSERT INTO sources(id, title, path) VALUES "
                           "('s-2222222222','Source Two','b.md')")

    def test_overlapping_dates_corroborate(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born about 1840', date_edtf='1840~', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1840-03-02', date_edtf='1840-03-02', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['groups']), 1)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_non_overlapping_dates_contradict(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1900', date_edtf='1900', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_overlapping_vital_dates_but_different_place_contradicts(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840 in New York', date_edtf='1840~',
                       place_text='New York', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1840 in Ohio', date_edtf='1840~',
                       place_text='Ohio', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_overlapping_vital_value_places_contradict_without_place_text(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born in New York', date_edtf='1840~', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born in Ohio', date_edtf='1840~', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_overlapping_vital_wording_without_place_still_corroborates(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born about 1840', date_edtf='1840~', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1840-03-02', date_edtf='1840-03-02', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_birth_claim_naming_a_parent_does_not_enter_the_parents_own_birth_bucket(self) -> None:
        # #126's xref twin: a mother's OWN birth claim and her son's birth
        # claim (which names her as `roles: {parent: [...]}`) used to land in
        # the same ('birth',) bucket for her, purely because persons: named
        # her on both - the same "everyone named" bucketing #63 already fixed
        # for marriage/divorce. Non-overlapping vital dates on two records of
        # two different people's births must never read as a contradiction.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Son','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1888', date_edtf='1888', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1918 to Test Person', date_edtf='1918',
                       persons=['p-bbbbbbbbbb', 'p-aaaaaaaaaa'],
                       roles={'p-bbbbbbbbbb': 'child', 'p-aaaaaaaaaa': 'parent'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_death_claim_with_no_roles_at_all_naming_two_people_enters_neither_bucket(
            self) -> None:
        # #126, reopened: a death claim naming two people with NO roles: map
        # at all (not even a partial one, unlike the roled parent/child shape
        # above) has not said which of them died - `vital_subjects` now
        # answers [] for this shape rather than "everyone named". Neither
        # person's own death bucket may be entered by a claim that has not
        # actually said it is about them.
        #
        # #172: unlike before, this claim must not just vanish - it is the
        # vital twin of the roles-less marriage certificate, and gets the
        # same `unscoped` treatment so a human can see it and add a roles:
        # map.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Kin','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'death',
                       'died 1940', date_edtf='1940', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'death',
                       'visited the grave in 1940, no roles noted',
                       date_edtf='1940', persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])
        unscoped_ids = [item['claim']['id'] for item in result['unscoped']]
        self.assertEqual(unscoped_ids, ['c-bbbbbbbbbb'])
        self.assertEqual(
            sorted(result['unscoped'][0]['persons']),
            sorted(['Test Person', 'Kin']),
        )

    def test_needs_review_vital_claim_with_no_roles_also_added_to_unscoped(self) -> None:
        # #172's specific gap over `fha lint`'s W132: W132 fires only on
        # ACCEPTED, non-negated claims (the derivation bargain W125/W126
        # share), but `fha xref` compares BOTH accepted and needs-review
        # claims (the module's own `_run_xref_queries` query). A needs-review
        # claim of the identical zero-role-signal shape must still be
        # reported here, even though it never trips W132.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Kin','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'death',
                       'died 1940, status still under review', date_edtf='1940',
                       status='needs-review', persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])
        unscoped_ids = [item['claim']['id'] for item in result['unscoped']]
        self.assertEqual(unscoped_ids, ['c-aaaaaaaaaa'])

    def test_birth_claim_with_no_roles_at_all_added_to_unscoped(self) -> None:
        # Finding 2's scope explicitly includes birth, not just
        # death/burial/baptism (W132 covers only the latter three - birth
        # already has its own W126 built on a different rule,
        # `_lib.parentage_parties` - but `_OTHER_VITAL_TYPES` in this file
        # includes birth, and the same zero-role-signal shape must not
        # silently drop out of `fha xref` for it either).
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Sibling','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'birth record names two people, no roles', date_edtf='1900',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])
        unscoped_ids = [item['claim']['id'] for item in result['unscoped']]
        self.assertEqual(unscoped_ids, ['c-aaaaaaaaaa'])

    def test_death_claim_with_shared_deceased_role_not_added_to_unscoped(self) -> None:
        # SPEC §8.3's `roles: deceased:` (this branch's #173 follow-up) names
        # both subjects of a shared death directly - `vital_subjects` case 3,
        # resolved fine. This must NOT land in `unscoped`: the claim answered
        # the question, it just answered it for two people at once.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Co-victim','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'death',
                       'both died in the same accident, 1940', date_edtf='1940',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'deceased', 'p-bbbbbbbbbb': 'deceased'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['unscoped'], [])

    def test_death_claim_with_old_unroled_convention_not_added_to_unscoped(self) -> None:
        # The older, still-valid convention (`vital_subjects` case 4): name
        # everyone else's part and leave the decedent unroled. The claim
        # answered the question via silence-about-one-person, not
        # silence-about-everyone, so it must not be reported as unscoped.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Widow','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'death',
                       'died 1940, survived by his wife', date_edtf='1940',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-bbbbbbbbbb': 'spouse'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['unscoped'], [])

    def test_negated_death_claim_naming_two_people_no_roles_stays_unscoped(
            self) -> None:
        # #173 follow-up, SECOND round (post-merge Codex review): the first
        # round's fix treated a `negated: true`, zero-role, 2+-person vital
        # claim as automatically "about everyone named" (modeled on the
        # marriage/divorce negation bullet), reasoning that a confirmed
        # "neither of them died" has answered the question for everyone it
        # names. Codex's follow-up review caught the over-correction: that
        # reasoning only holds for a GENUINE joint negation. The identical
        # zero-role shape is ALSO how a negated claim naming the real subject
        # plus an incidental bystander looks ("it wasn't A who died - B just
        # happened to be there too") - there is no way to tell the two apart
        # from the claim alone, which is exactly `vital_subjects`'s case 2a
        # ambiguity (#126), and negation only flips the assertion's polarity,
        # not which named person the claim is about. So this shape reverts to
        # the SAME `unscoped` treatment its positive counterpart already gets
        # (test_death_claim_with_no_roles_at_all_naming_two_people_enters_
        # neither_bucket above) - safe, consistent "ask a human via roles:"
        # behavior, not a regression to the pre-#173 bug (which was about a
        # POSITIVE ambiguous claim being silently mis-scoped as "everyone",
        # a different problem than "an ambiguous claim isn't auto-compared
        # until a human disambiguates it").
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Kin','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'death',
                       'neither died, contrary to a rumor', date_edtf='1940',
                       negated=1, persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'death',
                       'died 1940', date_edtf='1940', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        # Excluded from every comparison bucket - no fabricated contradiction
        # against Test Person's real death claim - and reported in `unscoped`
        # instead so a human can add `roles: deceased:` to say which of them
        # (if not both) the negation is actually about.
        self.assertEqual(result['groups'], [])
        unscoped_ids = [item['claim']['id'] for item in result['unscoped']]
        self.assertEqual(unscoped_ids, ['c-aaaaaaaaaa'])

    def test_negated_bystander_death_claim_does_not_contradict_bystanders_own_death(
            self) -> None:
        # The concrete failure case Codex's follow-up finding named: a
        # negated claim naming the real subject (A) and an incidental
        # bystander (B, "just happened to be there too") with no roles: map
        # must not read as contradicting B's own, unrelated, accepted death
        # claim. Guessing "about both" here (the over-corrected behavior)
        # would flag B's real death record as contradicting a negation that
        # was never about B in the first place.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Bystander','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'death',
                       "it wasn't Test Person who died - Bystander was just there too",
                       date_edtf='1940', negated=1,
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'death',
                       'Bystander died 1962, unrelated record', date_edtf='1962',
                       persons=['p-bbbbbbbbbb'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        # No group at all: the negated claim is unscoped (not in either
        # person's bucket), so Bystander's own real death claim never gets
        # compared against it.
        self.assertEqual(result['groups'], [])
        unscoped_ids = [item['claim']['id'] for item in result['unscoped']]
        self.assertEqual(unscoped_ids, ['c-aaaaaaaaaa'])

    def test_negated_death_claim_with_shared_deceased_role_compares_fine(self) -> None:
        # A roled negation is NOT the ambiguous case 2a shape and must keep
        # comparing normally either way: `roles: deceased:` (SPEC §8.3)
        # explicitly names who a negation is about, the same as it does for a
        # positive claim (test_death_claim_with_shared_deceased_role_not_
        # added_to_unscoped above) - there is nothing left to disambiguate,
        # so this is not affected by the case 2a reversion at all.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Kin','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'death',
                       'neither of them died in the wreck, contrary to reports',
                       date_edtf='1940', negated=1,
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'deceased', 'p-bbbbbbbbbb': 'deceased'})
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'death',
                       'died 1940', date_edtf='1940', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['unscoped'], [])
        self.assertEqual(len(result['groups']), 1)
        group = result['groups'][0]
        self.assertEqual(group['person_name'], 'Test Person')
        self.assertEqual(len(group['pairs']), 1)
        pair = group['pairs'][0]
        self.assertEqual(pair['kind'], 'contradicts')
        self.assertEqual(
            {pair['claim_a']['id'], pair['claim_b']['id']},
            {'c-aaaaaaaaaa', 'c-bbbbbbbbbb'},
        )

    def test_same_source_pair_excluded(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-1111111111', 'birth',
                       'born 1840 again', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_already_linked_pair_excluded(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1840 also', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        self.conn.execute(
            "INSERT INTO claim_links(claim_id, rel, target_id) VALUES ('c-aaaaaaaaaa','corroborates','c-bbbbbbbbbb')"
        )
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_different_type_not_paired(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'occupation',
                       'worked as a clerk', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_non_overlapping_substantive_dates_not_paired(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'residence',
                       'lived in Topeka', date_edtf='1880', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'residence',
                       'lived in Wichita', date_edtf='1900', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_census_claims_with_head_household_roles_still_compare(self) -> None:
        # Regression guard: `vital_subjects` must be scoped to genuine vital
        # types only (_VITAL_TYPES minus the couple types), never applied to
        # a substantive type like census. A census claim's `roles: {head:
        # [...], household_member: [...]}` legitimately roles EVERY person on
        # the record, so vital_subjects (which looks for whoever the roles:
        # map left UNROLED) would find nobody and wrongly empty this person's
        # census bucket entirely if it were consulted here - silently
        # breaking every census comparison archive-wide, not just this one.
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'census',
                       '1880 census, head of household', date_edtf='1880',
                       persons=['p-aaaaaaaaaa'], roles={'p-aaaaaaaaaa': 'head'})
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'census',
                       '1880 census, household member', date_edtf='1880',
                       persons=['p-aaaaaaaaaa'], roles={'p-aaaaaaaaaa': 'household_member'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(len(result['groups']), 1)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_duplicate_person_row_keeps_first_role_for_vital_subject(self) -> None:
        # claim_persons has no UNIQUE constraint (persons: [P-a,
        # "[[Alice Smith]]"] is one person written two ways and lands twice,
        # per the module's own ORDER BY position comment above). The FIRST
        # occurrence's role must win when building claim_role, same as every
        # other first-occurrence-wins contract in this codebase - otherwise a
        # duplicate row with a blank/different role can silently overwrite
        # the real one (e.g. the birth subject's own 'child' role) and
        # vital_subjects reads the wrong - or no - subject for the claim.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Parent','false','curated','y.md')")
        self.conn.execute(
            "INSERT INTO claims(id, source_id, type, date_edtf, value, status, negated) "
            "VALUES ('c-aaaaaaaaaa','s-1111111111','birth','1918','born 1918','accepted',0)"
        )
        # p-aaaaaaaaaa (the baby) appears TWICE: first correctly roled
        # 'child', then a duplicate (e.g. an ID entry after a name-alias
        # entry) mistakenly roled 'parent' - the trap a plain dict assignment
        # falls into, since it's inserted AFTER the correct row. A blank
        # duplicate role would still get caught by vital_subjects' own
        # "unroled" fallback case regardless of which role wins, so this
        # needs a duplicate with a DIFFERENT real role to actually diverge:
        # last-wins reads p-a as only ever 'parent' (never 'child'), which
        # `vital_subjects` cannot satisfy from ANY of its four cases (not the
        # subject role, and not unroled either) - subjects becomes [], and
        # p-a's own birth claim is wrongly excluded from her own bucket.
        self.conn.execute(
            "INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES "
            "('c-aaaaaaaaaa','p-aaaaaaaaaa',0,'child')"
        )
        self.conn.execute(
            "INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES "
            "('c-aaaaaaaaaa','p-bbbbbbbbbb',1,'parent')"
        )
        self.conn.execute(
            "INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES "
            "('c-aaaaaaaaaa','p-aaaaaaaaaa',2,'parent')"
        )
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1920, contradicting record', date_edtf='1920',
                       persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        # If the duplicate's blank role had won, p-aaaaaaaaaa would read as
        # NOT a subject of c-aaaaaaaaaa (nobody there carries 'child'), the
        # claim would be excluded from her own bucket, and there would be no
        # group/pair at all. The first ('child') role winning means it stays
        # in her bucket and compares against her other birth claim.
        self.assertEqual(len(result['groups']), 1)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_matching_place_id_corroborates_despite_place_text_wording(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840', date_edtf='1840', place_id='l-aaaaaaaaaa',
                       place_text='Fairview, Breton County, Kansas', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1840', date_edtf='1840', place_id='l-aaaaaaaaaa',
                       place_text='Fairview City, Breton, Kansas', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_differing_place_id_contradicts_even_with_similar_text(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840', date_edtf='1840', place_id='l-aaaaaaaaaa',
                       place_text='Fairview, Kansas', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1840', date_edtf='1840', place_id='l-bbbbbbbbbb',
                       place_text='Fairview, Kansas', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_negation_polarity_mismatch_contradicts_regardless_of_dates(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'marriage',
                       'married 1870', date_edtf='1870', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'marriage',
                       'never married', date_edtf=None, negated=1, persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_relationship_claims_with_different_roles_not_paired(self) -> None:
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Parent','false','curated','y.md')")
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-cccccccccc','Child','false','curated','z.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'relationship',
                       'child of parent', subtype='child-of',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'child', 'p-bbbbbbbbbb': 'parent'})
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'relationship',
                       'parent of child', subtype='child-of',
                       persons=['p-aaaaaaaaaa', 'p-cccccccccc'],
                       roles={'p-aaaaaaaaaa': 'parent', 'p-cccccccccc': 'child'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_absent_index_returns_failed_status(self) -> None:
        self.conn.close()
        empty_root = Path(tempfile.mkdtemp())
        try:
            result = xref.run_xref(empty_root)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['groups'], [])
        finally:
            import shutil
            shutil.rmtree(empty_root, ignore_errors=True)

    def test_non_overlapping_burial_dates_contradict(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'burial',
                       'buried 1840', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'burial',
                       'buried 1900', date_edtf='1900', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_overlapping_burial_value_places_contradict_without_place_text(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'burial',
                       'buried in Springfield', date_edtf='1840~', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'burial',
                       'buried in Fairview', date_edtf='1840~', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_negated_substantive_claim_not_overlapping_positive_not_paired(self) -> None:
        # A negated 1880 residence and a positive 1900 residence for the same
        # person are not in tension - they describe different periods, so
        # the non-overlap exclusion for recurring types should still apply
        # even when polarity differs.
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'residence',
                       'did not reside in Topeka', date_edtf='1880', negated=1,
                       persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'residence',
                       'resided in Topeka', date_edtf='1900', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_negated_substantive_claim_overlapping_positive_contradicts(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'residence',
                       'did not reside in Topeka', date_edtf='1880', negated=1,
                       persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'residence',
                       'resided in Topeka', date_edtf='1880', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_negated_substantive_claim_different_place_not_paired(self) -> None:
        # A negated residence claim about one place and a positive residence
        # claim about a different place, same year, aren't in conflict -
        # both can be true at once.
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'residence',
                       'did not reside in Topeka', date_edtf='1880', negated=1,
                       place_text='Topeka', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'residence',
                       'resided in Boston', date_edtf='1880',
                       place_text='Boston', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_negated_substantive_claim_same_place_text_contradicts(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'residence',
                       'did not reside there', date_edtf='1880', negated=1,
                       place_text='Topeka', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'residence',
                       'resided there', date_edtf='1880',
                       place_text='Topeka', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_marriage_to_different_spouse_not_paired(self) -> None:
        # Marriage claims always share the literal role "spouse" for both
        # parties, so role can't distinguish one marriage from another for
        # the same person - the counterpart must. A first-marriage claim and
        # a second-marriage claim (different spouse) shouldn't be compared.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Spouse One','false','curated','y.md')")
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-cccccccccc','Spouse Two','false','curated','z.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'marriage',
                       'married Spouse One', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-bbbbbbbbbb': 'spouse'})
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'marriage',
                       'married Spouse Two', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-cccccccccc'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-cccccccccc': 'spouse'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_marriage_to_same_spouse_corroborates(self) -> None:
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Spouse','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'marriage',
                       'married Spouse', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-bbbbbbbbbb': 'spouse'})
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'marriage',
                       'married Spouse', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-bbbbbbbbbb': 'spouse'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_negated_marriage_with_no_spouse_named_contradicts_positive_marriage(self) -> None:
        # "Never married" proof claims name no spouse, so they can't be
        # bucketed by counterpart the way a normal marriage claim is - they
        # have to be compared against every marriage claim for this person.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Spouse','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'marriage',
                       'never married', date_edtf='1870', negated=1,
                       persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'marriage',
                       'married Spouse', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-bbbbbbbbbb': 'spouse'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_divorce_from_different_spouse_not_paired(self) -> None:
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Spouse One','false','curated','y.md')")
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-cccccccccc','Spouse Two','false','curated','z.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'divorce',
                       'divorced Spouse One', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-bbbbbbbbbb': 'spouse'})
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'divorce',
                       'divorced Spouse Two', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-cccccccccc'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-cccccccccc': 'spouse'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_vital_place_phrase_excludes_trailing_date_clause(self) -> None:
        # "born in Springfield in 1840" should compare as place "Springfield",
        # not "Springfield in 1840" - the date belongs to date_edtf, not the
        # place phrase, so this isn't a place mismatch with bare "Springfield".
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born in Springfield in 1840', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born in Springfield', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_relationship_claim_bundling_extra_counterpart_still_pairs(self) -> None:
        # One source bundles two children under one parent claim
        # (roles: parent: [P2, P3]); another source only names one of them.
        # The bundled claim should still be compared against the claim that
        # names just the shared counterpart, since both assert the same
        # parent-of-P2 edge.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Child Two','false','curated','y.md')")
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-cccccccccc','Child Three','false','curated','z.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'relationship',
                       'parent of two children', subtype='child-of',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb', 'p-cccccccccc'],
                       roles={'p-aaaaaaaaaa': 'parent', 'p-bbbbbbbbbb': 'child',
                              'p-cccccccccc': 'child'})
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'relationship',
                       'parent of one child', subtype='child-of',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'parent', 'p-bbbbbbbbbb': 'child'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_missing_required_column_returns_failed_status(self) -> None:
        # A cache built against an older claims schema has all the required
        # tables (so the table probe passes) but is missing a column xref's
        # query selects - this must surface the documented incompatible-
        # schema message rather than an uncaught OperationalError.
        self.conn.execute('ALTER TABLE claims RENAME TO claims_old')
        self.conn.execute(
            '''CREATE TABLE claims(
                 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, type TEXT NOT NULL,
                 subtype TEXT, date_edtf TEXT, place_text TEXT, value TEXT NOT NULL,
                 status TEXT NOT NULL, negated INTEGER DEFAULT 0
               )'''
        )
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['groups'], [])

    # ── #63: marriage/divorce bucketing by spouse_parties, not "every other
    # person" ──────────────────────────────────────────────────────────────

    def test_two_records_of_one_marriage_are_compared_via_roles_scoped_certificate(self) -> None:
        # #63's original bug: a six-person certificate correctly scoped by
        # roles: spouse: and a plain two-person claim recording the SAME
        # marriage with a conflicting place must be compared. Bucketing by
        # "every other named person" (the pre-#63 behavior) keys the
        # certificate by all four bystanders and the plain claim by just the
        # other spouse - the two never land in the same bucket, so the
        # contradiction is missed and `fha xref` reports "no
        # contradictions" when it actually means "couldn't tell".
        self._seed_persons_sources()
        for pid, name in (
            ('p-bbbbbbbbbb', 'Bride'),
            ('p-cccccccccc', 'Groom Father'),
            ('p-dddddddddd', 'Groom Mother'),
            ('p-eeeeeeeeee', 'Bride Father'),
            ('p-ffffffffff', 'Bride Mother'),
        ):
            self.conn.execute(
                "INSERT INTO persons(id, name, living, tier, path) VALUES (?,?,'false','curated',?)",
                (pid, name, f'{pid}.md'),
            )
        self.conn.execute(
            "INSERT INTO sources(id, title, path) VALUES ('s-3333333333','Marriage Certificate','c.md')"
        )
        _insert_claim(
            self.conn, 'c-aaaaaaaaaa', 's-3333333333', 'marriage',
            'married in Kansas', date_edtf='1870', place_text='Kansas',
            persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb', 'p-cccccccccc',
                     'p-dddddddddd', 'p-eeeeeeeeee', 'p-ffffffffff'],
            roles={'p-aaaaaaaaaa': 'spouse', 'p-bbbbbbbbbb': 'spouse'},
        )
        _insert_claim(
            self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'marriage',
            'married in Missouri', date_edtf='1870', place_text='Missouri',
            persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
        )
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['status'], 'ok')
        # Only the couple get a bucket for this claim - never the
        # certificate's parents, who are named on it but are not one of the
        # two people roles: spouse: actually calls married (#63's second
        # open question).
        self.assertEqual(
            sorted(g['person_name'] for g in result['groups']),
            ['Bride', 'Test Person'],
        )
        for group in result['groups']:
            self.assertEqual(len(group['pairs']), 1)
            self.assertEqual(group['pairs'][0]['kind'], 'contradicts')
        self.assertEqual(result['unscoped'], [])

    def test_roles_less_certificate_not_compared_against_unrelated_marriage(self) -> None:
        # #63's second probe (the naive-fix regression): a roles-less
        # certificate (no roles: spouse: map, so spouse_parties cannot tell
        # the couple from the parents also named on it) must NOT be compared
        # against the parents' own, unrelated marriage. See
        # test_naive_spouse_parties_substitution_would_fabricate_contradiction
        # below for a concrete demonstration of why the obvious substitution
        # gets this wrong. The certificate should instead be reported in
        # `unscoped`, paired with nothing.
        self._seed_persons_sources()
        self.conn.execute(
            "INSERT INTO persons(id, name, living, tier, path) VALUES "
            "('p-bbbbbbbbbb','Bride','false','curated','y.md')")
        self.conn.execute(
            "INSERT INTO persons(id, name, living, tier, path) VALUES "
            "('p-cccccccccc','Father','false','curated','z.md')")
        self.conn.execute(
            "INSERT INTO persons(id, name, living, tier, path) VALUES "
            "('p-dddddddddd','Mother','false','curated','w.md')")
        self.conn.execute(
            "INSERT INTO sources(id, title, path) VALUES ('s-3333333333','Certificate','c.md')"
        )
        self.conn.execute(
            "INSERT INTO sources(id, title, path) VALUES ('s-4444444444','Parents Marriage','d.md')"
        )
        # The roles-less certificate: groom, bride, and the groom's parents -
        # no roles: map, so spouse_parties cannot tell the couple from the
        # parents.
        _insert_claim(
            self.conn, 'c-aaaaaaaaaa', 's-3333333333', 'marriage',
            'married, certificate names four people', date_edtf='1880',
            persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb', 'p-cccccccccc', 'p-dddddddddd'],
        )
        # The parents' own, properly-scoped 1860 marriage - unrelated to
        # their son's certificate above.
        _insert_claim(
            self.conn, 'c-bbbbbbbbbb', 's-4444444444', 'marriage',
            'married 1860', date_edtf='1860',
            persons=['p-cccccccccc', 'p-dddddddddd'],
            roles={'p-cccccccccc': 'spouse', 'p-dddddddddd': 'spouse'},
        )
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['groups'], [])
        unscoped_ids = [item['claim']['id'] for item in result['unscoped']]
        self.assertEqual(unscoped_ids, ['c-aaaaaaaaaa'])
        self.assertEqual(
            sorted(result['unscoped'][0]['persons']),
            sorted(['Test Person', 'Bride', 'Father', 'Mother']),
        )

    def test_naive_spouse_parties_substitution_would_fabricate_contradiction(self) -> None:
        # #63 measured that the "obvious" fix - swap _lib.spouse_parties in
        # for "every other named person" but keep the ORIGINAL two-bucket
        # split (by_group vs no_counterpart/all_of_type, where no_counterpart
        # entries are compared against every claim of that type) - is not
        # safe: an empty party set still lands a positive, merely-ambiguous
        # certificate in `no_counterpart`, which is the bucket built for
        # genuine negations and gets compared against every marriage claim
        # on file for that person.
        #
        # This reimplements exactly that naive substitution - NOT xref.py's
        # real code, which keeps ambiguous claims out of every bucket (see
        # _run_xref_queries's `unscoped_ids`) - against the same fixture as
        # test_roles_less_certificate_not_compared_against_unrelated_marriage,
        # to prove with real output that the naive approach fabricates a
        # contradiction between a certificate and an unrelated marriage.
        from _lib import spouse_parties as _spouse_parties

        cert = {
            'id': 'c-cert', 'source_id': 's-cert', 'type': 'marriage',
            'date_edtf': '1880', 'place_id': None, 'place_text': None,
            'value': 'married, certificate names four people', 'negated': 0,
        }
        parents_marriage = {
            'id': 'c-parents', 'source_id': 's-parents', 'type': 'marriage',
            'date_edtf': '1860', 'place_id': None, 'place_text': None,
            'value': 'married 1860', 'negated': 0,
        }
        cert_persons = [
            ('p-groom', None), ('p-bride', None),
            ('p-father', None), ('p-mother', None),
        ]
        parents_persons = [('p-father', 'spouse'), ('p-mother', 'spouse')]

        # The father's bucket, built the naive way.
        by_group: dict = {}
        no_counterpart: list = []
        all_of_type: list = []
        for claim, persons_with_roles in (
            (cert, cert_persons), (parents_marriage, parents_persons),
        ):
            others = frozenset(
                p for p in _spouse_parties(persons_with_roles) if p != 'p-father'
            )
            all_of_type.append(claim['id'])
            if others:
                by_group.setdefault(others, []).append(claim['id'])
            else:
                no_counterpart.append(claim['id'])

        # The certificate's spouse_parties is empty (no roles: map says
        # which two of the four are the couple), so the naive substitution
        # lands it in no_counterpart - and no_counterpart is compared
        # against every marriage claim for the father, including his own,
        # unrelated 1860 marriage.
        self.assertEqual(no_counterpart, ['c-cert'])
        self.assertIn('c-parents', all_of_type)

        kind = xref._classify_pair(cert, parents_marriage)
        self.assertEqual(
            kind, 'contradicts',
            'the naive substitution would report the certificate as '
            "contradicting the father's own, unrelated marriage")

    def test_negated_marriage_still_compares_broadly_after_scoping_fix(self) -> None:
        # Two-sided guard for #63: a genuine negation (SPEC §8.6) with no
        # spouse named must still be compared against every other marriage
        # claim for that person - the same behavior no_counterpart/
        # all_of_type gave before this fix (see also
        # test_negated_marriage_with_no_spouse_named_contradicts_positive_marriage
        # above). A negated claim is identified from its OWN `negated`
        # field, never from an empty spouse_parties result, so this must
        # keep working even though the bucketing now runs every
        # marriage/divorce claim through spouse_parties first.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Spouse','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'divorce',
                       'no divorce on record', date_edtf=None, negated=1,
                       persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'divorce',
                       'divorced Spouse', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-bbbbbbbbbb': 'spouse'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_serial_marriage_claim_still_compares_against_single_partner_claim(self) -> None:
        # spouse_parties' serial case (SPEC §8.3): one claim's roles: spouse:
        # names 3+ people, recording successive marriages on one record (e.g.
        # an obituary: "survived by his wife Mary, predeceased by his first
        # wife Jane"). Bucketing that claim by the WHOLE remaining party set
        # ({Jane, Mary}) instead of once per individual partner reintroduces
        # #63's exact bug one level up: a plain marriage certificate naming
        # just {Test Person, Mary} keys its own bucket by {Mary} alone, so
        # the two records of the Test Person/Mary marriage would never land
        # in the same bucket and the contradiction/corroboration check would
        # silently miss them - "no candidates" meaning "couldn't tell", not
        # "nothing to find".
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-jjjjjjjjjj','Jane','false','curated','j.md')")
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-mmmmmmmmmm','Mary','false','curated','m.md')")
        self.conn.execute("INSERT INTO sources(id, title, path) VALUES "
                           "('s-3333333333','Obituary','o.md')")
        _insert_claim(
            self.conn, 'c-aaaaaaaaaa', 's-3333333333', 'marriage',
            'survived by wife Mary, predeceased by first wife Jane',
            persons=['p-aaaaaaaaaa', 'p-jjjjjjjjjj', 'p-mmmmmmmmmm'],
            roles={'p-aaaaaaaaaa': 'spouse', 'p-jjjjjjjjjj': 'spouse', 'p-mmmmmmmmmm': 'spouse'},
        )
        _insert_claim(
            self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'marriage',
            'married Mary in Ohio', date_edtf='1849',
            persons=['p-aaaaaaaaaa', 'p-mmmmmmmmmm'],
            roles={'p-aaaaaaaaaa': 'spouse', 'p-mmmmmmmmmm': 'spouse'},
        )
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['unscoped'], [])
        self.assertEqual(
            sorted(g['person_name'] for g in result['groups']),
            ['Mary', 'Test Person'],
        )
        for group in result['groups']:
            self.assertEqual(
                [(p['claim_a']['id'], p['claim_b']['id']) for p in group['pairs']],
                [('c-aaaaaaaaaa', 'c-bbbbbbbbbb')],
            )

    def test_unscoped_persons_list_dedupes_a_person_named_twice(self) -> None:
        # `_lib.spouse_parties`'s own docstring: a claim can name the same
        # person twice - `persons: [P-a, "[[Alice Smith]]"]` is one man
        # written two ways - and `claim_persons` stores a row per entry with
        # no UNIQUE constraint to stop it. The `unscoped` list's `persons`
        # display must not repeat that person's name just because their row
        # appears twice.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Bride','false','curated','y.md')")
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-cccccccccc','Father','false','curated','z.md')")
        self.conn.execute("INSERT INTO sources(id, title, path) VALUES "
                           "('s-3333333333','Certificate','c.md')")
        # Roles-less (ambiguous -> unscoped), with the groom named twice -
        # once by id, once via an alias that resolved to the same person.
        _insert_claim(
            self.conn, 'c-aaaaaaaaaa', 's-3333333333', 'marriage',
            'married, certificate names the groom twice', date_edtf='1880',
            persons=['p-aaaaaaaaaa', 'p-aaaaaaaaaa', 'p-bbbbbbbbbb', 'p-cccccccccc'],
        )
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(len(result['unscoped']), 1)
        self.assertEqual(
            result['unscoped'][0]['persons'],
            ['Test Person', 'Bride', 'Father'],
        )

    def test_cmd_xref_prints_separate_sections_for_couple_and_vital_unscoped_claims(
            self) -> None:
        # #172: the CLI's "could not be scoped for comparison" section used
        # to name only marriage/divorce claims. With a roles-less certificate
        # AND a zero-role-signal death claim both unscoped in the same run,
        # the printed output must call out both kinds - each with its own
        # count and its own repair wording, not one sentence stretched to
        # cover both.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Bride','false','curated','y.md')")
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-cccccccccc','Kin','false','curated','z.md')")
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-dddddddddd','Bride Father','false','curated','w.md')")
        self.conn.execute("INSERT INTO sources(id, title, path) VALUES "
                           "('s-3333333333','Certificate','c.md')")
        # Three people, no roles: map at all - spouse_parties cannot tell the
        # couple from the third person, so this stays ambiguous (a plain
        # two-person, no-roles claim would resolve fine and never reach
        # `unscoped` at all).
        _insert_claim(
            self.conn, 'c-aaaaaaaaaa', 's-3333333333', 'marriage',
            'married, certificate names three people, no roles', date_edtf='1880',
            persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb', 'p-dddddddddd'],
        )
        _insert_claim(
            self.conn, 'c-bbbbbbbbbb', 's-1111111111', 'death',
            'died 1940, no roles noted', date_edtf='1940',
            persons=['p-aaaaaaaaaa', 'p-cccccccccc'],
        )
        self.conn.commit()
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        write_path_manifest(index_manifest_path(self.archive_root),
                             record_path_manifest(self.archive_root))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = xref._cmd_xref(argparse.Namespace(root=str(self.archive_root)))
        output = buf.getvalue()

        self.assertEqual(exit_code, xref.EXIT_CLEAN)
        self.assertIn('1 marriage/divorce claim(s) could not be scoped for comparison', output)
        self.assertIn('1 birth/death/baptism/burial claim(s) could not be scoped for comparison',
                       output)
        self.assertIn('which two of them were the couple', output)
        self.assertIn('which of them died', output)


class XrefStdoutIsValidUtf8Tests(unittest.TestCase):
    """Issue #64's sibling scope: `xref.py` was one of the 11 confirmed
    missing-`configure_utf8_stdout()` modules alongside lint.py. Unlike
    lint.py's fixed-string ellipsis, xref's non-ASCII text is entirely
    data-carried (`_fmt_claim` prints a claim's own `place_text`/`value`
    verbatim) - this covers that other shape of the bug, and doubles as the
    "one CLI-heavy tool besides lint.py" end-to-end case called for
    alongside the source-inspection sweep in test_tools_utf8_stdout.py.

    Same technique as LintStdoutIsValidUtf8Tests in test_lint.py: a real
    subprocess, with `PYTHONIOENCODING=cp1252` pinning the interpreter's
    startup stdout encoding to the Windows console/redirect default this bug
    needs, portably (this need not run on Windows to prove the issue)."""

    def _build_archive_with_corroborating_place(self) -> Path:
        """Two accepted birth claims for the same person, overlapping dates,
        sharing an accented place name with no `place_id` - xref's
        place-fallback path (`_place_from_vital_value`/`place_text`)
        classifies this pair as corroborating and `_fmt_claim` prints the
        place text verbatim, guaranteeing the accented text reaches
        stdout."""
        root = Path(tempfile.mkdtemp())
        conn = _make_index(root)
        conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                     "('p-aaaaaaaaaa','Test Person','false','curated','x.md')")
        conn.execute("INSERT INTO sources(id, title, path) VALUES "
                     "('s-1111111111','Source One','a.md')")
        conn.execute("INSERT INTO sources(id, title, path) VALUES "
                     "('s-2222222222','Source Two','b.md')")
        _insert_claim(conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                      'born in Kraków', date_edtf='1840', place_text='Kraków',
                      persons=['p-aaaaaaaaaa'])
        _insert_claim(conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                      'born 1840 Kraków', date_edtf='1840', place_text='Kraków',
                      persons=['p-aaaaaaaaaa'])
        conn.commit()
        conn.close()
        (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        # #48: this synthetic index.sqlite is hand-built via raw DDL,
        # bypassing build_index/upsert_source - the only two places that
        # write the #48 path manifest - so without this, open_index_db's
        # additive manifest check finds no manifest at all and (correctly,
        # per the bootstrapping rule) reads fha.yaml, just written above, as
        # newly "added", i.e. stale - a spurious "index may be stale"
        # warning on a subprocess run this test does not otherwise expect
        # one from.
        write_path_manifest(index_manifest_path(root), record_path_manifest(root))
        return root

    def test_accented_place_survives_redirected_stdout_as_valid_utf8(self) -> None:
        root = self._build_archive_with_corroborating_place()
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'cp1252'  # the Windows-default this bug needs
        proc = subprocess.run(
            [sys.executable, str(ROOT / 'tools' / 'xref.py'), '--root', str(root)],
            capture_output=True, check=False, env=env,
        )

        # A pure-ASCII substring check: once fixed, stdout is genuinely
        # UTF-8, and decoding genuine UTF-8 bytes as cp1252 mojibakes the
        # accented character (e.g. 'Kraków' -> 'KrakÃ³w') even though the
        # bytes are now correct - so this sanity check must not assume a
        # cp1252 decode reproduces the original text, only that the run
        # actually reached xref's corroboration-printing code path at all.
        stdout_lossy = proc.stdout.decode('cp1252')
        self.assertIn('corroborates', stdout_lossy, 'fixture did not print '
                       f'the corroborating pair - nothing to reproduce the issue with.\n{stdout_lossy}')
        self.assertIn('Krak', stdout_lossy)

        try:
            decoded = proc.stdout.decode('utf-8')
        except UnicodeDecodeError as e:
            self.fail(
                'xref stdout was not valid UTF-8 under a cp1252 default '
                f'stdout encoding (issue #64): {e}. Raw bytes near the '
                f'failure: {proc.stdout[max(0, e.start - 8):e.start + 8]!r}'
            )
        else:
            self.assertIn('Kraków', decoded)


if __name__ == '__main__':
    unittest.main()
