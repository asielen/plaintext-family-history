// test-harvest.js — unit tests for the pure JS helpers in src/lib/.
//
// Run with:   node --test browser-companion/tests/test-harvest.js
//   (from the repo root)
// Or via npm: npm --prefix browser-companion test
//
// Tests use only built-in `node:test` + `node:assert` — no extra dependencies.

'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');

const {
  NON_PERSON_TYPES,
  isNonPersonLdNode,
  harvestFromLd,
} = require('../src/lib/people-harvest.js');

const {
  slugify,
  bundleName,
  accessedDate,
  build,
  CAPTURE_JSON_SCHEMA,
} = require('../src/lib/capture-json-pure.js');

// ── people-harvest: isNonPersonLdNode ─────────────────────────────────────────

test('isNonPersonLdNode — Cemetery type is non-person', () => {
  assert.equal(isNonPersonLdNode({ '@type': 'Cemetery', name: 'Greenwood Memorial Park' }), true);
});

test('isNonPersonLdNode — Place type is non-person', () => {
  assert.equal(isNonPersonLdNode({ '@type': 'Place', name: 'Some Place' }), true);
});

test('isNonPersonLdNode — Organization type is non-person', () => {
  assert.equal(isNonPersonLdNode({ '@type': 'Organization', name: 'FHL' }), true);
});

test('isNonPersonLdNode — LocalBusiness type is non-person', () => {
  assert.equal(isNonPersonLdNode({ '@type': 'LocalBusiness', name: 'Acme' }), true);
});

test('isNonPersonLdNode — BreadcrumbList is non-person', () => {
  assert.equal(isNonPersonLdNode({ '@type': 'BreadcrumbList' }), true);
});

test('isNonPersonLdNode — ListItem is non-person', () => {
  assert.equal(isNonPersonLdNode({ '@type': 'ListItem', name: 'North America' }), true);
});

test('isNonPersonLdNode — node with address property is non-person', () => {
  assert.equal(isNonPersonLdNode({ name: 'Some Place', address: { streetAddress: '1 Main St' } }), true);
});

test('isNonPersonLdNode — node with geo property is non-person', () => {
  assert.equal(isNonPersonLdNode({ name: 'Some Place', geo: { latitude: 40.0 } }), true);
});

test('isNonPersonLdNode — Person type is not non-person', () => {
  assert.equal(isNonPersonLdNode({ '@type': 'Person', name: 'Alice Smith' }), false);
});

test('isNonPersonLdNode — explicit Person with an address is still a person', () => {
  assert.equal(isNonPersonLdNode({
    '@type': 'Person', name: 'Alice Smith',
    address: { '@type': 'PostalAddress', streetAddress: '1 Main St' },
  }), false);
});

test('harvestFromLd — keeps a Person that carries an address', () => {
  const ld = JSON.stringify({
    '@type': 'Person', name: 'Alice Smith',
    address: { '@type': 'PostalAddress', addressLocality: 'Boston' },
  });
  assert.deepEqual(harvestFromLd([ld]), ['Alice Smith']);
});

test('isNonPersonLdNode — plain object without @type is not non-person', () => {
  assert.equal(isNonPersonLdNode({ name: 'Generic Thing' }), false);
});

test('isNonPersonLdNode — null returns false', () => {
  assert.equal(isNonPersonLdNode(null), false);
});

test('isNonPersonLdNode — array returns false (not a single node)', () => {
  assert.equal(isNonPersonLdNode([{ '@type': 'Place' }]), false);
});

// ── people-harvest: harvestFromLd ─────────────────────────────────────────────

test('harvestFromLd — extracts a subject Person', () => {
  const ld = JSON.stringify({ '@type': 'Person', name: 'Alice Smith' });
  const names = harvestFromLd([ld]);
  assert.deepEqual(names, ['Alice Smith']);
});

