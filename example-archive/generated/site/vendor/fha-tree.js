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
 * Navigation: large trees render wider than the page, so the tree lives inside
 * a fixed-size viewport and is explored by dragging to pan and by wheel /
 * pinch / +/- to zoom. The whole tree is centred and fitted on first paint; a
 * "Fit" control returns to that view. Pan/zoom is done by moving the SVG
 * viewBox (not scaling a raster), so text and rules stay crisp at every zoom.
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

  // Zoom is expressed as pixels-per-content-unit (1 = drawn 1:1). Fit for a
  // wide tree lands well below 1; the ceiling keeps a single card from filling
  // the whole viewport.
  var MIN_SCALE = 0.1;
  var MAX_SCALE = 2.5;
  var FIT_PAD = 48;      // content-space breathing room around the tree on Fit
  var DRAG_SLOP = 4;     // px of travel before a press becomes a pan (vs a click)
  var ZOOM_STEP = 1.25;  // per +/- button press
  var HOME_SCALE = 1;    // px-per-unit when the Home button frames the home person

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

    // ---- viewport scaffold (built once; survives collapse redraws) ----------
    // The tree canvas is a fixed-size window; the tree may be far larger and is
    // reached by pan/zoom. Building this once (rather than per draw) keeps the
    // current pan/zoom stable when the reader expands or collapses a node.
    container.innerHTML = '';

    var viewport = document.createElement('div');
    viewport.className = 'fha-tree-viewport';
    viewport.style.position = 'relative';
    viewport.style.overflow = 'hidden';
    viewport.style.width = '100%';
    container.appendChild(viewport);

    var VH = Math.round(Math.min(620, Math.max(380, (global.innerHeight || 800) * 0.7)));
    viewport.style.height = VH + 'px';

    function viewportW() {
      return Math.max(viewport.clientWidth || container.clientWidth || 900, 1);
    }

    var s = svg('svg', { 'class': 'fha-tree-svg', 'preserveAspectRatio': 'xMidYMid meet' });
    s.style.width = '100%';
    s.style.height = VH + 'px';
    s.style.display = 'block';
    s.style.touchAction = 'none';   // we own touch drags (pan), not the page
    s.style.cursor = 'grab';
    s.style.userSelect = 'none';
    viewport.appendChild(s);

    // All tree geometry lives in one group so `draw()` can wipe and rebuild it
    // without disturbing the viewBox (the current pan/zoom).
    var content = svg('g', {});
    s.appendChild(content);

    // ---- controls: minimal, borrow the existing .btn look --------------------
    var controls = document.createElement('div');
    controls.className = 'fha-tree-controls';
    controls.style.position = 'absolute';
    controls.style.top = '8px';
    controls.style.right = '8px';
    controls.style.display = 'flex';
    controls.style.gap = '6px';
    controls.style.zIndex = '2';

    function mkBtn(label, aria, onClick) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn fha-tree-btn';
      b.textContent = label;
      b.setAttribute('aria-label', aria);
      b.title = aria;
      b.onclick = onClick;
      // Inline fallbacks so the controls read correctly even before the
      // stylesheet learns .fha-tree-btn: compact, and opaque over the tree.
      b.style.padding = '2px 8px';
      b.style.lineHeight = '1.4';
      b.style.background = 'var(--surface, #f8f5ee)';
      return b;
    }
    // Home first: jump to the designated home person. Only offered when a
    // homeId was supplied - otherwise it would just duplicate Fit.
    if (options.homeId != null) {
      controls.appendChild(mkBtn('⌂ Home', 'Center on the home person', function () { home(); }));
    }
    controls.appendChild(mkBtn('−', 'Zoom out', function () { zoomBy(1 / ZOOM_STEP); }));
    controls.appendChild(mkBtn('Fit', 'Fit tree to view', function () { fit(); }));
    controls.appendChild(mkBtn('+', 'Zoom in', function () { zoomBy(ZOOM_STEP); }));
    viewport.appendChild(controls);

    // ---- view (viewBox) state ------------------------------------------------
    var vx = 0, vy = 0, vw = 100, vh = 100;   // current viewBox rectangle
    var contentW = 100, contentH = 100;       // natural size of the drawn tree
    var userInteracted = false;               // pan/zoom/expand since last Fit?

    function applyView() {
      s.setAttribute('viewBox', vx + ' ' + vy + ' ' + vw + ' ' + vh);
    }
    // px-per-unit; derived from height, which is fixed across width resizes.
    function currentScale() { return VH / vh; }
    function clampScale(sc) { return Math.max(MIN_SCALE, Math.min(MAX_SCALE, sc)); }

    // Centre the whole tree and scale it to fit, preserving the viewport aspect
    // ratio so there is no distortion. This is both the initial view and the
    // "Fit" reset.
    function fit() {
      userInteracted = false;
      var vpW = viewportW();
      var sc = clampScale(Math.min(vpW / (contentW + FIT_PAD * 2),
                                   VH / (contentH + FIT_PAD * 2)));
      vw = vpW / sc; vh = VH / sc;
      vx = contentW / 2 - vw / 2;
      vy = contentH / 2 - vh / 2;
      applyView();
    }

    // Zoom about a client (screen) point so that point stays put under it.
    function zoomAt(clientX, clientY, factor) {
      var sc = currentScale();
      var next = clampScale(sc * factor);
      factor = next / sc;
      if (factor === 1) return;
      userInteracted = true;
      var rect = s.getBoundingClientRect();
      var fx = (clientX - rect.left) / rect.width;   // 0..1 across viewport
      var fy = (clientY - rect.top) / rect.height;
      var ux = vx + fx * vw;                         // anchor point, content coords
      var uy = vy + fy * vh;
      vw = vw / factor; vh = vh / factor;
      vx = ux - fx * vw;
      vy = uy - fy * vh;
      applyView();
    }
    function zoomBy(factor) {
      var rect = s.getBoundingClientRect();
      zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, factor);
    }

    // Find a node by id among the CURRENTLY VISIBLE set only: a node inside a
    // collapsed subtree is skipped (its _x/_y would be stale), so the caller can
    // treat "not found" as "not reachable right now". The node's own collapsed
    // flag hides its children, not itself, so it still matches.
    function visibleNodeById(id) {
      var found = null;
      (function walk(node) {
        if (found) return;
        if (node.id === id) { found = node; return; }
        if (collapsed[node.id]) return;   // don't descend into a collapsed subtree
        (node.children || []).forEach(walk);
      })(root);
      return found;
    }

    // Home: centre + zoom on the designated home person at a comfortable scale
    // (node + immediate neighbours in frame). Falls back to Fit when no homeId
    // was given, or the person isn't present / is collapsed out of view.
    function home() {
      var n = options.homeId != null ? visibleNodeById(options.homeId) : null;
      if (!n) { fit(); return; }
      userInteracted = true;
      var sc = clampScale(HOME_SCALE);
      var vpW = viewportW();
      vw = vpW / sc; vh = VH / sc;
      var nx = n._x * COL_W + COL_W / 2;   // same node-centre math as draw()'s px/py
      var ny = n._y * ROW_H + NODE_H / 2;
      vx = nx - vw / 2;
      vy = ny - vh / 2;
      applyView();
    }

    // ---- wheel / pinch zoom --------------------------------------------------
    // Plain wheel zooms; trackpad pinch arrives as a ctrlKey wheel and rides the
    // same path. preventDefault stops the page from scrolling under the tree.
    s.addEventListener('wheel', function (e) {
      e.preventDefault();
      zoomAt(e.clientX, e.clientY, Math.pow(1.0015, -e.deltaY));
    }, { passive: false });

    // ---- drag to pan ---------------------------------------------------------
    var dragging = false, dragMoved = false;
    var startX = 0, startY = 0, startVx = 0, startVy = 0, activeId = null;

    s.addEventListener('pointerdown', function (e) {
      if (e.button != null && e.button !== 0) return;   // primary button only
      dragging = true; dragMoved = false;
      startX = e.clientX; startY = e.clientY;
      startVx = vx; startVy = vy;
      activeId = e.pointerId;
      // Do NOT capture the pointer here: capturing on pointerdown steals the
      // click from the name link / collapse toggle underneath, so a plain tap
      // never navigates or toggles. Capture only once a real drag begins.
    });

    s.addEventListener('pointermove', function (e) {
      if (!dragging || e.pointerId !== activeId) return;
      var dx = e.clientX - startX, dy = e.clientY - startY;
      if (!dragMoved && (Math.abs(dx) > DRAG_SLOP || Math.abs(dy) > DRAG_SLOP)) {
        dragMoved = true; userInteracted = true;
        try { s.setPointerCapture(activeId); } catch (_) {}   // capture only while panning
        s.style.cursor = 'grabbing';
      }
      if (!dragMoved) return;
      var rect = s.getBoundingClientRect();
      vx = startVx - dx / rect.width * vw;
      vy = startVy - dy / rect.height * vh;
      applyView();
    });

    function endDrag() {
      if (!dragging) return;
      dragging = false;
      s.style.cursor = 'grab';
      try { if (activeId != null) s.releasePointerCapture(activeId); } catch (_) {}
      activeId = null;
    }
    s.addEventListener('pointerup', endDrag);
    s.addEventListener('pointercancel', endDrag);

    // A pan must not fire the name-link or the collapse toggle underneath it.
    // If the press travelled past the slop, swallow the trailing click in the
    // capture phase before it reaches the link/button. A genuine click (no
    // travel) is left untouched and navigates / toggles as normal.
    s.addEventListener('click', function (e) {
      if (dragMoved) { e.preventDefault(); e.stopPropagation(); dragMoved = false; }
    }, true);

    // Stretch: double-click recentres that point and zooms in a notch. (On a
    // link the first click already navigates, so this acts on the canvas.)
    s.addEventListener('dblclick', function (e) {
      e.preventDefault();
      var rect = s.getBoundingClientRect();
      var ux = vx + (e.clientX - rect.left) / rect.width * vw;
      var uy = vy + (e.clientY - rect.top) / rect.height * vh;
      zoomAt(e.clientX, e.clientY, 1.4);
      vx = ux - vw / 2;
      vy = uy - vh / 2;
      applyView();
    });

    // ---- resize: hold scale + horizontal centre; re-fit if never touched -----
    global.addEventListener('resize', function () {
      var vpW = viewportW();
      if (userInteracted) {
        var sc = currentScale();          // height-derived, stable on width change
        var cx = vx + vw / 2;
        vw = vpW / sc;
        vx = cx - vw / 2;
        applyView();
      } else {
        fit();
      }
    });

    // ---- draw: rebuild the content group from the current collapse state -----
    function draw() {
      var leaves = layout(root, collapsed);
      var depth = maxDepth(root, collapsed);
      var width = Math.max(leaves * COL_W, COL_W);
      var height = (depth + 1) * ROW_H;
      contentW = width; contentH = height;

      while (content.firstChild) content.removeChild(content.firstChild);

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
          content.appendChild(p);
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
            userInteracted = true;   // keep the reader's pan/zoom across the redraw
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
        content.appendChild(fo);

        if (!collapsed[node.id]) kids.forEach(nodes);
      })(root);
    }

    draw();
    fit();
    // If the container had no width yet at first paint (e.g. laid out a tick
    // later), correct the fit once the browser settles - unless the reader has
    // already grabbed the tree.
    if (global.requestAnimationFrame) {
      global.requestAnimationFrame(function () { if (!userInteracted) fit(); });
    }
  }

  global.FhaTree = { render: render };
})(window);
