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

  // Temperature card: temperature and dew point share the (hidden) temperature
  // scale; humidity has its own 0–100 % scale. The axes are hidden as on every
  // sparkline, so the legend names the lines and the tooltip gives all three values.
  function tempHumidity(s, units, tz, c) {
    const when = new Intl.DateTimeFormat(undefined, { timeZone: tz, weekday: 'short', hour: 'numeric', minute: '2-digit' });
    const col = { temp: token('--ws-temp-line'), dew: token('--ws-dewpoint-line'), hum: token('--ws-humidity-line') };
    const line = (name, data, color, axis, area) => {
      const last = data.length - 1;
      return {
        name, type: 'line', data, yAxisIndex: axis, showSymbol: true, sampling: 'lttb',
        symbol: 'circle', symbolSize: (v, p) => (p.dataIndex === last ? 8 : 0),
        itemStyle: { color, borderColor: c.surface, borderWidth: 2 },
        lineStyle: { color, width: 2, cap: 'round', join: 'round' },
        areaStyle: area ? { color, opacity: 0.1 } : undefined, emphasis: { disabled: true },
      };
    };
    const t = units.labels.temp, d = units.digits.temp;
    return {
      animation: false,
      grid: { left: 4, right: 6, top: 8, bottom: 4 },
      xAxis: { type: 'time', show: false },
      yAxis: [{ type: 'value', scale: true, show: false }, { type: 'value', min: 0, max: 100, show: false }],
      tooltip: {
        ...tooltipBase(c), trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: c.muted, width: 1, type: 'solid' } },
        formatter: (ps) => {
          const fmt = { Temperature: (v) => `${v.toFixed(d)} ${t}`, 'Dew point': (v) => `${v.toFixed(d)} ${t}`, Humidity: (v) => `${v.toFixed(0)}%` };
          let html = `<span style="color:${c.muted}">${when.format(new Date(ps[0].value[0]))}</span>`;
          ps.forEach((p) => {
            html += `<div style="display:flex;align-items:center;gap:8px;line-height:1.6"><span style="display:inline-block;width:12px;height:2px;background:${p.color}"></span><b>${fmt[p.seriesName](p.value[1])}</b><span style="color:${c.muted}">${p.seriesName}</span></div>`;
          });
          return html;
        },
      },
      series: [
        line('Humidity', s.humidity || [], col.hum, 1, false),
        line('Dew point', s.dewpoint || [], col.dew, 0, false),
        line('Temperature', s.temp, col.temp, 0, true),
      ],
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

  // Forecast pop-up: one day hour by hour. Two small charts sharing the hours
  // axis (never one chart with two scales): temperature as a line, chance of
  // rain as columns on a fixed 0–100 % scale.
  function hourAxis(hours, c, show) {
    return {
      type: 'category', data: hours, boundaryGap: true,
      axisLine: { show: true, lineStyle: { color: c.line, width: 1 } }, axisTick: { show: false },
      axisLabel: { show, color: c.muted, fontSize: 11, interval: (i) => Number(i) % 6 === 0, formatter: hourLabel },
    };
  }

  function hourLabel(hhmm) {
    const h = Number(String(hhmm).slice(0, 2));
    return h === 0 ? '12a' : h === 12 ? '12p' : h < 12 ? `${h}a` : `${h - 12}p`;
  }

  function forecastTemp(d, c) {
    const color = token('--ws-temp-line'), unit = d.units.labels.temp;
    return {
      animation: false,
      grid: { left: 30, right: 6, top: 8, bottom: 20 },
      xAxis: hourAxis(d.hours, c, true),
      yAxis: {
        type: 'value', scale: true, splitNumber: 3,
        axisLabel: { color: c.muted, fontSize: 11 }, splitLine: { lineStyle: { color: c.line, opacity: 0.6 } },
      },
      tooltip: {
        ...tooltipBase(c), trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: c.muted, width: 1, type: 'solid' } },
        formatter: (ps) => `<span style="color:${c.muted}">${hourLabel(ps[0].name)}m</span><br><b>${ps[0].value == null ? 'No data' : `${Number(ps[0].value).toFixed(0)} ${unit}`}</b>`,
      },
      series: [{
        type: 'line', data: d.temp, showSymbol: false, connectNulls: false,
        lineStyle: { color, width: 2, cap: 'round', join: 'round' }, itemStyle: { color, borderColor: c.surface, borderWidth: 2 },
        areaStyle: { color, opacity: 0.1 }, emphasis: { disabled: true },
      }],
    };
  }

  function forecastRain(d, c) {
    return {
      animation: false,
      grid: { left: 30, right: 6, top: 8, bottom: 20 },
      xAxis: hourAxis(d.hours, c, true),
      yAxis: {
        type: 'value', min: 0, max: 100, interval: 50,
        axisLabel: { color: c.muted, fontSize: 11 }, splitLine: { lineStyle: { color: c.line, opacity: 0.6 } },
      },
      tooltip: {
        ...tooltipBase(c), trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: c.line, opacity: 0.4 } },
        formatter: (ps) => `<span style="color:${c.muted}">${hourLabel(ps[0].name)}m</span><br><b>${ps[0].value == null ? 'No data' : `${ps[0].value}% chance of rain`}</b>`,
      },
      series: [{
        type: 'bar', data: d.rain_pct, barMaxWidth: 24, barCategoryGap: '20%',
        itemStyle: { color: token('--ws-cool'), borderRadius: [4, 4, 0, 0] }, emphasis: { disabled: true },
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
    // A chart reads the block's <script id="chart-data">, or the JSON script its
    // data-chart-data attribute names (the forecast pop-up brings its own).
    const holder = (root || document).querySelector('#chart-data');
    const data = holder ? JSON.parse(holder.textContent) : null;
    const u = data && data.units, c = colors();
    const build = {
      temp: () => tempHumidity(data.series, u, data.tz, c),
      wind: () => sparkline(data.series.wind, u.labels.wind, 1, data.tz, c),
      pressure: () => sparkline(data.series.pressure, u.labels.pressure, u.digits.pressure, data.tz, c),
      rain_days: () => columns(data.rain_days, u.labels.rain, u.digits.rain, c),
      forecast_temp: (own) => forecastTemp(own, c),
      forecast_rain: (own) => forecastRain(own, c),
    };
    (root || document).querySelectorAll('[data-chart]').forEach((el) => {
      const make = build[el.dataset.chart];
      const ownHolder = el.dataset.chartData && document.getElementById(el.dataset.chartData);
      if (!make || (!ownHolder && !data)) return;
      const existing = mounted.get(el);
      if (existing) existing.dispose();
      const chart = echarts.init(el, null, { renderer: 'svg' });
      chart.setOption(make(ownHolder ? JSON.parse(ownHolder.textContent) : null));
      mounted.set(el, chart);
    });
  }

  document.addEventListener('DOMContentLoaded', () => mount(document));
  document.addEventListener('htmx:beforeSwap', (e) => unmount(e.detail.target));
  document.addEventListener('htmx:afterSettle', (e) => mount(e.detail.target));
  window.addEventListener('ws4f:theme', () => { unmount(document); mount(document); });
  window.addEventListener('resize', () => mounted.forEach((chart) => chart.resize()));

  // ── Forecast pop-up (partials/forecast_dialog.html) ───────────────────────
  // A day button's htmx request targets the dialog's body: open the dialog as
  // the request starts, so a slow response shows "Loading" rather than nothing.
  const LOADING = '<div class="flex items-center justify-between gap-4 px-5 py-4 sm:px-6"><h2 id="forecast-day-title" class="text-sm text-muted">Loading the forecast…</h2>'
    + '<form method="dialog"><button class="btn btn-ghost btn-sm" aria-label="Close">&times;</button></form></div>';
  document.addEventListener('htmx:beforeRequest', (e) => {
    const body = e.detail.target;
    if (!body || body.id !== 'forecast-day-body') return;
    const dialog = body.closest('dialog');
    if (!dialog.open) dialog.showModal();
  });
  document.addEventListener('htmx:responseError', (e) => {
    if (e.detail.target && e.detail.target.id === 'forecast-day-body') {
      e.detail.target.innerHTML = '<p class="px-5 py-6 text-sm text-muted sm:px-6">That day isn\'t in the forecast any more. Close this and reload the page.</p>';
    }
  });
  // Clicking the backdrop (outside the dialog's box) closes it, like Esc does.
  document.addEventListener('click', (e) => {
    if (e.target instanceof HTMLDialogElement && e.target.id === 'forecast-day') {
      const r = e.target.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) e.target.close();
    }
  });
  document.addEventListener('close', (e) => {
    if (e.target.id !== 'forecast-day') return;
    const body = e.target.querySelector('#forecast-day-body');
    unmount(body);
    body.innerHTML = LOADING;
  }, true);

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