test('harvestFromLd — drops a Cemetery entity', () => {
  const ld = JSON.stringify({
    '@graph': [
      { '@type': 'Person', name: 'Calvin Hartley' },
      { '@type': 'Cemetery', name: 'Oak Woods Cemetery' },
    ],
  });
  const names = harvestFromLd([ld]);
  assert.ok(names.includes('Calvin Hartley'), 'should include the person');
  assert.ok(!names.includes('Oak Woods Cemetery'), 'should exclude the cemetery');
});

test('harvestFromLd — drops a Place entity', () => {
  const ld = JSON.stringify({
    '@graph': [
      { '@type': 'Person', name: 'Jane Doe' },
      { '@type': 'Place', name: 'Some Town' },
    ],
  });
  const names = harvestFromLd([ld]);
  assert.ok(!names.includes('Some Town'), 'should exclude the place');
  assert.ok(names.includes('Jane Doe'), 'should include the person');
});

test('harvestFromLd — drops BreadcrumbList items', () => {
  const ld = JSON.stringify({
    '@type': 'BreadcrumbList',
    itemListElement: [
      { '@type': 'ListItem', name: 'Memorials' },
      { '@type': 'ListItem', name: 'North America' },
      { '@type': 'ListItem', name: 'USA' },
    ],
  });
  const names = harvestFromLd([ld]);
  assert.equal(names.length, 0, 'no names should be harvested from a BreadcrumbList');
});

test('harvestFromLd — drops Organization entity', () => {
  const ld = JSON.stringify({
    '@graph': [
      { '@type': 'Person', name: 'John Public' },
      { '@type': 'Organization', name: 'Acme Corp' },
    ],
  });
  const names = harvestFromLd([ld]);
  assert.ok(!names.includes('Acme Corp'));
  assert.ok(names.includes('John Public'));
});

test('harvestFromLd — drops node with address property', () => {
  const ld = JSON.stringify({
    '@graph': [
      { '@type': 'Person', name: 'Bob Builder' },
      { name: 'Mystery Place', address: { streetAddress: '1 Main' } },
    ],
  });
  const names = harvestFromLd([ld]);
  assert.ok(!names.includes('Mystery Place'));
  assert.ok(names.includes('Bob Builder'));
});

test('harvestFromLd — keeps only unique names (dedup)', () => {
  const ld = JSON.stringify([
    { '@type': 'Person', name: 'Alice Smith' },
    { '@type': 'Person', name: 'Alice Smith' },
  ]);
  const names = harvestFromLd([ld]);
  assert.equal(names.filter((n) => n === 'Alice Smith').length, 1);
});

test('harvestFromLd — givenName + familyName fallback', () => {
  const ld = JSON.stringify({ '@type': 'Person', givenName: 'Mary', familyName: 'Jones' });
  const names = harvestFromLd([ld]);
  assert.deepEqual(names, ['Mary Jones']);
});

test('harvestFromLd — silently skips malformed JSON', () => {
  const names = harvestFromLd(['{not valid json']);
  assert.deepEqual(names, []);
});

test('harvestFromLd — caps output at 12 entries', () => {
  // Build a JSON-LD array with 20 people.
  const people = [];
  for (let i = 0; i < 20; i++) {
    people.push({ '@type': 'Person', name: 'Person ' + i });
  }
  const names = harvestFromLd([JSON.stringify(people)]);
  assert.equal(names.length, 12);
});

test('harvestFromLd — Find a Grave-like scenario: subject kept, cemetery dropped', () => {
  // Simulates the structured data pattern Find a Grave uses:
  // a Person block (the memorial subject) sitting alongside Cemetery + BreadcrumbList.
  const scriptBlocks = [
    // The memorial subject
    JSON.stringify({ '@type': 'Person', name: 'Alethe C Church Arnold' }),
    // The cemetery (should be dropped)
    JSON.stringify({ '@type': 'Cemetery', name: 'Mount Hope Cemetery' }),
    // The breadcrumb navigation (should be dropped)
    JSON.stringify({
      '@type': 'BreadcrumbList',
      itemListElement: [
        { '@type': 'ListItem', name: 'Memorials' },
        { '@type': 'ListItem', name: 'North America' },
        { '@type': 'ListItem', name: 'USA' },
        { '@type': 'ListItem', name: 'California' },
      ],
    }),
  ];
  const names = harvestFromLd(scriptBlocks);
  assert.deepEqual(names, ['Alethe C Church Arnold']);
});

