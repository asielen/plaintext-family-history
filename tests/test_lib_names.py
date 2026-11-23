"""Names that sound alike: the Daitch-Mokotoff and census Soundex codes.

When a surname is spelled four ways across a family's records, `fha find
--sounds-like` puts the spellings together by their sound code. A wrong code
looks exactly like a right one, so every expected value below comes from a
published coding, never from this code's own output. Each block names its
source:

  [JG]   JewishGen, "Soundex Coding" (Gary Mokotoff's coding chart and rules),
         https://www.jewishgen.org/InfoFiles/soundex.html, read 2026-11-22.
         Its worked examples print one code per name (the first branch); a
         name with a two-way letter (CH, CK, C, J, RS, RZ) has more.
  [WP]   Wikipedia, "Daitch-Mokotoff Soundex", revision oldid=1349915463
         (19 April 2026), Examples table: American Soundex and D-M columns.
  [ACC]  Apache Commons Codec, DaitchMokotoffSoundexTest.java at commit
         41565917 (Apache-2.0). Its tests cite the JewishGen page, Avotaynu's
         soundex page and Wikipedia, and were checked against Stephen
         Morse's calculator. Only the expected values are used here, as
         published reference data; none of its code.
  [NARA] U.S. National Archives, "The Soundex Indexing System",
         https://www.archives.gov/research/census/soundex (updated
         9 January 2024).
  [chart] Derived by hand from the [JG] chart, row by row, with the rows
         used written beside the value. These are the ones to re-check on
         a calculator if the rules ever change.

Wikipedia's "Jackson-Jackson" row is not used: its eight codes contradict
the chart (Apache's verified list for the same name differs), so it is a
misprint rather than a reference.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from _lib import (  # noqa: E402
    american_soundex,
    dm_soundex,
    given_name_equivalents,
    name_sound_codes,
    name_sound_words,
    translit,
)


def codes(name: str) -> set[str]:
    return set(dm_soundex(name))


class JewishGenExamplesTests(unittest.TestCase):
    """[JG]: the rules section's examples and the worked examples."""

    def test_rule_examples(self) -> None:
        self.assertEqual(codes('GOLDEN'), {'583600'})      # padded to six digits
        self.assertEqual(codes('Alpert'), {'087930'})      # A coded at the start
        self.assertEqual(codes('Breuer'), {'791900'})      # EU before a vowel
        self.assertEqual(codes('Haber'), {'579000'})       # H at the start
        self.assertEqual(codes('Manheim'), {'665600'})     # H before a vowel
        self.assertEqual(codes('Mintz'), {'664000'})       # TZ is one sound
        self.assertEqual(codes('Topf'), {'370000'})        # PF is one sound
        self.assertEqual(codes('Kleinman'), {'586660'})    # MN/NM coded separately

    def test_two_words_are_coded_as_one(self) -> None:
        # "Ben Aron" is treated as "Benaron".
        self.assertEqual(codes('Ben Aron'), codes('Benaron'))

    def test_worked_examples(self) -> None:
        # The page prints one code each; each must be among ours.
        for name, code in (
            ('AUERBACH', '097500'), ('OHRBACH', '097500'),
            ('LIPSHITZ', '874400'), ('LIPPSZYC', '874400'),
            ('LEWINSKY', '876450'), ('LEVINSKI', '876450'),
            ('SZLAMAWICZ', '486740'), ('SHLAMOVITZ', '486740'),
        ):
            with self.subTest(name=name):
                self.assertIn(code, codes(name))


class WikipediaExamplesTests(unittest.TestCase):
    """[WP]: the multi-code names, exactly."""

    def test_multi_code_names(self) -> None:
        self.assertEqual(codes('Peters'), {'739400', '734000'})
        self.assertEqual(codes('Peterson'), {'739460', '734600'})
        self.assertEqual(codes('Moskowitz'), {'645740'})
        self.assertEqual(codes('Moskovitz'), {'645740'})
        self.assertEqual(codes('Auerbach'), {'097500', '097400'})
        self.assertEqual(codes('Uhrbach'), {'097500', '097400'})
        self.assertEqual(codes('Jackson'), {'154600', '454600', '145460', '445460'})

    def test_american_soundex_column(self) -> None:
        for name, code in (('Peters', 'P362'), ('Peterson', 'P362'),
                           ('Moskowitz', 'M232'), ('Moskovitz', 'M213'),
                           ('Auerbach', 'A612'), ('Uhrbach', 'U612'),
                           ('Jackson', 'J250')):
            with self.subTest(name=name):
                self.assertEqual(american_soundex(name), code)


