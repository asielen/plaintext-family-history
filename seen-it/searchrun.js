// searchrun.js - when a Newspapers.com search counts as run again.
//
// Newspapers.com changes pages without reloading, so content.js lives as long
// as the tab. It must not log the same results page twice in one sitting (the
// count settling, a re-render), but the same search run again later - after
// lunch, or the next day in a tab left open - is a new run for the research
// log. A sitting is the add-on's usual 30 minutes.
//
// Classic script (window.SeenItSearch, loaded before content.js) and CommonJS (tests).

(function (root) {
  const SITTING_MS = 30 * 60 * 1000;

  // `last`: what was logged last ({ key, count, at }), or null.
  function isRepeat(last, key, count, now) {
    return !!last && last.key === key && last.count === count && now - last.at < SITTING_MS;
  }

  const api = { isRepeat, SITTING_MS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.SeenItSearch = api;
})(typeof window !== 'undefined' ? window : null);
