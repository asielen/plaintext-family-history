"""
test_site_face_crops.py - a person with no chosen portrait gets her face, cut
from a group photo that names her (`fha site`, TOOLING §12 "Web pictures").

Great-aunt Ada has no `profile_photo:`, but she is named on a face in the 1910
wedding photo; her page, the trees and the wall calendar show that face. The
photo is held to every rule a photo-strip photo is held to, because a crop of a
photo of a living person is still a photo of that person. Expected answers come
from those rules (SPEC §19, §21; TOOLING §12), not from the code's output.

Reuses the synthetic-index fixtures of tests/test_site_rings_calendar.py and a
hand-made photo catalog. Temp fixtures only.
"""

import io
import json
import sqlite3
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

from PIL import Image   # noqa: E402

from photoindex import _DDL as PHOTO_DDL   # noqa: E402
from test_site_rings_calendar import LIVE, ME, OTHER, _Base, site   # noqa: E402

ON = 'site:\n  calendar: true\n'
WEDDING = 'photos/1910/wedding.jpg'
AREA = {'X': 0.5, 'Y': 0.4, 'W': 0.1, 'H': 0.2, 'Unit': 'normalized'}


class BoxTests(unittest.TestCase):
    def test_a_centred_face_is_widened_and_made_square(self):
        # 100x200 px face (10% x 20% of 1000x1000), widened 30%, square on the longer side.
        self.assertEqual(site._face_box((0.5, 0.5, 0.1, 0.2), (1000, 1000)), (370, 370, 630, 630))

    def test_a_face_at_the_edge_slides_inside_the_picture(self):
        left, top, right, bottom = site._face_box((0.02, 0.5, 0.1, 0.1), (1000, 1000))
        self.assertEqual(left, 0)
        self.assertEqual((right - left, bottom - top), (130, 130))
        left, top, right, bottom = site._face_box((0.99, 0.01, 0.1, 0.1), (1000, 1000))
        self.assertEqual((right, top), (1000, 0))
        self.assertEqual(right - left, bottom - top)

    def test_a_box_bigger_than_the_picture_is_the_whole_short_side(self):
        self.assertEqual(site._face_box((0.5, 0.5, 1.0, 1.0), (400, 300)), (50, 0, 350, 300))


