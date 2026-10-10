// Word cloud: setupCloud(container, urlTemplate, {controls, onClick}).
// urlTemplate contains MODE (replaced by distinctive|frequent) and returns [[word, weight, count], …].
// Mode buttons: any .seg button[data-mode] inside `controls` switch ?mode=… and redraw.
// Returns { setMode(mode), redraw() } for pages that drive the mode themselves. A hidden cloud cannot be sized, so
// drawing is skipped while the container is hidden; call redraw()/setMode() once it is visible again.
// Hovering a word highlights it and shows how many times it was said (an overlay: the cloud itself is a canvas).
(function () {
  // Read from CSS at draw time.
  const readPalette = () => {
    const css = getComputedStyle(document.documentElement);
    return ['--c1', '--c2', '--c3', '--c4', '--c5'].map(v => css.getPropertyValue(v).trim());
  };

  const MAX_WORDS = 70;  // the API returns 120; fewer words keeps the cloud airy

  window.setupCloud = function (el, urlTemplate, { controls = null, onClick = null } = {}) {
    let mode = 'distinctive', ready = false, latest = 0;

    async function draw() {
      if (!el.clientWidth) return;  // hidden (e.g. another view is showing)
      const run = ++latest;
      const words = (await (await fetch(urlTemplate.replace('MODE', mode))).json()).slice(0, MAX_WORDS);
      const w = el.clientWidth, h = el.clientHeight;
      if (run !== latest || !w) return;  // a newer draw started meanwhile, or the container got hidden
      el.innerHTML = '';
      if (!words.length) { el.innerHTML = '<p class="muted">ข้อมูลไม่พอ</p>'; return; }
      const max = words[0][1], min = words[words.length - 1][1];
      const scale = v => 14 + (Math.sqrt(v - min) / Math.sqrt(Math.max(max - min, 1e-9))) * Math.min(64, w / 9);
      const palette = readPalette();
      const colorFor = word => palette[[...word].reduce((a, c) => a + c.codePointAt(0), 0) % palette.length];
      const counts = Object.fromEntries(words.map(x => [x[0], x[2]]));
      const canvas = document.createElement('canvas');
      canvas.width = w * devicePixelRatio; canvas.height = h * devicePixelRatio;
      canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
      el.append(canvas);

      // Hover state: a highlight box over the word's cell-aligned bounds, with a count label that flips below
      // near the top edge and is nudged to stay inside the cloud.
      const box = document.createElement('div');
      box.className = 'cloud-hover'; box.hidden = true;
      const label = document.createElement('span');
      box.append(label);
      el.append(box);
      const hide = () => { box.hidden = true; canvas.style.cursor = ''; };
      const show = (item, d) => {
        const k = devicePixelRatio;  // the library reports canvas pixels
        const x = d.x / k, y = d.y / k;
        box.style.left = x + 'px'; box.style.top = y + 'px';
        box.style.width = d.w / k + 'px'; box.style.height = d.h / k + 'px';
        label.textContent = `${counts[item[0]].toLocaleString()} ครั้ง`;
        box.hidden = false;
        label.style.left = Math.min(Math.max(x + d.w / k / 2 - label.offsetWidth / 2, 0), w - label.offsetWidth) - x + 'px';
        box.classList.toggle('below', y < 32);
        canvas.style.cursor = onClick ? 'pointer' : '';
      };
      canvas.addEventListener('mouseleave', hide);  // the library sends no event when the pointer leaves the canvas

      WordCloud(canvas, {
        list: words.map(([t, v]) => [t, scale(v) * devicePixelRatio]),
        fontFamily: '"Bai Jamjuree", "Noto Sans Thai", sans-serif',
        fontWeight: 600, rotateRatio: 0, gridSize: Math.round(12 * devicePixelRatio),
        shrinkToFit: true, drawOutOfBound: false, backgroundColor: 'transparent',
        color: colorFor,
        hover: (item, dimension) => (item && dimension ? show(item, dimension) : hide()),
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
    // wait for the web font before the first draw: the canvas measures text with whatever font is loaded
    document.fonts.ready.then(() => { ready = true; draw(); });
    return {
      redraw: () => { if (ready) draw(); },
      setMode(next) { mode = next; if (ready) draw(); },
    };
  };
})();
