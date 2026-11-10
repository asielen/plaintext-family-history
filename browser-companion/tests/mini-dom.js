// A small HTML reader for tests: turns a saved page into the node tree the
// Ancestry readers walk (childNodes, nodeType, tagName, id, className,
// getAttribute), so lib/ancestry-record.js runs on the same fixture files the
// Python recipe tests read. Enough for well-formed fixtures - not a browser
// parser: no implied end tags beyond the void elements, no CSS.

'use strict';

const VOID = new Set(['AREA', 'BASE', 'BR', 'COL', 'EMBED', 'HR', 'IMG', 'INPUT', 'LINK', 'META',
  'PARAM', 'SOURCE', 'TRACK', 'WBR']);
const RAW = new Set(['SCRIPT', 'STYLE']);

function decode(s) {
  return s.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (m, e) => {
    const k = e.toLowerCase();
    if (k[0] === '#') return String.fromCodePoint(k[1] === 'x' ? parseInt(k.slice(2), 16) : Number(k.slice(1)));
    return { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' }[k];
  });
}

// el.removeChild(child), as the DOM has it.
function removeChild(child) {
  this.childNodes = this.childNodes.filter((c) => c !== child);
  child.parentNode = null;
  return child;
}

function element(tag, attrs, parent) {
  return {
    nodeType: 1,
    tagName: tag,
    id: attrs.id || '',
    className: attrs.class || '',
    attrs,
    childNodes: [],
    parentNode: parent,
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    },
    removeChild,
  };
}

function parse(html) {
  const doc = { nodeType: 9, childNodes: [], title: '', removeChild };
  let cur = doc;
  const re = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=>/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|([^<]+|<)/gi;
  let m;
  while ((m = re.exec(html))) {
    if (m[1]) {
      const tag = m[1].toUpperCase();
      for (let n = cur; n && n !== doc; n = n.parentNode) {
        if (n.tagName === tag) {
          cur = n.parentNode;
          break;
        }
      }
    } else if (m[2]) {
      const tag = m[2].toUpperCase();
      const attrs = {};
      const ar = /([^\s=>/]+)(?:\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let a;
      while ((a = ar.exec(m[3] || ''))) {
        attrs[a[1].toLowerCase()] = decode(a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : a[5] || '');
      }
      const el = element(tag, attrs, cur);
      cur.childNodes.push(el);
      if (RAW.has(tag)) {
        const close = html.toLowerCase().indexOf('</' + tag.toLowerCase(), re.lastIndex);
        const end = close < 0 ? html.length : close;
        el.childNodes.push({ nodeType: 3, nodeValue: html.slice(re.lastIndex, end), parentNode: el });
        re.lastIndex = html.indexOf('>', end) + 1 || html.length;
      } else if (!VOID.has(tag) && !m[4]) {
        cur = el;
      }
    } else if (m[5] !== undefined && cur !== doc) {
      cur.childNodes.push({ nodeType: 3, nodeValue: decode(m[5]), parentNode: cur });
    }
  }
  const title = find(doc, (e) => e.tagName === 'TITLE');
  doc.title = title ? title.childNodes.map((t) => t.nodeValue || '').join('').trim() : '';
  return doc;
}

function find(root, pred) {
  for (const c of root.childNodes || []) {
    if (c.nodeType !== 1) continue;
    if (pred(c)) return c;
    const hit = find(c, pred);
    if (hit) return hit;
  }
  return null;
}

// A parsed tree written back as HTML (for a page the tests have edited, as a
// content script edits its copy of the page). Attribute values and text are
// escaped; script and style text is written as it was.
function serialize(node) {
  const esc = (s, attr) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(attr ? /"/g : /$^/, '&quot;');
  if (node.nodeType === 3) {
    return node.parentNode && RAW.has(node.parentNode.tagName) ? node.nodeValue : esc(node.nodeValue);
  }
  const inner = (node.childNodes || []).map(serialize).join('');
  if (node.nodeType !== 1) return inner;
  const tag = node.tagName.toLowerCase();
  const attrs = Object.keys(node.attrs).map((k) => ' ' + k + '="' + esc(node.attrs[k], true) + '"').join('');
  return '<' + tag + attrs + '>' + (VOID.has(node.tagName) ? '' : inner + '</' + tag + '>');
}

module.exports = { parse, find, serialize };