class RotatedPhotoTests(unittest.TestCase):
    def test_region_coordinates_are_read_on_the_stored_pixels(self):
        # MWG Guidelines 2.0 sec 5.9: a Creator MUST express region coordinates
        # relative to the stored image, prior to the application of the Exif
        # Orientation tag. So a region on the stored left edge of a photo stored
        # sideways (Orientation 6) is the red patch, and the crop is turned upright.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'sideways.jpg'
            im = Image.new('RGB', (400, 200), (0, 0, 255))
            im.paste((255, 0, 0), (0, 0, 100, 200))   # red on the stored left edge
            exif = Image.Exif()
            exif[0x0112] = 6
            im.save(src, exif=exif)
            data = site._picture_bytes(src, 128, '.jpg', (0.125, 0.5, 0.2, 0.4))
            self.assertIsNotNone(data)
            out = Image.open(io.BytesIO(data)).convert('RGB')
            r, g, b = out.getpixel((out.width // 2, out.height // 2))
            self.assertGreater(r, 200)
            self.assertLess(b, 60)


class FaceCropTests(_Base):
    def setUp(self):
        super().setUp()
        self.photos = sqlite3.connect(str(self.root / '.cache' / 'photos.sqlite'))
        self.photos.executescript(PHOTO_DDL)
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-14', [ME])
        self.config(ON)
        self.status = 'fresh'

    def tearDown(self):
        self.photos.close()
        super().tearDown()

    def image(self, rel=WEDDING, color=(200, 30, 30), size=(400, 300)):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', size, color).save(path)
        return path

    def catalog(self, rel=WEDDING, *, who=(ME,), group=None, primary=1, source=None,
                name='Ada Example', area=AREA, via='face-tag', color=(200, 30, 30)):
        self.image(rel, color)
        self.photos.execute(
            'INSERT OR REPLACE INTO photos(path, mtime, size, source_id, group_id, is_primary) '
            'VALUES (?,?,?,?,?,?)', (rel, 1.0, 1, source, group, primary))
        self.photos.execute(
            'INSERT INTO photo_face_regions(path, name, region_type, area_json) VALUES (?,?,?,?)',
            (rel, name, 'Face', json.dumps(area)))
        for pid in who:
            self.photos.execute('INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
                                (rel, pid, via))
        self.photos.commit()

    def build(self, **kw):
        with unittest.mock.patch.object(site, 'photoindex_status',
                                        return_value=(self.status, 0)):
            return super().build(**kw)

    def faces(self):
        return sorted((self.out / 'media' / 'faces').glob('*.jpg')) \
            if (self.out / 'media' / 'faces').is_dir() else []

    def test_the_face_is_her_portrait_on_the_calendar_and_her_page(self):
        self.catalog()
        self.build()
        self.assertEqual(len(self.faces()), 1)
        name = self.faces()[0].name
        self.assertIn(f'<img class="wall-face" src="media/faces/{name}"', self.page('calendar.html'))
        page = self.page(f'persons/{ME}.html')
        self.assertIn(f'src="../media/faces/{name}"', page)
        self.assertIn('From a group photo.', page)

    def test_the_crop_is_one_small_square_face_not_the_whole_photo(self):
        self.catalog()
        self.build()
        with Image.open(self.faces()[0]) as im:
            self.assertEqual(im.size[0], im.size[1])
            self.assertLessEqual(im.size[0], 128)

    def test_a_chosen_portrait_is_never_replaced_by_a_face(self):
        self.catalog()
        self.image('photos/chosen.jpg', color=(0, 0, 200))
        (self.root / 'people' / f'example_{ME}.md').write_text(
            f'---\nid: {ME}\nname: Ada Example\nprofile_photo: photos/chosen.jpg\n---\n# Ada\n',
            encoding='utf-8')
        self.photos.execute('INSERT INTO photos(path, mtime, size, is_primary) VALUES (?,?,?,1)',
                            ('photos/chosen.jpg', 1.0, 1))
        self.photos.commit()
        self.build()
        self.assertNotIn('From a group photo.', self.page(f'persons/{ME}.html'))

    def test_an_out_of_date_catalog_shows_no_face(self):
        self.catalog()
        self.status = 'stale'
        self.build()
        self.assertEqual(self.faces(), [])
        self.assertNotIn('wall-face', self.page('calendar.html'))

    def test_a_photo_with_a_living_person_tagged_in_it_shows_no_face(self):
        self.person(LIVE, 'Lena Example', living='true')
        self.catalog(who=(ME, LIVE))
        self.build()
        self.assertEqual(self.faces(), [])
        self.assertNotIn('wall-face', self.page('calendar.html'))

    def test_a_living_person_tagged_on_another_copy_of_the_photo_also_blocks_it(self):
        self.person(LIVE, 'Lena Example', living='true')
        self.catalog(group='g-1')
        self.catalog('photos/1910/wedding-copy.jpg', who=(LIVE,), group='g-1', primary=0,
                     name='Lena Example')
        self.build()
        self.assertEqual(self.faces(), [])

    def test_a_photo_from_a_withheld_source_shows_no_face(self):
        self.source('s-2222222222', 'Private letter', restricted=1)
        self.catalog(source='s-2222222222')
        self.build()
        self.assertEqual(self.faces(), [])

    def test_a_living_person_gets_no_face_of_her_own(self):
        self.person(LIVE, 'Lena Example', living='true')
        self.claim('c-3333333333', 's-1111111111', 'birth', '1990-06-14', [LIVE])
        self.catalog(who=(LIVE,), name='Lena Example')
        self.build()
        self.assertEqual(self.faces(), [])

    def test_a_name_only_guess_is_not_a_face(self):
        self.catalog(via='name-match')
        self.build()
        self.assertEqual(self.faces(), [])

    def test_a_region_naming_someone_else_is_not_her_face(self):
        self.person(OTHER, 'Fern Example')
        self.catalog(name='Fern Example')
        self.build()
        self.assertEqual(self.faces(), [])

    def test_the_biggest_face_in_the_primary_photo_wins(self):
        small = {'X': 0.5, 'Y': 0.5, 'W': 0.05, 'H': 0.05, 'Unit': 'normalized'}
        big = {'X': 0.3, 'Y': 0.3, 'W': 0.3, 'H': 0.3, 'Unit': 'normalized'}
        self.catalog('photos/1910/a.jpg', area=small, color=(250, 0, 0))
        self.catalog('photos/1910/b.jpg', area=big, color=(0, 0, 250))
        self.catalog('photos/1910/c.jpg', area=big, primary=0, color=(0, 250, 0))
        self.build()
        self.assertEqual(len(self.faces()), 1)
        with Image.open(self.faces()[0]) as im:
            r, g, b = im.getpixel((64, 64))
        self.assertGreater(b, 200)    # b.jpg: primary, and the biggest face

    def test_a_crop_that_would_take_in_a_neighbours_face_is_not_made(self):
        # A second named face sits shoulder to shoulder with hers: the margin of
        # her crop would show his face, so the photo gives her no portrait.
        self.catalog()
        self.photos.execute(
            'INSERT INTO photo_face_regions(path, name, region_type, area_json) VALUES (?,?,?,?)',
            (WEDDING, 'Someone Else', 'Face',
             json.dumps({'X': 0.56, 'Y': 0.4, 'W': 0.1, 'H': 0.2, 'Unit': 'normalized'})))
        self.photos.commit()
        self.build()
        self.assertEqual(self.faces(), [])

    def test_a_neighbour_whose_face_is_only_partly_in_the_crop_blocks_it(self):
        self.catalog()
        self.photos.execute(
            'INSERT INTO photo_face_regions(path, name, region_type, area_json) VALUES (?,?,?,?)',
            (WEDDING, 'Someone Else', 'Face',
             json.dumps({'X': 0.63, 'Y': 0.4, 'W': 0.1, 'H': 0.2, 'Unit': 'normalized'})))
        self.photos.commit()
        self.build()
        self.assertEqual(self.faces(), [])

    def test_a_neighbour_well_clear_of_the_crop_does_not_block_it(self):
        self.catalog()
        self.photos.execute(
            'INSERT INTO photo_face_regions(path, name, region_type, area_json) VALUES (?,?,?,?)',
            (WEDDING, 'Someone Else', 'Face',
             json.dumps({'X': 0.9, 'Y': 0.4, 'W': 0.1, 'H': 0.2, 'Unit': 'normalized'})))
        self.photos.commit()
        self.build()
        self.assertEqual(len(self.faces()), 1)

    def test_the_cache_key_differs_between_two_crops_of_one_photo(self):
        src = self.image()
        cache = site._DerivativeCache(self.root, {})
        st = src.stat()
        one = cache.entry_fields(src, st, 128, '.jpg', (0.2, 0.2, 0.1, 0.1))
        two = cache.entry_fields(src, st, 128, '.jpg', (0.7, 0.2, 0.1, 0.1))
        whole = cache.entry_fields(src, st, 128, '.jpg')
        self.assertNotEqual(cache.entry_key(one), cache.entry_key(two))
        self.assertNotIn('crop', whole)
        self.assertTrue(cache._fields_ok(one))
        self.assertTrue(cache._fields_ok(whole))

    def test_the_crop_is_the_same_bytes_with_and_without_the_cache(self):
        self.catalog()
        self.build()
        first = self.faces()[0].read_bytes()
        self.build()                       # second build reads the saved crop
        self.assertEqual(self.faces()[0].read_bytes(), first)
        direct = site._picture_bytes(self.root / WEDDING, 128, '.jpg',
                                     (0.5, 0.4, 0.1, 0.2))
        self.assertEqual(direct, first)

    def test_a_linked_preview_makes_no_face(self):
        self.catalog()
        self.build(linked=True)
        self.assertEqual(self.faces(), [])

    def test_without_pillow_there_is_no_face_and_no_new_warning(self):
        self.catalog()
        with unittest.mock.patch.object(site, '_PIL_AVAILABLE', False):
            res = self.build()
        self.assertEqual(self.faces(), [])
        self.assertFalse([m for m in getattr(res, 'messages', []) if 'face' in str(m).lower()])


if __name__ == '__main__':
    unittest.main()
