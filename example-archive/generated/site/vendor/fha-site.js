/* fha-site.js: the print helper every site page loads.
 *
 * For someone printing a page: charts folded shut on screen are opened so
 * they reach the paper, and a chart zoomed or dragged on screen prints whole.
 * Both are put back afterwards. The interactive descendant explorer is left
 * alone: it draws itself only when opened on screen, so it is not printed.
 * No network, no storage; it only reads and sets attributes on this page. */
(function () {
  var opened = [];

  window.addEventListener('beforeprint', function () {
    opened = [];
    document.querySelectorAll('details:not([open])').forEach(function (d) {
      if (d.closest('.fha-tree-explorer')) return;
      d.open = true;
      opened.push(d);
    });
    document.querySelectorAll('svg[data-fha-viewbox]').forEach(function (s) {
      s.setAttribute('data-fha-viewbox-screen', s.getAttribute('viewBox') || '');
      s.setAttribute('viewBox', s.getAttribute('data-fha-viewbox'));
    });
  });

  window.addEventListener('afterprint', function () {
    opened.forEach(function (d) { d.open = false; });
    opened = [];
    document.querySelectorAll('svg[data-fha-viewbox-screen]').forEach(function (s) {
      s.setAttribute('viewBox', s.getAttribute('data-fha-viewbox-screen'));
      s.removeAttribute('data-fha-viewbox-screen');
    });
  });
})();
