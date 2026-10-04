// Word cloud: setupCloud(container, urlTemplate, {controls, onClick}).
// urlTemplate contains MODE (replaced by distinctive|frequent) and returns [[word, weight, count], …].
// Mode buttons: any .seg button[data-mode] inside `controls` switch ?mode=… and redraw.
(function () {
  const css = getComputedStyle(document.documentElement);
  const palette = ['--c1', '--c2', '--c3', '--c4', '--c5'].map(v => css.getPropertyValue(v).trim());
  const colorFor = word => palette[[...word].reduce((a, c) => a + c.codePointAt(0), 0) % palette.length];

  window.setupCloud = function (el, urlTemplate, { controls = null, onClick = null } = {}) {
    let mode = 'distinctive';

    async function draw() {
      const words = await (await fetch(urlTemplate.replace('MODE', mode))).json();
      el.innerHTML = '';
      if (!words.length) { el.innerHTML = '<p class="muted">ข้อมูลไม่พอ</p>'; return; }
      const max = words[0][1], min = words[words.length - 1][1];
      const w = el.clientWidth, h = el.clientHeight;
      const scale = v => 14 + (Math.sqrt(v - min) / Math.sqrt(Math.max(max - min, 1e-9))) * Math.min(64, w / 9);
      const counts = Object.fromEntries(words.map(x => [x[0], x[2]]));
      const canvas = document.createElement('canvas');
      canvas.width = w * devicePixelRatio; canvas.height = h * devicePixelRatio;
      canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
      el.append(canvas);
      WordCloud(canvas, {
        list: words.map(([t, v]) => [t, scale(v) * devicePixelRatio]),
        fontFamily: '"IBM Plex Sans Thai", "Noto Sans Thai", sans-serif',
        fontWeight: 600, rotateRatio: 0, gridSize: Math.round(6 * devicePixelRatio),
        shrinkToFit: true, drawOutOfBound: false, backgroundColor: 'transparent',
        color: colorFor,
        hover: item => {
          canvas.style.cursor = item && onClick ? 'pointer' : 'default';
          canvas.title = item ? `${item[0]} — ${counts[item[0]].toLocaleString()} ครั้ง` : '';
        },
        click: onClick ? item => onClick(item[0]) : undefined,
      });
    }

    if (controls) {
      const buttons = controls.querySelectorAll('button[data-mode]');
      buttons.forEach(b => b.addEventListener('click', () => {
        buttons.forEach(x => x.classList.toggle('on', x === b));
        mode = b.dataset.mode; draw();
      }));
    }
    let t; addEventListener('resize', () => { clearTimeout(t); t = setTimeout(draw, 250); });
    document.fonts.ready.then(draw);
  };
})();
