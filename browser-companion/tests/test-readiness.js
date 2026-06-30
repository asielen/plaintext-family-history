// Tests for the capture-timing guard's pure core (capture-frontend-08 item A).
//   node --test browser-companion/tests/test-readiness.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { detailLooksUnpopulated } = require('../src/lib/capture-readiness.js');

test('flags an empty/unselected detail region', () => {
  assert.ok(detailLooksUnpopulated('No record has been selected'));
  assert.ok(detailLooksUnpopulated('<div>  No Record Selected  </div>'));
  assert.ok(detailLooksUnpopulated('Please select a record to view details'));
  assert.ok(detailLooksUnpopulated('No results to display'));
});

test('does not flag a populated record', () => {
  assert.ok(!detailLooksUnpopulated('Name: Calvin Hartley   Age: 42   Relation: Head'));
  assert.ok(!detailLooksUnpopulated('1940 United States Federal Census'));
  assert.ok(!detailLooksUnpopulated(''));
  assert.ok(!detailLooksUnpopulated(null));
});
