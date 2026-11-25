/*
 * tree-adapter.js - the single seam between the archive's neutral tree-JSON
 * contract and the vendored renderer (fha-tree.js).
 *
 * In the family explorer a husband and wife are drawn side by side, a
 * second marriage sits beside the first, each child hangs under their own
 * mother and father, and his parents and grandparents stand above him as
 * couples too. This file decides which cards go together; it draws
 * nothing.
 *
 * It is the ONLY code that understands both:
 *   - the neutral tree JSON (TOOLING §7, as emitted by `fha views tree
 *     --format json`, plus a per-node `url` the site adds for linking):
 *         { version, seed, mode, nodes: [{p_id, name, sex, vitals, url,
 *           line_confidence, private?}], edges: [{type, from, to, kind,
 *           confidence, family, dates}, ...] }
 *   - the renderer's family layout model, a tree of units:
 *         unit = { key, kind, people: [card, ...], main, extra, fam,
 *                  owner, edgeKind, edgeWeak, seeKey, children: [unit] }
 *         card = { id, name, dates, url, photo, weak, private, repeatOf }
 *     kind is 'family' (a couple, or one parent whose partner is not
 *     shown), 'single' (a person with no family), 'repeat' (a person met
 *     again: a small "shown here" card pointing at seeKey) or 'root' (a
 *     hidden unit holding the seed's families when there are several).
 *     `main` is the index in `people` of the person the unit belongs to
 *     (-1 on a later marriage, which holds only the new spouse); `extra`
 *     marks a later marriage, drawn beside the first with a dotted link.
 *
 * Descendants: each person's families come from the `family` key on their
 * child and spouse edges, in marriage order (undated last). A family whose
 * other parent is not in the data (never recorded, or withheld on the
 * shared site) is the person's own unit, drawn first with no partner card,
 * so a withheld partner leaves no gap. A person met a second time becomes
 * a repeat unit, so a cousin marriage or a cycle in the data never draws a
 * branch twice and never loops.
 *
 * Hourglass (and ancestors): the model also carries `up`, the seed's
 * ancestors as units drawn above them (see ancestorUnits), and `upOf`, the
 * seed's id. In ancestors mode the model itself is just the seed's card.
 *
 * Data without `family` keys (version 1 JSON): one person per unit, each
 * nested under the person they descend from (ancestors: the person they
 * are a parent of), as the explorer always drew them.
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

  function birthYear(n) {
    var m = /\d{4}/.exec((n && n.vitals && n.vitals.birth) || '');
    return m ? m[0] : null;
  }

  // Edge kind (SPEC §12.2): prefer the explicit `kind` ('genetic' | 'legal'
  // | 'other'); fall back to the older `genetic` boolean (false: a legal
  // parent/child bond) for back-compat.
  function edgeKind(e) {
    if (!e) return null;
    return e.kind || (e.genetic !== false ? 'genetic' : 'legal');
  }

  function fromNeutral(data) {
    data = data || {};
    var edges = data.edges || [];
    var byId = {};
    (data.nodes || []).forEach(function (n) { byId[n.p_id] = n; });

    function card(id) {
      var n = byId[id] || { p_id: id, name: id, vitals: {} };
      if (n.private) {
        // A living relative on the shared site: the word "Private" and
        // nothing else (no dates, photo, link or rating).
        return { id: n.p_id, name: 'Private', dates: '', url: null, photo: null,
                 weak: false, 'private': true, repeatOf: null };
      }
      return { id: n.p_id, name: n.name || n.p_id, url: n.url || null,
               dates: fmtDates(n.vitals), photo: n.photo || null,
               // Linked to the seed only through a low-confidence record:
               // drawn faded (chart plan 08-7).
               weak: n.line_confidence === 'low', 'private': false, repeatOf: null };
    }

    var hasFamilies = edges.some(function (e) { return !!e.family; });
    if (!hasFamilies) return perPerson(data, edges, card);
    var model;
    if (data.mode === 'ancestors') {
      model = { key: data.seed, kind: 'single', people: [card(data.seed)], main: 0,
                extra: false, fam: null, owner: data.seed, edgeKind: null, edgeWeak: false,
                seeKey: null, children: [] };
    } else {
      model = couples(data, edges, card, byId);
    }
    if (data.mode === 'ancestors' || data.mode === 'hourglass') {
      model.up = ancestorUnits(data, edges, card, byId);
      model.upOf = data.seed;
    }
    return model;
  }

  // His ancestors as couples, for the half of the explorer above him: his
  // parents as one unit over his own card, each parent's parents over that
  // parent, father's side first. A child's birth parents come first; an
  // adoptive or step-parent stands in a unit of their own beside them,
  // tied with a long dash, and a parent already on a card for that child
  // is not drawn twice. A couple met a second time (two of his ancestors
  // were brother and sister) is a small "already in the tree" card that
  // jumps to the first, which also ends a loop in the data.
  //
  //   up unit = { key: 'up:' + family, kind: 'family' | 'repeat', above: true,
  //               people: [card, ...], below: id of the child they are
  //               parents of, edgeKind, edgeWeak, seeKey, children: [up unit] }
  //
  // `children` of an up unit are the units ABOVE it (its people's parents).
  function ancestorUnits(data, edges, card, byId) {
    var tiesOf = {};   // child id -> {family key: [parent edge]}
    edges.forEach(function (e) {
      if (e.type !== 'parent' || !e.family) return;
      var f = tiesOf[e.from] = tiesOf[e.from] || {};
      (f[e.family] = f[e.family] || []).push(e);
    });
    function sexRank(id) {
      var s = byId[id] && byId[id].sex;
      return s === 'M' ? 0 : s === 'F' ? 1 : 2;
    }
    function byParentOrder(a, b) {
      var d = sexRank(a) - sexRank(b);
      if (d) return d;
      return a < b ? -1 : a > b ? 1 : 0;
    }
    function genetic(ties) {
      return ties.some(function (t) { return edgeKind(t) === 'genetic'; });
    }
    // The tie the line to the child is drawn by: a birth tie when there is one.
    function tieOf(ties) {
      return ties.slice().sort(function (a, b) {
        var ga = edgeKind(a) === 'genetic' ? 0 : 1, gb = edgeKind(b) === 'genetic' ? 0 : 1;
        if (ga !== gb) return ga - gb;
        return a.to < b.to ? -1 : a.to > b.to ? 1 : 0;
      })[0];
    }
    var drawn = {};    // family key -> unit key
    var repeats = 0;
    function parentsOf(pid) {
      var fams = tiesOf[pid] || {};
      var keys = Object.keys(fams).sort(function (a, b) {
        var ga = genetic(fams[a]) ? 0 : 1, gb = genetic(fams[b]) ? 0 : 1;
        if (ga !== gb) return ga - gb;
        return a < b ? -1 : a > b ? 1 : 0;
      });
      var onCard = {};
      var out = [];
      keys.forEach(function (fam) {
        var ids = [];
        fams[fam].forEach(function (t) {
          if (ids.indexOf(t.to) < 0 && !onCard[t.to]) ids.push(t.to);
        });
        if (!ids.length) return;
        ids.sort(byParentOrder);
        ids.forEach(function (id) { onCard[id] = true; });
        var tie = tieOf(fams[fam]);
        var u = { key: 'up:' + fam, kind: 'family', above: true, people: ids.map(card),
                  main: -1, extra: false, fam: fam, owner: pid, below: pid,
                  edgeKind: edgeKind(tie), edgeWeak: tie.confidence === 'low',
                  seeKey: null, children: [] };
        if (drawn[fam]) {
          repeats += 1;
          u.key = 'up-repeat:' + fam + ':' + repeats;
          u.kind = 'repeat';
          u.people = [card(ids[0])];
          u.fam = null;
          u.seeKey = drawn[fam];
        } else {
          drawn[fam] = u.key;
        }
        out.push(u);
      });
      out.forEach(function (u) {
        if (u.kind !== 'family') return;
        u.people.forEach(function (c) {
          Array.prototype.push.apply(u.children, parentsOf(c.id));
        });
      });
      return out;
    }
    return parentsOf(data.seed);
  }

  // One person per unit (ancestors mode, or version 1 JSON).
  function perPerson(data, edges, card) {
    var rel = data.mode === 'ancestors' ? 'parent' : 'child';
    var childrenOf = {};
    edges.forEach(function (e) {
      // For 'child' edges `from` is the parent and `to` the child; for
      // 'parent' edges the other way round. Either way `to` nests under `from`.
      if (e.type === rel) (childrenOf[e.from] = childrenOf[e.from] || []).push(e);
    });
    var placed = {}, repeats = 0;
    function build(id, tie) {
      var tieKind = edgeKind(tie);
      var tieWeak = !!tie && tie.confidence === 'low';
      if (placed[id]) {
        repeats += 1;
        return { key: 'repeat:' + id + ':' + repeats,
                 kind: 'repeat', people: [card(id)], main: 0, extra: false, fam: null,
                 owner: id, edgeKind: tieKind, edgeWeak: tieWeak, seeKey: placed[id],
                 children: [] };
      }
      var u = { key: id, kind: 'single', people: [card(id)], main: 0, extra: false,
                fam: null, owner: id, edgeKind: tieKind, edgeWeak: tieWeak, seeKey: null,
                children: [] };
      placed[id] = u.key;
      u.children = (childrenOf[id] || []).map(function (e) { return build(e.to, e); });
      return u;
    }
    return build(data.seed, null);
  }

  // Couples as one unit (descendants mode with `family` keys).
  function couples(data, edges, card, byId) {
    var famsOf = {};      // person id -> {family key: marriage date or ''}
    var kidsOf = {};      // family key -> [child id]
    var tiesInto = {};    // family key + ' ' + child id -> [edge]
    function addFam(pid, fam, date) {
      var f = famsOf[pid] = famsOf[pid] || {};
      if (!(fam in f) || (date && (!f[fam] || date < f[fam]))) f[fam] = date || f[fam] || '';
    }
    edges.forEach(function (e) {
      if (!e.family) return;
      if (e.type === 'spouse') {
        var date = (e.dates && e.dates.start) || '';
        addFam(e.from, e.family, date);
        addFam(e.to, e.family, date);
      } else if (e.type === 'child') {
        addFam(e.from, e.family, '');
        var k = e.family + ' ' + e.to;
        if (!tiesInto[k]) {
          tiesInto[k] = [];
          (kidsOf[e.family] = kidsOf[e.family] || []).push(e.to);
        }
        tiesInto[k].push(e);
      }
    });

    function partnersIn(fam, pid) {
      return fam.slice(4).split('+').filter(function (id) { return id !== pid; });
    }
    function byBirthThenId(a, b) {
      var ya = birthYear(byId[a]), yb = birthYear(byId[b]);
      if (ya !== yb) {
        if (ya === null) return 1;
        if (yb === null) return -1;
        return ya < yb ? -1 : 1;
      }
      return a < b ? -1 : a > b ? 1 : 0;
    }
    // A person's families: the one with no partner shown first (its
    // children hang straight from the person's own card), then marriages
    // by date, undated after them, ties broken by key.
    function familiesOf(pid) {
      var f = famsOf[pid] || {};
      return Object.keys(f).filter(function (fam) {
        return fam.slice(4).split('+').indexOf(pid) >= 0;
      }).sort(function (a, b) {
        var la = partnersIn(a, pid).length ? 1 : 0, lb = partnersIn(b, pid).length ? 1 : 0;
        if (la !== lb) return la - lb;
        var da = f[a] || '￿', db = f[b] || '￿';
        if (da !== db) return da < db ? -1 : 1;
        return a < b ? -1 : a > b ? 1 : 0;
      });
    }
    // The tie that brings child `kid` into family `fam`: the main parent's
    // own tie when there is one (a step tie from the other parent otherwise).
    function tieFrom(fam, kid, pid) {
      var ties = (tiesInto[fam + ' ' + kid] || []).slice().sort(function (a, b) {
        if ((a.from === pid) !== (b.from === pid)) return a.from === pid ? -1 : 1;
        return a.from < b.from ? -1 : a.from > b.from ? 1 : 0;
      });
      return ties[0] || null;
    }

    var mainOf = {};      // person id -> key of the unit they head
    var drawnAt = {};     // person id -> key of the first unit showing their card
    var famDone = {};     // family key -> unit key
    var repeats = 0;

    function repeatUnit(pid, seeKey, tie) {
      repeats += 1;
      return { key: 'repeat:' + pid + ':' + repeats, kind: 'repeat', people: [card(pid)],
               main: 0, extra: false, fam: null, owner: pid, edgeKind: edgeKind(tie),
               edgeWeak: !!tie && tie.confidence === 'low', seeKey: seeKey, children: [] };
    }

    function unitsFor(pid, tie) {
      if (mainOf[pid]) return [repeatUnit(pid, mainOf[pid], tie)];
      var fams = familiesOf(pid).filter(function (f) { return !famDone[f]; });
      // Already drawn as someone's husband or wife, with every family of
      // theirs already drawn there: point at it rather than draw it twice.
      if (drawnAt[pid] && !fams.length) return [repeatUnit(pid, drawnAt[pid], tie)];
      var tieKind = edgeKind(tie), tieWeak = !!tie && tie.confidence === 'low';
      var self = card(pid);
      if (drawnAt[pid]) self.repeatOf = drawnAt[pid];
      if (!fams.length) {
        var lone = { key: pid, kind: 'single', people: [self], main: 0, extra: false,
                     fam: null, owner: pid, edgeKind: tieKind, edgeWeak: tieWeak,
                     seeKey: null, children: [] };
        mainOf[pid] = drawnAt[pid] = lone.key;
        return [lone];
      }
      var out = fams.map(function (fam, i) {
        var partner = partnersIn(fam, pid)[0];
        var people = [];
        var pc = null;
        if (partner) {
          pc = card(partner);
          if (drawnAt[partner]) pc.repeatOf = drawnAt[partner];
        }
        if (i === 0) {
          // With later marriages to follow, the person stands on the right
          // of the first couple so each later spouse lines up beside them.
          if (!pc) people = [self];
          else people = fams.length > 1 ? [pc, self] : [self, pc];
        } else {
          people = [pc];
        }
        return { key: fam + '#' + pid, kind: 'family', people: people,
                 main: i === 0 ? people.indexOf(self) : -1, extra: i > 0, fam: fam,
                 owner: pid, edgeKind: i === 0 ? tieKind : null,
                 edgeWeak: i === 0 ? tieWeak : false, seeKey: null, children: [] };
      });
      mainOf[pid] = out[0].key;
      if (!drawnAt[pid]) drawnAt[pid] = out[0].key;
      out.forEach(function (u) {
        famDone[u.fam] = u.key;
        partnersIn(u.fam, pid).forEach(function (q) { if (!drawnAt[q]) drawnAt[q] = u.key; });
      });
      out.forEach(function (u) {
        var kids = (kidsOf[u.fam] || []).slice().sort(byBirthThenId);
        u.children = [];
        kids.forEach(function (kid) {
          Array.prototype.push.apply(u.children, unitsFor(kid, tieFrom(u.fam, kid, pid)));
        });
      });
      return out;
    }

    var roots = unitsFor(data.seed, null);
    if (roots.length === 1) return roots[0];
    return { key: 'root', kind: 'root', people: [], main: -1, extra: false, fam: null,
             owner: data.seed, edgeKind: null, edgeWeak: false, seeKey: null, children: roots };
  }

  global.FhaTreeAdapter = { fromNeutral: fromNeutral };
})(window);
