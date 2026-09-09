// 今日速览 + 雷达区渲染逻辑验证（DOM 桩 + fetch 桩）
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.error('no script'); process.exit(1); }
const src = m[1];
const readJson = p => JSON.parse(fs.readFileSync(path.join(__dirname, '..', p), 'utf8'));

// —— DOM 桩 ——
function makeEl(tag) {
  const el = {
    tagName: tag, children: [], parentNode: null, hidden: false, type: '',
    _text: '', _html: '', _class: '', listeners: {}, _scrolled: 0,
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
    scrollIntoView() { this._scrolled++; },
  };
  return el;
}
const els = {};
function byId(id) { if (!els[id]) els[id] = makeEl('div'); return els[id]; }
['radar-grid','radar-more','radar-modal','radar-modal-body','radar-modal-close','radar-modal-mask',
 'radar-note','radar-more-label','radar-sec','sec-note','nav-time',
 'ov-cheap-val','ov-cheap-note','ov-drop-val','ov-drop-note','ov-new-val','ov-new-note',
 'ov-radar-val','ov-radar-note','ov-radar-card'].forEach(byId);
// 价格表行（给 applyLive 用，空表也允许——applyLive 在无行时只更新速览? 实际 rows 有数据、modelRows 空则 container null，跳过动态行）
const leads = {};
for (let i = 0; i < 6; i++) { const e = makeEl('span'); leads[i] = e; }

const priceData = readJson('data/prices.json');
global.document = {
  createElement: makeEl,
  getElementById: byId,
  querySelector: sel => {
    const mm = sel.match(/data-axis="(\d)"/);
    return mm ? leads[+mm[1]] : null;
  },
  querySelectorAll: () => [],
  addEventListener: () => {},
  body: { style: {} },
};
global.fetch = url => Promise.resolve({ ok: true, json: () => Promise.resolve(readJson(url)) });

eval(src);

// 页面脚本内部自带 fetch(prices.json)（已被桩接管），会自动调用 applyLive → updateOverviewLive

setTimeout(() => {
  let ok = true;
  const t = (name, cond, got) => { if (!cond) { ok = false; console.error('FAIL:', name, 'got:', got); } };

  // 卡1 最低综合成本（数据源：prices.json mix 最小 = Qwen3.8 Flash 0.23）
  t('卡1值', els['ov-cheap-val'].textContent === '$0.23', els['ov-cheap-val'].textContent);
  t('卡1注', els['ov-cheap-note'].textContent.includes('Qwen3.8 Flash'), els['ov-cheap-note'].textContent);
  // 卡2 最大降幅（Sol d30 = -28.9）
  t('卡2值', els['ov-drop-val'].textContent === '−28.9%', els['ov-drop-val'].textContent);
  t('卡2注', els['ov-drop-note'].textContent.includes('GPT-5.6 Sol'), els['ov-drop-note'].textContent);
  // 卡3 本月新上线（models.json 30 天内 discovered = 4 款）
  t('卡3值', els['ov-new-val'].textContent === '4 款', els['ov-new-val'].textContent);
  t('卡3注', els['ov-new-note'].textContent.includes('等 4 款'), els['ov-new-note'].textContent);
  // 卡4 能力雷达综合第一（GPT-6 Astra 87.2）+ 点击滚动到雷达区
  t('卡4值', els['ov-radar-val'].textContent === 'GPT-6 Astra', els['ov-radar-val'].textContent);
  t('卡4注', els['ov-radar-note'].textContent.includes('87.2'), els['ov-radar-note'].textContent);
  els['ov-radar-card'].listeners.click[0]();
  t('卡4点击滚动', els['radar-sec']._scrolled === 1, els['radar-sec']._scrolled);

  // 雷达区（前一轮回归继续有效）
  const cards = els['radar-grid'].children, chips = els['radar-more'].children;
  t('内嵌 6', cards.length === 6, cards.length);
  t('chips 14', chips.length === 14, chips.length);
  for (let i = 0; i < 6; i++) t('axis' + i, leads[i].textContent.startsWith('领先 '), leads[i].textContent);

  console.log(ok ? 'ALL PASS' : 'HAS FAILURES');
  process.exit(ok ? 0 : 1);
}, 80);
