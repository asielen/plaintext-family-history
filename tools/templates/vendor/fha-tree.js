/*
 * fha-tree.js - minimal, self-contained collapsible tree renderer.
 *
 * This is the VENDORED rendering engine for fha site's interactive trees
 * (TOOLING §12 "borrow the engine"). It has no external dependencies (no D3,
 * no framework, no CDN) and works from file://. It draws an SVG tree of nodes
 * with expand/collapse toggles and clickable names that link to person pages.
 *
 * It knows NOTHING about the archive's neutral tree-JSON contract. Its only
 * input is its own simple hierarchy format, produced by tree-adapter.js:
 *
 *     { id, name, dates, url, children: [ <same shape>, ... ] }
 *
 * Swapping in a richer engine later (e.g. family-chart) means rewriting THIS
 * file and the adapter; nothing else in the generated site changes - that is
 * the whole point of keeping the adapter as the single seam.
 */
(function (global) {
  'use strict';

  var SVGNS = 'http://www.w3.org/2000/svg';
  var COL_W = 228;   // horizontal spacing per leaf
  var ROW_H = 112;   // vertical spacing per generation
  var NODE_W = 200;  // wider card: room for a portrait + a two-line name
  var NODE_H = 68;

  function svg(tag, attrs) {
    var e = document.createElementNS(SVGNS, tag);
    for (var k in attrs) { if (attrs.hasOwnProperty(k)) e.setAttribute(k, attrs[k]); }
    return e;
  }

  // Assign x (leaf-packed, parents centred over children) and y (= depth).
  // A node collapsed by the user contributes no visible children.
  function layout(root, collapsed) {
    var nextX = 0;
    (function walk(node, depth) {
      node._y = depth;
      var kids = collapsed[node.id] ? [] : (node.children || []);
      if (kids.length === 0) {
        node._x = nextX++;
      } else {
        for (var i = 0; i < kids.length; i++) walk(kids[i], depth + 1);
        node._x = (kids[0]._x + kids[kids.length - 1]._x) / 2;
      }
    })(root, 0);
    return nextX || 1;
  }

  function maxDepth(root, collapsed) {
    var m = 0;
    (function walk(node, depth) {
      if (depth > m) m = depth;
      if (!collapsed[node.id]) (node.children || []).forEach(function (c) { walk(c, depth + 1); });
    })(root, 0);
    return m;
  }

  // Colour each major line distinctly: every child-subtree of the root gets a
  // branch number (1..7, cycling), inherited by its descendants. The renderer
  // surfaces it as data-branch and the stylesheet underlines the name in the
  // matching --branch-N colour. The root itself is left uncoloured - it is the
  // shared subject/ancestor the branches fan out from.
  function assignBranches(root) {
    var branchOf = {};
    (root.children || []).forEach(function (child, i) {
      var b = (i % 7) + 1;
      (function mark(n) {
        branchOf[n.id] = b;
        (n.children || []).forEach(mark);
      })(child);
    });
    return branchOf;
  }

  function render(container, root, options) {
    options = options || {};
    var collapsed = {};   // node id -> true when the user has collapsed it
    var branchOf = assignBranches(root);

    // Bound the initial paint: nodes at or beyond options.initialDepth start
    // collapsed, so a large descendant explorer renders a few generations up
    // front and the reader expands forward on demand (the data is complete -
    // nothing is dropped, only hidden). Omitting initialDepth shows everything.
    if (options.initialDepth != null) {
      (function seed(node, depth) {
        var kids = node.children || [];
        if (depth >= options.initialDepth && kids.length) collapsed[node.id] = true;
        kids.forEach(function (c) { seed(c, depth + 1); });
      })(root, 0);
    }

    function draw() {
      var leaves = layout(root, collapsed);
      var depth = maxDepth(root, collapsed);
      var width = Math.max(leaves * COL_W, COL_W);
      var height = (depth + 1) * ROW_H;

      container.innerHTML = '';
      var s = svg('svg', { 'class': 'fha-tree-svg', width: width, height: height,
                           viewBox: '0 0 ' + width + ' ' + height });

      var px = function (n) { return n._x * COL_W + COL_W / 2; };
      var py = function (n) { return n._y * ROW_H + NODE_H / 2; };

      // Edges first, so nodes sit on top.
      (function edges(node) {
        if (collapsed[node.id]) return;
        (node.children || []).forEach(function (c) {
          // Edge kind decides the dash pattern the page CSS draws (genetic solid,
          // legal long-dash, other dotted). Fall back to the older genetic
          // boolean (false ⇒ legal) when no explicit kind is present.
          var kind = c.edgeKind || (c.edgeGenetic === false ? 'legal' : 'genetic');
          var edgeClass = 'fha-tree-edge'
            + (kind === 'legal' ? ' fha-tree-edge-legal'
               : kind === 'other' ? ' fha-tree-edge-other' : '');
          var p = svg('path', {
            'class': edgeClass,
            d: 'M' + px(node) + ',' + (py(node) + NODE_H / 2) +
               ' C' + px(node) + ',' + (py(node) + ROW_H / 2) +
               ' ' + px(c) + ',' + (py(c) - ROW_H / 2) +
               ' ' + px(c) + ',' + (py(c) - NODE_H / 2)
          });
          s.appendChild(p);
          edges(c);
        });
      })(root);

      // Nodes.
      (function nodes(node) {
        var fo = svg('foreignObject', {
          x: px(node) - NODE_W / 2, y: py(node) - NODE_H / 2, width: NODE_W, height: NODE_H
        });
        var box = document.createElement('div');
        box.className = 'fha-node' + (node.url ? '' : ' fha-node-nolink');
        if (branchOf[node.id]) box.setAttribute('data-branch', branchOf[node.id]);
        var kids = node.children || [];

        if (kids.length) {
          var toggle = document.createElement('button');
          toggle.type = 'button';
          toggle.className = 'fha-toggle';
          toggle.textContent = collapsed[node.id] ? '+' : '−'; // minus sign
          toggle.setAttribute('aria-label', collapsed[node.id] ? 'Expand' : 'Collapse');
          toggle.onclick = function (ev) {
            ev.preventDefault();
            collapsed[node.id] = !collapsed[node.id];
            draw();
          };
          box.appendChild(toggle);
        }

        // A small fixed-size portrait square, or a monogram placeholder when the
        // person has no profile photo. Size is locked in CSS so it never changes
        // the node's card geometry.
        var portrait;
        if (node.photo) {
          portrait = document.createElement('img');
          portrait.className = 'fha-portrait';
          portrait.src = node.photo;
          portrait.alt = '';
          portrait.loading = 'lazy';
        } else {
          portrait = document.createElement('span');
          portrait.className = 'fha-portrait fha-portrait-empty';
          portrait.setAttribute('aria-hidden', 'true');
          portrait.textContent = ((node.name || node.id || '?').charAt(0) || '?').toUpperCase();
        }
        box.appendChild(portrait);

        var text = document.createElement('div');
        text.className = 'fha-node-text';

        var name = document.createElement(node.url ? 'a' : 'span');
        name.className = 'fha-name';
        name.textContent = node.name || node.id;
        if (node.url) name.setAttribute('href', node.url);
        text.appendChild(name);

        if (node.dates) {
          var d = document.createElement('small');
          d.className = 'fha-dates';
          d.textContent = node.dates;
          text.appendChild(d);
        }
        box.appendChild(text);
        fo.appendChild(box);
        s.appendChild(fo);

        if (!collapsed[node.id]) kids.forEach(nodes);
      })(root);

      container.appendChild(s);
    }

    draw();
  }

  global.FhaTree = { render: render };
})(window);