// ── NON_PERSON_TYPES set ──────────────────────────────────────────────────────

test('NON_PERSON_TYPES includes the key types', () => {
  for (const t of ['Place', 'Cemetery', 'Organization', 'BreadcrumbList', 'ListItem', 'LocalBusiness']) {
    assert.ok(NON_PERSON_TYPES.has(t), `NON_PERSON_TYPES should include '${t}'`);
  }
});

test('NON_PERSON_TYPES does not include Person', () => {
  assert.ok(!NON_PERSON_TYPES.has('Person'));
});

// ── capture-json-pure ─────────────────────────────────────────────────────────

test('slugify — lowercases and replaces non-alnum with hyphens', () => {
  assert.equal(slugify('Hello World! 2024'), 'hello-world-2024');
});

test('slugify — trims leading/trailing hyphens', () => {
  assert.equal(slugify('  --foo--  '), 'foo');
});

test('slugify — empty input returns "capture"', () => {
  assert.equal(slugify(''), 'capture');
  assert.equal(slugify(null), 'capture');
});

test('accessedDate — returns YYYY-MM-DD', () => {
  const d = new Date(2026, 0, 5); // Jan 5 2026
  assert.equal(accessedDate(d), '2026-01-05');
});

test('bundleName — slug + timestamp', () => {
  const d = new Date(2026, 5, 29, 10, 30, 0); // 2026-06-29 10:30:00
  const name = bundleName('1880 Census Thomas', d);
  assert.equal(name, '1880-census-thomas-20260629-103000');
});

test('build — schema version is correct', () => {
  const cap = build({ url: 'https://example.com', assets: [] });
  assert.equal(cap.schema, CAPTURE_JSON_SCHEMA);
  assert.equal(cap.schema, 2);
});

test('build — url is required, title is optional', () => {
  const cap = build({ url: 'https://x.com', assets: [] });
  assert.equal(cap.url, 'https://x.com');
  assert.ok(!('title' in cap));
});

test('build — omits empty/blank fields', () => {
  const cap = build({ url: 'https://x.com', title: '', notes: '  ', assets: [] });
  assert.ok(!('title' in cap));
  assert.ok(!('notes' in cap));
});

test('build — assets list preserved with role/mode', () => {
  const cap = build({
    url: 'https://x.com',
    assets: [
      { file: 'page-copy.html', role: 'webpage', mode: 'singlefile' },
      { file: 'record.jpg', role: 'record', mode: 'fetch' },
    ],
  });
  assert.equal(cap.assets.length, 2);
  assert.equal(cap.assets[0].role, 'webpage');
  assert.equal(cap.assets[1].role, 'record');
});

test('build — provisional flag only emitted when true', () => {
  const cap = build({
    url: 'https://x.com',
    assets: [
      { file: 'record.jpg', role: 'record', mode: 'fetch', provisional: false },
    ],
  });
  assert.ok(!('provisional' in cap.assets[0]));
});

test('build — filters assets with no file', () => {
  const cap = build({
    url: 'https://x.com',
    assets: [{ file: '', role: 'record' }, { file: 'ok.jpg', role: 'record' }],
  });
  assert.equal(cap.assets.length, 1);
  assert.equal(cap.assets[0].file, 'ok.jpg');
});

test('build — people list is emitted when non-empty', () => {
  const cap = build({ url: 'https://x.com', assets: [], people: ['Alice', 'Bob'] });
  assert.deepEqual(cap.people, ['Alice', 'Bob']);
});

test('build — recipeHint maps to recipe_hint', () => {
  const cap = build({ url: 'https://x.com', assets: [], recipeHint: 'findagrave' });
  assert.equal(cap.recipe_hint, 'findagrave');
});