class ApacheCommonsVectorsTests(unittest.TestCase):
    """[ACC]: Avotaynu's place-name examples, adjacent-code cases, and the
    special letters, each checked there against Stephen Morse's calculator."""

    def test_avotaynu_examples(self) -> None:
        self.assertEqual(codes('Ceniow'), {'467000', '567000'})
        self.assertEqual(codes('Tsenyuv'), {'467000'})
        self.assertEqual(codes('Holubica'), {'587400', '587500'})
        self.assertEqual(codes('Golubitsa'), {'587400'})
        self.assertEqual(codes('Przemysl'), {'746480', '794648'})
        self.assertEqual(codes('Pshemeshil'), {'746480'})
        self.assertEqual(codes('Rosochowaciec'), {
            '944744', '944745', '944754', '944755',
            '945744', '945745', '945754', '945755'})
        self.assertEqual(codes('Rosokhovatsets'), {'945744'})

    def test_all_branches_of_jewishgen_names(self) -> None:
        self.assertEqual(codes('LIPPSZYC'), {'874400', '874500'})
        self.assertEqual(codes('Kleinmann'), {'586660'})
        self.assertEqual(codes('Mannheim'), {'665600'})

    def test_a_repeated_sound_inside_a_two_digit_code(self) -> None:
        # KS codes 54 before a consonant; the S after it repeats the 4.
        self.assertEqual(codes('AKSSOL'), {'054800'})
        self.assertEqual(codes('GERSCHFELD'), {'547830', '545783', '594783', '594578'})

    def test_punctuation_is_ignored(self) -> None:
        self.assertEqual(codes("O'Brien"), {'079600'})
        self.assertEqual(codes('OBrien'), {'079600'})

    def test_german_sharp_s_and_accents(self) -> None:
        self.assertEqual(codes('Straßburg'), {'294795'})
        self.assertEqual(codes('Strasburg'), {'294795'})
        self.assertEqual(codes('Éregon'), {'095600'})
        self.assertEqual(codes('Eregon'), {'095600'})

    def test_romanian_t_cedilla_and_t_comma(self) -> None:
        # [JG] chart: "Ţ (Romanian t-cedilla): 3 or 4" in every position.
        self.assertEqual(codes('ţamas'), {'364000', '464000'})
        self.assertEqual(codes('țamas'), {'364000', '464000'})


class ChartRowsTests(unittest.TestCase):
    """[chart]: values derived by hand from the JewishGen chart."""

    def test_slavic_surnames_share_a_code(self) -> None:
        # K 5, O start-only, W/V 7, A -, L 8, S 4, K 5, I/A/Y not after start.
        for name in ('Kowalski', 'Kowalska', 'Kovalsky'):
            with self.subTest(name=name):
                self.assertEqual(codes(name), {'578450'})

    def test_hartley_spellings(self) -> None:
        # H 5 at the start, A -, R 9, T 3, L 8, EY/Y/IE not after the start.
        for name in ('Hartley', 'Hartly', 'Hartlie'):
            with self.subTest(name=name):
                self.assertEqual(codes(name), {'593800'})

    def test_polish_letters_fold_except_the_ogonek_ones(self) -> None:
        # SZCZ 2 at the start, E -, P 7, A -, N 6 (ń is n), S 4, K 5.
        self.assertEqual(codes('Szczepański'), {'276450'})
        # Ł is L: L 8, U -, K 5, A -, SZ 4.
        self.assertEqual(codes('Łukasz'), {'854000'})
        self.assertEqual(codes('Łukasz'), codes('Lukasz'))

    def test_a_ogonek_is_six_or_not_coded(self) -> None:
        # Chart row "Ą (Polish a-ogonek): NC, NC, 6 or NC": Dąbrowski reads
        # both as Dombrowski (D 3, M 6, B 7, R 9, W 7, S 4) and as Dabrowski.
        self.assertEqual(codes('Dąbrowski'), {'367974', '379745'})
        self.assertEqual(codes('Dąbrowski'), codes('Dombrowski') | codes('Dabrowski'))
        # Same row for Ę: Węgrowski is Wengrowski or Wegrowski.
        self.assertEqual(codes('Węgrowski'), codes('Wengrowski') | codes('Wegrowski'))

    def test_umlaut_and_ue_spelling_agree(self) -> None:
        # Row "U, UE: 0, NC, NC": Müller, Mueller and Muller are one name.
        self.assertEqual(codes('Müller'), {'689000'})
        self.assertEqual(codes('Mueller'), {'689000'})
        self.assertEqual(codes('Muller'), {'689000'})

    def test_szcs_is_one_sound(self) -> None:
        # Row "SZCZ, SZCS: 2, 4, 4" (a spelling some indexes miss).
        self.assertEqual(codes('Szcsepanski'), codes('Szczepanski'))

    def test_every_code_is_six_digits(self) -> None:
        for name in ('A', 'Peters', 'Schwarzenegger', 'Rosochowaciec', 'Dąbrowski', 'Ng'):
            for code in dm_soundex(name):
                with self.subTest(name=name, code=code):
                    self.assertRegex(code, r'^[0-9]{6}$')

    def test_schwarzenegger(self) -> None:
        # SCH 4 at the start, W 7, A -, RZ 94 or 4, E -, N 6, E -, G 5, G 5
        # (one sound), E -, R 9; cut to six digits.
        self.assertEqual(codes('Schwarzenegger'), {'479465', '474659'})


