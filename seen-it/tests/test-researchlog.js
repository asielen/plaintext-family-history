// test-researchlog.js - the research-log export and the "papers you have read
// in" table. Fictional Hartley family / Fairview, Kansas data only.
const test = require('node:test');
const assert = require('node:assert');
const log = require('../researchlog.js');

const at = (d, h) => new Date(2026, 0, d, h || 10, 0).getTime();

function store() {
  return {
    views: {
      101: { t: [at(15, 10)], last: at(15, 10) },
      102: { t: [at(15, 11)], last: at(15, 11) },
      201: { t: [at(16, 9)], last: at(16, 9) },
    },
    pages: {
      101: { paper: 'The Fairview Courier', date: '1923-06-14', pageNo: '4', place: 'Fairview, Kansas', mark: 'nothing', markAt: at(15, 10), fromSearches: ['k1'] },
      102: { paper: 'The Fairview Courier', date: '1924-02-02', pageNo: '1', place: 'Fairview, Kansas', mark: 'nothing', markAt: at(15, 11), fromSearches: ['k1'] },
      201: { paper: 'Fairview Weekly Star', date: '1931-05-01', pageNo: '3', fromSearches: ['k2'] },
      301: { paper: 'Fairview Weekly Star', date: '1931-05-08', pageNo: '2', mark: 'later', markAt: at(17, 8), terms: ['Hartley'] },
    },
    passes: {},
    searches: {
      k1: { params: { keyword: 'Hartley', 'date-start': '1920', 'date-end': '1925', location: 'Kansas' },
        url: 'https://www.newspapers.com/search/?query=Hartley', runs: [{ at: at(15, 9), count: 12 }], opened: ['101', '102'] },
      k2: { params: { keyword: 'Margaret Hartley' }, url: 'https://www.newspapers.com/search/?query=Margaret+Hartley',
        runs: [{ at: at(16, 8), count: 3 }], opened: ['201'] },
    },
    clips: { 201: [{ id: 7, title: 'Hartley wedding', url: 'https://www.newspapers.com/article/7/' }] },
  };
}

test('a search where every opened page was ruled out logs as nil', () => {
  const { text } = log.build(store(), 0);
  const entry = text.split('- date: ').find((e) => e.includes('"Hartley"'));
  assert.match(entry, /^2026-01-15\n/);
  assert.match(entry, /collection: "Newspapers.com \(1920-1925; location: Kansas\)"/);
  assert.match(entry, /result: "nil \(12 matches; opened 2; nothing on The Fairview Courier 1923-06-14 p. 4, The Fairview Courier 1924-02-02 p. 1\)"/);
});

test('a search with a clipping logs as found', () => {
  const { text } = log.build(store(), 0);
  assert.match(text, /terms: "Margaret Hartley"\n  result: "found \(3 matches; opened 1; clipped on Fairview Weekly Star 1931-05-01 p. 3\)"/);
});

test('a verdict on a page no search explains gets its own entry', () => {
  const { count, text } = log.build(store(), 0);
  assert.strictEqual(count, 3);
  assert.match(text, /question: "Newspapers.com page review"[\s\S]*result: "partial \(marked to revisit\)"/);
  assert.match(text, /url: "https:\/\/www.newspapers.com\/image\/301\/"/);
});

test('since limits the export to what is newer', () => {
  const { count } = log.build(store(), at(16, 12));
  assert.strictEqual(count, 1);
  assert.strictEqual(log.build(store(), at(18, 0)).count, 0);
});

test('coverage counts pages and years per paper, most-read first', () => {
  const cov = log.coverage(store());
  assert.deepStrictEqual(cov[0], { paper: 'The Fairview Courier', place: 'Fairview, Kansas', pages: 2, ruledOut: 2, years: '1923–1924' });
  assert.deepStrictEqual(cov[1], { paper: 'Fairview Weekly Star', place: '', pages: 2, ruledOut: 0, years: '1931' });
});

test('quotes and backslashes in search terms cannot break the YAML', () => {
  const s = store();
  s.searches.k1.params.keyword = 'say "Hartley" \\ farm';
  const { text } = log.build(s, 0);
  assert.match(text, /terms: "say \\"Hartley\\" \\\\ farm"/);
});

// PR #212 review: a verdict made after the last export is never lost.
test('a page ruled out after its search was exported gets its own entry next time', () => {
  const s = store();
  // the 1923 page was opened from search k1 on Jan 15 and exported that day;
  // the verdict came on Jan 20
  s.pages[101].markAt = at(20, 10);
  const { count, text } = log.build(s, at(18, 0));
  assert.strictEqual(count, 1);
  assert.match(text, /- date: 2026-01-20\n  question: "Newspapers.com page review"\n  repository: Newspapers.com\n  collection: "The Fairview Courier 1923-06-14 p. 4, Fairview, Kansas"/);
  assert.match(text, /result: "nil \(page read, nothing relevant\)"/);
});

test('a verdict the same export already reports inside its search entry is not repeated', () => {
  const s = store();
  s.pages[101].markAt = at(20, 10);
  assert.strictEqual(log.build(s, 0).count, 3); // the Jan 15 search entry is in this export
});
