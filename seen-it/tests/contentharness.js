// contentharness.js - run a content script under node on a pretend page.
//
// Just enough of a page for a content script to start: an empty document
// whose queries are recorded (so a test can see whether the script drew its
// pills on that page), the location, timers, and chrome.storage /
// chrome.runtime that answer with nothing. No real DOM, no network.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function fakeElement(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(), children: [], childNodes: [], style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    textContent: '', innerText: '', id: '',
    appendChild(c) { el.children.push(c); return c; },
    insertAdjacentElement() {}, remove() {}, setAttribute() {}, getAttribute: () => null,
    addEventListener() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null,
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
  };
  return el;
}

function loadContentScript(file, { pathname, search = '', origin }) {
  const queries = [];
  const document = {
    scripts: [], title: '', visibilityState: 'visible', readyState: 'complete',
    body: fakeElement('body'), documentElement: fakeElement('html'),
    querySelectorAll: (sel) => { queries.push(sel); return []; },
    querySelector: (sel) => { queries.push(sel); return null; },
    getElementById: () => null,
    createElement: fakeElement,
    createTextNode: (t) => ({ textContent: t }),
    addEventListener() {},
  };
  const timers = [];
  const keep = (t) => { timers.push(t); return t; };
  const context = vm.createContext({
    document,
    location: { pathname, search, origin, href: origin + pathname + search, hostname: new URL(origin).hostname },
    chrome: {
      storage: { local: { get: async () => ({}), set: async () => {} }, onChanged: { addListener() {} } },
      runtime: { sendMessage: async () => null, getURL: (p) => p },
    },
    console: { log() {}, warn() {}, error() {} },
    setTimeout: (...a) => keep(setTimeout(...a)),
    clearTimeout,
    setInterval: (...a) => keep(setInterval(...a)),
    clearInterval,
    MutationObserver: class { observe() {} disconnect() {} },
    IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
    URL, URLSearchParams,
    fetch: async () => ({ ok: false, status: 0, json: async () => ({}) }),
  });
  context.window = context;
  context.window.addEventListener = () => {};
  const dir = path.join(__dirname, '..');
  for (const f of [].concat(file)) {
    vm.runInContext(fs.readFileSync(path.join(dir, f), 'utf8'), context, { filename: f });
  }
  return {
    queries,
    stop: () => { for (const t of timers) { clearTimeout(t); clearInterval(t); } },
  };
}

module.exports = { loadContentScript };
