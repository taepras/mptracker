// Per-page UI state (filters, sort, scroll) kept in sessionStorage, restored only when the page is
// reached via back/forward or reload — a fresh visit starts clean.
// Pages call PageState.restoreScroll() once their content is in the DOM; scroll is saved from then on.
(function () {
  const nav = performance.getEntriesByType('navigation')[0];
  const restoring = !!nav && (nav.type === 'back_forward' || nav.type === 'reload');
  const KEY = 'mpt:' + location.pathname;
  let state = {};
  try { state = JSON.parse(sessionStorage.getItem(KEY)) || {}; } catch (e) { /* storage unavailable */ }
  const save = () => { try { sessionStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* ignore */ } };

  let armed = false, queued = false;
  const saveScroll = () => { state.scroll = scrollY; save(); };
  addEventListener('scroll', () => {
    if (!armed || queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; saveScroll(); });
  }, { passive: true });
  addEventListener('pagehide', () => { if (armed) saveScroll(); });

  window.PageState = {
    restoring,
    get: (name, fallback) => (restoring && name in state ? state[name] : fallback),
    set(name, value) { state[name] = value; save(); },
    restoreScroll() {
      if (armed) return;
      history.scrollRestoration = 'manual';
      if (restoring && state.scroll) scrollTo(0, state.scroll);
      armed = true;
    },
  };

  // "← ย้อนกลับ" links (a.back) act like the browser's Back button when the previous page is on this site, so that
  // page returns with its search, filters, sort and scroll (a back navigation is what triggers the restore above).
  // Opened in a new tab or arriving from another site, they simply follow their href to the list page.
  document.addEventListener('click', e => {
    const link = e.target.closest('a.back');
    if (!link || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    let fromThisSite = false;
    try { fromThisSite = new URL(document.referrer).origin === location.origin; } catch (err) { /* no referrer */ }
    if (fromThisSite && history.length > 1) { e.preventDefault(); history.back(); }
  });
})();
