/*
 * fha-tree.js - the family explorer, and pan/zoom for the static charts.
 *
 * In the explorer a husband and wife stand side by side as one pair, a
 * later marriage sits beside the first with a dotted link, each child hangs
 * under their own mother and father, his parents and grandparents stand
 * above him as couples (an hourglass), and a person met a second time is a
 * small "already in the tree" card that jumps to the first one. Names link
 * to person pages; -/+ collapse and expand a family.
 *
 * This is the VENDORED rendering engine for fha site's interactive trees
 * (TOOLING §12 "borrow the engine"). render() lays the tree out with D3's
 * d3.hierarchy and d3-flextree (a tidy tree whose nodes have their own
 * widths, so a couple takes the room of two cards), both vendored beside
 * this file and loaded first with plain <script> tags, so it works from
 * file:// with no CDN. wrapStatic() needs neither.
 *
 * It knows NOTHING about the archive's neutral tree-JSON contract. Its only
 * input is the family layout model produced by tree-adapter.js: a tree of
 * units, each one or two person cards (see that file's header):
 *
 *     { key, kind, people: [card, ...], main, extra, owner, edgeKind,
 *       edgeWeak, seeKey, children: [ <same shape>, ... ] }
 *
 * plus, for an hourglass, `up` (the units above the seed, flagged `above`,
 * whose `children` are the generation above them) and `upOf` (the seed).
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
 *
 * wrapStatic(container, options) (#115) is a second, independent entry point:
 * it wraps an ALREADY-DRAWN static SVG (the home page's server-rendered
 * ancestor pedigree, tools/site.py's _render_pedigree_svg) in the same
 * pan/zoom viewport - drag to pan, wheel/pinch/+/- to zoom, a Fit button -
 * without running layout(), draw(), or anything else that assumes a
 * collapsible node hierarchy. It duplicates the pan/zoom scaffold from
 * render() below rather than sharing it, on purpose: render()'s scaffold is
 * entangled with collapse state and node layout in ways that are not worth
 * disentangling for this one extra caller. Both keep their own copy of the
 * same small pan/zoom MATH (the MIN/MAX_SCALE, FIT_PAD, DRAG_SLOP, ZOOM_STEP
 * constants above are shared); only the DOM wiring is duplicated.
 *
 * FhaTree._layoutModel(model, collapsed) is internal, for the tests only:
 * the unit positions render() would draw, as [{key, x, y, w}] (x the centre,
 * y the top).
 */
