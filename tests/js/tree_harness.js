/*
 * tree_harness.js - runs the site's explorer scripts outside a browser so
 * tests/test_site.py (TreeExplorerCouplesTests) can check that a man's two
 * marriages, a cousin marriage and a Private card come out right.
 *
 *   node tests/js/tree_harness.js <tree.json>
 *
 * Loads tools/templates/vendor/{d3.min.js, d3-flextree.js, fha-tree.js,
 * tree-adapter.js} into a sandbox whose `window` is the sandbox itself, the
 * way the page loads them with plain <script> tags. Prints one JSON object:
 *   model   - FhaTreeAdapter.fromNeutral(tree)
 *   layout  - FhaTree._layoutModel(model, {}), and again (`layout2`) so a
 *             test can see the same model always lands in the same place
 *   drawn   - FhaTree.render() into a minimal stand-in DOM: every element
 *             drawn, as {tag, cls, text, attrs}
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const VENDOR = path.join(__dirname, '..', '..', 'tools', 'templates', 'vendor');

// Just enough DOM for render(): elements with attributes, children, style,
// listeners and a fixed size.
class El {
  constructor(tag) {
    this.tagName = tag; this.attrs = {}; this.children = []; this.style = {};
    this.dataset = {}; this._text = ''; this.className = '';
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'class') this.className = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  insertBefore(c) { return this.appendChild(c); }
  removeChild(c) { this.children = this.children.filter(x => x !== c); return c; }
  get firstChild() { return this.children[0] || null; }
  set innerHTML(_) { this.children = []; }
  set textContent(t) { this._text = String(t); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set title(t) { this.attrs.title = String(t); }
  addEventListener() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: 900, height: 600 }; }
  get clientWidth() { return 900; }
  querySelector() { return null; }
  closest() { return null; }
}
const document = {
  createElement: tag => new El(tag),
  createElementNS: (_, tag) => new El(tag),
};

const sandbox = { document, innerHeight: 800, addEventListener() {}, console };
sandbox.window = sandbox;
vm.createContext(sandbox);
for (const f of ['d3.min.js', 'd3-flextree.js', 'fha-tree.js', 'tree-adapter.js']) {
  vm.runInContext(fs.readFileSync(path.join(VENDOR, f), 'utf8'), sandbox, { filename: f });
}

const tree = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const options = process.argv[3] ? JSON.parse(process.argv[3]) : {};
const model = sandbox.FhaTreeAdapter.fromNeutral(tree);
const layout = sandbox.FhaTree._layoutModel(model, {});
const layout2 = sandbox.FhaTree._layoutModel(sandbox.FhaTreeAdapter.fromNeutral(tree), {});

const mount = new El('div');
sandbox.FhaTree.render(mount, model, options);
const drawn = [];
(function walk(e) {
  drawn.push({ tag: e.tagName, cls: e.className || e.attrs['class'] || '',
               text: e._text, attrs: e.attrs });
  e.children.forEach(walk);
})(mount);

process.stdout.write(JSON.stringify({ model, layout, layout2, drawn }));
