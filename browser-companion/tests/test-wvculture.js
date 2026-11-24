// Tests for the West Virginia Archives image reader (lib/wvculture-record.js):
// the address of a page, the image its page shows, the size check and the
// citation. Built-in node:test only.
//   node --test browser-companion/tests/test-wvculture.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const wv = require('../src/lib/wvculture-record.js');

const PAGE = 'https://archive.wvculture.org/vrr/va_view2.aspx?FilmNumber=1984148&ImageNumber=2530';

test('a view page address gives its film and image numbers', () => {
  assert.deepStrictEqual(wv.wvParsePage(PAGE), { filmNumber: '1984148', imageNumber: '2530' });
});

test('query keys are read whatever their case', () => {
  assert.deepStrictEqual(
    wv.wvParsePage('https://archive.wvculture.org/vrr/va_view2.aspx?filmnumber=1984148&IMAGENUMBER=7'),
    { filmNumber: '1984148', imageNumber: '7' });
});

test('an address with no image number, or a non-numeric one, is not a page to read', () => {
  assert.strictEqual(wv.wvParsePage('https://archive.wvculture.org/vrr/va_view2.aspx?FilmNumber=1984148'), null);
  assert.strictEqual(wv.wvParsePage('https://archive.wvculture.org/vrr/va_view2.aspx?FilmNumber=1984148&ImageNumber=2x'), null);
  assert.strictEqual(wv.wvParsePage('https://archive.wvculture.org/vrr/va_view2.aspx?FilmNumber=abc&ImageNumber=2'), null);
});

test('another host or page is not a West Virginia view page', () => {
  assert.strictEqual(wv.wvParsePage('https://images.wvculture.org/vrr/va_view2.aspx?FilmNumber=1&ImageNumber=2'), null);
  assert.strictEqual(wv.wvParsePage('https://archive.wvculture.org.evil.example/vrr/va_view2.aspx?FilmNumber=1&ImageNumber=2'), null);
  assert.strictEqual(wv.wvParsePage('https://archive.wvculture.org/vrr/other.aspx?FilmNumber=1&ImageNumber=2'), null);
  assert.strictEqual(wv.wvParsePage('not a url'), null);
});

test('the image is the page own img src, with http upgraded to https (7-digit gif)', () => {
  const srcs = ['https://www.wvculture.org/logo.png', 'http://images.wvculture.org/1984148/0002530.gif'];
  assert.strictEqual(wv.wvImageUrl(srcs), 'https://images.wvculture.org/1984148/0002530.gif');
});

test('a 5-digit jpg register image is taken as the page gives it', () => {
  assert.strictEqual(wv.wvImageUrl(['http://images.wvculture.org/1923007/02530.jpg']),
    'https://images.wvculture.org/1923007/02530.jpg');
  assert.strictEqual(wv.wvExtension('https://images.wvculture.org/1923007/02530.jpg'), 'jpg');
  assert.strictEqual(wv.wvExtension('https://images.wvculture.org/1984148/0002530.gif'), 'gif');
});

test('an img src off the image host, or not an image, is never fetched', () => {
  assert.strictEqual(wv.wvImageUrl(['https://evil.example/1984148/0002530.gif']), null);
  assert.strictEqual(wv.wvImageUrl(['https://images.wvculture.org.evil.example/1/2.gif']), null);
  assert.strictEqual(wv.wvImageUrl(['http://images.wvculture.org/1984148/0002530.exe']), null);
  assert.strictEqual(wv.wvImageUrl(['ftp://images.wvculture.org/1984148/0002530.gif']), null);
  assert.strictEqual(wv.wvImageUrl([]), null);
  assert.strictEqual(wv.wvImageUrl(null), null);
});

test('the citation names the archive, the page as opened, the film and the image', () => {
  assert.strictEqual(
    wv.wvCitation({ filmNumber: '1984148', imageNumber: '2530', pageUrl: PAGE }, '2026-11-22'),
    'West Virginia Archives and History, Vital Research Records (' + PAGE + ' : accessed 2026-11-22), ' +
      'film 1984148, image 2530.');
});

test('a FamilySearch badge address becomes the page to follow, always over https, with the citation part', () => {
  const want = {
    site: 'wvculture',
    archive: 'West Virginia Archives and History',
    filmNumber: '1984148',
    imageNumber: '2530',
    pageUrl: PAGE,
    cite: 'image at West Virginia Archives and History, film 1984148, image 2530',
  };
  assert.deepStrictEqual(wv.wvPartnerPage(PAGE), want);
  assert.deepStrictEqual(wv.wvPartnerPage(PAGE.replace('https:', 'http:')), want);
});

test('a badge address that is not a view page is not followed', () => {
  assert.strictEqual(wv.wvPartnerPage('https://www.wvculture.org/history/'), null);
  assert.strictEqual(wv.wvPartnerPage('https://archive.wvculture.org/vrr/va_view2.aspx?FilmNumber=1984148'), null);
  assert.strictEqual(wv.wvPartnerPage(PAGE.replace('.org/', '.org:8443/')), null);
  assert.strictEqual(wv.wvPartnerPage(PAGE.replace('https://', 'https://a:b@')), null);
  assert.strictEqual(wv.wvPartnerPage(''), null);
  assert.strictEqual(wv.wvPartnerPage(null), null);
});
