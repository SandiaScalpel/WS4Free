/*
 * Growing page charts: accumulated GDD and chill by season (one line per season,
 * a season keeps its palette slot), and daily ET₀ vs rain columns. Reuses the
 * validated palette from charts.js (window.WS4FreeCharts).
 */
(function () {
  'use strict';
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const FONT = 'Inter, system-ui, sans-serif';
  const token = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const theme = () => (document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
  const ink = () => ({ surface: token('--ws-surface'), line: token('--ws-line'), ink: token('--ws-ink'), muted: token('--ws-muted') });
  const fmt = (v, d) => (v == null ? '—' : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));

  function tooltip(c) {
    return { backgroundColor: c.surface, borderColor: c.line, borderWidth: 1, padding: [8, 10], confine: true,
             textStyle: { color: c.ink, fontFamily: FONT, fontSize: 12 }, extraCssText: 'box-shadow: var(--ws-shadow); border-radius: 8px;' };
  }
  function row(color, value, name, h) {
    return `<div style="display:flex;align-items:center;gap:8px;line-height:1.6"><span style="display:inline-block;width:12px;height:${h || 2}px;border-radius:1px;background:${color}"></span><b>${value}</b><span style="color:${token('--ws-muted')}">${name}</span></div>`;
  }
  function axis(c) {
    return { axisLine: { lineStyle: { color: c.line } }, axisTick: { show: false },
             axisLabel: { color: c.muted, fontSize: 11, fontFamily: FONT }, splitLine: { lineStyle: { color: c.line, type: 'solid' } } };
  }

  // One line per season; x = day of season, labelled with the calendar date (non-leap year).
  function seasons(el, list, startMD, unit, digits, labelOf) {
    const c = ink(), pal = window.WS4FreeCharts.CATEGORICAL[theme()];
    const length = Math.max(0, ...list.map((s) => s.values.length));
    // Category values reach formatters as strings: coerce, or 1 + '120' becomes '1120'.
    const dayOf = (i) => new Date(Date.UTC(2001, startMD[0] - 1, startMD[1] + Number(i)));
    const shown = list.slice(-8);
    const newest = shown.length ? labelOf(shown[shown.length - 1]) : null;
    const ch = echarts.getInstanceByDom(el) || echarts.init(el);
    ch.setOption({
      animation: false, textStyle: { fontFamily: FONT },
      grid: { left: 56, right: 16, top: 34, bottom: 28 },
      legend: { type: 'scroll', top: 0, left: 0, right: 0, icon: 'rect', itemWidth: 14, itemHeight: 3, itemGap: 14,
                textStyle: { color: c.muted, fontSize: 11 }, pageIconColor: c.muted, pageTextStyle: { color: c.muted } },
      xAxis: Object.assign({ type: 'category', data: Array.from({ length }, (_, i) => i), boundaryGap: false }, axis(c), {
        splitLine: { show: false },
        axisLabel: { color: c.muted, fontSize: 11, interval: (i) => dayOf(i).getUTCDate() === 1 && (el.clientWidth > 560 || dayOf(i).getUTCMonth() % 2 === 0),
                     formatter: (i) => MONTHS[dayOf(i).getUTCMonth()] },
      }),
      yAxis: Object.assign({ type: 'value', min: 0 }, axis(c), { axisLine: { show: false },
                           axisLabel: { color: c.muted, fontSize: 11, formatter: (v) => fmt(v, 0) } }),
      tooltip: Object.assign(tooltip(c), {
        trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: c.muted, width: 1, type: 'solid' } },
        formatter: (ps) => {
          const d = dayOf(ps[0].dataIndex);
          let html = `<div style="color:${c.muted};margin-bottom:2px">${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}</div>`;
          ps.slice().reverse().forEach((p) => { if (p.value != null) html += row(p.color, `${fmt(p.value, digits)} ${unit}`, p.seriesName); });
          return html;
        },
      }),
      series: shown.map((s, i) => {
        const slot = (list.length - shown.length + i) % pal.length;     // a season keeps its colour
        const name = labelOf(s);
        return { name, type: 'line', data: s.values, showSymbol: false, connectNulls: false,
                 lineStyle: { width: name === newest ? 2.5 : 2, color: pal[slot] }, itemStyle: { color: pal[slot] },
                 emphasis: { focus: 'series' }, z: name === newest ? 3 : 2 };
      }),
    }, true);
  }

  function et0(el, days, units) {
    const c = ink(), pal = window.WS4FreeCharts.CATEGORICAL[theme()];
    const unit = units.labels.rain, digits = units.digits.rain + (units.rain === 'in' ? 1 : 0);
    const label = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' });
    const ch = echarts.getInstanceByDom(el) || echarts.init(el);
    ch.setOption({
      animation: false, textStyle: { fontFamily: FONT },
      grid: { left: 52, right: 12, top: 30, bottom: 26 },
      legend: { top: 0, right: 0, itemWidth: 12, itemHeight: 8, textStyle: { color: c.muted, fontSize: 11 } },
      xAxis: Object.assign({ type: 'category', data: days.map((d) => d[0]) }, axis(c), {
        splitLine: { show: false },
        axisLabel: { color: c.muted, fontSize: 11, hideOverlap: true,
                     formatter: (v) => { const d = new Date(v + 'T12:00:00Z'); return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`; } },
      }),
      yAxis: Object.assign({ type: 'value', min: 0 }, axis(c), { axisLine: { show: false },
                           axisLabel: { color: c.muted, fontSize: 11, formatter: (v) => fmt(v, units.digits.rain) } }),
      tooltip: Object.assign(tooltip(c), {
        trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: c.line, opacity: 0.4 } },
        formatter: (ps) => {
          const i = ps[0].dataIndex, d = days[i];
          let html = `<div style="color:${c.muted};margin-bottom:2px">${label.format(new Date(d[0] + 'T12:00:00Z'))}</div>`;
          ps.forEach((p) => { html += row(p.color, p.value == null ? 'no data' : `${fmt(p.value, digits)} ${unit}`, p.seriesName, 8); });
          if (d[3] === 'hargreaves') html += `<div style="color:${c.muted};margin-top:2px">ET₀ estimated from temperature only</div>`;
          return html;
        },
      }),
      series: [
        { name: 'Rain', type: 'bar', data: days.map((d) => d[2]), barMaxWidth: 12, barGap: '15%',
          itemStyle: { color: pal[0], borderRadius: [4, 4, 0, 0] }, emphasis: { disabled: true } },
        { name: 'ET₀', type: 'bar', data: days.map((d) => d[1]), barMaxWidth: 12,
          itemStyle: { color: pal[1], borderRadius: [4, 4, 0, 0] }, emphasis: { disabled: true } },
      ],
    }, true);
  }

  function render() {
    const holder = document.getElementById('growing-data');
    if (!holder || !window.echarts || !window.WS4FreeCharts) return;
    const data = JSON.parse(holder.textContent);
    const gddEl = document.querySelector('[data-g=gdd]');
    if (gddEl) seasons(gddEl, data.gdd, data.gdd_start, data.deg_label, 0, (s) => String(s.year));
    const chillEl = document.querySelector('[data-g=chill]');
    if (chillEl) seasons(chillEl, data.chill, data.chill_start, data.chill_unit, 0, (s) => s.label);
    const etEl = document.querySelector('[data-g=et0]');
    if (etEl) et0(etEl, data.et0, data.units);
  }

  document.addEventListener('DOMContentLoaded', render);
  window.addEventListener('ws4f:theme', render);
  window.addEventListener('resize', () => document.querySelectorAll('[data-g]').forEach((el) => {
    const ch = echarts.getInstanceByDom(el); if (ch) ch.resize();
  }));
})();
