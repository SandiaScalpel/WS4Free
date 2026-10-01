/*
 * Compare page: Alpine component `comparePage` + ECharts. Several stations on
 * shared charts, one line per station. A station keeps the same palette slot
 * however the selection changes (its position in the list of stations), so its
 * colour never jumps when another station is added or removed. Rain is shown
 * accumulated over the range, the comparable quantity. "Difference" mode plots
 * every station minus the first, bucket by bucket.
 * Visual conventions follow charts.js (validated palette, 2px lines, solid
 * crosshair, legend for ≥ 2 series, hover on everything).
 */
(function () {
  'use strict';
  const FONT = 'Inter, system-ui, sans-serif';
  const token = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const theme = () => (document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
  const ink = () => ({ surface: token('--ws-surface'), line: token('--ws-line'), ink: token('--ws-ink'), muted: token('--ws-muted') });
  const fmt = (v, d) => (v == null ? '—' : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const readJson = (id) => { const el = document.getElementById(id); return el ? JSON.parse(el.textContent) : []; };

  const CHARTS = [
    { key: 'temp', label: 'Temperature', q: 'temp' },
    { key: 'dewpoint', label: 'Dew point', q: 'temp' },
    { key: 'humidity', label: 'Humidity', q: 'pct' },
    { key: 'wind', label: 'Wind speed', q: 'wind' },
    { key: 'gust', label: 'Gust', q: 'wind' },
    { key: 'rain', label: 'Rain, accumulated', q: 'rain' },
    { key: 'pressure', label: 'Pressure', q: 'pressure' },
    { key: 'solar', label: 'Solar radiation', q: 'solar' },
  ];
  const STEP = { raw: 300000, hourly: 3600000, daily: 86400000 };

  document.addEventListener('alpine:init', () => {
    Alpine.data('comparePage', (cfg) => ({
      dataUrl: cfg.dataUrl, max: cfg.max,
      stations: readJson('compare-stations'), chosen: readJson('compare-chosen'),
      presets: [
        { value: '24h', label: '24 h' }, { value: '7d', label: '7 days' }, { value: '30d', label: '30 days' },
        { value: 'ytd', label: 'Year to date' }, { value: '1y', label: '1 year' }, { value: 'all', label: 'All' },
      ],
      range: '7d', start: '', end: '', diff: false, data: null, loading: false, chartDefs: CHARTS,

      init() {
        const q = new URLSearchParams(location.search);
        if (q.get('start')) { this.range = 'custom'; this.start = q.get('start'); this.end = q.get('end') || q.get('start'); }
        else if (q.get('range')) this.range = q.get('range');
        this.diff = q.get('diff') === '1';
        this.load();
        window.addEventListener('resize', () => this.eachChart((ch) => ch.resize()));
        window.addEventListener('ws4f:theme', () => this.render());
      },

      // ── selection ─────────────────────────────────────────────────────────
      isChosen(slug) { return this.chosen.includes(slug); },
      nameOf(slug) { const s = this.stations.find((x) => x.slug === slug); return s ? s.name : ''; },
      colorOf(slug) {
        const pal = window.WS4FreeCharts.CATEGORICAL[theme()];
        return pal[Math.max(0, this.stations.findIndex((x) => x.slug === slug)) % pal.length];
      },
      toggle(slug) {
        if (this.isChosen(slug)) { if (this.chosen.length > 1) this.chosen = this.chosen.filter((s) => s !== slug); }
        else if (this.chosen.length < this.max) this.chosen = this.chosen.concat([slug]);
        this.load();
      },
      query() {
        const q = new URLSearchParams();
        this.chosen.forEach((s) => q.append('s', s));
        if (this.range === 'custom') { q.set('start', this.start); q.set('end', this.end || this.start); } else q.set('range', this.range);
        return q;
      },
      async load() {
        const q = this.query();
        const page = new URLSearchParams(q); if (this.diff) page.set('diff', '1');
        history.replaceState(null, '', `?${page}`);
        this.loading = true;
        try {
          const res = await fetch(`${this.dataUrl}?${q}`, { credentials: 'same-origin' });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          this.data = await res.json();
          await this.$nextTick();
          this.render();
        } catch (e) { console.error(e); }
        this.loading = false;
      },

      // ── labels ────────────────────────────────────────────────────────────
      unit(qty) {
        if (!this.data) return '';
        const l = this.data.units.labels;
        return { temp: l.temp, pct: '%', wind: l.wind, rain: l.rain, pressure: l.pressure, solar: 'W/m²' }[qty];
      },
      digits(qty) {
        const d = this.data.units.digits;
        return { temp: d.temp, pct: 0, wind: 1, rain: d.rain, pressure: d.pressure, solar: 0 }[qty];
      },
      title(c) { return `${c.label}${this.diff && this.chosen.length > 1 ? ', difference' : ''} (${this.unit(c.q)})`; },
      note(c) {
        if (!this.data) return '';
        const res = this.data.resolution;
        if (c.key === 'rain') return 'Total since the start of the range';
        if (c.key === 'gust') return res === 'raw' ? '' : `Peak ${res === 'hourly' ? 'each hour' : 'each day'}`;
        return res === 'raw' ? '' : `${res === 'hourly' ? 'Hourly' : 'Daily'} mean`;
      },
      get resolutionLabel() {
        const r = this.data && this.data.resolution;
        return { raw: '5-minute readings', hourly: 'Hourly summaries', daily: 'Daily summaries' }[r] || '';
      },
      cell(v, qty) { return v == null ? '—' : `${fmt(v, this.digits(qty))} ${this.unit(qty)}`.replace(' %', '%'); },

      // ── charts ────────────────────────────────────────────────────────────
      eachChart(fn) {
        this.$root.querySelectorAll('[data-compare]').forEach((el) => { const ch = echarts.getInstanceByDom(el); if (ch) fn(ch); });
      },
      points(st, key) {
        const s = st.series, res = this.data.resolution;
        if (key !== 'rain') return s.time.map((t, i) => [t, s[key][i]]);
        let total = 0;
        const out = [[this.data.start, 0]];
        s.time.forEach((t, i) => {
          if (s.rain[i] == null) return;
          total += s.rain[i];
          out.push([Math.min(t + STEP[res], this.data.end), Math.round(total * 1000) / 1000]);
        });
        if (out.length > 1) out.push([this.data.end, out[out.length - 1][1]]);
        return out;
      },
      render() {
        if (!this.data || !window.echarts) return;
        const c = ink(), d = this.data, tz = d.tz, res = d.resolution, span = d.end - d.start;
        const when = new Intl.DateTimeFormat(undefined, res === 'daily'
          ? { timeZone: tz, weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }
          : { timeZone: tz, weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
        const tick = new Intl.DateTimeFormat(undefined, span <= 2 * 86400000 ? { timeZone: tz, hour: 'numeric' }
          : span <= 300 * 86400000 ? { timeZone: tz, month: 'short', day: 'numeric' } : { timeZone: tz, month: 'short', year: 'numeric' });
        const diff = this.diff && d.stations.length > 1 && d.aligned;
        const base = d.stations[0];
        const last = CHARTS[CHARTS.length - 1].key;

        this.$root.querySelectorAll('[data-compare]').forEach((el) => {
          const def = CHARTS.find((x) => x.key === el.dataset.compare);
          const digits = this.digits(def.q);
          // Axis labels: rain and inHg need their decimals; differences are small, so one decimal.
          const inhg = def.q === 'pressure' && d.units.pressure === 'inhg';
          const differences = this.diff && d.stations.length > 1;
          const axisDigits = def.q === 'rain' ? digits : inhg ? (differences ? 3 : 2) : (differences ? 1 : 0);
          const tipDigits = differences && inhg ? 3 : digits;
          let shown = d.stations;
          let series;
          if (diff) {
            const ref = new Map(this.points(base, def.key).filter((p) => p[1] != null));
            shown = d.stations.slice(1);
            series = shown.map((st) => this.points(st, def.key).map(([t, v]) => [t, v == null || !ref.has(t) ? null : Math.round((v - ref.get(t)) * 1000) / 1000]));
          } else {
            series = shown.map((st) => this.points(st, def.key));
          }
          const ch = echarts.getInstanceByDom(el) || echarts.init(el);
          ch.group = 'compare';
          ch.setOption({
            animation: false, textStyle: { fontFamily: FONT },
            grid: { left: 52, right: 16, top: 30, bottom: def.key === last ? 56 : 24 },
            legend: { top: 0, right: 0, icon: 'rect', itemWidth: 14, itemHeight: 3, itemGap: 14, textStyle: { color: c.muted, fontSize: 11 } },
            xAxis: { type: 'time', min: d.start, max: d.end, axisLine: { lineStyle: { color: c.line } }, axisTick: { show: false }, splitLine: { show: false },
                     axisLabel: { color: c.muted, fontSize: 11, hideOverlap: true, formatter: (v) => tick.format(new Date(v)) } },
            yAxis: { type: 'value', scale: !diff && def.key !== 'rain' && def.key !== 'solar', splitNumber: 4, axisLine: { show: false }, axisTick: { show: false },
                     splitLine: { lineStyle: { color: c.line } }, axisLabel: { color: c.muted, fontSize: 11, formatter: (v) => fmt(v, axisDigits) } },
            tooltip: {
              trigger: 'axis', confine: true, backgroundColor: c.surface, borderColor: c.line, borderWidth: 1, padding: [8, 10],
              textStyle: { color: c.ink, fontFamily: FONT, fontSize: 12 }, extraCssText: 'box-shadow: var(--ws-shadow); border-radius: 8px;',
              axisPointer: { type: 'line', lineStyle: { color: c.muted, width: 1, type: 'solid' } },
              formatter: (ps) => {
                let html = `<div style="color:${c.muted};margin-bottom:2px">${when.format(new Date(ps[0].value[0]))}</div>`;
                ps.forEach((p) => {
                  if (p.value[1] == null) return;
                  const v = diff ? (p.value[1] > 0 ? '+' : '') + fmt(p.value[1], tipDigits) : fmt(p.value[1], tipDigits);
                  html += `<div style="display:flex;align-items:center;gap:8px;line-height:1.6"><span style="display:inline-block;width:12px;height:2px;background:${p.color}"></span><b>${v}</b><span style="color:${c.muted}">${p.seriesName}</span></div>`;
                });
                return html;
              },
            },
            dataZoom: [{ type: 'inside', filterMode: 'none', throttle: 50 }].concat(def.key === last ? [{
              type: 'slider', height: 20, bottom: 8, filterMode: 'none', borderColor: c.line, backgroundColor: 'transparent',
              textStyle: { color: c.muted, fontSize: 10 }, labelFormatter: (v) => tick.format(new Date(v)),
            }] : []),
            series: shown.map((st, i) => ({
              name: diff ? `${st.name} − ${base.name}` : st.name, type: 'line', data: series[i], showSymbol: false,
              connectNulls: false, sampling: 'lttb', emphasis: { focus: 'series' },
              lineStyle: { width: 2, color: this.colorOf(st.slug), cap: 'round', join: 'round' }, itemStyle: { color: this.colorOf(st.slug) },
              markLine: diff && i === 0 ? { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: c.muted, type: 'solid', width: 1 }, data: [{ yAxis: 0 }] } : undefined,
            })),
          }, true);
        });
        echarts.connect('compare');
      },
    }));
  });
})();
