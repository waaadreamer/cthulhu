// Markdown 块级解析：文本 → 块树。纯函数、不碰 DOM，便于在 node 下单测；渲染在 dom.js。
// 覆盖模型常写的 GFM 子集：围栏代码、$$ 公式、表格、多级列表与任务列表、引用、标题、分隔线。
// 流式输出时文本可能停在半截：未闭合的围栏、公式按“到末尾为止”处理，下一帧补全后自然变对。

const FENCE = /^( *)(`{3,}|~{3,})\s*([^\s`]*)/;
const ITEM = /^( *)([-*+]|\d{1,9}[.)])(?:[ ]+(.*)|$)/;
const HR = /^ {0,3}([-*_])(?:[ ]*\1){2,}[ ]*$/;
const HEAD = /^ {0,3}(#{1,6})[ ]+(.*?)(?:[ ]+#+)?[ ]*$/;
const QUOTE = /^ {0,3}>[ ]?/;
const DELIM = /^ *\|? *:?-+:? *(?:\| *:?-+:? *)*\|? *$/;
const MATH = /^ *(\$\$|\\\[)(.*)$/;

const indent = l => l.match(/^ */)[0].length;
const blank = l => !l.trim();

// 按未转义、不在反引号里的 | 切分表格行。
export function cells(row) {
  let s = row.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const out = []; let cur = '', tick = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === '\\' && s[i + 1] === '|') { cur += '|'; i++; continue; }
    if (c === '`') tick = !tick;
    if (c === '|' && !tick) { out.push(cur.trim()); cur = ''; continue; }
    cur += c;
  }
  out.push(cur.trim());
  return out;
}

// 表头行后紧跟分隔行（|---|:--:|）才是表格；只有一列时分隔行必须带 |，免得把 “---” 下划线标题误认成表格。
const isTable = (lines, i) => i + 1 < lines.length && lines[i].includes('|') && lines[i + 1].includes('|') && DELIM.test(lines[i + 1]);

// 能打断段落的行：段落遇到它们就结束，不当成上一行的续行。
function starts(lines, i) {
  const l = lines[i];
  return FENCE.test(l) || MATH.test(l) || HR.test(l) || HEAD.test(l) || QUOTE.test(l) || ITEM.test(l) || isTable(lines, i);
}

export function parseBlocks(text) {
  return parse(String(text ?? '').replace(/\r\n?/g, '\n').replace(/\t/g, '    ').split('\n'));
}

function parse(lines) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (blank(l)) { i++; continue; }
    let m;
    if ((m = l.match(FENCE))) {
      // 闭合围栏须与开头同字符、不短于开头；缩进跟着开头去掉，保持代码原样。
      const [, pad, mark, lang] = m, body = [];
      for (i++; i < lines.length && !new RegExp(`^ {0,3}${mark[0] === '`' ? '`' : '~'}{${mark.length},}\\s*$`).test(lines[i]); i++)
        body.push(lines[i].slice(Math.min(pad.length, indent(lines[i]))));
      i++;
      out.push({ type: 'code', lang, text: body.join('\n') });
      continue;
    }
    if ((m = l.match(MATH))) {
      const close = m[1] === '$$' ? '$$' : '\\]', rest = m[2], body = [];
      if (rest.trimEnd().endsWith(close) && rest.trim().length >= close.length) {
        out.push({ type: 'math', text: rest.trimEnd().slice(0, -close.length).trim() }); i++; continue;
      }
      if (rest.trim()) body.push(rest);
      for (i++; i < lines.length; i++) {
        const k = lines[i].indexOf(close);
        if (k >= 0) { body.push(lines[i].slice(0, k)); i++; break; }
        body.push(lines[i]);
      }
      out.push({ type: 'math', text: body.join('\n').trim() });
      continue;
    }
    if (HR.test(l)) { out.push({ type: 'hr' }); i++; continue; }
    if ((m = l.match(HEAD))) { out.push({ type: 'heading', level: m[1].length, text: m[2] }); i++; continue; }
    if (QUOTE.test(l)) {
      const body = [];
      for (; i < lines.length && QUOTE.test(lines[i]); i++) body.push(lines[i].replace(QUOTE, ''));
      out.push({ type: 'quote', blocks: parse(body) });
      continue;
    }
    if (isTable(lines, i)) {
      const head = cells(l), align = cells(lines[i + 1]).map(c => c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : c.startsWith(':') ? 'left' : null);
      const rows = [];
      for (i += 2; i < lines.length && !blank(lines[i]) && lines[i].includes('|'); i++) {
        const r = cells(lines[i]);
        rows.push(head.map((_, k) => r[k] ?? ''));   // 列数以表头为准：多的丢弃，少的补空
      }
      out.push({ type: 'table', head, align: head.map((_, k) => align[k] ?? null), rows });
      continue;
    }
    if ((m = l.match(ITEM))) { i = list(lines, i, out); continue; }
    const para = [l.trim()];
    for (i++; i < lines.length && !blank(lines[i]) && !starts(lines, i); i++) para.push(lines[i].trim());
    out.push({ type: 'para', lines: para });
  }
  return out;
}

// 列表：同一缩进、同一种（有序/无序）的连续项为一组。项内比标记更深的行属于该项，递归解析出嵌套列表与段落；
// 模型常用 2 或 4 个空格缩进子列表，两种都按“比本项标记更深”处理。
function list(lines, i, out) {
  const first = lines[i].match(ITEM), base = first[1].length, ordered = /\d/.test(first[2]);
  const node = { type: 'list', ordered, start: ordered ? parseInt(first[2], 10) : null, items: [] };
  while (i < lines.length) {
    const m = lines[i].match(ITEM);
    if (!m || m[1].length !== base || /\d/.test(m[2]) !== ordered) break;
    const offset = Math.min(m[1].length + m[2].length + 1, base + 4), body = [m[3] ?? ''];
    for (i++; i < lines.length; i++) {
      const l = lines[i];
      if (blank(l)) {
        // 空行后若还有更深缩进的内容，仍属本项（项内多段）；否则列表或本项到此为止。
        let j = i + 1; while (j < lines.length && blank(lines[j])) j++;
        if (j < lines.length && indent(lines[j]) > base) { body.push(''); continue; }
        break;
      }
      if (indent(l) > base) { body.push(l.slice(Math.min(offset, indent(l)))); continue; }
      // 不缩进的续行（懒续行）只接在普通文字后面；遇到新块就结束本项。
      if (!starts(lines, i) && body.at(-1)?.trim()) { body.push(l.trim()); continue; }
      break;
    }
    let check = null;
    const t = body[0].match(/^\[([ xX])\][ ]+(.*)$/);
    if (t) { check = t[1] !== ' '; body[0] = t[2]; }
    node.items.push({ check, blocks: parse(body) });
    while (i < lines.length && blank(lines[i]) && lines[i + 1] !== undefined && ITEM.test(lines[i + 1]) && indent(lines[i + 1]) === base) i++;
  }
  out.push(node);
  return i;
}
