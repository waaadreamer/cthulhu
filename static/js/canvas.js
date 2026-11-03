// HTML 画布：Master 用 tell_owner(format='html') 写的可视化，在沙箱 iframe 里渲染。
// 安全边界见 static/canvas.html：iframe 只给 allow-scripts，不给 allow-same-origin，所以画布里的脚本
// 是不透明来源，读不到本页的 localStorage（凭证）、DOM，也不能导航本页或弹窗。
// 不要改用 srcdoc 或 blob: 地址：那样会继承本页来源或 CSP，要么画布被禁脚本，要么凭证暴露。
import { el } from './dom.js';

const HOST = new URL('../canvas.html', import.meta.url).href;
const MAX = 2400;           // 画布最高多少像素，再高就在框内滚动，免得一张图把会话撑得翻不动
const frames = new Set();   // { html, frame, at }

// 全局只挂一个监听：按消息来源窗口找到对应画布。宿主页先报 ready 再收 HTML，iframe 的 load 事件
// 和脚本执行谁先谁后不确定，以宿主页自己的 ready 为准就没有竞态。
// 第一次建画布时才挂（history.js 等模块也会在 node 测试里被导入，那里没有 window）。
let listening = false;
function listen() {
  if (listening) return;
  listening = true;
  addEventListener('message', e => {
    if (!e.data || !e.source) return;
    const f = [...frames].find(f => f.frame.contentWindow === e.source);
    if (!f) return;
    if (e.data.canvasReady) e.source.postMessage({ html: f.html }, '*');
    else if (typeof e.data.canvasHeight === 'number') f.frame.style.height = Math.min(MAX, Math.max(40, e.data.canvasHeight)) + 'px';
  });
}

export function canvas(html) {
  listen();
  const frame = el('iframe', { class: 'canvas-frame', sandbox: 'allow-scripts', src: HOST, title: 'Master 的 HTML 画布',
    referrerpolicy: 'no-referrer', loading: 'lazy' });
  const src = el('pre', { class: 'code canvas-src', hidden: true, text: html });
  const box = el('div', { class: 'canvas' });
  const full = el('button', { class: 'link', type: 'button', text: '全屏', onclick: () => {
    const on = box.classList.toggle('full'); full.textContent = on ? '退出全屏' : '全屏';
  } });
  const view = el('button', { class: 'link', type: 'button', text: '源码', onclick: () => {
    src.hidden = !src.hidden; view.textContent = src.hidden ? '源码' : '收起源码';
  } });
  box.append(el('div', { class: 'canvas-bar' }, el('span', { class: 'canvas-k', text: 'HTML 画布' }), full, view), frame, src);
  // 已移出页面的画布不再需要；新建时顺手清掉（刚建、还没插入页面的不算）。
  const now = Date.now();
  for (const f of frames) if (!f.frame.isConnected && now - f.at > 10000) frames.delete(f);
  frames.add({ html, frame, at: now });
  return box;
}
