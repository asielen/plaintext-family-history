// background.js - the service worker that opens the capture panel.
//
// The browser companion (TOOLING_INGESTION §5) is a *dumb, replaceable front-end*:
// it stages raw material into Downloads and the durable Python tooling
// (`fha capture --ingest`) decides what that material means. This worker does the
// one thing a content script and side panel cannot do for themselves - wire the
// toolbar button to the side panel - and otherwise stays out of the way. All the
// real work (reading the page, fetching the asset, writing the bundle) happens in
// the content script and the panel; there is deliberately no ambient page access
// and no background scraping here (§2.4, §7).

// Open the side panel when the human clicks the toolbar button (Phase 1, §5.3).
// setPanelBehavior is the modern one-call wiring; the onClicked fallback below
// covers a Chromium build where that call is unavailable, so the button always
// does something rather than silently failing.
chrome.runtime.onInstalled.addListener(() => {
  if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
    chrome.sidePanel
      .setPanelBehavior({ openPanelOnActionClick: true })
      .catch((err) => console.warn('fha-capture: setPanelBehavior failed', err));
  }
});

chrome.action.onClicked.addListener(async (tab) => {
  // onClicked only fires when openPanelOnActionClick did NOT already handle the
  // click (older builds, or if the call above failed), so opening here is safe
  // and never double-opens.
  if (!chrome.sidePanel || !chrome.sidePanel.open) return;
  try {
    if (tab && tab.windowId != null) {
      await chrome.sidePanel.open({ windowId: tab.windowId });
    }
  } catch (err) {
    console.warn('fha-capture: sidePanel.open failed', err);
  }
});
