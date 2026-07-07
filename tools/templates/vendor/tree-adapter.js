/*
 * tree-adapter.js - the single seam between the archive's neutral tree-JSON
 * contract and the vendored renderer (fha-tree.js).
 *
 * It is the ONLY code that understands both:
 *   - the neutral tree JSON (TOOLING §7/§14b, as emitted by `fha views tree
 *     --format json`, plus a per-node `url` the site adds for linking):
 *         { seed, mode, nodes: [{p_id, name, sex, vitals, url}], edges: [...] }
 *   - the renderer's hierarchy format:
 *         { id, name, dates, url, children: [...] }
 *
 * Mode decides which edge type builds the parent→child nesting:
 *   - "descendants": follow `child` edges (person -> their child)
 *   - "ancestors":   follow `parent` edges (person -> their parent), so the
 *                    pedigree fans upward from the subject
 * A visited set guards against cousin-marriage cycles.
 *
 * Swapping renderers later means rewriting this file plus fha-tree.js; the
 * generated pages and the server-side JSON are untouched.
 */
(function (global) {
  'use strict';

  function fmtDates(vitals) {
    vitals = vitals || {};
    var b = vitals.birth || '';
    var d = vitals.death || '';
    if (!b && !d) return '';
    return b + '–' + d;   // en-dash
  }

  function fromNeutral(data) {
    data = data || {};
    var byId = {};
    (data.nodes || []).forEach(function (n) { byId[n.p_id] = n; });

    // Per-mode child direction. Edge {type, from, to}: for 'child' edges
    // `from` is the parent and `to` the child; for 'parent' edges `from` is
    // the child and `to` the parent. Either way we nest `to` under `from`.
    var rel = data.mode === 'ancestors' ? 'parent' : 'child';
    var childrenOf = {};
    (data.edges || []).forEach(function (e) {
      if (e.type === rel) {
        // Carry the edge kind (SPEC §12.2). Prefer the explicit `kind`
        // ('genetic' | 'legal' | 'other'); fall back to the older `genetic`
        // boolean (false ⇒ a legal parent/child bond) for back-compat.
        var genetic = e.genetic !== false;
        var kind = e.kind || (genetic ? 'genetic' : 'legal');
        (childrenOf[e.from] = childrenOf[e.from] || []).push(
          { to: e.to, genetic: genetic, kind: kind });
      }
    });

    var seen = {};
    function build(id) {
      var n = byId[id] || { p_id: id, name: id, vitals: {} };
      var node = { id: n.p_id, name: n.name || n.p_id, url: n.url || null,
                   dates: fmtDates(n.vitals), photo: n.photo || null, children: [] };
      if (!seen[id]) {
        seen[id] = true;
        (childrenOf[id] || []).forEach(function (c) {
          var childNode = build(c.to);
          childNode.edgeGenetic = c.genetic;   // back-compat: genetic bond?
          childNode.edgeKind = c.kind;         // 'genetic' | 'legal' | 'other'
          node.children.push(childNode);
        });
      }
      return node;
    }
    return build(data.seed);
  }

  global.FhaTreeAdapter = { fromNeutral: fromNeutral };
})(window);
