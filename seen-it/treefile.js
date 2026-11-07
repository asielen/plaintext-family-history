// treefile.js - read a family-tree file (GEDCOM) for the records and
// memorials attached to each person, so search results can say "in your tree".
//
// Ancestry's tree export cites each attached record as
// "_APID 1,{dbId}::{recordId}" under the person's sources; a Find a Grave
// memorial shows up as a findagrave.com/memorial/{id} link anywhere in the
// person's lines. Nothing else in the file is kept - not dates, places or
// notes - only each person's name and those keys.
//
// Classic script (window.SeenItTree, loaded by popup.html) and CommonJS (tests).

(function (root) {
  function parseGedcom(text) {
    const people = [];
    let person = null;
    for (const raw of String(text || '').split(/\r?\n/)) {
      const m = /^\s*(\d+)\s+(?:@([^@]+)@\s+)?(\S+)\s*(.*)$/.exec(raw);
      if (!m) continue;
      const level = Number(m[1]);
      const tag = m[3];
      const value = (m[4] || '').trim();
      if (level === 0) {
        person = tag === 'INDI' && m[2] ? { id: m[2], name: null, keys: [] } : null;
        if (person) people.push(person);
        continue;
      }
      if (!person) continue;
      if (level === 1 && tag === 'NAME' && !person.name) {
        person.name = value.replace(/\//g, '').replace(/\s+/g, ' ').trim() || null;
      } else if (/findagrave\.com\/memorial\/\d+/i.test(value)) {
        // a Find a Grave memorial cited or linked for this person
        const g = /findagrave\.com\/memorial\/(\d+)/i.exec(value);
        if (!person.keys.includes('fag:' + g[1])) person.keys.push('fag:' + g[1]);
      } else if (tag === '_APID') {
        const a = /^\d+,(\d+)::(\d+)/.exec(value);
        if (a && !person.keys.includes('rec:' + a[1] + ':' + a[2])) person.keys.push('rec:' + a[1] + ':' + a[2]);
      }
    }
    return people.filter((p) => p.keys.length);
  }

  const api = { parseGedcom };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.SeenItTree = api;
})(typeof window !== 'undefined' ? window : null);
