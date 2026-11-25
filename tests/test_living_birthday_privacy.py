"""
test_living_birthday_privacy.py - a living relative's birthday never leaves
the owner's own terminal (SPEC §19, §21; build PR 07-12).

What a relative sees: nothing. The owner keeps a private list of living
cousins' coming birthdays (`fha calendar --birthdays`) so he can send a card.
A birth date is one of the facts most useful to someone stealing an identity,
so no shared copy may carry it, even as a bare day and month with no year:
not the family site (pages, the calendar panel, the search file, the tree
JSON), not a packet, a GEDCOM or WikiTree file, and not a printed report.

The fixture is the Hartley family from tests/test_views_reports.py, with exact
birth dates added for the three people who are not recorded as dead: Lena
(living: true), Otto (living: unknown) and Walter Cole (living: true, a
grandfather). Their dates use years and days nothing else in the fixture
uses, so a leak is a plain text match in any output file.
"""

import contextlib
import datetime
import importlib.util
import io
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom
import index as index_mod
import packet
import views
import wikitree
from _lib import living_birthdays, load_fha_yaml
from tests import test_views_reports as R

_spec = importlib.util.spec_from_file_location('fha_site_birthday_privacy',
                                               ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site_birthday_privacy'] = site
_spec.loader.exec_module(site)

LENA_BORN = '1947-08-23'
OTTO_BORN = '1949-11-05'
WALTER_BORN = '1911-02-17'

# Every way an output could print one of the three dates: the year, the
# ISO day, the day and month in either order (with or without a leading zero
# or an ordinal), in EDTF's yearless form, and with slashes.
LEAK = re.compile(
    r'1947|1949|1911|08-23|11-05|02-17'
    r'|\b23(?:rd)? aug|\baug(?:ust)?\.? 23(?:rd)?\b'
    r'|\b0?5(?:th)? nov|\bnov(?:ember)?\.? 0?5(?:th)?\b'
    r'|\b17(?:th)? feb|\bfeb(?:ruary)?\.? 17(?:th)?\b'
    r'|\b0?8/23\b|\b23/0?8\b|\b11/0?5\b|\b0?5/11\b|\b0?2/17\b|\b17/0?2\b', re.IGNORECASE)


def _leaks(text: str) -> list[str]:
    return [m.group(0) for m in LEAK.finditer(text)]


class _BirthdayBase(R._SheetBase):
    """The Hartley family plus exact birth dates for the living."""

    def sources(self):
        return super().sources() + [
            ('s-lvlvlvlvl1', 'Hartley family Bible, living cousins',
             'Hartley family Bible, cousins page.',
             R._claim('c-lenabirth0', 'birth', [R.CL], LENA_BORN, roles=f'{{child: {R.CL}}}')
             + R._claim('c-ottobirth0', 'birth', [R.CU], OTTO_BORN, roles=f'{{child: {R.CU}}}')
             + R._claim('c-waltbirth0', 'birth', [R.MF], WALTER_BORN,
                        roles=f'{{child: {R.MF}}}'), '', None),
        ]

    def setUp(self):
        super().setUp()
        # The calendar panel and search are the two site features most likely
        # to carry a birthday; switch both on.
        (self.root / 'fha.yaml').write_text(
            'site:\n  archive_name: Test Archive\n  calendar: true\n  search: true\n',
            encoding='utf-8')
        # Curate everyone, the living included: a stub gets no person page,
        # no tree and no packet, so a test over stubs would pass for that
        # reason and never reach the living-person rules it is meant to prove.
        for record in (self.root / 'people').rglob('*.md'):
            record.write_text(record.read_text(encoding='utf-8').replace(
                'tier: stub', 'tier: curated'), encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def assert_no_birthday(self, name: str, text: str) -> None:
        self.assertEqual(_leaks(text), [], f'{name} carries a living relative\'s birthday')

    def assert_folder_clean(self, folder: Path, suffixes=('.html', '.json', '.js', '.md',
                                                         '.txt', '.csv', '.ged', '.xml')) -> int:
        seen = 0
        for path in sorted(folder.rglob('*')):
            if path.is_file() and path.suffix.lower() in suffixes:
                seen += 1
                self.assert_no_birthday(
                    str(path.relative_to(folder)),
                    path.read_text(encoding='utf-8', errors='replace'))
        return seen


class ThePrivateListStillSeesThem(_BirthdayBase):
    """Guard the fixture: the dates are real, so a clean export is not clean
    just because the fixture lost them."""

    def test_the_owner_list_has_the_living_dates(self):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        try:
            found = {e['birth_date'] for e in
                     living_birthdays(conn, datetime.date(2026, 1, 1), 366)['upcoming']}
        finally:
            conn.close()
        self.assertEqual(found, {LENA_BORN, OTTO_BORN})   # Walter is past 110 years (115)


class SharedSiteTests(_BirthdayBase):
    def setUp(self):
        super().setUp()
        self.out = self.root / '.cache' / 'site'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            site.run_site(self.root, self.out, linked=False)

    def test_no_page_json_or_script_of_the_shared_site_carries_a_birthday(self):
        # Prove each surface was built, so a clean scan means a clean site.
        self.assertTrue(list(self.out.rglob(f'*{R.T}*.html')), 'no person page was built')
        self.assertTrue(list(self.out.rglob('tree_*.json')), 'no family-tree data was built')
        self.assertTrue(list(self.out.rglob('search-data.js')), 'search file was not built')
        self.assertGreater(self.assert_folder_clean(self.out), 5)

    def test_the_shared_calendar_panel_leaves_a_living_birthday_out(self):
        home = (self.out / 'index.html').read_text(encoding='utf-8')
        start = home.find('class="family-calendar"')
        self.assertGreater(start, -1, 'the calendar panel was not built')
        panel = home[start:home.find('</section>', start)]
        self.assertIn('Died:', panel)      # the dead are on it
        self.assert_no_birthday('calendar panel', panel)


class ExportTests(_BirthdayBase):
    def test_gedcom_everyone_and_connected_carry_no_living_birthday(self):
        for kwargs in ({'all_persons': True}, {'mode': 'descendants'}, {'mode': 'connected'}):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                res = gedcom.run_gedcom(
                    self.root, None if kwargs.get('all_persons') else R.T, **kwargs)
            self.assertTrue(res['text'], kwargs)
            self.assert_no_birthday(f'gedcom {kwargs}', res['text'])

    def test_gedcom_for_a_living_start_person_is_refused_not_dated(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            res = gedcom.run_gedcom(self.root, R.CL, mode='connected')
        self.assert_no_birthday('gedcom living start', res['text'] or '')

    def test_wikitree_carries_no_living_birthday_and_refuses_a_living_subject(self):
        for pid in (R.T, R.M, R.C1):
            res = wikitree.run_wikitree(self.root, pid)
            self.assert_no_birthday(f'wikitree {pid}', res['text'] or '')
        for pid in (R.CL, R.CU, R.MF):
            res = wikitree.run_wikitree(self.root, pid)
            self.assertEqual(res['status'], 'living-subject', pid)
            self.assert_no_birthday(f'wikitree living {pid}', res['text'] or '')

    def test_packet_folder_and_zip_carry_no_living_birthday(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            res = packet.run_packet(self.root, R.T, out, no_photos=True)
        self.assertEqual(res['status'], 'ok', res.get('messages'))
        self.assertGreater(self.assert_folder_clean(Path(res['packet_dir'])), 1)
        if res.get('zip_path'):
            with zipfile.ZipFile(res['zip_path']) as zf:
                for name in zf.namelist():
                    self.assert_no_birthday(f'zip {name}',
                                            zf.read(name).decode('utf-8', errors='replace'))

    def test_packet_for_a_living_person_is_refused(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        for pid in (R.CL, R.CU, R.MF):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                res = packet.run_packet(self.root, pid, out, no_photos=True)
            self.assertEqual(res['status'], 'living-subject', pid)
        if out.exists():
            self.assert_folder_clean(out)


class PrintedReportTests(_BirthdayBase):
    def test_printed_reports_count_living_relatives_without_dating_them(self):
        # Lena's own Ahnentafel is allowed (she is printed as a label); the
        # others reach the living as children or a grandfather.
        for runner, args in ((views.run_family_sheet, (R.T, None)),
                             (views.run_descendants, (R.T,)),
                             (views.run_descendants, (R.TF,)),
                             (views.run_ahnentafel, (R.C1,)),
                             (views.run_ahnentafel, (R.CL,))):
            for fmt in ('md', 'html'):
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    res = runner(self.root, *args, fmt=fmt)
                self.assertTrue(res.changed, f'{runner.__name__} {args} {fmt} wrote nothing')
                for written in res.changed:
                    self.assert_no_birthday(f'{runner.__name__} {written}',
                                            Path(written).read_text(encoding='utf-8'))


class OnlyTheCalendarCommandReadsTheList(unittest.TestCase):
    """The list is a private view: no shared-output tool may so much as call
    it, so a later change cannot quietly route it into a page or an export."""

    def test_no_other_tool_or_template_mentions_it(self):
        allowed = {'_lib.py', 'anniversaries.py'}
        offenders = []
        for path in sorted((ROOT / 'tools').rglob('*')):
            if path.suffix in ('.py', '.html', '.js', '.json') and path.name not in allowed:
                if 'living_birthdays' in path.read_text(encoding='utf-8', errors='replace'):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
