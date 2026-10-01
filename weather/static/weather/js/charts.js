/*
 * WS4Free Charts page: Alpine component `chartsPage` + ECharts rendering.
 *
 * Data comes from /stations/<slug>/charts/data/ already in the viewer's units.
 * Visual rules (dataviz skill): categorical hues in fixed order from the
 * validated reference palette, 2px lines, ~10% area washes, ≤24px bars with 4px
 * rounded ends, solid hairline grid, legend whenever there are ≥2 series, value-
 * first tooltips with line keys, hover on everything, previous render dimmed
 * (never blanked) while new data loads.
 */
(function () {
  'use strict';

  // Reference palette (validated light + dark), slots in fixed order.
  const CATEGORICAL = {
    light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'],
    dark: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
  };
  // Ordinal ramps for the wind-rose speed bins (validated with --ordinal on our surfaces).
  const ROSE = {
    light: ['#86b6ef', '#5598e7', '#2a78d6', '#1c5cab', '#0d366b'],
    dark: ['#184f95', '#256abf', '#3987e5', '#6da7ec', '#9ec5f4'],
  };
  // Sequential (rain) and diverging blue↔gray↔red (temperature) heat scales.
  const SEQUENTIAL = { light: ['#cde2fb', '#86b6ef', '#2a78d6', '#104281'], dark: ['#163152', '#256abf', '#6da7ec', '#cde2fb'] };
  const DIVERGING = {
    light: ['#0d366b', '#3987e5', '#b7d3f6', '#f0efec', '#f4b9b5', '#e34948', '#8f1f20'],
    dark: ['#cde2fb', '#3987e5', '#1c5cab', '#383835', '#a33a39', '#e66767', '#f4b9b5'],
  };
  const DIRS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  const theme = () => (document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
  const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const ink = () => ({ surface: token('--ws-surface'), line: token('--ws-line'), ink: token('--ws-ink'),
                       muted: token('--ws-muted'), faint: token('--ws-faint') });
  const FONT = 'Inter, system-ui, sans-serif';

  function chartAt(el) {
    return echarts.getInstanceByDom(el) || echarts.init(el, null, { renderer: 'canvas' });
  }

  function tooltip(c, extra) {
    return Object.assign({
      backgroundColor: c.surface, borderColor: c.line, borderWidth: 1, padding: [8, 10],
      textStyle: { color: c.ink, fontFamily: FONT, fontSize: 12 },
      extraCssText: 'box-shadow: var(--ws-shadow); border-radius: 8px;',
      confine: true,
    }, extra);
  }

  // Tooltip row: value first (strong), series name secondary, keyed by a short line.
  function row(color, value, name, keyHeight) {
    const h = keyHeight || 2;
    return `<div style="display:flex;align-items:center;gap:8px;line-height:1.6">` +
      `<span style="display:inline-block;width:12px;height:${h}px;border-radius:1px;background:${color}"></span>` +
      `<b>${value}</b><span style="color:${token('--ws-muted')}">${name}</span></div>`;
  }

  function axisCommon(c) {
    return {
      axisLine: { lineStyle: { color: c.line } },
      axisTick: { show: false },
      axisLabel: { color: c.muted, fontFamily: FONT, fontSize: 11 },
      splitLine: { lineStyle: { color: c.line, width: 1, type: 'solid' } },
    };
  }

  function fmt(v, digits) {
    return v == null ? '—' : Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  window.WS4FreeCharts = { CATEGORICAL, ROSE };

  document.addEventListener('alpine:init', () => {
    Alpine.data('chartsPage', (config) => ({
      dataUrl: config.dataUrl,
      years: config.years || [],
      presets: [
        { value: '24h', label: '24 h' }, { value: '7d', label: '7 days' }, { value: '30d', label: '30 days' },
        { value: 'ytd', label: 'Year to date' }, { value: '1y', label: '1 year' }, { value: 'all', label: 'All' },
      ],
      range: '7d', start: '', end: '',
      history: null, rose: null, calendarData: null, yoy: null,
      calMetric: 'high', calYear: null, yoyMetric: 'rain',
      loading: { history: false, rose: false, calendar: false, yoy: false },
      historyCharts: [],
      _zoomTimer: null,

      init() {
        const q = new URLSearchParams(location.search);
        if (q.get('start')) { this.range = 'custom'; this.start = q.get('start'); this.end = q.get('end') || q.get('start'); }
        else if (q.get('range')) { this.range = q.get('range'); }
        this.calYear = Number(q.get('year')) || this.years[this.years.length - 1] || new Date().getFullYear();
        this.calMetric = q.get('cal') || this.calMetric;
        this.yoyMetric = q.get('yoy') || this.yoyMetric;
        this.loadRange();
        this.loadCalendar();
        this.loadYoy();
        window.addEventListener('resize', () => this.eachChart((ch) => ch.resize()));
        window.addEventListener('ws4f:theme', () => this.renderAll());
      },

      // ── range & URL ──────────────────────────────────────────────────────
      rangeQuery() {
        return this.range === 'custom' ? `start=${this.start}&end=${this.end || this.start}` : `range=${this.range}`;
      },
      get csvUrl() { return `${this.dataUrl}?kind=history&format=csv&${this.rangeQuery()}`; },
      get resolutionLabel() {
        const r = this.history && this.history.resolution;
        return { raw: '5-minute readings', hourly: 'Hourly summaries', daily: 'Daily summaries' }[r] || '';
      },
      syncUrl() {
        const q = new URLSearchParams(this.rangeQuery());
        q.set('year', this.calYear); q.set('cal', this.calMetric); q.set('yoy', this.yoyMetric);
        history.replaceState(null, '', `?${q}`);
      },
      setPreset(value) { this.range = value; this.loadRange(); },
      applyCustom() { if (this.start) { this.range = 'custom'; this.loadRange(); } },
      showDay(day) {
        this.range = 'custom'; this.start = day; this.end = day;
        this.loadRange();
        window.scrollTo({ top: 0, behavior: 'smooth' });
      },

      async fetchJson(kind, query) {
        const res = await fetch(`${this.dataUrl}?kind=${kind}&${query}`, { credentials: 'same-origin' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      },

      async loadRange() {
        this.syncUrl();
        this.loading.history = this.loading.rose = true;
        try {
          const [h, r] = await Promise.all([this.fetchJson('history', this.rangeQuery()), this.fetchJson('rose', this.rangeQuery())]);
          this.history = h; this.rose = r;
          this.defineHistoryCharts();
          await this.$nextTick();
          this.renderHistory(); this.renderRose();
        } catch (e) { console.error(e); }
        this.loading.history = this.loading.rose = false;
      },
      async loadCalendar() {
        this.syncUrl();
        this.loading.calendar = true;
        try { this.calendarData = await this.fetchJson('calendar', `year=${this.calYear}&metric=${this.calMetric}`); this.renderCalendar(); }
        catch (e) { console.error(e); }
        this.loading.calendar = false;
      },
      async loadYoy() {
        this.syncUrl();
        this.loading.yoy = true;
        try { this.yoy = await this.fetchJson('yoy', `metric=${this.yoyMetric}`); this.renderYoy(); }
        catch (e) { console.error(e); }
        this.loading.yoy = false;
      },

      eachChart(fn) {
        this.$root.querySelectorAll('[data-history],[data-rose],[data-calendar],[data-yoy]').forEach((el) => {
          const ch = echarts.getInstanceByDom(el); if (ch) fn(ch);
        });
      },
      renderAll() { this.renderHistory(); this.renderRose(); this.renderCalendar(); this.renderYoy(); },
      resetZoom() {
        this.eachChart((ch) => ch.dispatchAction({ type: 'dataZoom', start: 0, end: 100 }));
      },

      // ── history ──────────────────────────────────────────────────────────
      defineHistoryCharts() {
        const u = this.history.units.labels, res = this.history.resolution;
        const per = { raw: 'per 5 minutes', hourly: 'per hour', daily: 'per day' }[res];
        this.historyCharts = [
          { key: 'temp', title: `Temperature (${u.temp})`, tall: true,
            note: res === 'raw' ? '' : 'Mean, with the low–high range shaded' },
          { key: 'humidity', title: 'Humidity (%)', note: res === 'raw' ? '' : 'Mean' },
          { key: 'wind', title: `Wind (${u.wind})`, note: res === 'raw' ? '' : 'Mean speed and peak gust' },
          { key: 'rain', title: `Rain (${u.rain})`, note: `Bars: total ${per} · line: accumulated since the start of the range` },
          { key: 'pressure', title: `Pressure (${u.pressure})`, note: res === 'raw' ? '' : 'Mean' },
          { key: 'solar', title: 'Solar radiation (W/m²)', note: res === 'daily' ? 'Daily peak' : (res === 'hourly' ? 'Mean' : '') },
        ].concat((this.history.sensor_charts || []).map((sc) => ({
          key: sc.key, title: sc.title, sensor: sc,
          note: sc.bars ? `Strikes ${per}` : (res === 'raw' ? '' : 'Mean'),
        })));
      },

      renderHistory() {
        if (!this.history || !window.echarts) return;
        const h = this.history, s = h.series, c = ink(), pal = CATEGORICAL[theme()];
        const u = h.units, d = u.digits, tz = h.tz, res = h.resolution;
        const when = new Intl.DateTimeFormat(undefined, res === 'daily'
          ? { timeZone: tz, weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }
          : { timeZone: tz, weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
        const span = h.end - h.start;
        const tick = new Intl.DateTimeFormat(undefined,
          span <= 2 * 86400000 ? { timeZone: tz, hour: 'numeric' }
          : span <= 300 * 86400000 ? { timeZone: tz, month: 'short', day: 'numeric' }
          : { timeZone: tz, month: 'short', year: 'numeric' });
        const pts = (key) => s.time.map((t, i) => [t, s[key][i]]);
        const hasAny = (key) => s[key].some((v) => v != null);
        const keys = this.historyCharts.map((x) => x.key);
        const self = this;
        const axisDigits = { rain: d.rain, pressure: u.pressure === 'inhg' ? 2 : 0 };

        const base = (key, series, legend, unitDigits) => ({
          animation: false,
          textStyle: { fontFamily: FONT },
          grid: { left: 52, right: 16, top: legend ? 28 : 10, bottom: key === keys[keys.length - 1] ? 56 : 24 },
          legend: legend ? { top: 0, right: 0, textStyle: { color: c.muted, fontSize: 11 }, itemWidth: 14, itemHeight: 3, itemGap: 14, data: legend } : undefined,
          xAxis: Object.assign({ type: 'time', min: h.start, max: h.end }, axisCommon(c),
            { splitLine: { show: false }, axisLabel: { color: c.muted, fontSize: 11, hideOverlap: true, formatter: (v) => tick.format(new Date(v)) } }),
          yAxis: Object.assign({ type: 'value', scale: key !== 'rain' && key !== 'solar', splitNumber: 4 }, axisCommon(c),
            { axisLine: { show: false }, axisLabel: { color: c.muted, fontSize: 11, formatter: (v) => fmt(v, axisDigits[key] || 0) } }),
          tooltip: tooltip(c, {
            trigger: 'axis',
            axisPointer: { type: 'line', lineStyle: { color: c.muted, width: 1, type: 'solid' } },
            formatter: (ps) => {
              const t = ps[0].value[0];
              let html = `<div style="color:${c.muted};margin-bottom:2px">${when.format(new Date(t))}</div>`;
              ps.forEach((p) => {
                if (p.seriesName === '_low') return;
                if (p.seriesName === 'Low–high range') {
                  const i = p.dataIndex, lo = s.temp_min[i], hi = s.temp_max[i];
                  if (lo != null && hi != null) html += row(p.color, `${fmt(lo, d.temp)} – ${fmt(hi, d.temp)}`, 'Low–high', 8);
                  return;
                }
                if (p.value[1] == null) return;
                html += row(p.color, fmt(p.value[1], unitDigits), p.seriesName, p.seriesType === 'bar' ? 8 : 2);
              });
              return html;
            },
          }),
          dataZoom: [{ type: 'inside', filterMode: 'none', throttle: 50 }].concat(
            key === keys[keys.length - 1] ? [{
              type: 'slider', height: 20, bottom: 8, filterMode: 'none', borderColor: c.line, backgroundColor: 'transparent',
              fillerColor: 'rgba(42,120,214,0.10)', dataBackground: { lineStyle: { color: c.line }, areaStyle: { color: c.line, opacity: 0.4 } },
              selectedDataBackground: { lineStyle: { color: pal[0] }, areaStyle: { color: pal[0], opacity: 0.15 } },
              handleStyle: { color: c.surface, borderColor: c.muted }, moveHandleStyle: { color: c.line },
              textStyle: { color: c.muted, fontSize: 10 }, labelFormatter: (v) => tick.format(new Date(v)),
            }] : []),
          series,
        });

        const line = (name, key, color, area) => ({
          name, type: 'line', data: pts(key), showSymbol: false, connectNulls: false, sampling: 'lttb',
          lineStyle: { width: 2, color, cap: 'round', join: 'round' }, itemStyle: { color },
          areaStyle: area ? { color, opacity: 0.1 } : undefined, emphasis: { disabled: true },
        });

        const defs = {
          temp: () => {
            const series = [];
            const band = hasAny('temp_min') && hasAny('temp_max');
            if (band) {
              series.push({ name: '_low', type: 'line', stack: 'band', data: pts('temp_min'), showSymbol: false,
                            lineStyle: { opacity: 0 }, itemStyle: { color: 'transparent' }, emphasis: { disabled: true } });
              series.push({ name: 'Low–high range', type: 'line', stack: 'band', showSymbol: false,
                            data: s.time.map((t, i) => [t, s.temp_max[i] != null && s.temp_min[i] != null ? s.temp_max[i] - s.temp_min[i] : null]),
                            lineStyle: { opacity: 0 }, itemStyle: { color: pal[0] }, areaStyle: { color: pal[0], opacity: 0.12 },
                            emphasis: { disabled: true } });
            }
            series.push(line('Temperature', 'temp', pal[0], false));
            series.push(line('Dew point', 'dewpoint', pal[1], false));
            const legend = [{ name: 'Temperature', icon: 'rect' }, { name: 'Dew point', icon: 'rect' }];
            if (band) legend.push({ name: 'Low–high range', icon: 'rect', itemStyle: { opacity: 0.35 } });
            return base('temp', series, legend, d.temp);
          },
          humidity: () => base('humidity', [line('Humidity', 'humidity', pal[0], true)], null, 0),
          wind: () => base('wind', [line('Speed', 'wind', pal[0], false), line('Gust', 'gust', pal[1], false)],
                           [{ name: 'Speed', icon: 'rect' }, { name: 'Gust', icon: 'rect' }], 1),
          rain: () => {
            // Requested by the owner: per-bucket bars on the left axis, and on a right axis
            // (in its own colour) the total accumulated since the start of the selected range,
            // marching up from zero. Both axes are rain in the same unit and start at zero; the
            // legend names each series' side and the tooltip shows both values.
            const step = { raw: 300000, hourly: 3600000, daily: 86400000 }[res];
            let total = 0;
            const accumulated = [[h.start, 0]];
            s.time.forEach((t, i) => {
              const v = s.rain[i];
              if (v == null) return;
              total += v;
              accumulated.push([Math.min(t + step, h.end), Math.round(total * 1000) / 1000]);
            });
            if (accumulated.length > 1) accumulated.push([h.end, accumulated[accumulated.length - 1][1]]);
            const bucket = { raw: '5 min', hourly: 'hour', daily: 'day' }[res];
            const opt = base('rain', [
              { name: `Rain per ${bucket} (left)`, type: 'bar', data: pts('rain'), barMaxWidth: 24, large: s.time.length > 1500,
                itemStyle: { color: pal[0], borderRadius: [4, 4, 0, 0] }, emphasis: { disabled: true } },
              { name: 'Accumulated (right)', type: 'line', yAxisIndex: 1, data: accumulated, showSymbol: false, step: false,
                lineStyle: { width: 2, color: pal[1], cap: 'round', join: 'round' }, itemStyle: { color: pal[1] },
                emphasis: { disabled: true }, z: 3 },
            ], [{ name: `Rain per ${bucket} (left)`, icon: 'rect' }, { name: 'Accumulated (right)', icon: 'rect' }], d.rain);
            opt.grid.right = 56;
            opt.yAxis = [opt.yAxis, Object.assign({ type: 'value', min: 0, position: 'right', splitNumber: 4 }, axisCommon(c), {
              splitLine: { show: false },
              axisLine: { show: true, lineStyle: { color: pal[1] } },
              axisLabel: { color: pal[1], fontSize: 11, formatter: (v) => fmt(v, d.rain) },
            })];
            opt.yAxis[0].min = 0;
            return opt;
          },
          pressure: () => base('pressure', [line('Pressure', 'pressure', pal[0], false)], null, d.pressure),
          solar: () => base('solar', [line('Solar radiation', 'solar', pal[0], true)], null, 0),
        };

        // Extra sensors: one line per channel (fixed palette slots, legend when ≥ 2), or
        // strike-count bars for lightning.
        this.historyCharts.filter((x) => x.sensor).forEach((x) => {
          const sc = x.sensor;
          defs[x.key] = () => {
            const series = sc.lines.map((ln, i) => (sc.bars
              ? { name: ln.name, type: 'bar', data: pts(ln.col), barMaxWidth: 24, itemStyle: { color: pal[i], borderRadius: [4, 4, 0, 0] }, emphasis: { disabled: true } }
              : line(ln.name, ln.col, pal[i], sc.lines.length === 1)));
            const legend = sc.lines.length > 1 ? sc.lines.map((ln) => ({ name: ln.name, icon: 'rect' })) : null;
            const opt = base(x.key, series, legend, sc.digits);
            if (sc.bars) opt.yAxis.min = 0;
            return opt;
          };
        });

        const charts = [];
        this.$root.querySelectorAll('[data-history]').forEach((el) => {
          const make = defs[el.dataset.history]; if (!make) return;
          const ch = chartAt(el);
          ch.group = 'history';
          ch.setOption(make(), true);
          ch.off('datazoom'); ch.on('datazoom', () => self.onZoom(ch));
          charts.push(ch);
        });
        echarts.connect('history');
      },

      // Zooming into a window short enough for finer data loads that detail.
      onZoom(ch) {
        clearTimeout(this._zoomTimer);
        this._zoomTimer = setTimeout(() => {
          const z = ch.getOption().dataZoom[0], h = this.history;
          const span = h.end - h.start;
          const from = h.start + span * (z.start / 100), to = h.start + span * (z.end / 100);
          const visible = to - from;
          const finer = (h.resolution === 'daily' && visible <= 120 * 86400000) || (h.resolution === 'hourly' && visible <= 3 * 86400000);
          if (!finer || z.start === 0 && z.end === 100) return;
          const tz = h.tz, day = (ms) => new Intl.DateTimeFormat('en-CA', { timeZone: tz }).format(new Date(ms));
          this.range = 'custom'; this.start = day(from); this.end = day(to);
          this.loadRange();
        }, 700);
      },

      // ── wind rose ────────────────────────────────────────────────────────
      renderRose() {
        const el = this.$root.querySelector('[data-rose]');
        if (!el || !this.rose || !window.echarts) return;
        const r = this.rose, c = ink(), ramp = ROSE[theme()];
        chartAt(el).setOption({
          animation: false,
          textStyle: { fontFamily: FONT },
          legend: { bottom: 0, textStyle: { color: c.muted, fontSize: 11 }, itemWidth: 12, itemHeight: 8, itemGap: 10 },
          polar: { radius: ['6%', '72%'], center: ['50%', '46%'] },
          angleAxis: { type: 'category', data: DIRS, startAngle: 90 + 360 / 32, clockwise: true, boundaryGap: true,
                       axisLine: { lineStyle: { color: c.line } }, axisTick: { show: false },
                       axisLabel: { color: c.muted, fontSize: 10, interval: (i) => i % 2 === 0 }, splitLine: { show: false } },
          radiusAxis: { type: 'value', axisLine: { show: false }, axisTick: { show: false },
                        axisLabel: { color: c.faint, fontSize: 10, formatter: '{value}%' },
                        splitLine: { lineStyle: { color: c.line, type: 'solid' } } },
          tooltip: tooltip(c, { trigger: 'item',
            formatter: (p) => `<div style="color:${c.muted};margin-bottom:2px">From ${DIRS[p.dataIndex]}</div>` +
                              row(p.color, `${fmt(p.value, 1)}%`, p.seriesName, 8) }),
          series: r.bins.map((name, i) => ({
            name, type: 'bar', coordinateSystem: 'polar', stack: 'rose', data: r.values[i],
            itemStyle: { color: ramp[i], borderColor: c.surface, borderWidth: 1 }, emphasis: { focus: 'series' },
          })),
        }, true);
      },

      // ── calendar ─────────────────────────────────────────────────────────
      renderCalendar() {
        const el = this.$root.querySelector('[data-calendar]');
        if (!el || !this.calendarData || !window.echarts) return;
        const cal = this.calendarData, c = ink(), u = cal.units, rain = cal.metric === 'rain';
        const values = cal.days.map((x) => x[1]).filter((v) => v != null).sort((a, b) => a - b);
        const q = (p) => values.length ? values[Math.min(values.length - 1, Math.floor(p * (values.length - 1)))] : 0;
        let min, max, colors;
        if (rain) { min = 0; max = Math.max(q(0.97), u.rain === 'in' ? 0.25 : 6); colors = SEQUENTIAL[theme()]; }
        else {
          const mid = q(0.5), k = Math.max(Math.abs(q(0.03) - mid), Math.abs(q(0.97) - mid), 1);
          min = Math.round(mid - k); max = Math.round(mid + k); colors = DIVERGING[theme()];
        }
        const digits = rain ? u.digits.rain : u.digits.temp, unit = rain ? u.labels.rain : u.labels.temp;
        const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
        const ch = chartAt(el);
        ch.setOption({
          animation: false,
          textStyle: { fontFamily: FONT },
          calendar: {
            range: String(cal.year), top: 22, left: 34, right: 8, bottom: 44, cellSize: ['auto', 'auto'],
            itemStyle: { color: 'transparent', borderColor: c.line, borderWidth: 1 },
            splitLine: { show: true, lineStyle: { color: c.faint, width: 1, type: 'solid', opacity: 0.6 } },
            dayLabel: { firstDay: 0, nameMap: ['S', 'M', 'T', 'W', 'T', 'F', 'S'], color: c.muted, fontSize: 10 },
            monthLabel: { color: c.muted, fontSize: 11 }, yearLabel: { show: false },
          },
          visualMap: { type: 'continuous', min, max, calculable: false, orient: 'horizontal', left: 'center', bottom: 0,
                       itemWidth: 10, itemHeight: 160, text: [`${fmt(max, rain ? digits : 0)} ${unit}`, `${fmt(min, rain ? 0 : 0)}${rain ? '' : ' ' + unit}`],
                       textStyle: { color: c.muted, fontSize: 11 }, inRange: { color: colors } },
          tooltip: tooltip(c, { trigger: 'item',
            formatter: (p) => `<div style="color:${c.muted};margin-bottom:2px">${dayFmt.format(new Date(p.value[0] + 'T12:00:00Z'))}</div>` +
                              `<b>${fmt(p.value[1], digits)} ${unit}</b>` }),
          series: [{ type: 'heatmap', coordinateSystem: 'calendar', data: cal.days,
                     itemStyle: { borderColor: c.surface, borderWidth: 1 }, emphasis: { itemStyle: { borderColor: c.ink, borderWidth: 1.5 } } }],
        }, true);
        ch.off('click'); ch.on('click', (p) => { if (p.value) this.showDay(p.value[0]); });
      },

      // ── year over year ───────────────────────────────────────────────────
      renderYoy() {
        const el = this.$root.querySelector('[data-yoy]');
        if (!el || !this.yoy || !window.echarts) return;
        const y = this.yoy, c = ink(), pal = CATEGORICAL[theme()], u = y.units, rain = y.metric === 'rain';
        const firstYear = this.years[0] || (y.series[0] && y.series[0].year);
        const series = y.series.slice(-8);   // never more than the 8 palette slots
        const days = Array.from({ length: 366 }, (_, i) => new Date(Date.UTC(2000, 0, 1 + i)));
        const narrow = el.clientWidth < 560;
        const monthStart = (i) => days[i].getUTCDate() === 1 && (!narrow || days[i].getUTCMonth() % 3 === 0);
        const digits = rain ? u.digits.rain : u.digits.temp, unit = rain ? u.labels.rain : u.labels.temp;
        const newest = series.length ? series[series.length - 1].year : null;
        chartAt(el).setOption({
          animation: false,
          textStyle: { fontFamily: FONT },
          grid: { left: 52, right: 16, top: 34, bottom: 28 },
          legend: { type: 'scroll', top: 0, left: 0, right: 0, textStyle: { color: c.muted, fontSize: 11 }, itemWidth: 14, itemHeight: 3, icon: 'rect', itemGap: 14,
                    pageIconColor: c.muted, pageTextStyle: { color: c.muted } },
          xAxis: Object.assign({ type: 'category', data: days.map((_, i) => i), boundaryGap: false }, axisCommon(c), {
            splitLine: { show: false },
            axisLabel: { color: c.muted, fontSize: 11, interval: (i) => monthStart(i), formatter: (i) => MONTHS[days[i].getUTCMonth()] },
          }),
          yAxis: Object.assign({ type: 'value', scale: !rain }, axisCommon(c),
            { axisLine: { show: false }, axisLabel: { color: c.muted, fontSize: 11, formatter: (v) => fmt(v, rain ? Math.min(digits, 1) : 0) } }),
          tooltip: tooltip(c, {
            trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: c.muted, width: 1, type: 'solid' } },
            formatter: (ps) => {
              const d = days[ps[0].dataIndex];
              let html = `<div style="color:${c.muted};margin-bottom:2px">${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}</div>`;
              ps.slice().sort((a, b) => Number(b.seriesName) - Number(a.seriesName)).forEach((p) => {
                if (p.value != null) html += row(p.color, `${fmt(p.value, digits)} ${unit}`, p.seriesName);
              });
              return html;
            },
          }),
          series: series.map((s) => {
            const color = pal[(s.year - firstYear) % pal.length];   // a year keeps its colour
            return {
              name: String(s.year), type: 'line', data: s.values, showSymbol: false, connectNulls: false,
              lineStyle: { width: s.year === newest ? 2.5 : 2, color }, itemStyle: { color },
              emphasis: { focus: 'series' }, z: s.year === newest ? 3 : 2,
            };
          }),
        }, true);
      },
    }));
  });
})();
