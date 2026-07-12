/* workbench.js - the real client for `fha serve`.
 *
 * Adapted from the wireframe's assets/workbench.js. The wireframe faked every
 * write in-page; this version talks to the running server:
 *   - every mutation is a two-step POST /api/run: dry_run:true (preview) then
 *     dry_run:false (apply), with the per-process CSRF token in X-FHA-CSRF.
 *   - the preview step renders the SERVER's own messages (the engines' plain
 *     diffs and next-steps) plus the cli_echo (the exact fha command the button
 *     equals - the parity rule made visible).
 *   - after a live apply the page reloads (refresh-on-use: serve invalidated the
 *     snapshot, so the next GET rebuilds it fresh).
 *   - the search box and the in-modal lookups call GET /api/find (debounced).
 *
 * Kept from the wireframe: template cloning, data-wb-fill prefill, the step
 * wizard, showif conditional fields, Escape/overlay close, the copy-echo button,
 * the review re-parent toggle, insert-at-cursor for prose links, the drop zone.
 * Nothing writes without the human clicking Apply on a real dry-run preview.
 */
(function () {
  'use strict';

  var overlay = null;
  var openerBtn = null;

  function csrfToken() {
    var m = document.querySelector('meta[name="fha-csrf"]');
    return m ? m.getAttribute('content') : '';
  }

  function $all(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  /* Insert text at a textarea/input caret (or append if unfocused). */
  function insertAtCursor(el, text) {
    var s = el.selectionStart, e = el.selectionEnd;
    if (typeof s === 'number') {
      el.value = el.value.slice(0, s) + text + el.value.slice(e);
      el.selectionStart = el.selectionEnd = s + text.length;
    } else {
      el.value += text;
    }
    el.focus();
  }

  /* Copy form-control values into any [data-wb-sub="<name>"] placeholder. */
  function substitute(modal) {
    $all('[data-wb-sub]', modal).forEach(function (el) {
      var name = el.getAttribute('data-wb-sub');
      var val = null;
      $all('[name="' + name + '"]', modal).forEach(function (c) {
        if (c.type === 'radio') { if (c.checked) val = c.value; }
        else val = c.value;
      });
      if (val !== null) el.textContent = val;
    });
  }

  function showStep(modal, idx) {
    $all('.wb-step', modal).forEach(function (s, i) { s.hidden = (i !== idx); });
    modal.scrollIntoView({ block: 'nearest' });
  }

  /* Show a field only when another control has a given value:
     data-wb-showif="mtype:Married"  (hidden fields are cleared so they don't submit). */
  function updateShowIf(modal) {
    $all('[data-wb-showif]', modal).forEach(function (el) {
      var spec = el.getAttribute('data-wb-showif').split(':');
      var ctrl = modal.querySelector('[name="' + spec[0] + '"]');
      var show = ctrl && ctrl.value === spec[1];
      el.hidden = !show;
      if (!show) $all('input, textarea', el).forEach(function (i) { i.value = ''; });
    });
  }

  function closeModal() {
    if (overlay) {
      overlay.remove();
      overlay = null;
      openerBtn = null;
      document.body.style.overflow = '';
    }
  }

  function openModal(btn, tplId) {
    var tpl = document.getElementById(tplId || btn.getAttribute('data-wb-open'));
    if (!tpl) return null;
    if (overlay) closeModal();   /* no stacking */
    openerBtn = btn;
    overlay = document.createElement('div');
    overlay.className = 'wb-overlay';
    var modal = document.createElement('div');
    modal.className = 'wb-modal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.appendChild(tpl.content.cloneNode(true));
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    document.body.style.overflow = 'hidden';

    /* Carry the verb/build + any fixed args onto the modal so the two-step
       runner can read them. A template declares its own default verb/build; the
       opener may override the verb and always supplies the fixed args (the
       C-id/P-id/S-id the button acts on). */
    var verb = btn.getAttribute('data-wb-verb') || tpl.getAttribute('data-wb-verb');
    var build = btn.getAttribute('data-wb-build') || tpl.getAttribute('data-wb-build');
    if (verb) modal.setAttribute('data-wb-verb', verb);
    if (build) modal.setAttribute('data-wb-build', build);
    if (btn.getAttribute('data-wb-args')) modal.setAttribute('data-wb-args', btn.getAttribute('data-wb-args'));

    /* opener may inject a filename (drop zone) or a person name (mint "+") */
    var fname = btn.getAttribute('data-wb-file-name');
    if (fname) { var fi = modal.querySelector('[name="filename"]'); if (fi) fi.value = fname; }
    var pname = btn.getAttribute('data-wb-name');
    if (pname) { var ni = modal.querySelector('[name="name"]'); if (ni) ni.value = pname; }

    /* generic field prefill: data-wb-fill="field=value|field2=value2" */
    var fill = btn.getAttribute('data-wb-fill');
    if (fill) {
      fill.split('|').forEach(function (pair) {
        var i = pair.indexOf('=');
        if (i < 0) return;
        var c = modal.querySelector('[name="' + pair.slice(0, i) + '"]');
        if (c) c.value = pair.slice(i + 1);
      });
    }
    substitute(modal);
    updateShowIf(modal);
    showStep(modal, 0);

    /* A "direct" modal (no input form - e.g. Accept as-is, Dispute) jumps
       straight to a real dry-run preview. */
    if (btn.hasAttribute('data-wb-direct')) runPreview(modal);
    else {
      var focusable = modal.querySelector('input, textarea, select, button');
      if (focusable) try { focusable.focus(); } catch (e) {}
    }
    return modal;
  }

  /* --- the two-step /api/run flow ------------------------------------------ */

  /* Gather {verb, args} from a modal. Fixed args (from data-wb-args JSON) win
     over form fields; blank fields are dropped so an optional flag is omitted. */
  function collect(modal) {
    var verb = modal.getAttribute('data-wb-verb');
    var args = {};
    $all('[name]', modal).forEach(function (c) {
      var name = c.getAttribute('name');
      if (!name) return;
      if (c.type === 'radio') { if (c.checked) args[name] = c.value; return; }
      if (c.type === 'checkbox') { args[name] = c.checked; return; }
      var v = c.value;
      if (v !== null && String(v).trim() !== '') args[name] = v;
    });
    var fixed = modal.getAttribute('data-wb-args');
    if (fixed) { try { var f = JSON.parse(fixed); for (var k in f) args[k] = f[k]; } catch (e) {} }
    /* A per-modal builder can rewrite (verb, args) - milestone routing, the
       sex/gender selector, the multi-field name lists. */
    var build = modal.getAttribute('data-wb-build');
    if (build && BUILDERS[build]) {
      var out = BUILDERS[build](args, modal);
      if (out && out.verb) verb = out.verb;
      if (out && out.args) args = out.args;
    }
    return { verb: verb, args: args };
  }

  function apiRun(verb, args, dryRun) {
    return fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-FHA-CSRF': csrfToken() },
      body: JSON.stringify({ verb: verb, args: args, dry_run: dryRun })
    }).then(function (r) { return r.json().then(function (j) { j._http = r.status; return j; }); });
  }

  function renderMessages(result) {
    var out = '';
    (result.messages || []).forEach(function (m) {
      var cls = m.level === 'error' ? 'del' : (m.level === 'warning' ? 'ctx' : 'add');
      out += '<span class="' + cls + '">' + esc(m.text) + '</span>';
      if (m.next_step) out += '<span class="ctx">  next: ' + esc(m.next_step) + '</span>';
    });
    return out || '<span class="ctx">(no changes)</span>';
  }

  function cliBlock(result) {
    var echo = result.cli_echo || '';
    if (!echo) return '';
    return '<p class="wb-cli-label">This button is exactly:</p>' +
      '<div class="wb-cli"><code>' + esc(echo) + '</code>' +
      '<button type="button" class="btn btn-sm" data-wb-copy>copy</button></div>';
  }

  function runPreview(modal) {
    var c = collect(modal);
    if (!c.verb) return;
    if (c.verb === '__unwritable__') {
      var host0 = ensurePreviewStep(modal);
      host0.innerHTML = '<p class="wb-kicker">No provisional slot</p>' +
        '<h3>This one needs a source</h3>' +
        '<pre class="wb-diff"><span class="ctx">An unsourced baptism or burial has no summary slot (owner decision). ' +
        'Record it with a source (it becomes a claim), or leave it as a research note ' +
        'until a record backs it.</span></pre>' +
        '<div class="wb-modal-foot"><button type="button" class="btn" data-wb-back>&larr; Back</button>' +
        '<button type="button" class="btn btn-primary" data-wb-close>Close</button></div>';
      showStepEl(modal, host0);
      return;
    }
    setBusy(modal, true);
    apiRun(c.verb, c.args, true).then(function (result) {
      setBusy(modal, false);
      modal._run = c;
      var host = ensurePreviewStep(modal);
      var ok = result.ok !== false && (result._http === 200);
      host.innerHTML =
        '<p class="wb-kicker">Dry run - nothing written yet</p>' +
        '<h3>' + (ok ? 'Preview' : 'Cannot apply yet') + '</h3>' +
        '<pre class="wb-diff">' + renderMessages(result) + '</pre>' +
        cliBlock(result) +
        '<div class="wb-modal-foot">' +
        '<button type="button" class="btn" data-wb-back>&larr; Back</button>' +
        '<button type="button" class="btn" data-wb-close>Cancel</button>' +
        (ok ? '<button type="button" class="btn btn-primary" data-wb-apply-run>Apply</button>' : '') +
        '</div>';
      showStepEl(modal, host);
    }).catch(function () { setBusy(modal, false); showError(modal, 'Could not reach fha serve - is it still running?'); });
  }

  function runApply(modal) {
    var c = modal._run || collect(modal);
    setBusy(modal, true);
    apiRun(c.verb, c.args, false).then(function (result) {
      setBusy(modal, false);
      var host = ensurePreviewStep(modal);
      var ok = result.ok !== false && (result._http === 200);
      host.innerHTML =
        '<p class="wb-kicker">' + (ok ? 'Applied' : 'Not applied') + '</p>' +
        '<h3>' + (ok ? 'Done' : 'Nothing was written') + '</h3>' +
        '<pre class="wb-diff">' + renderMessages(result) + '</pre>' +
        cliBlock(result) +
        '<div class="wb-modal-foot">' +
        '<button type="button" class="btn btn-primary" data-wb-close>' + (ok ? 'Done' : 'Close') + '</button>' +
        '</div>';
      showStepEl(modal, host);
      if (ok) setTimeout(function () { location.reload(); }, 700);
    }).catch(function () { setBusy(modal, false); showError(modal, 'Could not reach fha serve - is it still running?'); });
  }

  function ensurePreviewStep(modal) {
    var host = modal.querySelector('.wb-step-dynamic');
    if (!host) {
      host = document.createElement('div');
      host.className = 'wb-step wb-step-dynamic';
      modal.appendChild(host);
    }
    return host;
  }
  function showStepEl(modal, el) {
    $all('.wb-step', modal).forEach(function (s) { s.hidden = (s !== el); });
    modal.scrollIntoView({ block: 'nearest' });
  }
  function setBusy(modal, busy) {
    $all('button', modal).forEach(function (b) { b.disabled = busy; });
  }
  function showError(modal, msg) {
    var host = ensurePreviewStep(modal);
    host.innerHTML = '<p class="wb-kicker">Error</p><h3>Something went wrong</h3>' +
      '<pre class="wb-diff"><span class="del">' + esc(msg) + '</span></pre>' +
      '<div class="wb-modal-foot"><button type="button" class="btn btn-primary" data-wb-close>Close</button></div>';
    showStepEl(modal, host);
  }

  /* --- per-modal (verb, args) builders ------------------------------------ */

  /* Map the Unknown/Male/Female/Intersex/Other sex selector to sex/gender
     (owner decision 2026-07-11). Applies to person.new and add-family. */
  function applySexGender(args) {
    var s = args.sex; delete args.sex;
    var other = args.gender_other; delete args.gender_other;
    if (s === 'M' || s === 'Male') args.sex = 'M';
    else if (s === 'F' || s === 'Female') args.sex = 'F';
    else if (s === 'Intersex') args.sex = 'intersex';
    else if (s === 'Other' && other) args.gender = other;
    /* Unknown -> omit both */
    return args;
  }

  var BUILDERS = {
    /* person.new: name + optional sex/gender + provisional birth/death */
    person_new: function (args) {
      applySexGender(args);
      return { verb: 'person.new', args: args };
    },

    /* Milestone routing (contract SS6): the modal chooses the verb from the
       milestone type and whether a source was given.
         Born/Died  + no source -> person.estimate
         Married    + no source -> person.relate --spouse (hypothesis)
         Baptized/Buried + no source -> NOT writable (explained in modal)
         any vital  + source    -> claim.new with that --type, status accepted  */
    milestone: function (args) {
      var mtype = (args.mtype || '').toLowerCase(); delete args.mtype;
      var source = args.msource; delete args.msource;
      var date = args.mdate; delete args.mdate;
      var place = args.mplace; delete args.mplace;
      var spouse = args.mspouse; delete args.mspouse;
      var subject = args.person_id;
      var typeMap = { born: 'birth', died: 'death', married: 'marriage',
                      baptized: 'baptism', buried: 'burial' };
      if (source) {
        var people = [subject];
        if (mtype === 'married' && spouse) people.push(spouse);
        var out = { source_id: source, claim_type: (typeMap[mtype] || mtype),
                    value: (args.mvalue || (typeMap[mtype] || mtype) + ' of ' + (args.subject_name || subject)),
                    status: 'accepted' };
        if (date) out.date = date;
        if (place) { if (/^L-/.test(place)) out.place = place; else out.place_text = place; }
        out.persons = people.join(',');
        return { verb: 'claim.new', args: out };
      }
      if (mtype === 'born' || mtype === 'died') {
        var e = { person_id: subject };
        if (mtype === 'born' && date) e.birth = date;
        if (mtype === 'died' && date) e.death = date;
        return { verb: 'person.estimate', args: e };
      }
      if (mtype === 'married') {
        return { verb: 'person.relate',
                 args: { person_id: subject, relation_type: 'spouse', target_id: spouse } };
      }
      /* Baptized/Buried unsourced: no provisional slot (owner decision). Signal
         a no-op the preview explains. */
      return { verb: '__unwritable__', args: { mtype: mtype } };
    },

    /* add-family: relate the subject to a looked-up person. `target_id` is set
       by the lookup; a bare name with no id lets the engine report a plain miss
       and the modal note points at minting first. */
    add_family: function (args) {
      applySexGender(args);
      return { verb: 'person.relate', args: args };
    }
  };

  /* --- review page: regroup queue items by source / by person -------------- */
  function setReviewView(view) {
    $all('.wb-toggle button[data-wb-view]').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-wb-view') === view);
    });
    $all('.wb-group').forEach(function (g) {
      g.hidden = g.getAttribute('data-wb-groupview') !== view;
    });
    $all('.queue-item[data-group-' + view + ']').forEach(function (item) {
      var slot = document.querySelector(item.getAttribute('data-group-' + view));
      if (slot) slot.appendChild(item);
    });
  }

  /* --- GET /api/find lookups ---------------------------------------------- */
  function debounce(fn, ms) {
    var t = null;
    return function () { var a = arguments, self = this;
      clearTimeout(t); t = setTimeout(function () { fn.apply(self, a); }, ms); };
  }

  function runLookup(input, listEl, opts) {
    var q = input.value.trim();
    if (!q) { listEl.innerHTML = ''; return; }
    var url = '/api/find?q=' + encodeURIComponent(q) + '&limit=8';
    if (opts && opts.kind) url += '&kind=' + encodeURIComponent(opts.kind);
    fetch(url).then(function (r) { return r.json(); }).then(function (j) {
      var rows = (j.results || []).map(function (hit) {
        var wikilink = '[[' + hit.id + '|' + (hit.label || hit.id) + ']]';
        return '<li><button type="button" class="wb-hit"' +
          ' data-wb-ref="' + esc(wikilink) + '"' +
          ' data-wb-ref-plain="' + esc(hit.label || '') + '"' +
          ' data-wb-ref-id="' + esc(hit.id) + '">' +
          '<span class="wb-kind">' + esc(hit.type) + '</span> ' +
          esc(hit.label || hit.id) + ' <span class="wb-mono">' + esc(hit.id) + '</span>' +
          '</button></li>';
      }).join('');
      listEl.innerHTML = rows || '<li><span class="note">no matches - type more, or use "+ create"</span></li>';
    }).catch(function () { listEl.innerHTML = '<li><span class="note">lookup failed</span></li>'; });
  }
  var debouncedLookup = debounce(runLookup, 150);

  /* --- global click handling ---------------------------------------------- */
  document.addEventListener('click', function (e) {
    var t;
    if ((t = e.target.closest('[data-wb-view]'))) { setReviewView(t.getAttribute('data-wb-view')); return; }
    if ((t = e.target.closest('[data-wb-reindex]'))) {
      e.preventDefault();
      apiRun('index.rebuild', {}, false).then(function () { location.reload(); })
        .catch(function () { location.reload(); });
      return;
    }
    if ((t = e.target.closest('[data-wb-open]'))) { e.preventDefault(); openModal(t); return; }
    if ((t = e.target.closest('[data-wb-preview]'))) {
      var m1 = t.closest('.wb-modal'); substitute(m1); runPreview(m1); return;
    }
    if (e.target.closest('[data-wb-apply-run]')) { runApply(e.target.closest('.wb-modal')); return; }
    if (e.target.closest('[data-wb-back]')) { showStep(e.target.closest('.wb-modal'), 0); return; }

    /* inline reference lookup: toggle the panel that lives in the same .wb-field */
    if ((t = e.target.closest('[data-wb-lookuptoggle]'))) {
      e.preventDefault();
      var lfield = t.closest('.wb-field');
      var panel = lfield && lfield.querySelector('.wb-lookup');
      if (panel) { panel.hidden = !panel.hidden;
        if (!panel.hidden) { var qi = panel.querySelector('.wb-lookup-q'); if (qi) qi.focus(); } }
      return;
    }
    /* pick a result: insert the ref (wikilink for prose, plain/id for structured) */
    if ((t = e.target.closest('[data-wb-ref]'))) {
      e.preventDefault();
      var rfield = t.closest('.wb-field') || t.closest('.wb-search');
      var ctrl = rfield && rfield.querySelector('textarea, input.wb-target');
      if (ctrl) {
        var mode = ctrl.getAttribute('data-wb-refmode');   /* 'id' | 'plain' | (wikilink) */
        var ins = mode === 'id' ? t.getAttribute('data-wb-ref-id')
                : mode === 'plain' ? (t.getAttribute('data-wb-ref-plain') || t.getAttribute('data-wb-ref'))
                : t.getAttribute('data-wb-ref');
        if (ctrl.tagName === 'TEXTAREA') insertAtCursor(ctrl, ins);
        else if (ctrl.classList.contains('wb-multi') && ctrl.value.trim())
          ctrl.value = ctrl.value.replace(/\s*,?\s*$/, '') + ',' + ins;
        else ctrl.value = ins;
        /* keep a parallel id field in sync when the visible field shows a name */
        var idTarget = rfield.querySelector('input[data-wb-idfield]');
        if (idTarget) idTarget.value = t.getAttribute('data-wb-ref-id');
      }
      /* a search-bar hit is a navigation, not an insert */
      if (t.closest('.wb-search-results') && t.getAttribute('data-wb-ref-id')) {
        location.href = hitUrl(t.getAttribute('data-wb-ref-id'));
        return;
      }
      var rpanel = t.closest('.wb-lookup'); if (rpanel) rpanel.hidden = true;
      return;
    }
    if (e.target.closest('[data-wb-close]')) { closeModal(); return; }
    if (overlay && e.target === overlay) { closeModal(); return; }
    if ((t = e.target.closest('[data-wb-copy]'))) {
      var code = t.parentElement.querySelector('code');
      if (code && navigator.clipboard) navigator.clipboard.writeText(code.textContent);
      t.textContent = 'copied';
      setTimeout(function () { t.textContent = 'copy'; }, 1200);
      return;
    }
  });

  function hitUrl(id) {
    var pfx = (id || '').charAt(0).toLowerCase();
    var dir = pfx === 'p' ? 'persons' : pfx === 's' ? 'sources' : pfx === 'l' ? 'places' : null;
    if (!dir) return '/index.html';
    return '/' + dir + '/' + id.toLowerCase() + '.html';
  }

  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeModal(); });

  document.addEventListener('change', function (e) {
    var m = e.target.closest && e.target.closest('.wb-modal');
    if (m) updateShowIf(m);
  });

  /* lookup typing (delegated so it works inside cloned modals) */
  document.addEventListener('input', function (e) {
    var q = e.target.closest('.wb-lookup-q');
    if (q) {
      var panel = q.closest('.wb-lookup');
      var list = panel && panel.querySelector('.wb-lookup-results');
      if (list) debouncedLookup(q, list, { kind: q.getAttribute('data-wb-kind') });
      return;
    }
    var sb = e.target.closest('.wb-search input[name="wbq"]');
    if (sb) {
      var form = sb.closest('.wb-search');
      var res = form.querySelector('.wb-search-results');
      var ul = res && res.querySelector('.wb-lookup-results');
      if (ul) { res.hidden = false; debouncedLookup(sb, ul, {}); }
    }
  });

  /* the search box submits to /api/find; block the default GET navigation */
  document.addEventListener('submit', function (e) {
    var form = e.target.closest('form.wb-search');
    if (form) {
      e.preventDefault();
      var sb = form.querySelector('input[name="wbq"]');
      var res = form.querySelector('.wb-search-results');
      var ul = res && res.querySelector('.wb-lookup-results');
      if (sb && ul) { res.hidden = false; runLookup(sb, ul, {}); }
    }
  });

  /* default review view */
  if (document.querySelector('.wb-toggle [data-wb-view]')) setReviewView('source');

  /* --- inbox drop zone: upload real bytes via POST /api/upload ------------- */
  var drop = document.getElementById('wb-drop');
  if (drop) {
    var fileInput = document.getElementById('wb-file');
    var pending = null;
    var openUpload = function (file) {
      pending = file;
      var m = openModal(drop, drop.getAttribute('data-wb-drop-open'));
      if (m) { var fi = m.querySelector('[name="filename"]'); if (fi) fi.value = file ? file.name : ''; }
    };
    drop.addEventListener('click', function () { if (fileInput) fileInput.click(); });
    if (fileInput) fileInput.addEventListener('change', function () {
      if (fileInput.files.length) openUpload(fileInput.files[0]);
      fileInput.value = '';
    });
    ['dragover', 'dragenter'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('wb-over'); });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('wb-over'); });
    });
    drop.addEventListener('drop', function (e) {
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) openUpload(f);
    });

    /* the upload modal's Apply button does a multipart POST, not /api/run */
    document.addEventListener('click', function (e) {
      var b = e.target.closest('[data-wb-upload-apply]');
      if (!b) return;
      var modal = b.closest('.wb-modal');
      if (!pending) { showError(modal, 'No file chosen. Close and drop a file again.'); return; }
      var fd = new FormData();
      fd.append('file', pending, pending.name);
      var what = modal.querySelector('[name="what"]'); if (what && what.value.trim()) fd.append('what', what.value);
      var who = modal.querySelector('[name="who"]'); if (who && who.value.trim()) fd.append('who', who.value);
      setBusy(modal, true);
      fetch('/api/upload', { method: 'POST', headers: { 'X-FHA-CSRF': csrfToken() }, body: fd })
        .then(function (r) { return r.json().then(function (j) { j._http = r.status; return j; }); })
        .then(function (result) {
          setBusy(modal, false);
          var host = ensurePreviewStep(modal);
          var ok = result.ok !== false && result._http === 200;
          host.innerHTML = '<p class="wb-kicker">' + (ok ? 'In the inbox' : 'Not added') + '</p>' +
            '<h3>' + (ok ? 'Added' : 'Upload refused') + '</h3>' +
            '<pre class="wb-diff">' + renderMessages(result) + '</pre>' +
            '<div class="wb-modal-foot"><button type="button" class="btn btn-primary" data-wb-close>Done</button></div>';
          showStepEl(modal, host);
          if (ok) setTimeout(function () { location.reload(); }, 700);
        }).catch(function () { setBusy(modal, false); showError(modal, 'Upload failed - is fha serve still running?'); });
    });
  }

  /* open a file in the OS editor via POST /api/open (buttons with data-wb-open-file) */
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-wb-open-file]');
    if (!b) return;
    e.preventDefault();
    var path = b.getAttribute('data-wb-open-file');
    fetch('/api/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-FHA-CSRF': csrfToken() },
      body: JSON.stringify({ path: path })
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (j.ok === false) alert((j.messages && j.messages[0] && j.messages[0].text) || 'Could not open the file.');
    }).catch(function () { alert('Could not reach fha serve.'); });
  });
})();
