/* fha-search.js: the search page of the family site.
 *
 * A cousin types a name, a place or a record title and sees every match on
 * this site, grouped as People, Places and Records. It works from a USB
 * stick with no internet: the words to search are in data/search-data.js
 * (window.FHA_SEARCH, written by fha site from the pages this site already
 * shows), and nothing is fetched or stored.
 *
 * Spelling: the query is folded by the same rule the build used for the
 * names (fold(), driven by the table in the data file), so "Kowalsky" finds
 * Kowalski. Every word typed must start some word of an entry; a person can
 * also be found by a married partner's surname, ranked below a match on
 * their own names. Results are built with text nodes only, never as HTML.
 *
 * Also loadable from node (module.exports), so the build's tests can check
 * that fold() and search() agree with the Python side. */
(function () {
  'use strict';

  var MIN_LETTERS = 2;     // a shorter query shows nothing
  var GROUP_LIMIT = 25;    // results shown per group before "Show all"
  var DEBOUNCE_MS = 150;

  /* The search words of `text`: accents folded (table.chars, then NFKD with
   * every mark dropped), lower case, table.pairs replaced in order, doubled
   * letters once, split on anything that is not a-z or 0-9. The same steps
   * as site.py's _search_fold. */
  function fold(text, table) {
    return finish(plain(text, table), table);
  }

  /* The first steps of fold(): accents folded, lower case. */
  function plain(text, table) {
    var chars = (table && table.chars) || {};
    var s = '';
    for (var ch of String(text == null ? '' : text)) {
      s += Object.prototype.hasOwnProperty.call(chars, ch) ? chars[ch] : ch;
    }
    return s.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase();
  }

  /* The rest of fold(): the pairs in order, doubled letters once, split. */
  function finish(s, table) {
    var pairs = (table && table.pairs) || [];
    for (var i = 0; i < pairs.length; i++) {
      s = s.split(pairs[i][0]).join(pairs[i][1]);
    }
    s = s.replace(/([a-z])\1+/g, '$1');
    return s.split(/[^a-z0-9]+/).filter(Boolean);
  }

  /* The query as a list of words, each a list of the folded forms that may
   * start a word of an entry. The box searches as she types, so a word can
   * stop partway through a spelling the fold rewrites: "Step" is on its way
   * to "Stephen", shipped as "stefen". Such a word also tries each way of
   * finishing that spelling ("Steph" -> "stef"), so it keeps finding the
   * name while she types. These forms only add matches, never lose one. */
  function queryWords(query, table) {
    var pairs = (table && table.pairs) || [];
    return plain(query, table).split(/[^a-z0-9]+/).filter(Boolean).map(function (raw) {
      var forms = [];
      var add = function (text) {
        var f = finish(text, table);
        if (f.length === 1 && forms.indexOf(f[0]) < 0) forms.push(f[0]);
      };
      add(raw);
      pairs.forEach(function (pair) {
        for (var n = 1; n < pair[0].length; n++) {
          if (raw.slice(-n) === pair[0].slice(0, n)) add(raw + pair[0].slice(n));
        }
      });
      return forms;
    });
  }

  function tokensOf(t) {
    return t ? t.split(' ') : [];
  }

  /* How well one entry matches the query words (from queryWords()): 3 for
   * each word that is a whole word of the entry, 2 for each that starts
   * one, 1 for each found only in a married surname. A word typed partway
   * through a folded spelling counts as starting one. 0 when any word
   * matches nothing. */
  function score(words, entry) {
    var own = tokensOf(entry.t);
    var total = 0;
    var married = null;
    for (var i = 0; i < words.length; i++) {
      var forms = words[i];
      var best = 0;
      for (var j = 0; j < own.length && best < 3; j++) {
        for (var f = 0; f < forms.length && best < 3; f++) {
          if (own[j].indexOf(forms[f]) !== 0) continue;
          best = Math.max(best, f === 0 && own[j] === forms[0] ? 3 : 2);
        }
      }
      if (!best && entry.m) {
        for (var k = 0; k < entry.m.length && !best; k++) {
          var mt = tokensOf(entry.m[k].t);
          for (var n = 0; n < mt.length && !best; n++) {
            for (var g = 0; g < forms.length; g++) {
              if (mt[n].indexOf(forms[g]) === 0) {
                best = 1; married = married || entry.m[k].label; break;
              }
            }
          }
        }
      }
      if (!best) return { score: 0 };
      total += best;
    }
    return { score: total, married: married };
  }

  /* Every match in one list, best first; equal scores keep the list's own
   * (alphabetical) order. */
  function rank(words, list, isPerson) {
    var hits = [];
    (list || []).forEach(function (entry, order) {
      var r = score(words, entry);
      if (!r.score) return;
      var out = {};
      for (var key in entry) {
        if (key !== 't' && key !== 'm') out[key] = entry[key];
      }
      if (isPerson && r.married) out.married = r.married;
      hits.push({ score: r.score, order: order, entry: out });
    });
    hits.sort(function (a, b) { return (b.score - a.score) || (a.order - b.order); });
    return hits.map(function (h) { return h.entry; });
  }

  /* Whether `query` is long enough to search: two letters or digits as
   * typed, before the fold ("Yi" folds to one letter and still counts). */
  function longEnough(query, table) {
    return plain(query, table).replace(/[^a-z0-9]+/g, '').length >= MIN_LETTERS;
  }

  /* {people, places, sources}: the matches for `query` in `data`
   * (window.FHA_SEARCH). Fewer than two letters finds nothing. */
  function search(data, query) {
    var table = data && data.fold;
    var words = queryWords(query, table);
    if (!words.length || !longEnough(query, table)) return { people: [], places: [], sources: [] };
    return {
      people: rank(words, data.people, true),
      places: rank(words, data.places, false),
      sources: rank(words, data.sources, false)
    };
  }

  var api = { fold: fold, search: search };
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof window === 'undefined' || typeof document === 'undefined') return;
  window.FhaSearch = api;

  // The page.

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  function resultItem(entry, kind) {
    var li = el('li');
    var a = el('a', null, kind === 'sources' ? entry.title : entry.name);
    a.href = entry.href;
    li.appendChild(a);
    var extra = '';
    if (kind === 'people') {
      if (entry.years) extra += ' (' + entry.years + ')';
      if (entry.married) extra += ', ' + entry.married;
    } else if (kind === 'places') {
      if (entry.within) extra += ', ' + entry.within;
    } else if (entry.year && entry.title.indexOf(entry.year) < 0) {
      extra += ' (' + entry.year + ')';
    }
    if (extra) li.appendChild(document.createTextNode(extra));
    return li;
  }

  function group(title, list, kind) {
    var section = el('section', 'search-group');
    section.appendChild(el('h2', null, title));
    var ul = el('ul', 'search-list');
    list.forEach(function (entry, i) {
      var li = resultItem(entry, kind);
      if (i >= GROUP_LIMIT) li.hidden = true;
      ul.appendChild(li);
    });
    section.appendChild(ul);
    if (list.length > GROUP_LIMIT) {
      var more = el('button', 'btn search-more', 'Show all ' + list.length);
      more.type = 'button';
      more.addEventListener('click', function () {
        ul.querySelectorAll('li[hidden]').forEach(function (li) { li.hidden = false; });
        more.remove();
      });
      section.appendChild(more);
    }
    return section;
  }

  function show(box, results, data, query) {
    while (results.firstChild) results.removeChild(results.firstChild);
    if (!data) {
      results.appendChild(el('p', 'empty',
        'The list of names for this search is missing from this copy of the site.'));
      return;
    }
    if (!longEnough(query, data.fold)) return;
    var found = search(data, query);
    var groups = [['People', found.people, 'people'], ['Places', found.places, 'places'],
                  ['Records', found.sources, 'sources']];
    var any = false;
    groups.forEach(function (g) {
      if (!g[1].length) return;
      any = true;
      results.appendChild(group(g[0], g[1], g[2]));
    });
    if (!any) {
      var p = el('p', 'empty');
      p.appendChild(document.createTextNode('Nothing on this site matches "' + query.trim() +
        '". Try fewer letters, or just the surname. Everyone on the site is also listed on the '));
      var home = el('a', null, 'home page');
      home.href = 'index.html#people';
      p.appendChild(home);
      p.appendChild(document.createTextNode('.'));
      results.appendChild(p);
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    var box = document.getElementById('search-q');
    var results = document.getElementById('search-results');
    if (!box || !results) return;
    var data = window.FHA_SEARCH || null;
    var timer = null;
    function run() {
      clearTimeout(timer);
      show(box, results, data, box.value);
    }
    box.value = new URLSearchParams(window.location.search).get('q') || '';
    run();
    box.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(run, DEBOUNCE_MS);
    });
    box.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter') return;
      ev.preventDefault();
      run();
      var first = results.querySelector('.search-list a');
      if (first) window.location.href = first.href;
    });
  });
})();