class EmptyAndShortNamesTests(unittest.TestCase):

    def test_empty_and_letterless_names_have_no_code(self) -> None:
        for name in ('', '   ', "-'-", '1880', 'משה'):
            with self.subTest(name=name):
                self.assertEqual(dm_soundex(name), [])
                self.assertEqual(american_soundex(name), '')

    def test_one_letter(self) -> None:
        # [JG] A is coded 0 at the start, B is 7, padded with zeros.
        self.assertEqual(codes('A'), {'000000'})
        self.assertEqual(codes('B'), {'700000'})
        # [NARA] the first letter, then zeros.
        self.assertEqual(american_soundex('L'), 'L000')


class NaraSoundexTests(unittest.TestCase):
    """[NARA] and [JG]: the census Soundex (letter and three digits)."""

    def test_nara_examples(self) -> None:
        for name, code in (('Washington', 'W252'), ('Lee', 'L000'),
                           ('Gutierrez', 'G362'), ('Pfister', 'P236'),
                           ('Jackson', 'J250'), ('Tymczak', 'T522'),
                           ('Ashcraft', 'A261')):
            with self.subTest(name=name):
                self.assertEqual(american_soundex(name), code)

    def test_prefix_is_coded_as_written(self) -> None:
        # NARA: VanDeusen is V532 as written, D250 without the prefix.
        self.assertEqual(american_soundex('VanDeusen'), 'V532')
        self.assertEqual(american_soundex('Deusen'), 'D250')

    def test_jewishgen_s655_group(self) -> None:
        for name in ('Scherman', 'Schurman', 'Sherman', 'Shireman', 'Shurman'):
            with self.subTest(name=name):
                self.assertEqual(american_soundex(name), 'S655')

    def test_accented_letters_fold(self) -> None:
        self.assertEqual(american_soundex('Łukasz'), american_soundex('Lukasz'))
        self.assertEqual(american_soundex('Müller'), 'M460')


class CyrillicAndGivenNameTests(unittest.TestCase):

    def test_passport_transliteration(self) -> None:
        # ICAO Doc 9303 (Russian passports): Колодин is Kolodin.
        self.assertEqual(translit('Колодин', 'passport'), 'Kolodin')
        self.assertEqual(translit('Иван', 'passport'), 'Ivan')
        self.assertEqual(translit('Иван', 'german'), 'Iwan')
        self.assertEqual(translit('Иван', 'polish'), 'Iwan')
        self.assertEqual(translit('Hartley', 'passport'), 'Hartley')

    def test_cyrillic_names_take_their_latin_codes(self) -> None:
        # [chart] I 0 at the start, V/W 7, A -, N 6.
        self.assertEqual(name_sound_codes('Иван'), {'076000'})
        self.assertEqual(name_sound_codes('Иван'), name_sound_codes('Ivan'))
        # K 5, O -, L 8, O -, D 3, I -, N 6.
        self.assertEqual(name_sound_codes('Колодин'), {'583600'})
        self.assertEqual(name_sound_codes('Колодин'), name_sound_codes('Kołodin'))

    def test_given_name_equivalents(self) -> None:
        jan = given_name_equivalents('Jan')
        for other in ('John', 'Johann', 'Ivan', 'Иван'):
            self.assertIn(other, jan)
        self.assertNotIn('Jan', jan)
        self.assertEqual(given_name_equivalents('Hartley'), [])
        self.assertIn('Paul', given_name_equivalents('Paweł'))
        self.assertIn('Paul', given_name_equivalents('pawel'))

    def test_hanna_is_not_agatha(self) -> None:
        # Ганна is the Ukrainian Anna; the upstream table also listed it
        # under Agafya, which would make every Hanna an Agatha.
        self.assertIn('Anna', given_name_equivalents('Ганна'))
        self.assertNotIn('Agata', given_name_equivalents('Ганна'))


class NameWordsTests(unittest.TestCase):

    def test_words_of_a_name(self) -> None:
        self.assertEqual(name_sound_words('Margaret A. Cole'), ['Margaret', 'A', 'Cole'])
        self.assertEqual(name_sound_words("Mary O'Brien-Smith"), ['Mary', 'OBrien', 'Smith'])
        self.assertEqual(name_sound_words('  '), [])
        self.assertEqual(name_sound_words('Иван Колодин'), ['Иван', 'Колодин'])


if __name__ == '__main__':
    unittest.main()
