/*
 * WS4Free front-end glue. Loaded (deferred) before Alpine so the components
 * below are registered when Alpine starts.
 *
 * Theme: <html data-theme> is set before first paint by the inline script in
 * base_generic.html. The preference is 'light' | 'dark' | 'auto'; 'auto' follows
 * the OS and keeps following it while the page is open. Signed-in users' choice
 * is saved to their profile; everyone's is mirrored to localStorage.
 */
(function () {
  const STORAGE_KEY = 'ws4f-theme';
  const media = window.matchMedia('(prefers-color-scheme: dark)');

  function resolve(pref) {
    return pref === 'dark' || (pref === 'auto' && media.matches) ? 'dark' : 'light';
  }

  function apply(pref) {
    const theme = resolve(pref);
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.themePref = pref;
    window.dispatchEvent(new CustomEvent('ws4f:theme', { detail: { theme, pref } }));
  }

  function store(pref) {
    try { localStorage.setItem(STORAGE_KEY, pref); } catch (e) { /* private mode */ }
  }

  media.addEventListener('change', () => {
    if (document.documentElement.dataset.themePref === 'auto') apply('auto');
  });

  window.WS4Free = {
    theme: {
      current: () => document.documentElement.dataset.theme,
      pref: () => document.documentElement.dataset.themePref || 'auto',
      set(pref, saveUrl) {
        apply(pref);
        store(pref);
        if (saveUrl) {
          const body = new FormData();
          body.append('theme', pref);
          fetch(saveUrl, {
            method: 'POST',
            body,
            headers: { 'X-CSRFToken': document.querySelector('meta[name=csrf-token]')?.content || '' },
            credentials: 'same-origin',
          }).catch(() => {});
        }
      },
    },
  };

  // ── Charts (Apache ECharts) ────────────────────────────────────────────────
  // Every [data-chart] element inside a block is drawn from the block's
  // <script id="chart-data"> JSON (already converted to the viewer's units).
  // Colours come from CSS tokens, so charts follow the theme; marks follow the
  // data-viz spec: 2px lines, 10% area wash, 8px end dot with a 2px surface
  // ring, columns ≤ 24px with 4px rounded tops, hover tooltip on everything.
  const mounted = new Map();   // element -> echarts instance

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function colors() {
    return {
      series: token('--ws-series-1'), surface: token('--ws-surface'), line: token('--ws-line'),
      ink: token('--ws-ink'), muted: token('--ws-muted'),
    };
  }

  function tooltipBase(c) {
    return {
      backgroundColor: c.surface, borderColor: c.line, borderWidth: 1, padding: [6, 10],
      textStyle: { color: c.ink, fontFamily: 'Inter, system-ui, sans-serif', fontSize: 12 },
      extraCssText: 'box-shadow: var(--ws-shadow); border-radius: 8px;',
    };
  }

  function sparkline(points, unit, digits, tz, c) {
    const last = points.length - 1;
    const when = new Intl.DateTimeFormat(undefined, { timeZone: tz, weekday: 'short', hour: 'numeric', minute: '2-digit' });
    return {
      animation: false,
      grid: { left: 4, right: 6, top: 8, bottom: 4 },
      xAxis: { type: 'time', show: false },
      yAxis: { type: 'value', scale: true, show: false },
      tooltip: {
        ...tooltipBase(c), trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: c.muted, width: 1, type: 'solid' } },
        formatter: (ps) => {
          const [t, v] = ps[0].value;
          return `<span style="color:${c.muted}">${when.format(new Date(t))}</span><br><b>${v.toFixed(digits)} ${unit}</b>`;
        },
      },
      series: [{
        type: 'line', data: points, showSymbol: true, sampling: 'lttb',
        symbol: 'circle', symbolSize: (v, p) => (p.dataIndex === last ? 8 : 0),
        itemStyle: { color: c.series, borderColor: c.surface, borderWidth: 2 },
        lineStyle: { color: c.series, width: 2, cap: 'round', join: 'round' },
        areaStyle: { color: c.series, opacity: 0.1 },
        emphasis: { disabled: true },
      }],
    };
  }

  function columns(days, unit, digits, c) {
    const label = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' });
    return {
      animation: false,
      grid: { left: 0, right: 0, top: 6, bottom: 1 },
      xAxis: {
        type: 'category', data: days.map((d) => d[0]),
        axisLine: { show: true, lineStyle: { color: c.line, width: 1 } }, axisTick: { show: false }, axisLabel: { show: false },
      },
      yAxis: { type: 'value', min: 0, show: false },
      tooltip: {
        ...tooltipBase(c), trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: c.line, opacity: 0.4 } },
        formatter: (ps) => {
          const [day, v] = [ps[0].name, ps[0].value];
          const text = v == null ? 'No data' : `${Number(v).toFixed(digits)} ${unit}`;
          return `<span style="color:${c.muted}">${label.format(new Date(day + 'T12:00:00Z'))}</span><br><b>${text}</b>`;
        },
      },
      series: [{
        type: 'bar', data: days.map((d) => d[1]), barMaxWidth: 24, barCategoryGap: '25%',
        itemStyle: { color: c.series, borderRadius: [4, 4, 0, 0] }, emphasis: { disabled: true },
      }],
    };
  }

  function unmount(root) {
    for (const [el, chart] of mounted) {
      if (!el.isConnected || (root && root.contains(el))) { chart.dispose(); mounted.delete(el); }
    }
  }

  function mount(root) {
    if (!window.echarts) return;
    const holder = (root || document).querySelector('#chart-data');
    if (!holder) return;
    const data = JSON.parse(holder.textContent);
    const u = data.units, c = colors();
    const build = {
      temp: () => sparkline(data.series.temp, u.labels.temp, u.digits.temp, data.tz, c),
      wind: () => sparkline(data.series.wind, u.labels.wind, 1, data.tz, c),
      pressure: () => sparkline(data.series.pressure, u.labels.pressure, u.digits.pressure, data.tz, c),
      rain_days: () => columns(data.rain_days, u.labels.rain, u.digits.rain, c),
    };
    (root || document).querySelectorAll('[data-chart]').forEach((el) => {
      const make = build[el.dataset.chart];
      if (!make) return;
      const existing = mounted.get(el);
      if (existing) existing.dispose();
      const chart = echarts.init(el, null, { renderer: 'svg' });
      chart.setOption(make());
      mounted.set(el, chart);
    });
  }

  document.addEventListener('DOMContentLoaded', () => mount(document));
  document.addEventListener('htmx:beforeSwap', (e) => unmount(e.detail.target));
  document.addEventListener('htmx:afterSettle', (e) => mount(e.detail.target));
  window.addEventListener('ws4f:theme', () => { unmount(document); mount(document); });
  window.addEventListener('resize', () => mounted.forEach((chart) => chart.resize()));

  document.addEventListener('alpine:init', () => {
    // Header toggle: flips between light and dark from whatever is showing now.
    Alpine.data('themeToggle', (saveUrl) => ({
      dark: document.documentElement.dataset.theme === 'dark',
      init() {
        window.addEventListener('ws4f:theme', (e) => { this.dark = e.detail.theme === 'dark'; });
      },
      toggle() {
        WS4Free.theme.set(this.dark ? 'light' : 'dark', saveUrl);
      },
    }));

    // Toast messages that dismiss themselves.
    Alpine.data('toast', (timeout = 6000) => ({
      show: true,
      init() { if (timeout) setTimeout(() => { this.show = false; }, timeout); },
    }));
  });
})();
