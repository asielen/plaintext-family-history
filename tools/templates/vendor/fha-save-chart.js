/* fha-save-chart.js: "Save this chart" for the family charts on the site.
 *
 * For a relative who wants the family chart on paper or in an email: under a
 * chart there are two buttons, one that saves a picture (PNG) and one that
 * saves a drawing (SVG) that stays sharp at poster size. The saved file is
 * a copy of the chart as the page shows it (a private card stays "Private"),
 * with the links, page data and pictures of people left out. Nothing is
 * fetched, stored or sent; the file is made in the browser and downloaded.
 *
 * Page contract: the chart's root <svg> carries data-fha-chart="{name}" and a
 * <div class="chart-save" data-for="{name}"> sits under it. The buttons look
 * the chart up when clicked, so a chart drawn later (the descendant explorer)
 * works too. The chart's full size comes from data-fha-viewbox (the static
 * charts, set by fha-tree.js's wrapStatic) or data-fha-save-viewbox (the
 * explorer), so a chart zoomed on screen still saves whole.
 *
 * Approach adapted from topola-viewer, Apache License 2.0 (its
 * copyComputedStyles in src/chart/chart_export.ts): copy the computed styles
 * onto the copy, because the saved file cannot carry the site's stylesheet.
 * This code is written fresh for this site. */
