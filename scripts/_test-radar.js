// 雷达区渲染逻辑验证（DOM 桩 + fetch 桩）
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.error('no script'); process.exit(1); }
const src = m[1];

// —— DOM 桩 ——
function makeEl(tag) {
  const el = {
    tagName: tag, children: [], parentNode: null, hidden: false, type: '',
    _text: '', _html: '', _class: '', listeners: {},
    get className() { return this._class; },
    set className(v) { this._class = v; },
    get classList() {
      const self = this;
      return {
        contains: c => self._class.split(/\s+/).includes(c),
        add: (...c) => { const s = new Set(self._class.split(/\s+/).filter(Boolean)); c.forEach(x => s.add(x)); self._class = [...s].join(' '); },
        remove: (...c) => { const s = new Set(self._class.split(/\s+/).filter(Boolean)); c.forEach(x => s.delete(x)); self._class = [...s].join(' '); },
      };
    },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this._text = String(v).replace(/<[^>]*>/g, ''); },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    setAttribute() {},
  };
  return el;
}
const els = {};
function byId(id) { if (!els[id]) els[id] = makeEl('div'); return els[id]; }
['radar-grid','radar-more','radar-modal','radar-modal-body','radar-modal-close','radar-modal-mask','radar-note','radar-more-label'].forEach(byId);
// 六维领先卡
const leads = {};
for (let i = 0; i < 6; i++) { const e = makeEl('span'); e._axis = i; leads[i] = e; }

global.document = {
  createElement: makeEl,
  getElementById: byId,
  querySelector: sel => {
    const mm = sel.match(/data-axis="(\d)"/);
    return mm ? leads[+mm[1]] : null;
  },
  addEventListener: () => {},
  body: { style: {} },
};
global.fetch = () => Promise.resolve({
  ok: true,
  json: () => Promise.resolve(JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'models.json'), 'utf8'))),
});

eval(src);

setTimeout(() => {
  const grid = els['radar-grid'], more = els['radar-more'];
  const cards = grid.children, chips = more.children;
  console.log('内嵌雷达卡:', cards.length, '| chips:', chips.length);
  console.log('note:', els['radar-note'].textContent);
  console.log('label:', els['radar-more-label'].textContent);
  const cardNames = cards.map(c => (c.innerHTML.match(/rc-name">([^<]+)</) || [])[1]);
  console.log('内嵌 6 款:', cardNames.join(', '));
  const chipNames = chips.map(c => c.textContent);
  console.log('chips:', chipNames.join(' | '));
  for (let i = 0; i < 6; i++) console.log('axis' + i, leads[i].textContent);

  let ok = true;
  if (cards.length !== 6) { ok = false; console.error('FAIL: 内嵌应为 6'); }
  if (chips.length !== 20 - 6) { ok = false; console.error('FAIL: chips 应为 14'); }
  // 新模型 GPT-6 Astra / Astra Pro 综合分最高，必须内嵌且带「新」徽章
  if (!cardNames[0] || !cardNames[0].includes('GPT-6 Astra') || !cards[0].innerHTML.includes('rc-new')) { ok = false; console.error('FAIL: GPT-6 Astra 应内嵌带新徽章, got', cardNames[0]); }
  if (!cardNames.some(n => n && n.includes('Astra Pro'))) { ok = false; console.error('FAIL: Astra Pro 应内嵌'); }
  if (!chipNames.some(n => n.includes('（新）'))) { ok = false; console.error('FAIL: chips 中应有带（新）的新模型'); }
  // 领先卡已填充
  for (let i = 0; i < 6; i++) if (!leads[i].textContent.startsWith('领先 ')) { ok = false; console.error('FAIL: axis' + i + ' 未填充'); }
  // 弹层：点击一个 chip
  chips[0].listeners.click[0]();
  if (els['radar-modal'].hidden !== false) { ok = false; console.error('FAIL: 弹层未打开'); }
  console.log(ok ? 'ALL PASS' : 'HAS FAILURES');
  process.exit(ok ? 0 : 1);
}, 50);
