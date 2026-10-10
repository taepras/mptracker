// Multi-column sortable table.
//
// <table class="sortable"> with <th data-key="…" [data-type="num"]><button>…</button></th>;
// each <tr> carries data-<key> values. A cell with class "num" in the first column gets the row number.
// Clicking a column makes it the primary sort; previously clicked columns become tie-breakers
// (only the latest is highlighted). Clicking the primary column again flips its direction.
// Empty values always sort last; so does the party-list "province" (แบบบัญชีรายชื่อ).
(function () {
  const PARTY_LIST = 'แบบบัญชีรายชื่อ';
  const MAX_KEYS = 3;
  const collator = new Intl.Collator('th');
  const titles = (window.TITLES || []).map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const TITLE_RE = titles.length ? new RegExp(`^(?:${titles.join('|')})\\s?`) : null;

  function stripTitles(s) {
    if (!TITLE_RE) return s;
    for (let i = 0; i < 3; i++) {
      const m = s.match(TITLE_RE);
      if (!m || s.length - m[0].length < 2) break;
      s = s.slice(m[0].length);
    }
    return s;
  }

  const tail = (key, v) => !v ? 2 : (key === 'province' && v === PARTY_LIST ? 1 : 0);

  window.SortableTable = function (table, { sort = [], filter = null, onRender = null } = {}) {
    const tbody = table.tBodies[0];
    const rows = [...tbody.rows];
    // a sort key sits on a <th> (containing one button) or directly on a <button> (several per <th>)
    const heads = [...table.querySelectorAll('thead [data-key]')];
    const buttonOf = h => h.matches('button') ? h : h.querySelector('button');
    const numeric = new Set(heads.filter(h => h.dataset.type === 'num').map(h => h.dataset.key));
    const sortName = new Map(rows.map(r => [r, stripTitles(r.dataset.name || '')]));
    const keys = new Set(heads.map(h => h.dataset.key));
    let stack = PageState.get('sort', sort).filter(s => keys.has(s.key));

    function cmpKey(a, b, key, dir) {
      const va = a.dataset[key] ?? '', vb = b.dataset[key] ?? '';
      if (numeric.has(key)) return dir * ((+va) - (+vb));
      const ta = tail(key, va), tb = tail(key, vb);
      if (ta !== tb) return ta - tb;
      return dir * (key === 'name' ? collator.compare(sortName.get(a), sortName.get(b)) : collator.compare(va, vb));
    }

    function render() {
      rows.sort((a, b) => {
        for (const { key, dir } of stack) {
          const c = cmpKey(a, b, key, dir);
          if (c) return c;
        }
        return 0;
      });
      tbody.append(...rows);
      let n = 0;
      for (const r of rows) {
        const ok = !filter || filter(r);
        r.hidden = !ok;
        if (ok && r.cells[0].classList.contains('num')) r.cells[0].textContent = ++n;
        else if (ok) n++;
      }
      // only the latest (primary) sort is shown; earlier ones still apply as tie-breakers
      for (const h of heads) {
        const btn = buttonOf(h), isTh = h.matches('th');
        const primary = stack[0] && stack[0].key === h.dataset.key;
        if (primary) {
          if (isTh) h.setAttribute('aria-sort', stack[0].dir > 0 ? 'ascending' : 'descending');
          btn.dataset.arrow = stack[0].dir > 0 ? '↑' : '↓';
          btn.classList.add('active');
        } else {
          if (isTh) h.removeAttribute('aria-sort');
          btn.dataset.arrow = '↕';
          btn.classList.remove('active');
        }
      }
      if (onRender) onRender(n);
    }

    for (const h of heads) buttonOf(h).addEventListener('click', () => {
      const key = h.dataset.key;
      if (stack[0] && stack[0].key === key) stack[0].dir *= -1;
      else {
        stack = stack.filter(s => s.key !== key);
        stack.unshift({ key, dir: numeric.has(key) ? -1 : 1 });  // numbers high→low, text ก→ฮ
        stack = stack.slice(0, MAX_KEYS);
      }
      PageState.set('sort', stack);
      render();
    });

    // Whole-row click: follow the row's main link (a.row-link) unless the click was on another link/button
    // or ended a text selection; ctrl/cmd-click opens a new tab.
    tbody.addEventListener('click', e => {
      if (e.target.closest('a, button, input') || String(getSelection()).length) return;
      const link = e.target.closest('tr')?.querySelector('a.row-link');
      if (!link) return;
      if (e.ctrlKey || e.metaKey) window.open(link.href, '_blank');
      else location.href = link.href;
    });

    render();
    return { render };
  };
})();
