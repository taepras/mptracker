// An MP's speeches, rendered in the browser from speeches.json so the page also works as a static site.
// Search (?q=), meeting filter (?meeting=) and paging (?page=) are kept in the URL.
(function () {
  const PAGE_SIZE = 20;
  const PREVIEW = 400;
  const MONTHS = ['ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.', 'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.'];

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const thaiDate = iso => { const [y, m, d] = iso.split('-').map(Number); return `${d} ${MONTHS[m - 1]} ${y + 543}`; };
  const mmss = sec => { sec = Math.floor(sec || 0); return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`; };
  const num = n => (n || 0).toLocaleString('en-US');

  function highlight(text, q) {
    let out = esc(text).replace(/\n/g, '<br>');
    if (q) {
      const pat = esc(q).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      out = out.replace(new RegExp(pat, 'gi'), m => `<mark>${m}</mark>`);
    }
    return out;
  }

  // Preview window of a long text: starts at the beginning, or — when searching — is centred on the first
  // match, with the skipped lead-in replaced by "…".
  function preview(text, q) {
    const at = q ? text.toLowerCase().indexOf(q.toLowerCase()) : -1;
    const start = at > PREVIEW / 2 ? Math.max(0, at - Math.floor((PREVIEW - q.length) / 2)) : 0;
    return (start ? '…' : '') + text.slice(start, start + PREVIEW);
  }

  function speechHtml(s, q) {
    const meta = [
      `<strong>${thaiDate(s.date)}</strong>`,
      s.clock ? `<span>${esc(s.clock.slice(0, 5))} น.</span>` : '',
      `<span class="muted">${esc(s.title)}</span>`,
      `<span class="muted">${num(s.words)} คำ</span>`,
      s.source === 'inferred'
        ? '<span class="badge" title="ระบบระบุผู้พูดจากเครื่องหมายในบันทึก ไม่ใช่ป้ายของเว็บไซต์">ระบุผู้พูดอัตโนมัติ</span>' : '',
      s.label ? `<span class="muted">(${esc(s.label)})</span>` : '',
      `<a class="video" target="_blank" rel="noopener" href="https://asrs.parliament.go.th/video/${s.phase}/${s.meeting}/${s.clip}">▶ คลิป ${s.clip} @ ${mmss(s.start)}</a>`,
    ].join('');
    const body = s.text.length > PREVIEW
      ? `<details><summary>${highlight(preview(s.text, q), q)}…</summary><div class="full">${highlight(s.text, q)}</div></details>`
      : `<p>${highlight(s.text, q)}</p>`;
    return `<li class="speech"><div class="speech-meta">${meta}</div>${body}</li>`;
  }

  window.setupSpeeches = function (root, url) {
    const form = root.querySelector('#sp-form'), qInput = root.querySelector('#sp-q'),
          meetingSel = root.querySelector('#sp-meeting'), clear = root.querySelector('#sp-clear'),
          list = root.querySelector('#sp-list'), pager = root.querySelector('#sp-pager'),
          total = root.querySelector('#sp-total');
    const params = new URLSearchParams(location.search);
    const state = { q: params.get('q') || '', meeting: params.get('meeting') || '', page: +params.get('page') || 1 };
    let all = [];

    function syncUrl() {
      const p = new URLSearchParams();
      if (state.q) p.set('q', state.q);
      if (state.meeting) p.set('meeting', state.meeting);
      if (state.page > 1) p.set('page', state.page);
      const qs = p.toString();
      history.replaceState(null, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
    }

    function render() {
      const q = state.q.trim().toLowerCase();
      const rows = all.filter(s => (!state.meeting || s.date === state.meeting) &&
                                   (!q || s.text.toLowerCase().includes(q)));
      const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
      state.page = Math.min(Math.max(1, state.page), pages);
      const slice = rows.slice((state.page - 1) * PAGE_SIZE, state.page * PAGE_SIZE);
      total.textContent = `(${num(rows.length)})`;
      list.innerHTML = slice.length ? slice.map(s => speechHtml(s, state.q.trim())).join('')
                                    : '<li class="muted">ไม่พบการอภิปราย</li>';
      pager.hidden = pages <= 1;
      pager.innerHTML = (state.page > 1 ? '<a href="#speeches" data-go="-1">← ก่อนหน้า</a>' : '') +
        `<span class="muted">หน้า ${state.page} / ${pages}</span>` +
        (state.page < pages ? '<a href="#speeches" data-go="1">ถัดไป →</a>' : '');
      clear.hidden = !state.q && !state.meeting;
      if (qInput.value.trim() !== state.q.trim()) qInput.value = state.q;  // don't clobber text being typed
      meetingSel.value = state.meeting;
      syncUrl();
    }

    // Live search: re-filter shortly after typing stops (Enter just runs it immediately).
    let timer;
    const run = () => { clearTimeout(timer); state.q = qInput.value; state.page = 1; render(); };
    qInput.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 200); });
    form.addEventListener('submit', e => { e.preventDefault(); run(); });
    meetingSel.addEventListener('change', () => { state.meeting = meetingSel.value; state.page = 1; render(); });
    // These are #speeches links; handle them here so they don't add history entries (the "← ย้อนกลับ" link and the
    // browser's Back button should leave the page, not undo a jump within it).
    clear.addEventListener('click', e => {
      e.preventDefault(); state.q = ''; state.meeting = ''; state.page = 1; render(); root.scrollIntoView();
    });
    pager.addEventListener('click', e => {
      const go = e.target.closest('[data-go]');
      if (go) { e.preventDefault(); state.page += +go.dataset.go; render(); root.scrollIntoView(); }
    });

    fetch(url).then(r => r.json()).then(data => {
      all = data;
      const counts = new Map();
      for (const s of all) counts.set(s.date, (counts.get(s.date) || 0) + 1);
      meetingSel.insertAdjacentHTML('beforeend', [...counts].map(([d, n]) =>
        `<option value="${d}">${thaiDate(d)} (${n})</option>`).join(''));
      render();
      PageState.restoreScroll();
      if (!PageState.restoring && location.hash === '#speeches' && (state.q || state.meeting)) root.scrollIntoView();
    });

    return {
      search(q) { state.q = q; state.meeting = ''; state.page = 1; render(); root.scrollIntoView({ behavior: 'smooth' }); },
    };
  };
})();