(function (global) {
  'use strict';

  var SVGNS = 'http://www.w3.org/2000/svg';
  var CARD_W = 200;     // one person's card: room for a portrait + a two-line name
  var CARD_H = 68;
  var COUPLE_GAP = 16;  // between husband and wife in one couple
  var H_GAP = 28;       // between neighbouring units in a row
  var V_GAP = 44;       // between generations
  var REPEAT_W = 150;   // the smaller "already in the tree" card
  var LATER_RISE = 10;  // a later-marriage link that has to pass other cards runs this far above them

  // Zoom is expressed as pixels-per-content-unit (1 = drawn 1:1). Fit for a
  // wide tree lands well below 1; the ceiling keeps a single card from filling
  // the whole viewport.
  //
  // MIN_SCALE bounds MANUAL zoom-out only (the "-" button, wheel, pinch - see
  // clampScale() in render()/wrapStatic() below), not what fit() itself is
  // allowed to compute (#152 review fix, P2). At the deep end of a
  // configured home pedigree (home_pedigree_generations 7-8, the documented
  // max) with a well-populated tree, the chart's drawn height can run
  // several thousand content units tall; fitting that into the at-most-620px
  // viewport needs a scale below 0.1. fit() used to run its computed scale
  // through the same clampScale() as manual zoom, which floored it at 0.1 -
  // so "Fit chart to view" could not actually show the whole pedigree, and
  // there was no way to zoom out any further to see the rest. fit() now
  // computes and uses whatever scale the content actually needs (still
  // capped by MAX_SCALE, so a tiny tree does not over-zoom on Fit); only a
  // deliberate manual zoom-out is still floored at MIN_SCALE.
  var MIN_SCALE = 0.1;
  var MAX_SCALE = 2.5;
  var FIT_PAD = 48;      // content-space breathing room around the tree on Fit
  var DRAG_SLOP = 4;     // px of travel before a press becomes a pan (vs a click)
  var ZOOM_STEP = 1.25;  // per +/- button press
  var HOME_SCALE = 1;    // px-per-unit when the Home button frames the home person

  // Effective next scale for one manual zoom step from `sc` by `factor`
  // (factor < 1 zooms out, > 1 zooms in). Normally just the ordinary
  // MIN/MAX_SCALE clamp - but once fit() has already dropped the view below
  // MIN_SCALE (a very deep pedigree; see MIN_SCALE's own comment above),
  // clamping a further zoom-OUT the ordinary way would raise it back up to
  // MIN_SCALE - i.e. REVERSE the requested direction, so pressing "zoom out"
  // would visibly zoom in (#152 review fix, P2, finding 2). Below the floor,
  // an outward request is instead allowed to continue past MIN_SCALE (still
  // capped by MAX_SCALE); an inward request from below the floor is
  // unaffected - it may land back at MIN_SCALE, same as the ordinary clamp.
  // Shared by render() and wrapStatic(): both duplicate the DOM wiring
  // around this per this file's own design note above (`s`/`vx`/`vw`/...
  // are per-instance local state), but the pan/zoom MATH itself is one
  // function so a fix here can't land in only one of the two copies.
  function nextZoomScale(sc, factor) {
    if (factor < 1 && sc < MIN_SCALE) return Math.min(MAX_SCALE, sc * factor);
    return Math.max(MIN_SCALE, Math.min(MAX_SCALE, sc * factor));
  }

  function svg(tag, attrs) {
    var e = document.createElementNS(SVGNS, tag);
    for (var k in attrs) { if (attrs.hasOwnProperty(k)) e.setAttribute(k, attrs[k]); }
    return e;
  }

  function unitWidth(u) {
    if (u.kind === 'root') return 1;
    if (u.kind === 'repeat') return REPEAT_W;
    var n = Math.max(u.people.length, 1);
    return n * CARD_W + (n - 1) * COUPLE_GAP;
  }

  // Place every visible unit with d3-flextree: each unit is as wide as its
  // cards, children sit one generation below, and neighbouring units keep
  // H_GAP apart (a little more between cousins than between siblings). A
  // unit collapsed by the reader contributes no children. Returns the
  // units with x (centre) and y (top), shifted so the drawing starts at 0,0.
  function flexLayout(model, collapsed) {
    var flex = global.d3.flextree()
      .nodeSize(function (n) {
        return [unitWidth(n.data) + H_GAP, n.data.kind === 'root' ? 0 : CARD_H + V_GAP];
      })
      .spacing(function (a, b) { return a.parent === b.parent ? 0 : H_GAP; });
    var h = flex.hierarchy(model, function (u) {
      return collapsed[u.key] || !(u.children && u.children.length) ? null : u.children;
    });
    flex(h);
    return h;
  }

  // The card index of person `id` in a placed unit (-1 when not on it).
  function cardIndex(unit, id) {
    for (var i = 0; i < unit.people.length; i++) {
      if (unit.people[i] && unit.people[i].id === id) return i;
    }
    return -1;
  }

  // The unit the seed's ancestors stand over: the one they head (their
  // first marriage), else any unit their card is on.
  function seedUnit(model, id) {
    var heads = null, appears = null;
    (function find(x) {
      if (heads) return;
      if (x.kind !== 'repeat' && x.kind !== 'root') {
        if (x.main >= 0 && x.people[x.main].id === id) { heads = x; return; }
        if (!appears && cardIndex(x, id) >= 0) appears = x;
      }
      (x.children || []).forEach(find);
    })(model);
    return heads || appears;
  }

  // Place every visible unit with d3-flextree: each unit is as wide as its
  // cards, children sit one generation below, and neighbouring units keep
  // H_GAP apart (a little more between cousins than between siblings). A
  // unit collapsed by the reader contributes no children. Returns the
  // units with x (centre) and y (top), shifted so the drawing starts at 0,0.
  //
  // With ancestors (model.up, an hourglass), they are laid out as a second
  // tree and turned upside down: his parents' couple one row above his own
  // card and centred on it, their parents a row above that, and so on.
  // Each placed ancestor unit carries `belowKey`, the unit it hangs over.
  function layoutUnits(model, collapsed) {
    var placed = [];
    var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    function keep(p) {
      placed.push(p);
      minX = Math.min(minX, p.x - p.w / 2); maxX = Math.max(maxX, p.x + p.w / 2);
      minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y + CARD_H);
    }
    var at = {};
    flexLayout(model, collapsed).each(function (n) {
      if (n.data.kind === 'root') return;   // the hidden holder of several families
      var p = { unit: n.data, x: n.x, y: n.y, w: unitWidth(n.data) };
      at[n.data.key] = p;
      keep(p);
    });
    var seed = model.up && model.up.length ? seedUnit(model, model.upOf) : null;
    var base = seed && at[seed.key];
    if (base) {
      var i = Math.max(cardIndex(base.unit, model.upOf), 0);
      var cx = base.x - base.w / 2 + i * (CARD_W + COUPLE_GAP) + CARD_W / 2;
      var top = { key: 'up:root', kind: 'root', people: [], children: model.up };
      var h = flexLayout(top, collapsed);
      var dx = cx - h.x;
      h.each(function (n) {
        if (n === h) return;
        keep({ unit: n.data, x: n.x + dx, y: base.y - (CARD_H + V_GAP) - n.y,
               w: unitWidth(n.data), belowKey: n.parent === h ? base.unit.key : n.parent.data.key });
      });
    }
    if (!placed.length) return { units: [], width: CARD_W, height: CARD_H };
    placed.forEach(function (p) { p.x -= minX; p.y -= minY; });
    return { units: placed, width: maxX - minX, height: maxY - minY };
  }

  function layoutModel(model, collapsed) {
    return layoutUnits(model, collapsed || {}).units.map(function (p) {
      return { key: p.unit.key, x: p.x, y: p.y, w: p.w };
    });
  }

  // Colour each major line distinctly: every child of the top couple gets a
  // branch number (1..7, cycling), inherited by their descendants; a later
  // marriage keeps its person's colour. The renderer surfaces it as
  // data-branch and the stylesheet underlines the name in the matching
  // --branch-N colour. The top couple itself is left uncoloured; when the
  // first person had several marriages, each marriage is a branch.
  function assignBranches(model) {
    var branchOf = {};
    var b = 0, lastOwner = null;
    (model.children || []).forEach(function (u) {
      if (model.kind === 'root' || !(u.extra && u.owner === lastOwner)) b += 1;
      lastOwner = u.owner;
      var n = ((b - 1) % 7) + 1;
      (function mark(x) {
        branchOf[x.key] = n;
        (x.children || []).forEach(mark);
      })(u);
    });
    return branchOf;
  }

  function render(container, model, options) {
    options = options || {};
    var collapsed = {};   // unit key -> true when the reader has collapsed it
    var branchOf = assignBranches(model);

    // Who is where in the model: each unit's parent, and for a later
    // marriage the first unit of the same person (its dotted link starts
    // at that person's card there).
    var parentOf = {}, firstOf = {}, siblingIndex = {};
    (function index(u) {
      var firstByOwner = {};
      (u.children || []).forEach(function (c, i) {
        parentOf[c.key] = u;
        siblingIndex[c.key] = i;
        if (!c.extra) firstByOwner[c.owner] = c;
        else if (firstByOwner[c.owner]) firstOf[c.key] = firstByOwner[c.owner];
        index(c);
      });
    })(model);
    // Ancestors: each unit's "parent" here is the unit it stands over, so
    // opening a collapsed couple reveals the generation above it.
    var up = model.up || [];
    up.forEach(function index(u) {
      (u.children || []).forEach(function (c) { parentOf[c.key] = u; index(c); });
    });

    function findUnit(key) {
      var hit = null;
      function find(x) {
        if (hit) return;
        if (x.key === key) { hit = x; return; }
        (x.children || []).forEach(find);
      }
      find(model);
      up.forEach(find);
      return hit;
    }

    // Bound the initial paint: families at or beyond options.initialDepth
    // generations start collapsed, so a large descendant explorer renders a
    // few generations up front and the reader expands forward on demand
    // (the data is complete - nothing is dropped, only hidden). Omitting
    // initialDepth shows everything.
    if (options.initialDepth != null) {
      (function seed(u, depth) {
        var kids = u.children || [];
        if (depth >= options.initialDepth && kids.length) collapsed[u.key] = true;
        kids.forEach(function (c) { seed(c, depth + 1); });
      })(model, model.kind === 'root' ? -1 : 0);
    }
    // Above him, his parents and grandparents show at first
    // (options.initialAncestorDepth, default 2 generations); "+" on a
    // couple shows the generation above them.
    var ancestorDepth = options.initialAncestorDepth != null ? options.initialAncestorDepth : 2;
    up.forEach(function seed(u) {
      var depth = 1;
      for (var a = parentOf[u.key]; a; a = parentOf[a.key]) depth += 1;
      if (depth >= ancestorDepth && (u.children || []).length) collapsed[u.key] = true;
      (u.children || []).forEach(seed);
    });

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
      // Not clampScale(): MIN_SCALE must not stop Fit from showing the whole
      // tree (see the MIN_SCALE comment above) - only cap the ceiling here.
      var sc = Math.min(MAX_SCALE, vpW / (contentW + FIT_PAD * 2),
                                   VH / (contentH + FIT_PAD * 2));
      vw = vpW / sc; vh = VH / sc;
      vx = contentW / 2 - vw / 2;
      vy = contentH / 2 - vh / 2;
      applyView();
    }

    // Zoom about a client (screen) point so that point stays put under it.
    function zoomAt(clientX, clientY, factor) {
      var sc = currentScale();
      var next = nextZoomScale(sc, factor);
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

    // Where each visible unit was drawn last (unit key -> {unit, x, y, w}).
    var pos = {};

    function cardLeft(p, i) { return p.x - p.w / 2 + i * (CARD_W + COUPLE_GAP); }

    // Centre on one unit at a comfortable scale (the family and its
    // neighbours in frame), first opening any collapsed family above it so
    // it is drawn. With a person id, centre on that person's card. Falls
    // back to Fit when the unit is not in the tree.
    function focusUnit(key, pid) {
      var u = findUnit(key);
      if (!u) { fit(); return; }
      var opened = false;
      for (var a = parentOf[key]; a; a = parentOf[a.key]) {
        if (collapsed[a.key]) { collapsed[a.key] = false; opened = true; }
      }
      if (opened || !pos[key]) draw();
      var p = pos[key];
      if (!p) { fit(); return; }
      var cx = p.x;
      if (pid != null && u.kind !== 'repeat') {
        for (var i = 0; i < u.people.length; i++) {
          if (u.people[i] && u.people[i].id === pid) cx = cardLeft(p, i) + CARD_W / 2;
        }
      }
      userInteracted = true;
      var sc = clampScale(HOME_SCALE);
      var vpW = viewportW();
      vw = vpW / sc; vh = VH / sc;
      vx = cx - vw / 2;
      vy = p.y + CARD_H / 2 - vh / 2;
      applyView();
    }

    // Home: centre + zoom on the designated home person. Prefers the unit
    // the person heads over one where they appear as someone's husband or
    // wife; falls back to Fit when no homeId was given or the person is not
    // in the tree.
    function home() {
      if (options.homeId == null) { fit(); return; }
      var target = seedUnit(model, options.homeId);
      for (var i = 0; !target && i < up.length; i++) target = seedUnit(up[i], options.homeId);
      if (!target) { fit(); return; }
      focusUnit(target.key, options.homeId);
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
    function line(cls, d) { content.appendChild(svg('path', { 'class': cls, d: d })); }

    function edgeClass(kind, weak) {
      // Edge kind decides the dash pattern the page CSS draws (genetic solid,
      // legal long-dash, other dotted); a tie rated low is also thin and
      // broken (chart plan 08-7).
      return 'fha-tree-edge'
        + (kind === 'legal' ? ' fha-tree-edge-legal'
           : kind === 'other' ? ' fha-tree-edge-other' : '')
        + (weak ? ' fha-tree-edge-weak' : '');
    }

    // The later-marriage link: from the person's card in their first couple
    // to the new spouse. Beside it, a straight dotted line at mid-height;
    // past other cards, up over them and down into the spouse's card.
    // Returns the point the couple's children hang from.
    function laterLink(p) {
      var first = firstOf[p.unit.key] && pos[firstOf[p.unit.key].key];
      var sx = cardLeft(p, 0);
      var midY = p.y + CARD_H / 2;
      if (!first || first.unit.main < 0) return { x: sx + CARD_W / 2, y: p.y + CARD_H };
      var mx = cardLeft(first, first.unit.main);
      var beside = first.unit.main === first.unit.people.length - 1 &&
                   siblingIndex[p.unit.key] === siblingIndex[first.unit.key] + 1;
      if (beside) {
        line('fha-tree-couple fha-tree-couple-later',
             'M' + (mx + CARD_W) + ',' + midY + ' H' + sx);
        return { x: (mx + CARD_W + sx) / 2, y: midY };
      }
      var top = p.y - LATER_RISE;
      line('fha-tree-couple fha-tree-couple-later',
           'M' + (mx + CARD_W - LATER_RISE * 2) + ',' + p.y + ' V' + top +
           ' H' + (sx + CARD_W / 2) + ' V' + p.y);
      return { x: sx + CARD_W / 2, y: p.y + CARD_H };
    }

    function cardBox(c, unit, isAnchor) {
      var box = document.createElement('div');
      box.className = 'fha-node' + (c.url ? '' : ' fha-node-nolink')
        + (c['private'] ? ' fha-node-private' : '') + (c.weak ? ' fha-node-weak' : '');
      // A Private card shows no line colour, as on the family chart.
      if (branchOf[unit.key] && !c['private']) box.setAttribute('data-branch', branchOf[unit.key]);
      if (c.weak) box.setAttribute('title', 'Linked to the first person in this tree only through a low-confidence record.');

      var kids = unit.children || [];
      if (isAnchor && kids.length) {
        var toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'fha-toggle';
        toggle.textContent = collapsed[unit.key] ? '+' : '−'; // minus sign
        toggle.setAttribute('aria-label', unit.above
          ? (collapsed[unit.key] ? 'Show their parents' : 'Hide their parents')
          : (collapsed[unit.key] ? 'Show this family\'s children' : 'Hide this family\'s children'));
        toggle.onclick = function (ev) {
          ev.preventDefault();
          collapsed[unit.key] = !collapsed[unit.key];
          userInteracted = true;   // keep the reader's pan/zoom across the redraw
          draw();
        };
        box.appendChild(toggle);
      }

      // A small fixed-size portrait square, or a monogram placeholder when the
      // person has no profile photo (a Private card gets a blank square).
      // Size is locked in CSS so it never changes the card geometry.
      var portrait;
      if (c.photo) {
        portrait = document.createElement('img');
        portrait.className = 'fha-portrait';
        portrait.src = c.photo;
        portrait.alt = '';
        portrait.loading = 'lazy';
      } else {
        portrait = document.createElement('span');
        portrait.className = 'fha-portrait fha-portrait-empty';
        portrait.setAttribute('aria-hidden', 'true');
        if (!c['private']) portrait.textContent = ((c.name || c.id || '?').charAt(0) || '?').toUpperCase();
      }
      box.appendChild(portrait);

      var text = document.createElement('div');
      text.className = 'fha-node-text';
      var name = document.createElement(c.url ? 'a' : 'span');
      name.className = 'fha-name';
      name.textContent = c.name || c.id;
      if (c.url) name.setAttribute('href', c.url);
      text.appendChild(name);
      if (c.dates) {
        var d = document.createElement('small');
        d.className = 'fha-dates';
        d.textContent = c.dates;
        text.appendChild(d);
      }
      if (c.repeatOf) {
        // This husband or wife is also drawn elsewhere in the tree.
        var also = document.createElement('button');
        also.type = 'button';
        also.className = 'fha-also-shown';
        also.textContent = 'Also in the tree: go there';
        also.onclick = function (ev) { ev.preventDefault(); focusUnit(c.repeatOf, c.id); };
        text.appendChild(also);
      }
      box.appendChild(text);
      return box;
    }

    // A person met a second time: a smaller card that jumps to the first.
    function repeatBox(c, unit) {
      var box = document.createElement('div');
      box.className = 'fha-node fha-node-repeat' + (c['private'] ? ' fha-node-private' : '');
      if (branchOf[unit.key] && !c['private']) box.setAttribute('data-branch', branchOf[unit.key]);
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'fha-repeat-link';
      var name = document.createElement('span');
      name.className = 'fha-name';
      name.textContent = c.name || c.id;
      var note = document.createElement('small');
      note.className = 'fha-dates';
      note.textContent = 'Already in the tree: go there';
      b.appendChild(name);
      b.appendChild(note);
      b.onclick = function (ev) { ev.preventDefault(); focusUnit(unit.seeKey, c.id); };
      box.appendChild(b);
      return box;
    }

    function place(box, x, w, y) {
      var fo = svg('foreignObject', { x: x, y: y, width: w, height: CARD_H });
      fo.appendChild(box);
      content.appendChild(fo);
    }

    function draw() {
      var lay = layoutUnits(model, collapsed);
      contentW = Math.max(lay.width, CARD_W);
      contentH = Math.max(lay.height, CARD_H);
      pos = {};
      lay.units.forEach(function (p) { pos[p.unit.key] = p; });

      while (content.firstChild) content.removeChild(content.firstChild);

      // Lines first, so the cards sit on top. Each unit's children hang from
      // one point: the middle of the couple's join, the middle of a later
      // marriage's dotted link, or the bottom of a lone card.
      var hangFrom = {};
      lay.units.forEach(function (p) {
        var u = p.unit, midY = p.y + CARD_H / 2;
        if (u.kind === 'repeat') return;
        if (u.extra) { hangFrom[u.key] = laterLink(p); return; }
        if (u.people.length === 2) {
          var gapL = cardLeft(p, 0) + CARD_W, gapR = cardLeft(p, 1);
          line('fha-tree-couple', 'M' + gapL + ',' + midY + ' H' + gapR);
          hangFrom[u.key] = { x: (gapL + gapR) / 2, y: midY };
        } else {
          hangFrom[u.key] = { x: cardLeft(p, 0) + CARD_W / 2, y: p.y + CARD_H };
        }
      });
      function elbowTo(from, tx, q, kind, weak) {
        var elbow = q.y - V_GAP / 2;
        line(edgeClass(kind, weak),
             'M' + from.x + ',' + from.y + ' V' + elbow + ' H' + tx + ' V' + q.y);
      }
      lay.units.forEach(function (p) {
        var u = p.unit;
        if (u.above || collapsed[u.key] || !hangFrom[u.key]) return;
        var from = hangFrom[u.key];
        (u.children || []).forEach(function (c) {
          var q = pos[c.key];
          if (!q || c.extra) return;   // a later marriage has its dotted link instead
          var tx = c.kind === 'repeat' ? q.x : cardLeft(q, Math.max(c.main, 0)) + CARD_W / 2;
          elbowTo(from, tx, q, c.edgeKind, c.edgeWeak);
        });
      });
      // Each ancestor couple's line runs down to the card of the child
      // they are parents of, in the unit below them.
      lay.units.forEach(function (p) {
        var u = p.unit, q = pos[p.belowKey];
        if (!u.above || !q) return;
        var from = u.kind === 'repeat' ? { x: p.x, y: p.y + CARD_H } : hangFrom[u.key];
        var i = cardIndex(q.unit, u.below);
        var tx = i < 0 ? q.x : cardLeft(q, i) + CARD_W / 2;
        elbowTo(from, tx, q, u.edgeKind, u.edgeWeak);
      });

      // Cards.
      lay.units.forEach(function (p) {
        var u = p.unit;
        if (u.kind === 'repeat') {
          place(repeatBox(u.people[0], u), p.x - p.w / 2, p.w, p.y);
          return;
        }
        var anchor = u.extra ? 0 : Math.max(u.main, 0);
        u.people.forEach(function (c, i) {
          place(cardBox(c, u, i === anchor), cardLeft(p, i), CARD_W, p.y);
        });
      });
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

  // Wrap an already-drawn static SVG (#115) in the same pan/zoom viewport
  // render() builds for the collapsible tree - drag to pan, wheel/pinch/+/-
  // to zoom, a Fit button - with no layout pass of its own: the SVG's own
  // viewBox (as server-rendered) IS the content size and starting view.
  // `container` must already contain exactly one <svg> child (the static
  // chart); a missing or malformed one is left alone rather than guessed at,
  // so a broken wrap degrades to "the plain SVG stays visible, unscrolled" -
  // never a blank box.
  function wrapStatic(container, options) {
    options = options || {};
    var s = container.querySelector('svg');
    if (!s) return;
    var vb = (s.getAttribute('viewBox') || '').split(/\s+/).map(Number);
    if (vb.length !== 4 || vb.some(function (n) { return !isFinite(n); })) return;
    var vx0 = vb[0], vy0 = vb[1], contentW = vb[2], contentH = vb[3];
    if (contentW <= 0 || contentH <= 0) return;

    // Printing: the server-drawn view box is the whole chart. Keep it where
    // fha-site.js can put it back before the page goes to paper, so a chart
    // zoomed on screen still prints whole.
    s.dataset.fhaViewbox = vb.join(' ');

    var viewport = document.createElement('div');
    viewport.className = 'fha-tree-viewport';
    viewport.style.position = 'relative';
    viewport.style.overflow = 'hidden';
    viewport.style.width = '100%';

    var VH = Math.round(Math.min(620, Math.max(380, (global.innerHeight || 800) * 0.7)));
    viewport.style.height = VH + 'px';

    // Move the existing SVG into the new viewport wrapper, in place - no
    // rebuild, so its already-drawn content (cards, links, the axis label)
    // is untouched.
    s.parentNode.insertBefore(viewport, s);
    viewport.appendChild(s);
    s.style.width = '100%';
    s.style.height = VH + 'px';
    s.style.display = 'block';
    s.style.touchAction = 'none';   // we own touch drags (pan), not the page
    s.style.cursor = 'grab';
    s.style.userSelect = 'none';
    s.setAttribute('preserveAspectRatio', 'xMidYMid meet');

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
      b.style.padding = '2px 8px';
      b.style.lineHeight = '1.4';
      b.style.background = 'var(--surface, #f8f5ee)';
      return b;
    }
    controls.appendChild(mkBtn('−', 'Zoom out', function () { zoomBy(1 / ZOOM_STEP); }));
    controls.appendChild(mkBtn('Fit', 'Fit chart to view', function () { fit(); }));
    controls.appendChild(mkBtn('+', 'Zoom in', function () { zoomBy(ZOOM_STEP); }));
    viewport.appendChild(controls);

    function viewportW() {
      return Math.max(viewport.clientWidth || container.clientWidth || 900, 1);
    }

    var vx = vx0, vy = vy0, vw = contentW, vh = contentH;
    var userInteracted = false;

    function applyView() {
      s.setAttribute('viewBox', vx + ' ' + vy + ' ' + vw + ' ' + vh);
    }
    function currentScale() { return VH / vh; }

    function fit() {
      userInteracted = false;
      var vpW = viewportW();
      // No MIN_SCALE floor here: it must not stop Fit from showing the whole
      // chart (see the MIN_SCALE comment above) - only cap the ceiling here.
      // (No clampScale() in this scope - wrapStatic has no home() to need
      // one; zoomAt below uses the shared nextZoomScale() instead.)
      var sc = Math.min(MAX_SCALE, vpW / (contentW + FIT_PAD * 2),
                                   VH / (contentH + FIT_PAD * 2));
      vw = vpW / sc; vh = VH / sc;
      vx = vx0 + contentW / 2 - vw / 2;
      vy = vy0 + contentH / 2 - vh / 2;
      applyView();
    }

    function zoomAt(clientX, clientY, factor) {
      var sc = currentScale();
      var next = nextZoomScale(sc, factor);
      factor = next / sc;
      if (factor === 1) return;
      userInteracted = true;
      var rect = s.getBoundingClientRect();
      var fx = (clientX - rect.left) / rect.width;
      var fy = (clientY - rect.top) / rect.height;
      var ux = vx + fx * vw;
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

    s.addEventListener('wheel', function (e) {
      e.preventDefault();
      zoomAt(e.clientX, e.clientY, Math.pow(1.0015, -e.deltaY));
    }, { passive: false });

    var dragging = false, dragMoved = false;
    var startX = 0, startY = 0, startVx = 0, startVy = 0, activeId = null;

    s.addEventListener('pointerdown', function (e) {
      if (e.button != null && e.button !== 0) return;
      dragging = true; dragMoved = false;
      startX = e.clientX; startY = e.clientY;
      startVx = vx; startVy = vy;
      activeId = e.pointerId;
    });

    s.addEventListener('pointermove', function (e) {
      if (!dragging || e.pointerId !== activeId) return;
      var dx = e.clientX - startX, dy = e.clientY - startY;
      if (!dragMoved && (Math.abs(dx) > DRAG_SLOP || Math.abs(dy) > DRAG_SLOP)) {
        dragMoved = true; userInteracted = true;
        try { s.setPointerCapture(activeId); } catch (_) {}
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

    // A pan must not fire the name-link underneath it - same swallow-the-
    // trailing-click rule render() uses.
    s.addEventListener('click', function (e) {
      if (dragMoved) { e.preventDefault(); e.stopPropagation(); dragMoved = false; }
    }, true);

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

    global.addEventListener('resize', function () {
      var vpW = viewportW();
      if (userInteracted) {
        var sc = currentScale();
        var cx = vx + vw / 2;
        vw = vpW / sc;
        vx = cx - vw / 2;
        applyView();
      } else {
        fit();
      }
    });

    fit();
    if (global.requestAnimationFrame) {
      global.requestAnimationFrame(function () { if (!userInteracted) fit(); });
    }
  }

  global.FhaTree = { render: render, wrapStatic: wrapStatic, _layoutModel: layoutModel };
})(window);