(function (global) {
  'use strict';

  var SVGNS = 'http://www.w3.org/2000/svg';
  var XHTMLNS = 'http://www.w3.org/1999/xhtml';
  var FONT = "Georgia, 'Times New Roman', serif";
  var BTN_PNG = 'Save as a picture (PNG)';
  var BTN_SVG = 'Save as a drawing (SVG)';
  var NO_PICS = 'Pictures of people are left out of the saved chart.';
  var FAILED = 'This browser would not make a picture of this chart. ' +
    'Try "Save as a drawing (SVG)", or print the page and choose Save as PDF.';

  // The computed styles that make a chart look the same outside the site.
  // (Reading document.styleSheets is not an option: Chrome blocks it for
  // file:// stylesheets.)
  var STYLE_PROPS = [
    'fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-dasharray', 'stroke-opacity',
    'stroke-linecap', 'opacity', 'visibility', 'font-family', 'font-size', 'font-weight',
    'font-style', 'letter-spacing', 'text-anchor', 'dominant-baseline', 'color',
    'background-color', 'border-top', 'border-right', 'border-bottom', 'border-left',
    'border-radius', 'padding', 'margin', 'display', 'text-decoration', 'text-align',
    'text-overflow', 'line-height', 'white-space', 'overflow', 'box-sizing',
    '-webkit-line-clamp', '-webkit-box-orient', 'flex-direction', 'align-items',
    'justify-content', 'gap'
  ];

  // The picture is made at least twice the size (and about 2400 px on its long side, so a small chart still prints sharp), but never past what a browser
  // canvas can hold (32767 px a side, 268,435,456 px in all).
  function pngScale(w, h) {
    if (!(w > 0 && h > 0)) return 1;
    var want = Math.max(2, 2400 / Math.max(w, h));   // sharp enough to print
    return Math.min(want, 32767 / w, 32767 / h, Math.sqrt(268435456 / (w * h)));
  }

  function viewBoxOf(live) {
    var raw = live.getAttribute('data-fha-viewbox') || live.getAttribute('data-fha-save-viewbox') ||
              live.getAttribute('viewBox') || '';
    var vb = raw.split(/\s+/).map(Number);
    if (vb.length !== 4 || vb.some(function (n) { return !isFinite(n); }) || vb[2] <= 0 || vb[3] <= 0) {
      return null;
    }
    return vb;
  }

  // Copy the computed styles from the live element onto its copy, walking
  // both trees side by side. Whatever the page hides (display: none) is
  // dropped from the copy, so the file shows what the page shows.
  function inlineStyles(live, copy, drop) {
    var cs = global.getComputedStyle(live);
    if (cs.display === 'none') { drop.push(copy); return; }
    var tag = (live.localName || '').toLowerCase();
    if (tag !== 'title' && tag !== 'desc' && tag !== 'defs' && tag !== 'style') {
      for (var i = 0; i < STYLE_PROPS.length; i++) {
        var p = STYLE_PROPS[i];
        var v = cs.getPropertyValue(p);
        if (v) copy.style.setProperty(p, p === 'font-family' ? FONT : v);
      }
      if (live.namespaceURI === XHTMLNS && cs.display !== 'inline') {
        copy.style.setProperty('width', cs.width);
        copy.style.setProperty('height', cs.height);
      }
    }
    var kids = live.children, copies = copy.children;
    for (var k = 0; k < kids.length && k < copies.length; k++) inlineStyles(kids[k], copies[k], drop);
  }

  // Links, pictures and page data stay out of the saved file. A link is
  // unwrapped (its text stays); a picture is removed (a file saved from
  // the site cannot load other files, and a drawn photo would block the PNG).
  function cleanCopy(root) {
    var all = Array.prototype.slice.call(root.querySelectorAll('*'));
    all.unshift(root);
    all.forEach(function (el) {
      Array.prototype.slice.call(el.attributes).forEach(function (a) {
        if (a.name.indexOf('data-') === 0) el.removeAttribute(a.name);
      });
    });
    Array.prototype.slice.call(root.querySelectorAll('img, image')).forEach(function (el) {
      if (el.parentNode) el.parentNode.removeChild(el);
    });
    Array.prototype.slice.call(root.querySelectorAll('a')).forEach(function (el) {
      var parent = el.parentNode;
      if (!parent) return;
      while (el.firstChild) parent.insertBefore(el.firstChild, el);   // unwrap
      parent.removeChild(el);
    });
  }

  function buildCopy(live) {
    var vb = viewBoxOf(live);
    if (!vb) return null;
    var copy = live.cloneNode(true);
    var drop = [];
    inlineStyles(live, copy, drop);
    drop.forEach(function (el) { if (el.parentNode) el.parentNode.removeChild(el); });
    cleanCopy(copy);
    copy.removeAttribute('style');
    copy.setAttribute('xmlns', SVGNS);
    copy.setAttribute('viewBox', vb.join(' '));
    copy.setAttribute('width', String(Math.ceil(vb[2])));
    copy.setAttribute('height', String(Math.ceil(vb[3])));
    var bg = global.getComputedStyle(global.document.body).backgroundColor;
    if (!bg || bg === 'transparent' || /rgba\(.*,\s*0\)$/.test(bg)) bg = '#ffffff';
    var rect = global.document.createElementNS(SVGNS, 'rect');
    rect.setAttribute('x', String(vb[0]));
    rect.setAttribute('y', String(vb[1]));
    rect.setAttribute('width', String(vb[2]));
    rect.setAttribute('height', String(vb[3]));
    rect.setAttribute('fill', bg);
    copy.insertBefore(rect, copy.firstChild);
    return { svg: copy, width: Math.ceil(vb[2]), height: Math.ceil(vb[3]) };
  }

  function svgText(copy) {
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + new global.XMLSerializer().serializeToString(copy);
  }

  function download(blob, filename) {
    var url = global.URL.createObjectURL(blob);
    var a = global.document.createElement('a');
    a.href = url;
    a.download = filename;
    global.document.body.appendChild(a);
    a.click();
    global.document.body.removeChild(a);
    global.setTimeout(function () { global.URL.revokeObjectURL(url); }, 4000);
  }

  function findChart(name) {
    var all = global.document.querySelectorAll('svg[data-fha-chart]');
    for (var i = 0; i < all.length; i++) {
      if (all[i].getAttribute('data-fha-chart') === name) return all[i];
    }
    return null;
  }

  function saveSvg(name, say) {
    var live = findChart(name);
    var built = live && buildCopy(live);
    if (!built) { say(FAILED); return; }
    download(new global.Blob([svgText(built.svg)], { type: 'image/svg+xml' }), name + '.svg');
  }

  function savePng(name, say) {
    var live = findChart(name);
    var built = live && buildCopy(live);
    if (!built) { say(FAILED); return; }
    var scale = pngScale(built.width, built.height);
    var img = new global.Image();
    img.onload = function () {
      try {
        var canvas = global.document.createElement('canvas');
        canvas.width = Math.max(1, Math.floor(built.width * scale));
        canvas.height = Math.max(1, Math.floor(built.height * scale));
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        canvas.toBlob(function (blob) {
          if (!blob) { say(FAILED); return; }
          download(blob, name + '.png');
        }, 'image/png');
      } catch (e) { say(FAILED); }
    };
    img.onerror = function () { say(FAILED); };
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgText(built.svg));
  }

  function attach(holder) {
    var name = holder.getAttribute('data-for');
    if (!name || holder.querySelector('button')) return;
    var msg = global.document.createElement('span');
    msg.className = 'chart-save-msg';
    msg.setAttribute('role', 'status');
    function say(text) { msg.textContent = text; }
    function mk(label, fn) {
      var b = global.document.createElement('button');
      b.type = 'button';
      b.className = 'btn chart-save-btn';
      b.textContent = label;
      b.title = NO_PICS;
      b.onclick = function () {
        say('');
        try { fn(name, say); } catch (e) { say(FAILED); }
      };
      return b;
    }
    holder.appendChild(mk(BTN_PNG, savePng));
    holder.appendChild(mk(BTN_SVG, saveSvg));
    holder.appendChild(msg);
  }

  function init() {
    Array.prototype.forEach.call(global.document.querySelectorAll('.chart-save[data-for]'), attach);
  }

  // svgFor is also how a check can read exactly what would be saved.
  function svgFor(name) {
    var live = findChart(name);
    var built = live && buildCopy(live);
    return built ? svgText(built.svg) : null;
  }

  var api = { attach: attach, pngScale: pngScale, svgFor: svgFor };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (global.document) {
    global.FhaSaveChart = api;
    if (global.document.readyState === 'loading') {
      global.document.addEventListener('DOMContentLoaded', init);
    } else {
      init();
    }
  }
})(typeof window !== 'undefined' ? window : this);
