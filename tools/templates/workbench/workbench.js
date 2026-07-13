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
  /* Set instead of an immediate reload when a successful result still carries
     a warning (e.g. the asset saved but its note sidecar did not) - reloading
     right away would erase the warning before the human can read or copy it.
     closeModal() honors this the moment they dismiss the modal themselves. */
  var reloadOnClose = false;

  function csrfToken() {
    var m = document.querySelector('meta[name="fha-csrf"]');
    return m ? m.getAttribute('content') : '';
  }

  /* Which vitals get a provisional (unsourced) slot - read once at startup
     from the <meta name="fha-provisional"> tag site.py renders from
     _lib.PROVISIONAL_VITAL_FIELDS, so this list cannot drift from the
     server's copy. Falls back to the historical birth/death pair when the
     meta tag is missing (a non-workbench page, or an older build) so the
     milestone router still degrades safely. */
  var PROVISIONAL_FIELDS = (function () {
    var m = document.querySelector('meta[name="fha-provisional"]');
    var content = m ? m.getAttribute('content') : '';
    var fields = (content || '').trim().split(/\s+/).filter(function (s) { return s; });
    return fields.length ? fields : ['birth', 'death'];
  })();

  function $all(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  function esc(s) {
    // Every call site splices this into HTML - some (the lookup-result
    // buttons) into double-quoted attributes via innerHTML. Without
    // escaping '"' too, a label carrying one (`John "Jack" Smith`) closes
    // the attribute early and lets the rest of the label - or a crafted
    // archive label - inject new attributes into the element.
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
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

  /* Show one `.wb-step` and hide the rest. `target` is either a zero-based
     wizard-step index (the static form steps) or the step element itself
     (the server-rendered preview/apply/error result, built on the fly by
     ensurePreviewStep - it has no fixed index to name). */
  function showStep(modal, target) {
    $all('.wb-step', modal).forEach(function (s, i) {
      s.hidden = (typeof target === 'number') ? (i !== target) : (s !== target);
    });
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
    if (reloadOnClose) {
      reloadOnClose = false;
      location.reload();
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

    /* generic field prefill: data-wb-fill="field=value|field2=value2".
       A radio GROUP shares one `name` across several inputs - querySelector
       would grab only the first of them and stomp its OWN value attribute
       (silently turning "deceased" into whatever value was being prefilled,
       still unchecked) rather than checking the one that actually matches.
       querySelectorAll + per-element handling covers both shapes; every
       existing single-target usage (a <select> or plain <input>) still has
       exactly one match, so this is a no-op behavior change for those. */
    var fill = btn.getAttribute('data-wb-fill');
    if (fill) {
      fill.split('|').forEach(function (pair) {
        var i = pair.indexOf('=');
        if (i < 0) return;
        var val = pair.slice(i + 1);
        $all('[name="' + pair.slice(0, i) + '"]', modal).forEach(function (c) {
          if (c.type === 'radio') c.checked = (c.value === val);
          else c.value = val;
        });
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

  /* Gather {verb, args} from a modal. Fixed args (from data-wb-args JSON) seed
     args FIRST; named form fields are read after and OVERRIDE them when the
     field has a non-blank value, so a form control that shares a fixed arg's
     name (e.g. relation_type on tpl-add-family, prefilled by the opener but
     still a live <select> the human can change) reflects what is actually on
     screen. A blank field never overrides a fixed arg - it is simply dropped,
     same as before - so person_id/claim_id/source_id/path style fixed args
     (which have no same-named form control) are unaffected either way. */
  function collect(modal) {
    var verb = modal.getAttribute('data-wb-verb');
    var args = {};
    var fixed = modal.getAttribute('data-wb-args');
    if (fixed) { try { var f = JSON.parse(fixed); for (var k in f) args[k] = f[k]; } catch (e) {} }
    $all('[name]', modal).forEach(function (c) {
      var name = c.getAttribute('name');
      if (!name) return;
      if (c.type === 'radio') { if (c.checked) args[name] = c.value; return; }
      if (c.type === 'checkbox') { args[name] = c.checked; return; }
      var v = c.value;
      if (v !== null && String(v).trim() !== '') args[name] = v;
    });
    /* A hidden `data-wb-idfield="otherName"` control (set by the lookup
       click handler below when a result is picked by id) names the
       plain-text field it supersedes: when both are non-blank, drop the
       plain-text one so only the resolved id travels (e.g. a claim's
       `place` L-id instead of a `place_text` wikilink - submitting both
       is a refused mutually-exclusive pair server-side). A manually typed
       plain-text field with no lookup pick is unaffected: the idfield
       stays blank and collect() already dropped it above. */
    $all('[data-wb-idfield]', modal).forEach(function (idEl) {
      var idName = idEl.getAttribute('name');
      var pairName = idEl.getAttribute('data-wb-idfield');
      if (idName && pairName && args[idName] !== undefined) delete args[pairName];
    });
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

  /* A non-2xx JSON payload (the normal "engine refused this" shape) still
     flows through as a resolved result - callers already check result.ok /
     result._http. But a non-2xx response that is NOT JSON (a CSRF or Host-
     header check failing before the request ever reaches the engine, which
     answers with a plain-text or HTML refusal) used to hit r.json()'s parse
     failure and get swallowed by the generic "Could not reach fha serve"
     catch-all - hiding the one message ("reload the page you opened") that
     would actually get the human unstuck. Read that body as text instead and
     reject with it tagged, so the .catch handlers below can tell a real
     network failure from a readable server refusal and show the right one.
     Shared by apiRun (JSON body) and the upload handler (multipart body) -
     both hit the same CSRF/Host gate ahead of their own handler, which
     answers with this same plain-text shape either way. */
  function fetchJsonOrRefusal(url, opts) {
    return fetch(url, opts).then(function (r) {
      var ctype = r.headers.get('content-type') || '';
      if (!r.ok && ctype.indexOf('application/json') === -1) {
        return r.text().then(function (text) {
          var err = new Error(text.trim() || ('The server refused this request (status ' + r.status + ').'));
          err.wbServerText = true;
          throw err;
        });
      }
      return r.json().then(function (j) { j._http = r.status; return j; });
    });
  }

  function apiRun(verb, args, dryRun) {
    return fetchJsonOrRefusal('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-FHA-CSRF': csrfToken() },
      body: JSON.stringify({ verb: verb, args: args, dry_run: dryRun })
    });
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

  /* The shared body of the two-step /api/run flow. `dryRun` picks which of
     the two calls this is and, since the two calls differ only in wording
     and which buttons/follow-up appear (never in the request/response
     handling), also picks the right copy and footer for the result:
       preview (dryRun=true)  - collects fresh args every time, refuses a
         blank or '__unwritable__' verb before ever calling the server, and
         stashes the collected {verb, args} on the modal (`modal._run`) so
         the apply step reuses exactly what was previewed rather than
         re-reading the form (which may have changed under the human's
         cursor while the request was in flight).
       apply (dryRun=false)   - reuses `modal._run` when present (falling
         back to a fresh collect only if apply is somehow invoked without a
         prior preview), and reloads the page on success so the rebuilt
         snapshot (serve invalidates it after any write) is what the human
         sees next. */
  function runVerb(modal, dryRun) {
    var c = dryRun ? collect(modal) : (modal._run || collect(modal));
    if (dryRun) {
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
        showStep(modal, host0);
        return;
      }
    }
    setBusy(modal, true);
    apiRun(c.verb, c.args, dryRun).then(function (result) {
      setBusy(modal, false);
      if (dryRun) {
        modal._run = c;
        /* A minting verb's dry run draws a REAL id (mint_ids picks randomly
           on every call, by design - see person.run_new/claim.run_claim_new)
           and shows it in the preview diff. Thread that id back into the
           args Apply will send, so the record Apply actually creates is the
           SAME one the human just approved, not a second independently-
           minted id (P2 codex finding, round 5, PR #30). */
        if (result.ok && result.data) {
          if (c.verb === 'person.new' && result.data.person_id) {
            modal._run.args.person_id = result.data.person_id;
          } else if (c.verb === 'claim.new' && result.data.claim_id) {
            modal._run.args.claim_id = result.data.claim_id;
          }
        }
      }
      var host = ensurePreviewStep(modal);
      var ok = result.ok !== false && (result._http === 200);
      host.innerHTML =
        '<p class="wb-kicker">' + (dryRun ? 'Dry run - nothing written yet' : (ok ? 'Applied' : 'Not applied')) + '</p>' +
        '<h3>' + (dryRun ? (ok ? 'Preview' : 'Cannot apply yet') : (ok ? 'Done' : 'Nothing was written')) + '</h3>' +
        '<pre class="wb-diff">' + renderMessages(result) + '</pre>' +
        cliBlock(result) +
        '<div class="wb-modal-foot">' +
        (dryRun
          ? ('<button type="button" class="btn" data-wb-back>&larr; Back</button>' +
             '<button type="button" class="btn" data-wb-close>Cancel</button>' +
             (ok ? '<button type="button" class="btn btn-primary" data-wb-apply-run>Apply</button>' : ''))
          : ('<button type="button" class="btn btn-primary" data-wb-close>' + (ok ? 'Done' : 'Close') + '</button>')) +
        '</div>';
      showStep(modal, host);
      if (!dryRun && ok) {
        var hasWarning = (result.messages || []).some(function (m) { return m.level === 'warning'; });
        if (hasWarning) reloadOnClose = true;
        else setTimeout(function () { location.reload(); }, 700);
      }
    }).catch(function (e) {
      setBusy(modal, false);
      showError(modal, (e && e.wbServerText && e.message) || 'Could not reach fha serve - is it still running?');
    });
  }

  function runPreview(modal) { runVerb(modal, true); }
  function runApply(modal) { runVerb(modal, false); }

  function ensurePreviewStep(modal) {
    var host = modal.querySelector('.wb-step-dynamic');
    if (!host) {
      host = document.createElement('div');
      host.className = 'wb-step wb-step-dynamic';
      modal.appendChild(host);
    }
    return host;
  }
  function setBusy(modal, busy) {
    $all('button', modal).forEach(function (b) { b.disabled = busy; });
  }
  function showError(modal, msg) {
    var host = ensurePreviewStep(modal);
    host.innerHTML = '<p class="wb-kicker">Error</p><h3>Something went wrong</h3>' +
      '<pre class="wb-diff"><span class="del">' + esc(msg) + '</span></pre>' +
      '<div class="wb-modal-foot"><button type="button" class="btn btn-primary" data-wb-close>Close</button></div>';
    showStep(modal, host);
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
      var pastedId = args.msource_id; delete args.msource_id;
      if (source === '__paste__') source = (pastedId || '').trim();
      var date = args.mdate; delete args.mdate;
      var place = args.mplace; delete args.mplace;
      var placeId = args.mplace_id; delete args.mplace_id;
      var spouse = args.mspouse; delete args.mspouse;
      var subject = args.person_id;
      var typeMap = { born: 'birth', died: 'death', married: 'marriage',
                      baptized: 'baptism', buried: 'burial' };
      var claimType = typeMap[mtype] || mtype;
      if (source) {
        var people = [subject];
        if (mtype === 'married' && spouse) people.push(spouse);
        var out = { source_id: source, claim_type: claimType,
                    value: (args.mvalue || claimType + ' of ' + (args.subject_name || subject)),
                    status: 'accepted' };
        if (date) out.date = date;
        /* placeId is set only when the lookup resolved a place (collect()
           drops the paired mplace text arg in that case - see the
           data-wb-idfield handling there); a plain-typed L-id (any case,
           no lookup used) is accepted too, since a human copy-pasting one
           from elsewhere types it in whatever case they found it. Anything
           else is prose - "the old farmhouse" - never a wikilink: the
           lookup no longer inserts one into this field. */
        if (placeId) out.place = placeId;
        else if (place) { if (/^l-/i.test(place)) out.place = place; else out.place_text = place; }
        out.persons = people.join(',');
        return { verb: 'claim.new', args: out };
      }
      /* Provisional (unsourced) slot: driven by PROVISIONAL_FIELDS (the meta
         tag mirroring _lib.PROVISIONAL_VITAL_FIELDS), not a hardcoded
         born/died literal - a policy change on the server side (e.g. adding
         a provisional field some day) needs no matching edit here. */
      if (PROVISIONAL_FIELDS.indexOf(claimType) !== -1) {
        var e = { person_id: subject };
        if (date) e[claimType] = date;
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
      apiRun('index.rebuild', {}, false).then(function (result) {
        var ok = result.ok !== false && result._http === 200;
        if (ok) {
          location.reload();
        } else {
          alert((result.messages && result.messages[0] && result.messages[0].text) || 'Could not rebuild the index.');
        }
      }).catch(function (e2) {
        alert((e2 && e2.wbServerText && e2.message) || 'Could not reach fha serve - is it still running?');
      });
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
    /* A genuine user edit to a lookup-backed field invalidates whatever id a
       PRIOR pick resolved to - clear the paired hidden idfield so collect()
       doesn't override the human's just-typed text with a stale selection.
       Only fires on real typing/paste: setting `.value =` from the pick
       handler itself does not dispatch a native `input` event, so this
       never fights the pick that just happened. */
    if (e.target.classList && e.target.classList.contains('wb-target')) {
      var wbField = e.target.closest('.wb-field');
      var idEl = wbField && wbField.querySelector('input[data-wb-idfield]');
      if (idEl && idEl.value) idEl.value = '';
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
      fetchJsonOrRefusal('/api/upload', { method: 'POST', headers: { 'X-FHA-CSRF': csrfToken() }, body: fd })
        .then(function (result) {
          setBusy(modal, false);
          var host = ensurePreviewStep(modal);
          var ok = result.ok !== false && result._http === 200;
          host.innerHTML = '<p class="wb-kicker">' + (ok ? 'In the inbox' : 'Not added') + '</p>' +
            '<h3>' + (ok ? 'Added' : 'Upload refused') + '</h3>' +
            '<pre class="wb-diff">' + renderMessages(result) + '</pre>' +
            '<div class="wb-modal-foot"><button type="button" class="btn btn-primary" data-wb-close>Done</button></div>';
          showStep(modal, host);
          if (ok) {
            var hasWarning = (result.messages || []).some(function (m) { return m.level === 'warning'; });
            if (hasWarning) reloadOnClose = true;
            else setTimeout(function () { location.reload(); }, 700);
          }
        }).catch(function (e) {
          setBusy(modal, false);
          showError(modal, (e && e.wbServerText && e.message) || 'Upload failed - is fha serve still running?');
        });
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
