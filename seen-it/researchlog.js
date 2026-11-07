// researchlog.js - turn the add-on's search log and page verdicts into entries
// for the family archive's research log (SPEC: dated YAML items with date,
// question, repository, collection, terms, result), ready to append to
// notes/research-log.md. A logged nil is a finding: it stops the next session
// re-running the same dead end.
//
// Classic script (window.SeenItLog) and CommonJS (tests).

(function (root) {
  function q(s) {
    return '"' + String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  }

  function day(ms) {
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }

  function pageLabel(id, pages) {
    const p = pages[id] || {};
    const bits = [p.paper || 'page ' + id, p.date, p.pageNo ? 'p. ' + p.pageNo : null].filter(Boolean);
    return bits.join(' ');
  }

  // "Newspapers.com" plus any filters other than the keyword itself.
  function collectionOf(params) {
    const parts = [];
    const ds = params['date-start'];
    const de = params['date-end'];
    if (ds || de) parts.push(ds && de && ds !== de ? ds + '-' + de : ds || de);
    for (const [k, v] of Object.entries(params)) {
      if (['keyword', 'date-start', 'date-end'].includes(k)) continue;
      parts.push(k + ': ' + v);
    }
    return 'Newspapers.com' + (parts.length ? ' (' + parts.join('; ') + ')' : '');
  }

  function resultOf(run, s, pages, clips, viewsByDay, passDays) {
    const opened = (s.opened || []).filter((id) => viewsByDay(id).includes(day(run.at)));
    const clipped = opened.filter((id) => (clips[id] || []).length);
    const ruled = opened.filter((id) => (pages[id] || {}).mark === 'nothing' && !(clips[id] || []).length);
    const later = opened.filter((id) => (pages[id] || {}).mark === 'later');
    const passed = (s.shown || []).filter((id) => !(s.opened || []).includes(id) && passDays(id).includes(day(run.at)));
    const count = run.count == null ? 'match count not read' : run.count.toLocaleString('en-US') + ' matches';
    const bits = [count];
    if (!opened.length) bits.push('no pages opened');
    else bits.push('opened ' + opened.length);
    if (clipped.length) bits.push('clipped on ' + clipped.map((id) => pageLabel(id, pages)).join(', '));
    if (ruled.length) bits.push('nothing on ' + ruled.map((id) => pageLabel(id, pages)).join(', '));
    if (later.length) bits.push('to revisit ' + later.map((id) => pageLabel(id, pages)).join(', '));
    if (passed.length) bits.push('passed over ' + passed.length + ' without opening');
    const kind = clipped.length ? 'found' : opened.length && ruled.length === opened.length ? 'nil' : 'partial';
    return kind + ' (' + bits.join('; ') + ')';
  }

  // Build the log text. `since` (ms) limits it to runs and verdicts after a date.
  function build(store, since) {
    const views = store.views || {};
    const pages = store.pages || {};
    const searches = store.searches || {};
    const clips = store.clips || {};
    const from = since || 0;
    const viewsByDay = (id) => ((views[id] && views[id].t) || []).map(day);
    const passes = store.passes || {};
    const passDays = (id) => ((passes[id] && passes[id].t) || []).map(day);
    const entries = [];
    // Which search entries THIS export writes, by search key and day: a
    // verdict is folded into a search entry only when that entry is in this
    // export (and covers a day the page was opened) - otherwise it would be
    // dropped from an incremental export just because the search that found
    // the page was exported last time.
    const written = {};

    for (const [key, s] of Object.entries(searches)) {
      const params = s.params || {};
      const byDay = {};
      for (const r of s.runs || []) if (r.at > from) byDay[day(r.at)] = r; // last run that day
      written[key] = new Set(Object.keys(byDay));
      for (const d of Object.keys(byDay)) {
        const run = byDay[d];
        entries.push({
          at: run.at,
          text: [
            '- date: ' + d,
            '  question: ' + q('Newspapers.com search'),
            '  repository: Newspapers.com',
            '  collection: ' + q(collectionOf(params)),
            '  terms: ' + q(params.keyword || params.terms || Object.values(params).join(' ')),
            '  result: ' + q(resultOf(run, s, pages, clips, viewsByDay, passDays)),
            '  url: ' + q(s.url || ''),
          ].join('\n'),
        });
      }
    }

    // Verdicts on pages that no search entry in this export accounts for
    // (opened from a link, a bookmark, before the add-on was installed, or
    // from a search already exported before the verdict was made).
    const accounted = (id, p) => (p.fromSearches || []).some((k) =>
      written[k] && viewsByDay(id).some((d) => written[k].has(d)));
    for (const [id, p] of Object.entries(pages)) {
      if (!p.mark || !(p.markAt > from) || accounted(id, p)) continue;
      entries.push({
        at: p.markAt,
        text: [
          '- date: ' + day(p.markAt),
          '  question: ' + q('Newspapers.com page review'),
          '  repository: Newspapers.com',
          '  collection: ' + q(pageLabel(id, pages) + (p.place ? ', ' + p.place : '')),
          '  terms: ' + q((p.terms || []).join('; ')),
          '  result: ' + q(p.mark === 'nothing' ? 'nil (page read, nothing relevant)' : 'partial (marked to revisit)'),
          '  url: ' + q('https://www.newspapers.com/image/' + id + '/'),
        ].join('\n'),
      });
    }

    entries.sort((a, b) => a.at - b.at);
    const header = [
      '<!-- Exported ' + new Date().toISOString().slice(0, 16).replace('T', ' ') +
        ' by the "Newspapers.com: Seen It" browser add-on. Append the entries below to',
      '     notes/research-log.md (under its ## Research Log heading). "found" = you clipped',
      '     something on a page from that search; "nil" = every page you opened was marked',
      '     "Nothing here"; "partial" = anything in between. -->',
      '',
      '## Research Log',
      '',
    ].join('\n');
    return { count: entries.length, text: header + entries.map((e) => e.text).join('\n') + '\n' };
  }

  // Coverage: which papers and years you have actually opened pages in.
  function coverage(store) {
    const pages = store.pages || {};
    const views = store.views || {};
    const byPaper = {};
    for (const [id, p] of Object.entries(pages)) {
      if (!p.paper) continue;
      const looked = (views[id] && ((views[id].t || []).length || views[id].n)) || 0;
      if (!looked && !p.mark) continue;
      const c = (byPaper[p.paper] = byPaper[p.paper] || { paper: p.paper, place: p.place || '', pages: 0, ruledOut: 0, years: new Set() });
      c.pages++;
      if (p.mark === 'nothing') c.ruledOut++;
      if (p.date) c.years.add(String(p.date).slice(0, 4));
      if (!c.place && p.place) c.place = p.place;
    }
    return Object.values(byPaper)
      .map((c) => {
        const ys = [...c.years].sort();
        return { paper: c.paper, place: c.place, pages: c.pages, ruledOut: c.ruledOut, years: ys.length ? (ys[0] === ys[ys.length - 1] ? ys[0] : ys[0] + '–' + ys[ys.length - 1]) : '' };
      })
      .sort((a, b) => b.pages - a.pages);
  }

  const api = { build, coverage, collectionOf, day };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.SeenItLog = api;
})(typeof window !== 'undefined' ? window : null);
