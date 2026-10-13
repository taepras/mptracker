// A list of speeches (one MP's, or a whole party's), rendered in the browser from speeches.json so the page also
// works as a static site. Search (?q=), meeting filter (?meeting=) and paging (?page=) are kept in the URL.
//
//   setupSpeeches(root, url, { lazy, speakerUrl, parties, meetingUrl })
//     lazy        don't download the JSON until load() or search() is called (a party's file is several MB)
//     speakerUrl  pid => URL of the speaker's page; when given, each speech starts with its speaker's name
//     parties     {party id: {name, style, logo, url}} (app.party_icons): a small party icon before the speaker's name
//     meetingUrl  date => URL of that day's meeting page; when given, each speech's date links to it
//   returns { search(q), load() } — search() filters the list by q and highlights the matches
(function () {
  const PAGE_SIZE = 20;
  const PREVIEW = 400;
  const MONTHS = ['ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.', 'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.'];

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const thaiDate = iso => { const [y, m, d] = iso.split('-').map(Number); return `${d} ${MONTHS[m - 1]} ${y + 543}`; };
  const mmss = sec => { sec = Math.floor(sec || 0); return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`; };
  const num = n => (n || 0).toLocaleString('en-US');

  // Word boundaries come with each speech as a compact string (`b`, see pipeline/thai_tokenizer.encode_breaks): the
  // gaps between offsets, one character each from DIGITS, or "_<n>_" for gaps of 62 or more. Thai has no spaces, so
  // these are what let a search match whole words: คดี is not found inside โชคดี, วัง not inside หวัง.
  // Decoded once per speech into one flag per text position.
  const DIGITS = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ';
  function boundaryFlags(s) {
    if (s.bf !== undefined) return s.bf;
    if (!s.b) return (s.bf = null);  // no boundary data (an older database): any substring counts
    const flags = new Uint8Array(s.text.length + 1);
    let pos = 0;
    for (let i = 0; i < s.b.length;) {
      let gap;
      if (s.b[i] === '_') { const j = s.b.indexOf('_', i + 1); gap = +s.b.slice(i + 1, j); i = j + 1; }
      else { gap = DIGITS.indexOf(s.b[i]); i++; }
      pos += gap;
      if (pos <= s.text.length) flags[pos] = 1;
    }
    return (s.bf = flags);
  }

  // Start offsets of q (lower-cased) in a speech: whole words only — start and end on word boundaries — unless
  // `partial`. Lower-cased text is cached: a party has thousands of speeches and this runs on every keystroke.
  function findHits(s, q, partial) {
    const lc = (s.lc ??= s.text.toLowerCase());
    const flags = partial ? null : boundaryFlags(s);
    const hits = [];
    for (let i = lc.indexOf(q); i >= 0; i = lc.indexOf(q, i + 1))
      if (!flags || (flags[i] && flags[i + q.length])) hits.push(i);
    return hits;
  }

  // HTML for text[from, to) with the hits (start offsets, all of length `len`) marked.
  function markup(text, hits, len, from = 0, to = text.length) {
    let html = '', at = from;
    for (const h of hits) {
      if (h + len <= at || h >= to) continue;
      const a = Math.max(h, at), b = Math.min(h + len, to);
      html += esc(text.slice(at, a)) + '<mark>' + esc(text.slice(a, b)) + '</mark>';
      at = b;
    }
    return (html + esc(text.slice(at, to))).replace(/\n/g, '<br>');
  }

  // The round party icon (logo, or the party's first letter) as the party_icon macro draws it, in a small size.
  function partyIcon(p) {
    if (!p) return '';
    const inner = p.logo ? `<img src="${esc(p.logo)}" alt="" loading="lazy">` : esc(p.name[0]);
    return `<a class="avatar xs" style="${esc(p.style)}" href="${esc(p.url)}" title="พรรค${esc(p.name)}">${inner}</a>`;
  }

  function speechHtml(s, hits, qlen, opts) {
    const { speakerUrl, parties, meetingUrl } = opts;
    const date = thaiDate(s.date);
    const meta = [
      speakerUrl && s.name
        ? `<span class="speaker">${parties ? partyIcon(parties[s.pt]) : ''}<a href="${esc(speakerUrl(s.pid))}">${
             s.nameHit ? `<mark>${esc(s.name)}</mark>` : esc(s.name)}</a></span>` : '',
      meetingUrl ? `<a class="date" href="${esc(meetingUrl(s.date))}" title="ดูการประชุมวันนี้">${date}</a>` : `<strong>${date}</strong>`,
      s.clock ? `<span>${esc(s.clock.slice(0, 5))} น.</span>` : '',
      `<span class="muted">${esc(s.title)}</span>`,
      `<span class="muted">${num(s.words)} คำ</span>`,
      s.source === 'inferred'
        ? '<span class="badge" title="ระบบระบุผู้พูดจากเครื่องหมายในบันทึก ไม่ใช่ป้ายของเว็บไซต์">ระบุผู้พูดอัตโนมัติ</span>' : '',
      s.label ? `<span class="muted">(${esc(s.label)})</span>` : '',
      `<a class="video" target="_blank" rel="noopener" href="https://asrs.parliament.go.th/video/${s.phase}/${s.meeting}/${s.clip}">▶ คลิป ${s.clip} @ ${mmss(s.start)}</a>`,
    ].join('');
    let body;
    if (s.text.length > PREVIEW) {
      // preview: the start of the speech, or — when searching — a window centred on the first match
      const first = hits.length ? hits[0] : -1;
      const start = first > PREVIEW / 2 ? Math.max(0, first - Math.floor((PREVIEW - qlen) / 2)) : 0;
      const summary = (start ? '…' : '') + markup(s.text, hits, qlen, start, start + PREVIEW);
      body = `<details><summary>${summary}…</summary><div class="full">${markup(s.text, hits, qlen)}` +
             ' <span class="less">ย่อ ▴</span></div></details>';
    } else {
      body = `<p>${markup(s.text, hits, qlen)}</p>`;
    }
    return `<li class="speech"><div class="speech-meta">${meta}</div>${body}</li>`;
  }

  window.setupSpeeches = function (root, url, { lazy = false, speakerUrl = null, parties = null, meetingUrl = null } = {}) {
    const opts = { speakerUrl, parties, meetingUrl };
    const form = root.querySelector('#sp-form'), qInput = root.querySelector('#sp-q'),
          meetingSel = root.querySelector('#sp-meeting'), clear = root.querySelector('#sp-clear'),
          list = root.querySelector('#sp-list'), pager = root.querySelector('#sp-pager'),
          total = root.querySelector('#sp-total');
    const params = new URLSearchParams(location.search);
    const state = { q: params.get('q') || '', meeting: params.get('meeting') || '', page: +params.get('page') || 1 };
    let all = [], started = false, loaded = false;

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
      const inMeeting = all.filter(s => !state.meeting || s.date === state.meeting);
      let rows = inMeeting, partial = false;
      if (q) {
        // a speech matches when its speaker's name contains q, or its text does: whole words first; if q is not a
        // whole word anywhere in the text (e.g. part of a word), fall back to any match
        const byName = s => (s.nameHit = !!s.name && s.name.toLowerCase().includes(q));
        rows = inMeeting.filter(s => byName(s) | (s.hits = findHits(s, q, false)).length > 0);
        if (!rows.some(s => s.hits.length)) {
          const partialRows = inMeeting.filter(s => s.nameHit | (s.hits = findHits(s, q, true)).length > 0);
          partial = partialRows.length > rows.length;
          rows = partialRows;
        }
      } else {
        inMeeting.forEach(s => { s.nameHit = false; });
      }
      const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
      state.page = Math.min(Math.max(1, state.page), pages);
      const slice = rows.slice((state.page - 1) * PAGE_SIZE, state.page * PAGE_SIZE);
      // filtered: matches out of everything listed, e.g. (12/340)
      total.textContent = q || state.meeting ? `(${num(rows.length)}/${num(all.length)})` : `(${num(all.length)})`;
      const note = partial && rows.length
        ? `<li class="muted small search-note">ไม่พบ “${esc(state.q.trim())}” เป็นคำเต็ม — แสดงผลที่มีข้อความนี้อยู่ภายในคำอื่น</li>` : '';
      list.innerHTML = slice.length ? note + slice.map(s => speechHtml(s, q ? s.hits : [], q.length, opts)).join('')
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
    // The pager links point at #speeches; handle them here so they don't add history entries (the "← ย้อนกลับ" link
    // and the browser's Back button should leave the page, not undo a jump within it). ล้าง is a button.
    clear.addEventListener('click', e => {
      e.preventDefault(); state.q = ''; state.meeting = ''; state.page = 1; render(); root.scrollIntoView();
    });
    // An expanded speech collapses again when its text is clicked (but not when the click ends a text selection,
    // or lands on a link). If its top has scrolled out of view, bring it back so the reader keeps their place.
    list.addEventListener('click', e => {
      const full = e.target.closest('.full');
      if (!full || e.target.closest('a') || (getSelection().toString() && !e.target.closest('.less'))) return;
      const details = full.closest('details');
      details.open = false;
      const speech = details.closest('.speech');
      if (speech.getBoundingClientRect().top < 0) speech.scrollIntoView();
    });
    pager.addEventListener('click', e => {
      const go = e.target.closest('[data-go]');
      if (go) { e.preventDefault(); state.page += +go.dataset.go; render(); root.scrollIntoView(); }
    });

    function load() {
      if (started) return;
      started = true;
      fetch(url).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }).then(data => {
        all = data;
        const counts = new Map();
        for (const s of all) counts.set(s.date, (counts.get(s.date) || 0) + 1);
        meetingSel.insertAdjacentHTML('beforeend', [...counts].map(([d, n]) =>
          `<option value="${d}">${thaiDate(d)} (${n})</option>`).join(''));
        loaded = true;
        render();
        PageState.restoreScroll();
        if (!PageState.restoring && location.hash === '#speeches' && (state.q || state.meeting)) root.scrollIntoView();
      }).catch(() => {
        started = false;  // allow another try the next time the list is opened
        list.innerHTML = '<li class="muted">โหลดคำอภิปรายไม่สำเร็จ ลองใหม่อีกครั้ง</li>';
      });
    }
    if (!lazy) load();

    return {
      load,
      search(q) {
        state.q = q; state.meeting = ''; state.page = 1;
        if (loaded) render(); else load();  // not loaded yet: the first render picks the new search up
        root.scrollIntoView({ behavior: 'smooth' });
      },
    };
  };
})();
