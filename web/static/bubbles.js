// Bubble chart: circle area ∝ value. Reads [{label, value, color, fg, url, sub}] from #bubble-data.
// Where the chart's box has a height of its own (the flex column on wide screens) the bubbles fill that box;
// elsewhere the chart picks its own shape from its width (portrait on phones, landscape on tablets) and is
// trimmed to the bubbles. It is redrawn whenever that box changes size (and first drawn once visible).
(() => {
  const el = document.getElementById('bubbles');
  const data = JSON.parse(document.getElementById('bubble-data').textContent).filter(d => d.value > 0);
  if (!el || !data.length || !window.d3) return;
  const SHORT = 900, fmt = n => n.toLocaleString('th-TH');
  // Entries with a `group` are nested: group circle (party) → bubbles. Others are laid out flat.
  const grouped = data.some(d => d.group);
  const groupsData = grouped ? [...d3.group(data, d => d.group)].map(([, items]) => items) : data.map(d => [d]);
  const total = d3.sum(data, d => d.value);

  // Layout in units where the box's shorter side is SHORT (as the old square chart was, so label sizes keep their
  // meaning): each group is circle-packed on its own, then the groups are spread over the box by a force layout
  // (pulled to the centre harder along the short side, so the whole takes the box's shape) and scaled to fit it.
  function layout(w, h) {
    const sEst = Math.sqrt(0.6 * w * h / (Math.PI * total));  // rough final scale, to size the gaps
    const units = groupsData.map(items => {
      const root = d3.pack().radius(d => Math.sqrt(d.value)).padding(2 / sEst)(
        d3.hierarchy({ children: items }).sum(d => d.value).sort((a, b) => b.value - a.value));
      const r = grouped ? root.r + 3 / sEst : root.r;
      return { r, item: items[0], value: root.value,
               leaves: root.leaves().map(n => ({ dx: n.x - root.x, dy: n.y - root.y, r: n.r, data: n.data })) };
    }).sort((a, b) => b.value - a.value);
    const aspect = w / h, spread = Math.sqrt(d3.sum(units, u => u.r * u.r));
    units.forEach((u, i) => {  // deterministic start: a spiral stretched to the box's shape
      const t = i * 2.4, rad = spread * Math.sqrt((i + 0.5) / units.length);
      u.x = Math.cos(t) * rad * Math.sqrt(aspect);
      u.y = Math.sin(t) * rad / Math.sqrt(aspect);
    });
    const extent = () => [d3.min(units, u => u.x - u.r), d3.max(units, u => u.x + u.r),
                          d3.min(units, u => u.y - u.r), d3.max(units, u => u.y + u.r)];
    // a few big circles resist the pull's shape, so measure the result and adjust the pull until it matches the box
    let stretch = aspect, x0, x1, y0, y1;
    for (let round = 0; round < 4; round++) {
      const sim = d3.forceSimulation(units)
        .force('x', d3.forceX(0).strength(0.04 / stretch))
        .force('y', d3.forceY(0).strength(0.04 * stretch))
        .force('collide', d3.forceCollide(u => u.r + (grouped ? 4 : 2) / sEst).iterations(4))
        .stop();
      for (let i = 0; i < 300; i++) sim.tick();
      [x0, x1, y0, y1] = extent();
      const got = (x1 - x0) / (y1 - y0);
      if (Math.abs(Math.log(got / aspect)) < 0.05) break;
      stretch = Math.min(16, Math.max(1 / 16, stretch * aspect / got));
    }
    const k = Math.min(w / (x1 - x0), h / (y1 - y0)) * 0.98;
    const ox = w / 2 - k * (x0 + x1) / 2, oy = h / 2 - k * (y0 + y1) / 2;
    const groups = units.map(u => ({ x: ox + k * u.x, y: oy + k * u.y, r: k * u.r, value: u.value, data: { group: u.item } }));
    const leaves = units.flatMap(u => u.leaves.map(n => ({ x: ox + k * (u.x + n.dx), y: oy + k * (u.y + n.dy), r: k * n.r, data: n.data })));
    return { groups, leaves, top: oy + k * y0, bottom: oy + k * y1 };
  }

  let drawn = '';
  function draw() {
    const fill = parseFloat(getComputedStyle(el).flexGrow) > 0;  // the box has a height of its own
    // from the width only when not filling: the box's height follows the drawing (and the viewport's height jitters
    // as phone browsers show/hide their address bar)
    const cw = el.clientWidth, ch = fill ? el.clientHeight : Math.round(cw * (cw <= 600 ? 1.25 : 0.75));
    if (!cw || !ch) return;  // hidden
    const W = Math.round(SHORT * cw / Math.min(cw, ch)), H = Math.round(SHORT * ch / Math.min(cw, ch));
    if (`${W}x${H}` === drawn) return;
    drawn = `${W}x${H}`;
    el.replaceChildren();
    render(W, H, !fill);
  }
  let pending = null;  // a layout takes up to ~0.3 s, so wait for a window resize to settle
  new ResizeObserver(() => { clearTimeout(pending); pending = setTimeout(draw, drawn ? 150 : 0); }).observe(el);

  function render(W, H, trim) {
    const { groups: packedGroups, leaves, top, bottom } = layout(W, H);
    if (trim) {  // drop empty bands above and below the bubbles
      const pad = 6, shift = top - pad;
      for (const n of [...packedGroups, ...leaves]) n.y -= shift;
      H = Math.ceil(bottom - top + 2 * pad);
    }
    const svg = d3.select(el).append('svg').attr('viewBox', `0 0 ${W} ${H}`).attr('role', 'img');
    const view = svg.append('g');  // everything that pans/zooms
    if (grouped) {
      const groups = view.selectAll('g.group').data(packedGroups).join('g').attr('class', 'group')
        .attr('transform', n => `translate(${n.x},${n.y})`);
      groups.classed('link', n => !!n.data.group.group_url)
        .on('click', (_, n) => { if (n.data.group.group_url) location.href = n.data.group.group_url; });
      groups.append('title').text(n => `${n.data.group.group} · ${fmt(n.value)} คำ (${(n.value / total * 100).toFixed(1)}%)`);
      groups.append('circle').attr('r', n => n.r)
        .attr('fill', n => n.data.group.group_color || 'var(--bubble-other)').attr('fill-opacity', 0.12)
        .attr('stroke', n => n.data.group.group_color || 'var(--bubble-other)').attr('stroke-opacity', 0.6);
    }
    const node = view.selectAll('g.leaf').data(leaves).join(d => {
      const g = d.append('g').attr('class', 'leaf').attr('transform', n => `translate(${n.x},${n.y})`);
      g.each(function (n) { if (n.data.url) d3.select(this).classed('link', true); });
      return g;
    }).on('click', (_, n) => {
      // pages can take over a bubble's click: a cancelled 'bubble-click' event (detail = the datum) skips the link
      const go = el.dispatchEvent(new CustomEvent('bubble-click', { detail: n.data, cancelable: true }));
      if (go && n.data.url) location.href = n.data.url;
    });

    node.append('title').text(n => `${n.data.label}${n.data.group ? ' · ' + n.data.group : ''}\n${fmt(n.data.value)} คำ (${(n.data.value / total * 100).toFixed(1)}%)`);
    node.append('circle').attr('r', n => n.r)
      .attr('fill', n => n.data.color || 'var(--bubble-other)').attr('fill-opacity', 0.9);

    // Labels are all created, then shown or hidden by on-screen size (see update), so zooming reveals small ones.
    const labels = [];
    node.each(function (n) {
      const label = n.data.label, fg = n.data.fg || 'var(--text)';
      // Thai glyphs ≈ 0.55em wide; shrink the font to fit the circle.
      const fs = Math.min(n.r / 3, (n.r * 1.7) / (label.length * 0.55));
      const t = d3.select(this).append('text').attr('text-anchor', 'middle').attr('fill', fg)
        .attr('font-size', fs).attr('pointer-events', 'none');
      const main = t.append('tspan').attr('x', 0).text(label);
      // The second line (e.g. "1,325,073 คำ") shrinks to fit the circle instead of being dropped: big circles cap the
      // name at r/3, which used to leave the count a hair too wide at 80% of that size — so the biggest bubbles had none.
      const subFs = n.data.sub ? Math.min(fs * 0.8, (n.r * 1.7) / (n.data.sub.length * 0.55)) : 0;
      const sub = subFs >= fs * 0.5
        ? t.append('tspan').attr('x', 0).attr('dy', '1.25em').attr('font-size', subFs).attr('fill-opacity', 0.8).text(n.data.sub)
        : null;
      labels.push({ fs, t, main, sub });
    });

    // Party names at the top of groups big enough to hold one (small groups have a tooltip only).
    if (grouped) {
      view.selectAll('text.group-label').data(packedGroups.filter(n => n.r > 70)).join('text')
        .attr('class', 'group-label').attr('text-anchor', 'middle').attr('pointer-events', 'none')
        .attr('x', n => n.x).text(n => n.data.group.group);
    }
    const groupLabels = view.selectAll('text.group-label');

    // Zoom/pan. Wheel and drag act on wide screens; on narrow ones the page must stay scrollable, so
    // only pinch, ctrl+wheel and the buttons zoom there.
    const wide = () => matchMedia('(min-width: 900px)').matches;
    const zoom = d3.zoom().scaleExtent([1, 14]).extent([[0, 0], [W, H]]).translateExtent([[0, 0], [W, H]])
      .filter(e => e.type === 'wheel' ? wide() || e.ctrlKey
        : e.type.startsWith('touch') ? wide() || e.touches.length > 1 : !e.button)
      .on('zoom', e => { view.attr('transform', e.transform); update(e.transform.k); });
    svg.call(zoom).on('dblclick.zoom', null);

    function update(k) {
      for (const { fs, t, main, sub } of labels) {
        const px = fs * k;  // size relative to the viewBox, whose short side is SHORT units
        t.attr('display', px >= 7 ? null : 'none');
        main.attr('dy', sub && px > 9 ? '-0.1em' : '0.35em');
        if (sub) sub.attr('display', px > 9 ? null : 'none');
      }
      // keep party names a constant size while the chart scales
      groupLabels.attr('font-size', 12 / k).attr('y', n => n.y - n.r + 14 / k)
        .style('stroke-width', `${3 / k}px`);
    }
    update(1);

    const ctl = d3.select(el).append('div').attr('class', 'zoom-ctl');
    const btn = (text, title, fn) => ctl.append('button').attr('type', 'button').attr('title', title).attr('aria-label', title)
      .text(text).on('click', fn);
    btn('+', 'ซูมเข้า', () => svg.transition().duration(200).call(zoom.scaleBy, 1.8));
    btn('−', 'ซูมออก', () => svg.transition().duration(200).call(zoom.scaleBy, 1 / 1.8));
    btn('⟲', 'รีเซ็ต', () => svg.transition().duration(300).call(zoom.transform, d3.zoomIdentity));
  }
})();
