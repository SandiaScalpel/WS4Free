/*
 * Neighbours page charts: your temperature / humidity against the neighbours'
 * average (highest and lowest left out) over the period, and the difference by hour of the day. Data from the
 * page's #neighbours-data JSON, already in the viewer's units. Reuses the
 * validated palette from charts.js (window.WS4FreeCharts): slot 1 = you,
 * slot 2 = neighbours, on every chart.
 */
(function () {
  'use strict';
  const FONT = 'Inter, system-ui, sans-serif';
  const GAP_MS = 30 * 60 * 1000;          // longer than this without a match breaks the lines
  const token = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const theme = () => (document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
  const ink = () => ({ surface: token('--ws-surface'), line: token('--ws-line'), ink: token('--ws-ink'), muted: token('--ws-muted') });
  const fmt = (v, d) => (v == null ? '—' : Number(v).toFixed(d));
  const signed = (v, d) => (v == null ? '—' : (v > 0 ? '+' : v < 0 ? '−' : '±') + Math.abs(v).toFixed(d));
  const hourLabel = (h) => { h = Number(h); return h === 0 ? '12a' : h === 12 ? '12p' : h < 12 ? `${h}a` : `${h - 12}p`; };

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

  // [t, you, neighbours, n] points with nulls where matches stop for a while.
  function points(rows, ours, theirs, count) {
    const you = [], them = [];
    let last = null;
    rows.forEach((r) => {
      const t = Date.parse(r[0]);
      if (last !== null && t - last > GAP_MS) { you.push([t - 1, null]); them.push([t - 1, null, 0]); }
      you.push([t, r[ours]]);
      them.push([t, r[theirs], r[count]]);
      last = t;
    });
    return [you, them];
  }

  function series(el, data, ours, theirs, count, unit, digits) {
    const c = ink(), pal = window.WS4FreeCharts.CATEGORICAL[theme()];
    const [you, them] = points(data.rows, ours, theirs, count);
    const when = new Intl.DateTimeFormat(undefined, { timeZone: data.tz, weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    const line = (name, d, color) => ({ name, type: 'line', data: d, showSymbol: false, connectNulls: false, sampling: 'lttb',
                                        lineStyle: { width: 2, color }, itemStyle: { color }, emphasis: { disabled: true } });
    const ch = echarts.getInstanceByDom(el) || echarts.init(el);
    ch.setOption({
      animation: false, textStyle: { fontFamily: FONT },
      grid: { left: 44, right: 12, top: 30, bottom: 26 },
      legend: { top: 0, left: 0, icon: 'rect', itemWidth: 14, itemHeight: 3, textStyle: { color: c.muted, fontSize: 11 } },
      xAxis: Object.assign({ type: 'time' }, axis(c), { splitLine: { show: false },
        axisLabel: { color: c.muted, fontSize: 11, hideOverlap: true } }),
      yAxis: Object.assign({ type: 'value', scale: true }, axis(c), { axisLine: { show: false } }),
      tooltip: Object.assign(tooltip(c), {
        trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: c.muted, width: 1, type: 'solid' } },
        formatter: (ps) => {
          const mine = ps.find((p) => p.seriesIndex === 0), theirs = ps.find((p) => p.seriesIndex === 1);
          const a = mine && mine.value[1], b = theirs && theirs.value[1];
          let html = `<div style="color:${c.muted};margin-bottom:2px">${when.format(new Date(ps[0].value[0]))}</div>`;
          if (mine) html += row(mine.color, `${fmt(a, digits)} ${unit}`, 'You');
          if (theirs) html += row(theirs.color, `${fmt(b, digits)} ${unit}`, `Neighbours (average of ${theirs.value[2]})`);
          if (a != null && b != null) html += `<div style="color:${c.muted};margin-top:2px">Difference <b style="color:${c.ink}">${signed(a - b, digits)} ${unit}</b></div>`;
          return html;
        },
      }),
      series: [line('You', you, pal[0]), line('Neighbours (average)', them, pal[1])],
    }, true);
  }

  function hours(el, values, unit, digits) {
    const c = ink(), pal = window.WS4FreeCharts.CATEGORICAL[theme()];
    const ch = echarts.getInstanceByDom(el) || echarts.init(el);
    ch.setOption({
      animation: false, textStyle: { fontFamily: FONT },
      grid: { left: 44, right: 12, top: 12, bottom: 26 },
      xAxis: Object.assign({ type: 'category', data: values.map((_, h) => h) }, axis(c), {
        splitLine: { show: false },
        axisLabel: { color: c.muted, fontSize: 11, interval: (i) => Number(i) % 3 === 0, formatter: hourLabel },
      }),
      yAxis: Object.assign({ type: 'value' }, axis(c), { axisLine: { show: false },
                           axisLabel: { color: c.muted, fontSize: 11, formatter: (v) => signed(v, Number.isInteger(Math.round(v * 1000) / 1000) ? 0 : 1) } }),
      tooltip: Object.assign(tooltip(c), {
        trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: c.line, opacity: 0.4 } },
        formatter: (ps) => {
          const h = Number(ps[0].name), v = ps[0].value;
          const label = `${hourLabel(h)}m – ${hourLabel((h + 1) % 24)}m`;
          return `<div style="color:${c.muted};margin-bottom:2px">${label}</div>`
            + (v == null ? 'No matched readings' : `<b>${signed(v, digits)} ${unit}</b> <span style="color:${c.muted}">you minus neighbours</span>`);
        },
      }),
      series: [{
        type: 'bar', barMaxWidth: 24, barCategoryGap: '25%',
        itemStyle: { color: pal[0], borderRadius: [4, 4, 0, 0] }, emphasis: { disabled: true },
        // Bars below zero round their bottom ends instead.
        data: values.map((v) => (v != null && v < 0 ? { value: v, itemStyle: { borderRadius: [0, 0, 4, 4] } } : v)),
      }],
    }, true);
  }

  function render() {
    const holder = document.getElementById('neighbours-data');
    if (!holder || !window.echarts || !window.WS4FreeCharts) return;
    const data = JSON.parse(holder.textContent), u = data.units;
    const at = (name) => document.querySelector(`[data-n=${name}]`);
    if (at('temp-series')) series(at('temp-series'), data, 1, 2, 3, u.labels.temp, 1);
    if (at('humidity-series')) series(at('humidity-series'), data, 4, 5, 6, '%', 0);
    if (at('temp-hours')) hours(at('temp-hours'), data.by_hour.temp_c, u.labels.temp, 1);
    if (at('humidity-hours')) hours(at('humidity-hours'), data.by_hour.humidity, '%', 0);
  }

  document.addEventListener('DOMContentLoaded', render);
  window.addEventListener('ws4f:theme', render);
  window.addEventListener('resize', () => document.querySelectorAll('[data-n]').forEach((el) => {
    const ch = echarts.getInstanceByDom(el); if (ch) ch.resize();
  }));
})();
