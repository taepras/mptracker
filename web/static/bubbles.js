// Bubble chart: circle area ∝ value. Reads [{label, value, color, fg, url, sub}] from #bubble-data.
(() => {
  const el = document.getElementById('bubbles');
  const data = JSON.parse(document.getElementById('bubble-data').textContent).filter(d => d.value > 0);
  if (!el || !data.length || !window.d3) return;
  const SIZE = 900, fmt = n => n.toLocaleString('th-TH');
  // Entries with a `group` are nested: group circle (party) → bubbles. Others are laid out flat.
  const grouped = data.some(d => d.group);
  const tree = grouped
    ? { children: [...d3.group(data, d => d.group)].map(([, items]) => ({ group: items[0], children: items })) }
    : { children: data };
  const root = d3.pack().size([SIZE, SIZE]).padding(grouped ? 3 : 2)(
    d3.hierarchy(tree).sum(d => d.value).sort((a, b) => b.value - a.value));
  const total = d3.sum(data, d => d.value);

  const svg = d3.select(el).append('svg').attr('viewBox', `0 0 ${SIZE} ${SIZE}`).attr('role', 'img');
  const view = svg.append('g');  // everything that pans/zooms
  if (grouped) {
    const groups = view.selectAll('g.group').data(root.children).join('g').attr('class', 'group')
      .attr('transform', n => `translate(${n.x},${n.y})`);
    groups.classed('link', n => !!n.data.group.group_url)
      .on('click', (_, n) => { if (n.data.group.group_url) location.href = n.data.group.group_url; });
    groups.append('title').text(n => `${n.data.group.group} · ${fmt(n.value)} คำ (${(n.value / total * 100).toFixed(1)}%)`);
    groups.append('circle').attr('r', n => n.r)
      .attr('fill', n => n.data.group.group_color || 'var(--bubble-other)').attr('fill-opacity', 0.12)
      .attr('stroke', n => n.data.group.group_color || 'var(--bubble-other)').attr('stroke-opacity', 0.6);
  }
  const node = view.selectAll('g.leaf').data(root.leaves()).join(d => {
    const g = d.append('g').attr('class', 'leaf').attr('transform', n => `translate(${n.x},${n.y})`);
    g.each(function (n) { if (n.data.url) d3.select(this).classed('link', true); });
    return g;
  }).on('click', (_, n) => { if (n.data.url) location.href = n.data.url; });

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
    view.selectAll('text.group-label').data(root.children.filter(n => n.r > 70)).join('text')
      .attr('class', 'group-label').attr('text-anchor', 'middle').attr('pointer-events', 'none')
      .attr('x', n => n.x).text(n => n.data.group.group);
  }
  const groupLabels = view.selectAll('text.group-label');

  // Zoom/pan. Wheel and drag act on wide screens; on narrow ones the page must stay scrollable, so
  // only pinch, ctrl+wheel and the buttons zoom there.
  const wide = () => matchMedia('(min-width: 900px)').matches;
  const zoom = d3.zoom().scaleExtent([1, 14]).extent([[0, 0], [SIZE, SIZE]]).translateExtent([[0, 0], [SIZE, SIZE]])
    .filter(e => e.type === 'wheel' ? wide() || e.ctrlKey
      : e.type.startsWith('touch') ? wide() || e.touches.length > 1 : !e.button)
    .on('zoom', e => { view.attr('transform', e.transform); update(e.transform.k); });
  svg.call(zoom).on('dblclick.zoom', null);

  function update(k) {
    for (const { fs, t, main, sub } of labels) {
      const px = fs * k;  // on-screen size relative to the 900-unit viewBox
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
})();
