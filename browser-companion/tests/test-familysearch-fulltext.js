// FamilySearch Full-Text (11-2): when FamilySearch has already machine-read an
// image, the words come along with the capture, marked unchecked. This file
// tests the reader (lib/familysearch-record.js fsFullTextFrom) on the panel as
// recorded from one public Full-Text image (tests/fixtures/capture-samples/
// familysearch-fulltext.html). The panel's own part is in
// test-panel-familysearch.js.
//
//   node --test browser-companion/tests/test-familysearch-fulltext.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const fsr = require('../src/lib/familysearch-record.js');

const REPO = path.join(__dirname, '..', '..');
const FULLTEXT_HTML = fs.readFileSync(
  path.join(REPO, 'tests', 'fixtures', 'capture-samples', 'familysearch-fulltext.html'), 'utf8');
const ADDR = 'https://www.familysearch.org/ark:/61903/3:1:ABCD-1234?view=fullText&lang=en';

// A page whose panel is the recorded one (FamilySearch draws one word to a span).
function docOf(html) {
  const m = /<div data-testid="full-text-transcript">([\s\S]*?)<\/div>/.exec(html);
  return {
    querySelector: (sel) => (sel === '[data-testid="full-text-transcript"]' && m
      ? { textContent: m[1].replace(/<[^>]+>/g, '') } : null),
  };
}

test('fsFullTextFrom reads the machine transcription the Full-Text panel shows, as one run of words', () => {
  const r = fsr.fsFullTextFrom(docOf(FULLTEXT_HTML), ADDR, 'abcd-1234');
  assert.deepStrictEqual(r, {
    ok: true,
    text: '- In the name of God Amen , I Joseph Blackwell of Fauquier County do make this my last will and testament .',
  });
});

test('fsFullTextFrom: no panel, an empty panel, another image or a broken page is no text, never a failure', () => {
  const none = { ok: true, text: null };
  assert.deepStrictEqual(fsr.fsFullTextFrom(docOf('<p>no panel</p>'), ADDR, 'ABCD-1234'), none);
  assert.deepStrictEqual(fsr.fsFullTextFrom(docOf('<div data-testid="full-text-transcript">  </div>'), ADDR, 'ABCD-1234'), none);
  // The panel is for the image in the address; a capture of another image never takes it.
  assert.deepStrictEqual(fsr.fsFullTextFrom(docOf(FULLTEXT_HTML), ADDR, 'OTHER-IMG'), none);
  assert.deepStrictEqual(fsr.fsFullTextFrom(docOf(FULLTEXT_HTML),
    'https://www.familysearch.org/ark:/61903/1:1:ABCD-1234', 'ABCD-1234'), none);
  assert.deepStrictEqual(fsr.fsFullTextFrom({ querySelector() { throw new Error('boom'); } }, ADDR, 'ABCD-1234'), none);
  assert.deepStrictEqual(fsr.fsFullTextFrom(null, ADDR, 'ABCD-1234'), none);
});
