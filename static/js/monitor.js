// 监控页：一项一面板。目前是流量；新增监控项时在 PANELS 里加一个渲染函数即可。
import { $, el, api, toast } from './dom.js';

// 页面 CSP 不允许 style 属性：图例与提示里的色块用 .key.out / .key.in（app.css），这里的常量只用于 SVG fill。
// 强调式配色：出站是计费方向，用强调色；入站作为对照用低调的灰（灰色有意低于彩度下限）。
// 用 dataviz 校验脚本在面板底色 #1a1d26 上验证：两色亮度在暗色带内、对比度 ≥ 3:1，正常视觉 ΔE 20.9、色弱 ΔE 20.4。
const OUT = '#7783fe', IN = '#656878', GRID = '#2a2f3d';
const NS = 'http://www.w3.org/2000/svg';
const svg = (tag, attrs = {}) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };

export function bytes(n) {
  if (n == null) return '–';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0; while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i === 0 || n >= 100 ? Math.round(n) : n.toFixed(1)) + ' ' + units[i];
}
const rate = n => n == null ? '–' : bytes(n) + '/s';
const hourLabel = at => String(new Date(at * 1000).getHours()).padStart(2, '0') + ':00';

// 刻度取“整”值：1、2、5 × 1024 的幂，最多 4 格。
function niceMax(v) {
  if (!v) return 1024;
  const p = 1024 ** Math.floor(Math.log(v) / Math.log(1024));
  for (const m of [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000]) if (m * p >= v) return m * p;
  return v;
}

function tile(label, value, sub, primary) {
  return el('div', { class: 'stat' + (primary ? ' primary' : '') },
    el('div', { class: 'stat-l', text: label }), el('div', { class: 'stat-v', text: value }), sub ? el('div', { class: 'stat-s', text: sub }) : null);
}

function chart(hours) {
  const W = 720, H = 200, L = 56, R = 8, T = 10, B = 24, w = W - L - R, h = H - T - B;
  const max = niceMax(Math.max(...hours.map(x => Math.max(x.tx, x.rx))));
  const band = w / hours.length, bar = Math.min(10, (band - 6) / 2);
  const y = v => T + h - (v / max) * h;
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'traffic-svg', role: 'img', 'aria-label': '最近 24 小时每小时出站与入站流量' });
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i, yy = y(v);
    root.append(svg('line', { x1: L, x2: W - R, y1: yy, y2: yy, stroke: GRID, 'stroke-width': 1 }));
    const t = svg('text', { x: L - 8, y: yy + 4, class: 'tick', 'text-anchor': 'end' }); t.textContent = i ? bytes(v) : '0'; root.append(t);
  }
  // 柱：顶端 4px 圆角，底部方角贴着基线；同一小时的两根柱之间留 2px 背景色间隙。
  const column = (x, v, color) => {
    if (!v) return null;
    const top = y(v), r = Math.min(4, bar / 2, (T + h - top));
    return svg('path', { fill: color, d: `M${x},${T + h}V${top + r}q0,-${r} ${r},-${r}h${bar - 2 * r}q${r},0 ${r},${r}V${T + h}Z` });
  };
  const tip = el('div', { class: 'traffic-tip', role: 'status', hidden: true });
  hours.forEach((x, i) => {
    const x0 = L + band * i, cx = x0 + band / 2;
    const g = svg('g', { class: 'hour', tabindex: 0, 'aria-label': `${hourLabel(x.at)}，出站 ${bytes(x.tx)}，入站 ${bytes(x.rx)}` });
    // 命中区域是整个小时的竖条，比柱子大得多。
    g.append(svg('rect', { x: x0, y: T, width: band, height: h, class: 'hit' }));
    for (const n of [column(cx - bar - 1, x.tx, OUT), column(cx + 1, x.rx, IN)]) if (n) g.append(n);
    const show = () => {
      tip.replaceChildren(el('div', { class: 'tip-h', text: hourLabel(x.at) + (i === hours.length - 1 ? '（本小时）' : '') }),
        el('div', { class: 'tip-r' }, el('span', { class: 'key out' }), el('strong', { text: bytes(x.tx) }), el('span', { class: 'muted', text: '出站（计费）' })),
        el('div', { class: 'tip-r' }, el('span', { class: 'key in' }), el('strong', { text: bytes(x.rx) }), el('span', { class: 'muted', text: '入站' })));
      tip.hidden = false;
      // 按像素夹在图表宽度内：窄屏上百分比定位会让提示框超出对话框。
      const box = tip.parentElement.clientWidth, half = tip.offsetWidth / 2;
      tip.style.left = Math.min(Math.max((cx / W) * box, half), box - half) + 'px';
      g.classList.add('on');
    };
    const hide = () => { tip.hidden = true; g.classList.remove('on'); };
    g.addEventListener('pointerenter', show); g.addEventListener('pointerleave', hide);
    g.addEventListener('focus', show); g.addEventListener('blur', hide);
    root.append(g);
    if (i % 6 === 0 || i === hours.length - 1) {
      const t = svg('text', { x: cx, y: H - 6, class: 'tick', 'text-anchor': 'middle' }); t.textContent = hourLabel(x.at); root.append(t);
    }
  });
  return el('div', { class: 'traffic-chart' }, root, tip);
}

function table(hours) {
  const rows = [...hours].reverse().map(x => el('tr', {}, el('td', { text: hourLabel(x.at) }), el('td', { text: bytes(x.tx) }), el('td', { text: bytes(x.rx) })));
  return el('details', { class: 'traffic-table' }, el('summary', { text: '查看表格' }),
    el('table', {}, el('thead', {}, el('tr', {}, el('th', { text: '小时' }), el('th', { text: '出站' }), el('th', { text: '入站' }))), el('tbody', {}, ...rows)));
}

const PANELS = {
  traffic(p) {
    const legend = el('div', { class: 'legend' },
      el('span', {}, el('span', { class: 'key out' }), '出站（其他机器从本机拉取，计费）'),
      el('span', {}, el('span', { class: 'key in' }), '入站'));
    return el('section', { class: 'panel' },
      el('header', { class: 'panel-h' }, el('h3', { text: '流量' }),
        el('span', { class: 'muted', text: `网卡 ${p.iface || '未知'}，整机计数（含本机所有服务）` })),
      el('div', { class: 'stats' },
        tile('今日出站', bytes(p.today.tx), '入站 ' + bytes(p.today.rx), true),
        tile(`本月出站（${p.month.label}）`, bytes(p.month.tx), '入站 ' + bytes(p.month.rx), true),
        tile('当前出站速率', rate(p.rate?.tx), '入站 ' + rate(p.rate?.rx))),
      el('div', { class: 'chart-h' }, el('span', { text: '最近 24 小时' }), legend),
      chart(p.hours), table(p.hours),
      p.since ? el('p', { class: 'muted note', text: '从 ' + new Date(p.since * 1000).toLocaleString() + ' 开始记录' }) : null);
  },
};

let timer = null;
async function load() {
  try {
    const data = await api('api/monitor');
    $('#monitor-body').replaceChildren(...Object.entries(data).map(([k, p]) => (PANELS[k] ? PANELS[k](p) : null)).filter(Boolean));
  } catch (e) { toast(e.message); }
}

export function initMonitor() {
  const d = $('#monitor-dialog');
  $('#monitor-open').onclick = () => { d.showModal(); load(); timer = setInterval(load, 30000); };
  $('#monitor-close').onclick = () => d.close();
  d.addEventListener('close', () => { clearInterval(timer); timer = null; });
}
