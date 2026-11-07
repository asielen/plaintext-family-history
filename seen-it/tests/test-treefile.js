// test-treefile.js - the tree-file (GEDCOM) reader behind "Load your tree".
// Fictional Hartley family data only.
const test = require('node:test');
const assert = require('node:assert');
const { parseGedcom } = require('../treefile.js');

const GED = [
  '0 HEAD',
  '1 SOUR Ancestry.com Family Trees',
  '0 @I1@ INDI',
  '1 NAME Thomas /Hartley/',
  '1 BIRT',
  '2 DATE 1852',
  '2 PLAC Fairview, Kansas',
  '1 SOUR @S1@',
  '2 _APID 1,1001::2002',
  '1 SOUR @S2@',
  '2 _APID 1,1001::2002',
  '1 SOUR @S3@',
  '2 _APID 1,3003::4004',
  '1 NOTE Buried at https://www.findagrave.com/memorial/5005/thomas-hartley',
  '0 @I2@ INDI',
  '1 NAME Margaret  /Hartley/ ',
  '0 @F1@ FAM',
  '1 HUSB @I1@',
  '2 _APID 1,9009::9009',
  '0 TRLR',
].join('\r\n');

test('each person keeps their attached Ancestry records and memorials, once each', () => {
  const people = parseGedcom(GED);
  assert.deepStrictEqual(people, [
    { id: 'I1', name: 'Thomas Hartley', keys: ['rec:1001:2002', 'rec:3003:4004', 'fag:5005'] },
  ]);
});

test('a person with nothing attached, and lines outside a person, are left out', () => {
  const ids = parseGedcom(GED).map((p) => p.id);
  assert.ok(!ids.includes('I2'));
  assert.ok(!parseGedcom(GED).some((p) => p.keys.includes('rec:9009:9009')));
});

test('an empty or non-GEDCOM file gives no one', () => {
  assert.deepStrictEqual(parseGedcom(''), []);
  assert.deepStrictEqual(parseGedcom(null), []);
  assert.deepStrictEqual(parseGedcom('just some text\nnot a tree'), []);
});
