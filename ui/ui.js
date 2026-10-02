'use strict';
/* 学习通作业助手 · 前端 */
const $ = s => document.querySelector(s);
const token = $('meta[name="app-token"]').content;
const GROUPS = {active: '待完成', review: '待核实', history: '历史'};
const TYPES = {single: '单选题', multiple: '多选题', judgment: '判断题', blank: '填空题', essay: '简答题', upload: '附件题', unsupported: '未识别题型'};
const SITES = {chaoxing: '学习通主站', school: '学校学习通'};
const SUBS = {
  review: [['all', '全部'], ['deadline', '截止时间待核实'], ['status', '提交状态待核实']],
  history: [['all', '全部'], ['expired', '已截止'], ['submitted', '已提交'], ['completed', '本机已完成']],
};
const MAX_FILE = 30 * 1024 * 1024;
const NO_SUBMIT = '填入后请在学习通核对并提交。';

const S = {
  records: [], aliases: {}, key: null, group: 'active', sub: 'all',
  progressFilter: 'all', dueFilter: 'all',
  loaded: false, loadError: null, lastSuccess: null, warnings: [],
  job: {busy: false, action: '', message: ''}, jobKey: null, revision: -1,
  auth: null, reminder: null, online: navigator.onLine, serverLost: false,
  selecting: 0, printed: null, statusMemo: '', exited: false,
};
const drafts = new Map(), dirty = new Map(), timers = new Map(), saving = new Map(), saveState = new Map();
const draftChanges = new Map();
let loginPhase = 'form', loginError = '';
let fillPhase = 'check', fillResult = null;
let accountMenuOpen = false;

/* ---------- Small helpers ---------- */
const ICONS = {
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  back: '<path d="M15 18l-6-6 6-6"/>',
  chevron: '<path d="M9 18l6-6-6-6"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  clip: '<path d="M21 11.5l-8.5 8.5a5 5 0 0 1-7-7L14 4.5a3.5 3.5 0 0 1 5 5L10.5 18a2 2 0 0 1-3-3l7.5-7.5"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4"/><path d="M12 17.5v.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8v.01"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  bell: '<path d="M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  pen: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  offline: '<path d="M3 3l18 18"/><path d="M8.5 16a5 5 0 0 1 7 0"/><path d="M5 12.5a10 10 0 0 1 4-2.3M19 12.5a10 10 0 0 0-3.5-2.1"/><path d="M12 20v.01"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  logout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="M10 16l-4-4 4-4"/><path d="M6 12h10"/>',
  loader: '<path d="M12 3a9 9 0 1 0 9 9"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  home: '<path d="M4 10.5L12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5.5h-5V20H5a1 1 0 0 1-1-1z"/>',
  sparkle: '<path d="M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9L12 17.5l-1.9-5.1L5 10.5l5.1-1.9z"/><path d="M18.5 16l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M4.2 6.2l2.1 2.1M17.7 15.7l2.1 2.1M2.5 12h3M18.5 12h3M4.2 17.8l2.1-2.1M17.7 8.3l2.1-2.1"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  trash: '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4h6v3"/>',
  calendar: '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
  flag: '<path d="M5 21V4"/><path d="M5 4h11l-2 4 2 4H5"/>',
  inbox: '<path d="M4 13l2.5-8h11L20 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z"/><path d="M4 13h5l1 2h4l1-2h5"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  undo: '<path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/>',
};
function icon(name, cls = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', ('icon ' + cls + (name === 'loader' ? ' spin' : '')).trim());
  svg.innerHTML = ICONS[name] || '';
  return svg;
}
function spark(cls = 'spark') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', cls);
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = '<g stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 3v6.2"/><path d="M12 14.8V21"/><path d="M3 12h6.2"/><path d="M14.8 12H21"/><path d="M5.6 5.6l4.4 4.4"/><path d="M14 14l4.4 4.4"/><path d="M18.4 5.6L14 10"/><path d="M10 14l-4.4 4.4"/></g>';
  return svg;
}
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}
function btn(label, cls, onclick, iconName) {
  const b = el('button', 'btn ' + (cls || ''));
  b.type = 'button';
  if (iconName) b.append(icon(iconName));
  if (label) b.append(el('span', '', label));
  if (onclick) b.onclick = onclick;
  return b;
}
// 统计卡片：renderWelcome 与 renderLogin 原本各自内联了一份逐字符相同的实现。
function statCard(value, label, tone = '') {
  const card = el('div', 'stat ' + tone);
  card.append(el('b', '', String(value)), el('small', '', label));
  return card;
}
function externalLink(href, label, cls = 'link-chip') {
  const a = el('a', cls);
  a.href = href;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.append(icon('external'), el('span', '', label));
  return a;
}
function toast(message, tone = '') {
  const box = $('#toasts');
  // The same message twice in a row replaces the first instead of stacking.
  for (const old of box.children) if (old.dataset.message === message) old.remove();
  const t = el('div', 'toast ' + tone);
  t.dataset.message = message;
  t.setAttribute('role', tone === 'error' ? 'alert' : 'status');
  t.append(icon(tone === 'error' ? 'alert' : tone === 'warn' ? 'info' : 'check'), el('span', '', message));
  const dismiss = () => { t.classList.add('leaving'); setTimeout(() => t.remove(), 200); };
  if (tone === 'error' || tone === 'warn') {
    const x = el('button', 'toast-x');
    x.type = 'button';
    x.setAttribute('aria-label', '关闭提示');
    x.append(icon('x'));
    x.onclick = dismiss;
    t.append(x);
  }
  box.append(t);
  while (box.children.length > 3) box.firstChild.remove();
  setTimeout(dismiss, tone === 'error' ? 7000 : tone === 'warn' ? 4500 : 2600);
}
async function api(url, data, raw = false) {
  const opts = {credentials: 'same-origin', cache: 'no-store'};
  if (data !== undefined) {
    opts.method = 'POST';
    opts.headers = {'X-App-Token': token};
    if (!raw) opts.headers['Content-Type'] = 'application/json';
    opts.body = raw ? data : JSON.stringify(data);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch {
    throw Object.assign(Error('无法连接本地助手服务，请确认助手仍在运行'), {offline: true});
  }
  let result = {};
  try { result = await res.json(); } catch {}
  if (!res.ok) throw Object.assign(Error(result.error || '操作失败，请重试'), {status: res.status});
  return result;
}

/* ---------- Dates ---------- */
const pad = n => String(n).padStart(2, '0');
function parseDate(v) {
  if (!v) return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}
function hm(d) { return pad(d.getHours()) + ':' + pad(d.getMinutes()); }
function dayLabel(d) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((day - today) / 864e5);
  if (diff === 0) return '今天';
  if (diff === 1) return '明天';
  if (diff === 2) return '后天';
  if (diff === -1) return '昨天';
  return (d.getFullYear() !== today.getFullYear() ? d.getFullYear() + '年' : '') + (d.getMonth() + 1) + '月' + d.getDate() + '日';
}
function stamp(v) {
  const d = parseDate(v);
  return d ? dayLabel(d) + ' ' + hm(d) : '';
}
function left(ms) {
  const m = Math.floor(ms / 6e4), h = Math.floor(m / 60), d = Math.floor(h / 24);
  if (d >= 1) return `还剩 ${d} 天${h % 24 ? ' ' + (h % 24) + ' 小时' : ''}`;
  if (h >= 1) return `还剩 ${h} 小时${m % 60 ? ' ' + (m % 60) + ' 分' : ''}`;
  return `还剩 ${Math.max(1, m)} 分钟`;
}
function due(r) {
  const d = parseDate(r.deadline);
  if (!d) {
    if (r.deadline_kind === 'none') return {text: '平台未设置截止时间', short: '不限截止', tone: ''};
    return {text: '截止时间待核实', short: '截止待核实', tone: 'unknown'};
  }
  const diff = d - Date.now();
  if (diff <= 0) return {text: stamp(r.deadline) + ' 已截止', short: dayLabel(d) + '截止', tone: 'past'};
  const tone = diff < 864e5 ? 'urgent' : diff < 3 * 864e5 ? 'soon' : '';
  return {text: stamp(r.deadline) + ' 截止', short: stamp(r.deadline), tone, left: left(diff)};
}

/* ---------- Record status ---------- */
function reviewReasons(r) {
  const out = [];
  if (!parseDate(r.deadline) && r.deadline_kind !== 'none') out.push('deadline');
  if (r.status !== 'pending' && r.status !== 'submitted') out.push('status');
  return out;
}
function statusInfo(r) {
  if (r.local_settings?.completed) return {label: '已完成 · 本机标记', tone: 'muted'};
  if (r.group === 'active') return {label: '未截止 · 未提交', tone: 'ok'};
  if (r.group === 'review') {
    const reasons = reviewReasons(r).map(x => x === 'deadline' ? '截止时间待核实' : '提交状态待核实');
    return {label: reasons.join(' · ') || '信息待核实', tone: 'warn'};
  }
  if (r.status === 'submitted') return {label: '已提交', tone: 'muted'};
  if (r.availability === 'closed') return {label: '平台已关闭', tone: 'muted'};
  return {label: '已截止', tone: 'muted'};
}
function matchesSub(r, id) {
  if (id === 'all') return true;
  if (r.group === 'review') return reviewReasons(r).includes(id);
  if (r.group === 'history') {
    if (id === 'completed') return !!r.local_settings?.completed;
    if (id === 'submitted') return r.status === 'submitted';
    return !r.local_settings?.completed && r.status !== 'submitted';
  }
  return true;
}
function sorter(group) {
  return (a, b) => {
    const x = a.deadline || '', y = b.deadline || '';
    if (group === 'history') return y.localeCompare(x);
    return (x || '9999').localeCompare(y || '9999');
  };
}
function sectionOf(r) {
  const d = parseDate(r.deadline);
  if (!d) return '不限截止时间';
  const diff = d - Date.now();
  if (diff < 864e5) return '24 小时内截止';
  if (diff < 3 * 864e5) return '3 天内截止';
  if (diff < 7 * 864e5) return '一周内截止';
  return '更晚截止';
}
function canonicalKey(key) {
  const seen = new Set();
  while (S.aliases[key] && !seen.has(key)) { seen.add(key); key = S.aliases[key]; }
  return key;
}
function record() { return S.records.find(r => r.key === S.key); }
function isUnsupported(q) {
  return !TYPES[q.type] || q.type === 'unsupported' || (['single', 'multiple'].includes(q.type) && !q.options?.length);
}

/* ---------- Answers ---------- */
function hasValue(v) {
  return Array.isArray(v) ? v.some(x => String(x ?? '').trim()) : String(v ?? '').trim() !== '';
}
function answered(a) { return !!a && (hasValue(a.value) || !!a.files?.length); }
function changedAnswer(q, a) { return !!a && a.signature !== q.signature && hasValue(a.value); }
// Returns a copy that can be edited for the current question version. A value
// saved for an older version of the question is kept under `stale`, never lost.
function editable(q, draft) {
  const a = draft.answers[q.id];
  if (!a) return {value: '', signature: q.signature, files: []};
  if (a.signature === q.signature) return {...a};
  const next = {...a, value: '', signature: q.signature, files: a.files || []};
  if (hasValue(a.value)) next.stale = {value: a.value, signature: a.signature || null};
  return next;
}
function qState(q, a) {
  if (isUnsupported(q)) return a && hasValue(a.value) ? ['draft', '已记草稿 · 不自动填入'] : ['unsupported', '需在学习通手动完成'];
  if (changedAnswer(q, a)) return ['changed', '题目内容已变化'];
  if (a && a.signature === q.signature && answered(a)) return HomeworkWorkspace.questionComplete(q, a)
    ? ['done', a.files?.length && !hasValue(a.value) ? '已添加附件' : '已作答'] : ['draft', '部分作答'];
  return ['empty', '未作答'];
}
function showValue(v) {
  if (Array.isArray(v)) return v.map((x, i) => v.length > 1 ? `第 ${i + 1} 空：${x || '（空）'}` : String(x)).join('\n');
  return String(v ?? '');
}

/* ---------- Draft persistence ---------- */
function discardHistoryDraft(key) {
  for (const old of new Set([key, ...Object.keys(S.aliases).filter(k => canonicalKey(k) === key)])) {
    clearTimeout(timers.get(old)); timers.delete(old);
    dirty.delete(old); drafts.delete(old); saveState.delete(old); draftChanges.delete(old);
    try { localStorage.removeItem('homework-recovery:' + old); } catch {}
  }
}
function removeAttachmentFromRecovery(key, qid, fileId) {
  const keys = [...new Set([key, ...Object.keys(S.aliases).filter(k => canonicalKey(k) === key)])];
  for (const recoveryKey of keys) {
    const storageKey = 'homework-recovery:' + recoveryKey;
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
      const answer = saved?.draft?.answers?.[qid];
      if (!answer?.files?.length) continue;
      const files = answer.files.filter(file => file.id !== fileId);
      if (files.length === answer.files.length) continue;
      answer.files = files;
      localStorage.setItem(storageKey, JSON.stringify(saved));
    } catch {}
  }
}
function persistRecovery(key) {
  try {
    localStorage.setItem('homework-recovery:' + key, JSON.stringify({draft: draftPayload(key), savedAt: Date.now()}));
  } catch {
    toast('浏览器恢复副本不可用，请等到显示“已保存到本机”后再关闭窗口', 'warn');
  }
}
function setSave(key, state, extra = {}) {
  saveState.set(key, {state, ...extra});
  if (key === S.key) renderSave();
}
function draftPayload(key) {
  return {...structuredClone(drafts.get(key)), _changed_questions: [...(draftChanges.get(key)?.keys() || [])]};
}
function markDirty(key, qid = null) {
  dirty.set(key, (dirty.get(key) || 0) + 1);
  if (!draftChanges.has(key)) draftChanges.set(key, new Map());
  for (const id of qid === null ? Object.keys(drafts.get(key)?.answers || {}) : qid ? [String(qid)] : []) draftChanges.get(key).set(id, dirty.get(key));
  persistRecovery(key);
  setSave(key, 'saving');
  clearTimeout(timers.get(key));
  timers.set(key, setTimeout(() => flush(key).catch(() => {}), 450));
  renderList();
}
async function flush(key) {
  if (S.records.find(r => r.key === canonicalKey(key))?.group === 'history') { discardHistoryDraft(canonicalKey(key)); return; }
  clearTimeout(timers.get(key));
  if (saving.has(key)) {
    await saving.get(key).catch(() => {});
    if (dirty.has(key)) return flush(key);
    return;
  }
  if (!dirty.has(key)) return;
  const generation = dirty.get(key), payload = draftPayload(key);
  setSave(key, 'saving');
  const pending = api('/api/draft', {key, draft: payload}).then(result => {
    for (const [qid, version] of draftChanges.get(key) || []) if (version <= generation) draftChanges.get(key).delete(qid);
    if (dirty.get(key) === generation) {
      dirty.delete(key);
      if (drafts.has(key)) drafts.get(key).updated_at = result.updated_at;
      try { localStorage.removeItem('homework-recovery:' + key); } catch {}
      setSave(key, 'saved', {at: result.updated_at});
    }
  }).catch(e => {
    if (e.message.includes('历史作业仅保留基本信息')) { discardHistoryDraft(canonicalKey(key)); return; }
    setSave(key, 'failed', {error: e.message});
    clearTimeout(timers.get(key));
    timers.set(key, setTimeout(() => flush(key).catch(() => {}), 8000));
    throw e;
  }).finally(() => saving.delete(key));
  saving.set(key, pending);
  await pending;
  if (dirty.has(key)) return flush(key);
}
async function flushAll() { await Promise.all([...dirty.keys()].map(flush)); }
async function getDraft(key) {
  if (S.records.find(r => r.key === key)?.group === 'history') {
    discardHistoryDraft(key);
    return {answers: {}};
  }
  if (drafts.has(key)) return drafts.get(key);
  const draft = await api('/api/draft?key=' + encodeURIComponent(key));
  draft.answers ??= {};
  let recovered = false;
  try {
    const keys = [...new Set([key, ...Object.keys(S.aliases).filter(k => canonicalKey(k) === key)])];
    const recoveries = keys
      .map(k => ({key: k, data: JSON.parse(localStorage.getItem('homework-recovery:' + k))}))
      .filter(r => r.data?.draft && r.data.savedAt > Math.max(new Date(draft.updated_at || 0).getTime(), new Date(S.records.find(x => x.key === key)?.history_cleaned_at || 0).getTime()))
      .sort((a, b) => a.data.savedAt - b.data.savedAt);
    for (const {data} of recoveries) {
      const changes = data.draft._changed_questions || Object.keys(data.draft.answers || {});
      for (const qid of changes) if (data.draft.answers?.[qid]) draft.answers[qid] = data.draft.answers[qid];
      if (data.draft.legacy_text) draft.legacy_text = data.draft.legacy_text;
      recovered = true;
    }
    drafts.set(key, draft);
    if (recovered) {
      markDirty(key);
      for (const {key: oldKey} of recoveries) if (oldKey !== key) localStorage.removeItem('homework-recovery:' + oldKey);
      toast('已恢复上次未完成保存的答案', 'warn');
    }
  } catch {}
  drafts.set(key, draft);
  if (!saveState.has(key)) saveState.set(key, {state: 'saved', at: draft.updated_at});
  return draft;
}

/* ---------- Rich content ---------- */
function mediaFallback(kind, src) {
  const box = el('div', 'media-fallback');
  box.append(icon('image'));
  if (kind === 'uncached') {
    box.append(el('span', 'mf-text', '图片未下载'));
    box.append(externalLink(src, '打开原图', 'btn btn-sm'));
  } else if (kind === 'media') {
    box.append(el('span', 'mf-text', '音视频需在原网页播放'));
    box.append(externalLink(src, '打开音视频', 'btn btn-sm'));
  } else {
    box.append(el('span', 'mf-text', '图片加载失败'));
    const reread = btn('重新读取', 'btn-sm', null, 'refresh'); reread.dataset.act = 'reread';
    const open = btn('查看原题', 'btn-sm', null, 'external'); open.dataset.act = 'open';
    box.append(reread, open);
  }
  return box;
}
function renderRich(node, html, text) {
  node.classList.add('rich');
  if (!html) {
    node.textContent = text || '';
    return;
  }
  node.innerHTML = html;
  node.querySelectorAll('script,style,iframe,object,embed,form,input,button,textarea,select').forEach(n => n.remove());
  node.querySelectorAll('*').forEach(n => {
    for (const attr of [...n.attributes]) if (/^on/i.test(attr.name) || attr.name === 'style') n.removeAttribute(attr.name);
  });
  node.querySelectorAll('a').forEach(a => {
    const href = a.getAttribute('href') || '';
    if (!/^https?:\/\//i.test(href)) { a.removeAttribute('href'); return; }
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
  });
  node.querySelectorAll('img').forEach(img => {
    const src = img.getAttribute('src') || '';
    if (/^https?:\/\//i.test(src)) { img.replaceWith(mediaFallback('uncached', src)); return; }
    img.decoding = 'async';
    img.onload = () => { if (img.naturalWidth > 160 || img.naturalHeight > 60) img.classList.add('block'); };
    img.onerror = () => img.replaceWith(mediaFallback('failed'));
    if (img.complete && img.naturalWidth) img.onload();
  });
  node.querySelectorAll('audio,video').forEach(media => {
    const src = media.getAttribute('src') || media.querySelector('source')?.getAttribute('src') || '';
    if (/^https?:\/\//i.test(src)) media.replaceWith(mediaFallback('media', src));
    else media.remove();
  });
}

/* ---------- Sidebar ---------- */
function progressFor(r) {
  return drafts.has(r.key) ? HomeworkWorkspace.progress(r, drafts.get(r.key)) : r.progress || HomeworkWorkspace.progress(r);
}
function overview() {
  return HomeworkWorkspace.dashboard(S.records.map(r => ({...r, progress: progressFor(r)})));
}
function showWorkspaceList(group, progress = 'all', dueRange = 'all') {
  S.group = group; S.sub = 'all'; S.progressFilter = progress; S.dueFilter = dueRange;
  $('#search').value = '';
  renderList(); setView('list');
}
async function goHome() {
  ++S.selecting; S.key = null; S.printed = null;
  $('.main').querySelector('.reading')?.remove();
  setView('detail'); renderList(); renderWelcome();
  try { await flushAll(); await load(); }
  catch (e) { toast('首页显示本机数据，更新失败：' + e.message, 'warn'); }
}
function renderList() {
  for (const g of Object.keys(GROUPS)) {
    const n = S.records.filter(r => r.group === g).length, count = $('#count-' + g);
    count.textContent = n;
    count.classList.toggle('has', n > 0 && g !== 'history');
  }
  document.querySelectorAll('.tab').forEach(t => t.setAttribute('aria-selected', String(t.dataset.group === S.group)));
  $('#home').setAttribute('aria-current', S.key ? 'false' : 'page');
  $('#workspace-filters').hidden = !S.loaded || S.group !== 'active';
  $('#progress-filter').value = S.progressFilter;
  $('#due-filter').value = S.dueFilter;
  $('#progress-filter').classList.toggle('on', S.progressFilter !== 'all');
  $('#due-filter').classList.toggle('on', S.dueFilter !== 'all');
  const subs = SUBS[S.group], box = $('#subfilters');
  box.replaceChildren();
  box.hidden = !subs || !S.loaded;
  if (subs) {
    const inGroup = S.records.filter(r => r.group === S.group);
    for (const [id, label] of subs) {
      const chip = el('button', 'chip', label);
      chip.type = 'button';
      chip.append(el('span', 'n', String(inGroup.filter(r => matchesSub(r, id)).length)));
      chip.setAttribute('aria-pressed', String(S.sub === id));
      chip.onclick = () => { S.sub = id; renderList(); };
      box.append(chip);
    }
  }
  const list = $('#assignment-list');
  list.replaceChildren();
  if (!S.loaded) {
    if (S.loadError) {
      const empty = el('div', 'list-empty'), row = el('div', 'row');
      empty.append(el('strong', '', '作业读取失败'), el('span', '', S.loadError));
      row.append(btn('重试', '', () => boot(), 'refresh'));
      empty.append(row);
      list.append(empty);
    } else {
      for (let i = 0; i < 5; i++) list.append(el('div', 'skeleton'));
    }
    return;
  }
  const query = $('#search').value.trim().toLowerCase();
  const match = r => !query || `${r.course || ''} ${r.title || ''}`.toLowerCase().includes(query);
  const dueKeys = S.group === 'active' && S.dueFilter !== 'all' ? new Set(overview().keys[S.dueFilter] || []) : null;
  const items = S.records.filter(r => r.group === S.group && matchesSub(r, S.sub) && match(r) &&
    (S.group !== 'active' || S.progressFilter === 'all' || progressFor(r).state === S.progressFilter) &&
    (!dueKeys || dueKeys.has(r.key))).sort(sorter(S.group));
  if (!items.length) { list.append(listEmpty(query, match)); return; }
  let section = null;
  for (const r of items) {
    if (S.group === 'active') {
      const s = sectionOf(r);
      if (s !== section) {
        section = s;
        const heading = el('div', 'list-section ' + due(r).tone);
        heading.append(icon('clock'), document.createTextNode(s));
        list.append(heading);
      }
    }
    list.append(listItem(r));
  }
}
function listItem(r) {
  const d = due(r), b = el('button', 'item' + (r.group === 'active' && d.tone === 'urgent' ? ' urgent' : ''));
  b.type = 'button';
  if (r.key === S.key) b.setAttribute('aria-current', 'true');
  b.title = `${r.course || '未命名课程'}\n${r.title || '未命名作业'}`;
  b.append(el('span', 'item-title', r.course || '未命名课程'), el('span', 'item-sub', r.title || '未命名作业'));
  const foot = el('span', 'item-foot');
  if (r.group === 'review') {
    foot.append(el('span', 'flag warn', statusInfo(r).label));
  } else if (r.group === 'history') {
    foot.append(el('span', 'flag', statusInfo(r).label), dueTag(r, true));
  } else {
    foot.append(dueTag(r));
    const p = progressFor(r);
    if (p.total) {
      const mp = el('span', 'mini-progress' + (p.state === 'complete' ? ' complete' : '')), bar = el('i'), fill = el('b');
      fill.style.width = p.percentage + '%';
      bar.append(fill);
      mp.append(bar, document.createTextNode(`${p.answered}/${p.total}`));
      mp.title = HomeworkWorkspace.labels[p.state];
      foot.append(mp);
    } else {
      foot.append(el('span', 'mini-progress', '题目待读取'));
    }
  }
  b.append(foot);
  b.onclick = () => select(r.key);
  return b;
}
function dueTag(r, plain = false) {
  const d = due(r), tag = el('span', 'due ' + (plain ? '' : d.tone));
  if (!plain) tag.append(icon('clock'));
  tag.append(document.createTextNode(!plain && d.tone === 'urgent' && d.left ? d.left.replace('还剩 ', '剩 ') : d.short));
  return tag;
}
function listEmpty(query, match) {
  const box = el('div', 'list-empty'), row = el('div', 'row');
  if (!S.records.length) {
    box.append(el('strong', '', '还没有作业'));
    box.append(el('span', '', S.auth?.state === 'logged_in' ? '刷新一次即可读取学习通作业。' : '登录后即可读取学习通作业。'));
    row.append(S.auth?.state === 'logged_in' ? btn('刷新作业', 'btn-primary btn-sm', () => startAction('refresh'), 'refresh') : btn('登录作业通', 'btn-primary btn-sm', openLogin));
    box.append(row);
    return box;
  }
  if (query) {
    box.append(el('strong', '', '没有匹配的作业'));
    const others = Object.keys(GROUPS).filter(g => g !== S.group).map(g => [g, S.records.filter(r => r.group === g && match(r)).length]).filter(([, n]) => n);
    for (const [g, n] of others) row.append(btn(`${GROUPS[g]}中有 ${n} 项`, 'btn-sm', () => { S.group = g; S.sub = 'all'; renderList(); }));
    row.append(btn('清除搜索', 'btn-sm btn-ghost', () => { $('#search').value = ''; renderList(); $('#search').focus(); }));
    box.append(row);
    return box;
  }
  if (S.sub !== 'all' || (S.group === 'active' && (S.progressFilter !== 'all' || S.dueFilter !== 'all'))) {
    box.append(el('strong', '', '这个筛选下没有作业'));
    row.append(btn('清除筛选', 'btn-sm', () => { S.sub = 'all'; S.progressFilter = S.dueFilter = 'all'; renderList(); }));
    box.append(row);
    return box;
  }
  box.append(el('strong', '', {active: '没有待完成的作业', review: '没有待核实的作业', history: '还没有历史作业'}[S.group]));
  if (S.group === 'active') box.append(el('span', '', '可以休息一下了。'));
  return box;
}
function renderAccount() {
  const box = $('#account');
  box.replaceChildren();
  const a = S.auth || {state: 'checking', sites: {}};
  const loggingIn = S.job.busy && S.job.action === 'login';
  const checking = loggingIn || a.state === 'checking';
  const loggedIn = a.state === 'logged_in';
  const username = a.credentials?.username?.trim();
  const row = el('button', 'account-row'), avatar = el('div', 'avatar' + (loggedIn ? '' : ' off')),
    text = el('div', 'account-text');
  row.type = 'button';
  row.id = 'account-button';
  row.setAttribute('aria-haspopup', 'menu');
  row.setAttribute('aria-controls', 'account-menu');
  row.setAttribute('aria-expanded', String(accountMenuOpen));
  if (checking) {
    avatar.append(icon('loader'));
    text.append(el('strong', '', loggingIn ? '正在登录…' : '正在检查登录'));
    text.append(el('small', '', loggingIn && S.job.message !== '正在处理…' ? S.job.message : '请稍候'));
    row.disabled = true;
  } else {
    avatar.textContent = loggedIn ? '学' : '未';
    text.append(el('strong', '', loggedIn ? (username || '作业通账号') : '登录作业通'));
    text.append(el('small', '', loggedIn ? '作业通' : everLoggedIn() ? '登录已失效' : '尚未登录'));
    row.onclick = e => { e.stopPropagation(); toggleAccountMenu(); };
  }
  if (!checking) row.append(icon('chevron', 'account-chevron'));
  row.prepend(avatar, text);
  box.append(row);
  renderAccountMenu();
}
function renderAccountMenu() {
  const menu = $('#account-menu'), a = S.auth || {state: 'checking', sites: {}};
  menu.hidden = !accountMenuOpen;
  menu.setAttribute('role', 'menu');
  const username = a.credentials?.username?.trim();
  $('#account-menu-name').textContent = username || (a.state === 'logged_in' ? '作业通账号' : '作业助手');
  $('#account-menu-state').textContent = a.state === 'logged_in' ? '作业通已登录' : everLoggedIn() ? '登录已失效' : '尚未登录';
  $('#account-login-state').textContent = a.state === 'logged_in' ? '重新登录' : '登录';
}
function toggleAccountMenu() {
  accountMenuOpen = !accountMenuOpen;
  renderAccount();
  if (accountMenuOpen) $('#account-login-button').focus({preventScroll: true});
}
function closeAccountMenu() {
  if (!accountMenuOpen) return;
  accountMenuOpen = false;
  renderAccount();
}
function everLoggedIn() {
  try { return localStorage.getItem('homework-ever-logged-in') === '1'; } catch { return false; }
}
function renderReminderRow() {
  const r = S.reminder, small = $('#reminder-next');
  $('#reminder-label').textContent = '作业提醒';
  small.textContent = !r ? '读取中…' : r.enabled ? reminderNextText(r) : '已关闭';
  small.classList.toggle('off', !!r && !r.enabled);
}
function renderSyncLine() {
  const line = $('.sync-line');
  line.replaceChildren();
  const busy = S.job.busy && ['refresh', 'open', 'fill'].includes(S.job.action);
  line.classList.toggle('busy', busy);
  if (busy) {
    line.append(icon('loader'), el('span', '', S.job.message && S.job.message !== '正在处理…' ? S.job.message : ACTION_LABEL[S.job.action] + '…'));
  } else if (!S.loaded) {
    line.append(el('span', '', S.loadError ? '本地作业读取失败' : '正在读取本地作业…'));
  } else {
    line.append(el('span', '', S.lastSuccess ? '更新于 ' + stamp(S.lastSuccess) : '尚未更新'));
  }
  const refresh = $('#refresh');
  refresh.replaceChildren(icon(S.job.busy && S.job.action === 'refresh' && !S.jobKey ? 'loader' : 'refresh'));
  refresh.disabled = S.job.busy || !S.online;
  refresh.title = !S.online ? '网络不可用' : S.job.busy ? '请等待当前操作完成' : '刷新作业';
}

/* ---------- Banners ---------- */
const ACTION_LABEL = {refresh: '正在刷新作业', read: '正在读取题目', open: '正在打开学习通', fill: '正在自动填入', login: '正在登录作业通', ai: 'AI 作答中', 'ai-test': '正在测试 AI 连接'};
function banner(tone, iconName, title, text, actions = []) {
  const b = el('div', 'banner ' + tone);
  b.setAttribute('role', tone === 'danger' ? 'alert' : 'status');
  b.append(icon(iconName));
  const body = el('div', 'banner-text');
  body.append(el('strong', '', title));
  if (text) body.append(el('span', '', text));
  b.append(body);
  if (actions.length) {
    const box = el('div', 'banner-actions');
    box.append(...actions);
    b.append(box);
  }
  return b;
}
function renderBanners() {
  const box = $('#banners');
  box.replaceChildren();
  if (S.serverLost) box.append(banner('danger', 'alert', '与助手的连接已中断', '已写的答案保存在本机，请重新打开助手。'));
  if (!S.online) box.append(banner('warn', 'offline', '网络不可用', '可以继续作答，刷新和填入需要联网。'));
  const a = S.auth;
  // Home and sidebar already show the login entry; the banner is for open assignments.
  if (S.key && a && a.state === 'login_required' && !(S.job.busy && S.job.action === 'login')) {
    box.append(banner('login', 'lock', everLoggedIn() ? '登录已失效' : '尚未登录', '登录后才能读取题目和填入答案。', [btn('登录作业通', 'btn-sm btn-primary', openLogin)]));
  }
  if (S.job.busy && S.job.action !== 'login') {
    const label = S.job.action === 'refresh' && S.jobKey ? ACTION_LABEL.read : ACTION_LABEL[S.job.action] || '正在处理';
    const detail = S.job.message && S.job.message !== '正在处理…' && S.job.message !== label ? S.job.message : '';
    box.append(banner('busy', 'loader', label + '…', detail));
  }
  if (S.warnings?.length && S.loaded && !S.job.busy) {
    const b = el('div', 'banner warn');
    b.append(icon('alert'));
    const d = el('details', 'banner-text');
    d.append(el('summary', '', `上次更新有 ${S.warnings.length} 项未读取成功`));
    const ul = el('ul');
    for (const w of S.warnings) ul.append(el('li', '', String(w)));
    d.append(ul);
    b.append(d);
    box.append(b);
  }
}
function renderStatus(force = false) {
  const memo = JSON.stringify([S.job.busy, S.job.action, S.job.message, S.jobKey, S.auth, S.reminder, S.online, S.serverLost, S.loaded, S.lastSuccess, S.warnings?.length, S.key]);
  if (!force && memo === S.statusMemo) return;
  S.statusMemo = memo;
  renderAccount();
  renderReminderRow();
  renderSyncLine();
  renderBanners();
  const r = record();
  if (r) renderContentWarning(r);
  window.HomeworkAI?.lockInputs(r);
  if (r && drafts.has(r.key)) { renderDock(r); renderReading(r); }
  else if (!S.key) renderWelcome();
  if ($('#login-dialog').open) renderLogin();
  if ($('#fill-dialog').open && fillPhase === 'running') renderFill();
  if ($('#reminder-dialog').open) renderReminder();
}

/* ---------- Home ---------- */
function greeting() {
  const h = new Date().getHours();
  return h < 5 ? '夜深了' : h < 11 ? '早上好' : h < 13 ? '中午好' : h < 18 ? '下午好' : '晚上好';
}
function setDock(visible) {
  document.body.classList.toggle('no-dock', !visible);
}
function callout(tone, iconName, title, text, action) {
  const c = el('div', 'callout ' + tone), ic = el('div', 'callout-icon'), main = el('div', 'callout-main');
  ic.append(icon(iconName));
  main.append(el('strong', '', title));
  if (text) main.append(el('span', '', text));
  c.append(ic, main);
  if (action) c.append(action);
  return c;
}
function dateBadge(r) {
  const box = el('span', 'up-date'), d = parseDate(r.deadline);
  if (!d) { box.append(el('b', '', '—'), el('small', '', '不限')); return box; }
  const label = dayLabel(d);
  box.append(el('b', '', String(d.getDate())), el('small', '', ['今天', '明天', '后天'].includes(label) ? label : (d.getMonth() + 1) + '月'));
  return box;
}
function renderWelcome() {
  const dock = $('#dock');
  dock.hidden = true;
  dock.replaceChildren();
  setDock(false);
  $('#top-save').hidden = true;
  $('#crumb').textContent = '首页';
  $('#home-top').hidden = true;
  const detail = $('#detail');
  const w = el('div', 'welcome'), greet = el('div', 'greet');
  greet.append(spark(), el('h1', '', greeting()));
  const active = S.records.filter(r => r.group === 'active').sort(sorter('active'));
  const review = S.records.filter(r => r.group === 'review').length;
  const line = el('p');
  if (!S.loaded) line.textContent = S.loadError ? '本地作业读取失败：' + S.loadError : '正在读取本地作业…';
  else if (!S.records.length) line.textContent = '还没有作业记录。';
  else if (active.length) {
    line.append(el('b', '', String(active.length)), document.createTextNode(' 项作业待完成'));
    if (review) line.append(document.createTextNode('，'), el('b', '', String(review)), document.createTextNode(' 项待核实'));
  } else line.textContent = review ? `待完成作业都清空了，还有 ${review} 项待核实。` : '待完成作业都清空了。';
  greet.append(line);
  w.append(greet);

  if (S.auth?.state === 'login_required') {
    const b = btn('登录作业通', 'btn-primary', openLogin);
    b.disabled = S.job.busy;
    w.append(callout('login', 'lock', everLoggedIn() ? '登录已失效' : '登录作业通', '登录后才能读取作业和填入答案。', b));
  }
  if (!S.loaded) { detail.replaceChildren(w); return; }
  if (!S.records.length) {
    const empty = el('div', 'empty'), ic = el('div', 'empty-icon'), row = el('div', 'notice-actions');
    ic.append(icon('inbox'));
    empty.append(ic, el('h2', '', '还没有作业'), el('p', '', S.auth?.state === 'logged_in' ? '刷新后会读取学习通里的作业。' : '登录后刷新即可看到作业。'));
    if (S.auth?.state === 'logged_in') row.append(Object.assign(btn('刷新作业', 'btn-primary', () => startAction('refresh'), 'refresh'), {disabled: S.job.busy || !S.online}));
    empty.append(row);
    w.append(empty);
    detail.replaceChildren(w);
    return;
  }

  const summary = overview();
  const today = active.filter(r => summary.keys.today.includes(r.key));
  if (today.length) {
    const first = today[0], d = due(first);
    w.append(callout('urgent', 'clock', `今天有 ${today.length} 项作业截止`, `${first.course || '未命名课程'} · ${d.left || d.short}`, btn(today.length > 1 ? '查看' : '去完成', 'btn-primary', () => today.length > 1 ? showWorkspaceList('active', 'all', 'today') : select(first.key))));
  }
  const metrics = el('div', 'metrics');
  for (const [key, title, group, range] of [
    ['today', '今天截止', 'active', 'today'], ['within_3_days', '3 天内截止', 'active', 'within_3_days'],
    ['within_7_days', '7 天内截止', 'active', 'within_7_days'], ['review', '待核实', 'review', 'all'],
  ]) {
    const n = summary.counts[key];
    const metric = btn('', 'overview-metric' + (!n ? ' zero' : key === 'today' ? ' hot' : key === 'review' ? ' caution' : ''), () => showWorkspaceList(group, 'all', range));
    metric.dataset.overview = key;
    metric.append(el('strong', '', String(n)), el('span', '', title));
    metrics.append(metric);
  }
  w.append(metrics);

  const progress = el('section', 'panel'), head = el('div', 'panel-head');
  const {answered, total} = summary.questions, percent = total ? Math.round(answered * 100 / total) : 0;
  head.append(el('h2', '', '作答进度'), el('span', '', total ? `已写 ${answered} / ${total} 题` : '暂无已读取的题目'));
  const bar = el('div', 'big-progress'), fill = el('i');
  fill.style.width = percent + '%';
  bar.append(fill);
  const meta = el('div', 'progress-meta');
  meta.append(el('b', '', percent + '%'), el('span', '', '写完后仍需在学习通提交'));
  const chips = el('div', 'state-chips');
  for (const [state, label] of Object.entries(HomeworkWorkspace.labels)) {
    const chip = el('button', 'chip', label);
    chip.type = 'button';
    chip.append(el('span', 'n', String(summary.counts[state])));
    chip.disabled = !summary.counts[state];
    chip.onclick = () => showWorkspaceList('active', state);
    chips.append(chip);
  }
  progress.append(head, bar, meta, chips);
  w.append(progress);

  if (active.length) {
    const panel = el('section', 'panel'), ph = el('div', 'panel-head'), list = el('div', 'upcoming');
    ph.append(el('h2', '', '近期截止'), el('span', '', `共 ${active.length} 项待完成`));
    for (const r of active.slice(0, 5)) {
      const d = due(r), row = el('button', 'up-item ' + d.tone), main = el('span', 'up-main'), side = el('span', 'up-side');
      row.type = 'button';
      main.append(el('strong', '', r.course || '未命名课程'), el('span', '', r.title || '未命名作业'));
      const p = progressFor(r);
      side.append(dueTag(r), el('span', 'flag' + (p.state === 'complete' ? ' ok' : ''), p.total ? `${p.answered}/${p.total} 题` : '题目待读取'));
      row.append(dateBadge(r), main, side);
      row.onclick = () => select(r.key);
      list.append(row);
    }
    panel.append(ph, list);
    if (active.length > 5) {
      const more = btn(`查看全部 ${active.length} 项`, 'btn-ghost btn-sm', () => showWorkspaceList('active'), 'arrow');
      more.style.marginTop = '6px';
      panel.append(more);
    }
    w.append(panel);
  }
  if (review) w.append(callout('warn', 'alert', `${review} 项作业信息待核实`, '截止时间或提交状态不确定，核实前不会计入待完成。', btn('去核实', '', () => showWorkspaceList('review'))));
  const foot = el('div', 'home-foot');
  foot.append(el('span', '', S.lastSuccess ? '更新于 ' + stamp(S.lastSuccess) : '尚未更新'));
  foot.append(Object.assign(btn('刷新', 'btn-ghost btn-sm', () => startAction('refresh'), 'refresh'), {disabled: S.job.busy || !S.online}));
  w.append(foot);
  detail.replaceChildren(w);
}

/* ---------- Detail ---------- */
function fingerprint(r) {
  return JSON.stringify([r.questions, r.group, r.status, r.deadline, r.deadline_kind, r.content_complete, r.content_warnings, r.content_error, r.content_status, r.attachments, r.title, r.course, r.local_settings]);
}
function setView(v) { $('#app').dataset.view = v; }
async function select(key, keep = false) {
  const generation = ++S.selecting;
  if (S.key && S.key !== key && dirty.has(S.key)) flush(S.key).catch(() => {});
  S.key = key;
  const selected = record();
  if (selected && S.group !== selected.group) { S.group = selected.group; S.sub = 'all'; }
  $('#home-top').hidden = false;
  setView('detail');
  renderList();
  const detail = $('#detail');
  if (!drafts.has(key)) {
    const loading = el('div', 'empty');
    loading.append(icon('loader'), el('p', '', '正在打开…'));
    detail.replaceChildren(loading);
  }
  try {
    const draft = await getDraft(key);
    if (generation !== S.selecting) return;
    renderDetail(record(), draft, keep);
  } catch (e) {
    if (generation !== S.selecting) return;
    const box = el('div', 'empty'), ic = el('div', 'empty-icon');
    ic.append(icon('alert'));
    box.append(ic, el('h2', '', '无法打开这份作业'), el('p', '', e.message));
    const row = el('div', 'notice-actions');
    row.append(btn('重试', 'btn-primary', () => select(key)), btn('刷新作业', '', () => startAction('refresh'), 'refresh'));
    box.append(row);
    detail.replaceChildren(box);
    $('#dock').hidden = true;
    setDock(false);
  }
}
function notice(tone, iconName, title, lines = [], actions = []) {
  const n = el('div', 'notice ' + tone);
  n.append(icon(iconName));
  const body = el('div', 'notice-body');
  if (title) body.append(el('p', 'notice-title', title));
  for (const l of lines) body.append(el('p', '', l));
  if (actions.length) {
    const row = el('div', 'notice-actions');
    row.append(...actions);
    body.append(row);
  }
  n.append(body);
  return n;
}
function rereadButton(r) {
  return Object.assign(btn('重新读取题目', 'btn-sm', () => startAction('refresh', r.key), 'refresh'), {disabled: S.job.busy || !S.online});
}
function renderContentWarning(r) {
  const box = $('#content-warning');
  if (!box) return;
  box.replaceChildren();
  if (!r.content_error || r.group === 'history') return;
  const login = /需要重新登录|LOGIN_REQUIRED/.test(r.content_error);
  if (login && r.auth_platform && S.auth?.sites?.[r.auth_platform]) return;
  box.append(login
    ? notice('danger', 'lock', '该课程的平台需要重新登录', ['登录后重新读取题目即可。'], [btn('登录作业通', 'btn-sm btn-primary', openLogin)])
    : notice('danger', 'alert', '题目读取失败', [r.content_error], [rereadButton(r)]));
}
function renderDetail(r, draft, keep = false) {
  if (!r) return;
  const detail = $('#detail');
  const scroll = keep ? detail.scrollTop : 0;
  const col = el('div', 'column');
  const live = r.group !== 'history';

  const hero = el('header', 'hero'), top = el('div', 'hero-top'), titles = el('div', 'hero-titles');
  titles.append(el('div', 'hero-course', r.title || '未命名作业'), el('h1', '', r.course || '未命名课程'));
  top.append(titles, btn('管理作业', 'btn-sm', () => openAssignmentManager(r), 'settings'));
  hero.append(top);
  const facts = el('div', 'facts'), st = statusInfo(r), d = due(r);
  const fact = (text, tone = '', iconName) => { const f = el('span', 'fact ' + tone); if (iconName) f.append(icon(iconName)); f.append(el('span', '', text)); return f; };
  facts.append(fact(st.label, st.tone));
  facts.append(fact(live && d.left && d.tone !== 'urgent' ? `${d.text} · ${d.left}` : d.text, !live ? 'muted' : d.tone === 'urgent' ? 'clay' : d.tone === 'unknown' ? 'warn' : '', 'clock'));
  if (live) {
    if (r.questions?.length) {
      const p = progressFor(r);
      facts.append(fact(`${p.answered}/${p.total} 题已作答`, p.state === 'complete' ? 'ok' : '', 'pen'));
    } else facts.append(fact('题目待读取', 'warn'));
    if (r.content_last_read) facts.append(fact('读取于 ' + stamp(r.content_last_read), 'muted'));
  }
  hero.append(facts);
  col.append(hero);

  if (!live) {
    const card = el('div', 'summary-card'), kv = el('dl', 'kv');
    const row = (k, v) => kv.append(el('dt', '', k), el('dd', '', v));
    row('课程', r.course || '未命名课程');
    row('作业', r.title || '未命名作业');
    row('截止', d.text);
    row('状态', st.label);
    if (r.local_settings?.deadline) row('自设截止', stamp(r.local_settings.deadline));
    card.append(kv);
    col.append(card, notice('soft', 'info', '', ['历史作业只保留基本信息。']));
    detail.replaceChildren(col);
    S.printed = fingerprint(r);
    $('#crumb').textContent = `${GROUPS[r.group]} · ${r.course || '未命名课程'}`;
    renderDock(r); renderSave(); renderReading(r);
    detail.scrollTop = scroll;
    return;
  }

  if (r.group === 'active' && d.tone === 'urgent' && d.left) {
    const strip = el('div', 'deadline-strip'), text = el('div');
    strip.append(icon('clock'));
    text.append(el('strong', '', d.left), el('span', '', ` · ${stamp(r.deadline)} 截止`));
    strip.append(text);
    col.append(strip);
  }
  if (r.group === 'review') {
    const lines = reviewReasons(r).map(x => x === 'deadline' ? '截止时间不确定，不会计入待完成。' : '无法确认是否已提交。');
    col.append(notice('warn', 'alert', '信息待核实', lines, [
      btn('设置截止时间', 'btn-sm', () => openAssignmentSettings(r, 'deadline'), 'calendar'),
      btn('标为已完成', 'btn-sm btn-ghost', () => openAssignmentSettings(r, 'completed'), 'check'),
    ]));
  }
  if (r.local_settings?.deadline && !r.local_settings?.completed) {
    col.append(notice('clay', 'calendar', '', ['使用自设截止时间：' + stamp(r.local_settings.deadline)]));
  }
  const warning = el('div'); warning.id = 'content-warning'; col.append(warning);
  const issues = [...new Set([...(r.content_complete === false && r.questions?.length ? [`只读取到 ${r.questions.length} 道题，缺失的题目请在学习通完成。`] : []), ...(r.content_warnings || [])].filter(Boolean))];
  if (issues.length) col.append(notice('warn', 'alert', '题目可能不完整', issues, [rereadButton(r)]));
  else if (r.questions?.length && r.content_status && r.content_status !== 'ready') col.append(notice('info', 'info', '建议重新读取题目', [r.content_status], [rereadButton(r)]));

  if (draft.legacy_text) {
    const legacy = el('details', 'legacy');
    legacy.append(el('summary', '', '旧版草稿'), el('p', '', '请手动复制到对应题目。'));
    const input = el('textarea', 'textarea short');
    input.value = draft.legacy_text;
    input.setAttribute('aria-label', '旧版草稿');
    input.oninput = () => { draft.legacy_text = input.value; markDirty(r.key, ''); };
    legacy.append(input);
    col.append(legacy);
  }
  const links = (r.attachments || []).filter(a => /^https?:\/\//i.test(a.url || ''));
  if (links.length) {
    const n = notice('soft', 'clip', '题目附件');
    const box = el('div', 'links');
    for (const a of links) box.append(externalLink(a.url, a.name || a.title || '查看附件'));
    n.querySelector('.notice-body').append(box);
    col.append(n);
  }

  if (!r.questions?.length) {
    const empty = el('div', 'empty'), ic = el('div', 'empty-icon');
    ic.append(icon('inbox'));
    empty.append(ic, el('h2', '', '还没有读取到题目'));
    empty.append(el('p', '', r.content_status && r.content_status !== 'ready' ? r.content_status : '重新读取后即可在这里作答。'));
    const row = el('div', 'notice-actions');
    row.append(Object.assign(btn('重新读取题目', 'btn-primary', () => startAction('refresh', r.key), 'refresh'), {disabled: S.job.busy || !S.online}));
    empty.append(row);
    col.append(empty);
  } else {
    const nav = el('div', 'qnav'), label = el('div', 'qnav-label'), dots = el('div', 'qnav-dots'), bar = el('div', 'progress');
    label.id = 'qnav-label';
    dots.setAttribute('aria-label', '题号导航');
    bar.append(el('i'));
    r.questions.forEach((q, i) => {
      const dot = el('button', 'qdot', String(i + 1));
      dot.type = 'button';
      dot.id = 'qd-' + q.id;
      dot.onclick = () => document.getElementById('q-' + q.id)?.scrollIntoView({block: 'start'});
      dots.append(dot);
    });
    nav.append(label, dots, bar);
    col.append(nav);
    r.questions.forEach((q, i) => col.append(questionCard(q, i, draft, r.key, r)));
    const end = el('p', 'end-note');
    end.append(spark(), document.createTextNode('全部题目到此结束'));
    col.append(end);
  }

  detail.replaceChildren(col);
  renderContentWarning(r);
  S.printed = fingerprint(r);
  $('#crumb').textContent = `${GROUPS[r.group]} · ${r.course || '未命名课程'}`;
  renderDock(r);
  renderSave();
  renderReading(r);
  updateProgress();
  watchQuestions();
  window.HomeworkAI?.lockInputs(r);
  detail.scrollTop = scroll;
}
let questionObserver = null;
function watchQuestions() {
  questionObserver?.disconnect();
  if (!('IntersectionObserver' in window)) return;
  questionObserver = new IntersectionObserver(entries => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      document.querySelectorAll('.qdot.here').forEach(d => d.classList.remove('here'));
      const dot = document.getElementById('qd-' + e.target.id.slice(2));
      if (dot) { dot.classList.add('here'); dot.scrollIntoView({block: 'nearest', inline: 'nearest'}); }
    }
  }, {root: $('#detail'), rootMargin: '-20% 0px -70% 0px'});
  document.querySelectorAll('#detail .question').forEach(q => questionObserver.observe(q));
}
function renderReading(r) {
  const main = $('.main');
  main.querySelector('.reading')?.remove();
  if (!(S.job.busy && S.job.action === 'refresh' && S.jobKey === r.key)) return;
  const overlay = el('div', 'reading'), card = el('div', 'reading-card');
  card.append(spark(), el('span', '', '正在重新读取题目…' + (S.job.message && S.job.message !== '正在处理…' ? ' ' + S.job.message : '')));
  overlay.append(card);
  main.append(overlay);
}
function updateProgress() {
  const r = record(), draft = drafts.get(S.key);
  if (!r || !draft || !r.questions?.length) return;
  let done = 0;
  for (const q of r.questions) {
    const [state, text] = qState(q, draft.answers[q.id]);
    if (state === 'done') done++;
    const s = document.getElementById('qs-' + q.id);
    if (s) { s.className = 'q-state s-' + state; s.replaceChildren(icon(state === 'done' ? 'check' : state === 'changed' ? 'alert' : 'pen'), el('span', '', text)); }
    const dot = document.getElementById('qd-' + q.id);
    if (dot) {
      const here = dot.classList.contains('here');
      dot.className = 'qdot s-' + state + (here ? ' here' : '');
      dot.title = `第 ${r.questions.indexOf(q) + 1} 题 · ${text}`;
      dot.setAttribute('aria-label', dot.title);
    }
  }
  const total = r.questions.length, percent = Math.round(done * 100 / total);
  const label = $('#qnav-label');
  if (label) label.replaceChildren(document.createTextNode('已作答 '), el('b', '', `${done} / ${total}`));
  const bar = document.querySelector('.qnav .progress i');
  if (bar) bar.style.width = percent + '%';
  const ring = document.querySelector('.dock .ring');
  if (ring) { ring.style.setProperty('--p', percent); ring.classList.toggle('complete', done === total); }
  const count = $('#dock-count');
  if (count) count.textContent = done === total ? `全部 ${total} 题已作答` : `已作答 ${done} / ${total}`;
}

/* ---------- Questions ---------- */
function removeRepeatedQuestionNumber(body, number) {
  // School question markup wraps its ordinal as <div><i>N</i><div>stem…</div></div>.
  // Match that structure only; numbers in paragraphs, lists and formulas remain.
  let container = body;
  while (container) {
    const nodes = [...container.childNodes].filter(n => n.nodeType !== Node.TEXT_NODE || n.textContent.trim());
    if (nodes.length === 1 && nodes[0].nodeName === 'DIV') { container = nodes[0]; continue; }
    if (nodes.length === 2 && nodes[0].nodeName === 'I' && nodes[0].childElementCount === 0 &&
        nodes[0].textContent.trim() === String(number) && nodes[1].nodeName === 'DIV') {
      nodes[0].remove();
    }
    break;
  }
}
function questionCard(q, index, draft, key) {
  const art = el('article', 'question');
  art.id = 'q-' + q.id;
  const head = el('div', 'q-head'), state = el('span', 'q-state');
  state.id = 'qs-' + q.id;
  head.append(el('span', 'q-num', String(index + 1)), el('span', 'q-type' + (isUnsupported(q) ? ' unsupported' : ''), isUnsupported(q) ? TYPES.unsupported : TYPES[q.type] || q.label), state);
  art.append(head);
  const body = el('div', 'q-body');
  renderRich(body, q.html, q.text);
  removeRepeatedQuestionNumber(body, index + 1);
  if (!q.html && !q.text) body.append(el('span', '', '（这道题的题干暂未读取）'));
  art.append(body);

  const staleBox = el('div');
  art.append(staleBox);
  const paintStale = () => {
    staleBox.replaceChildren();
    const a = draft.answers[q.id];
    const old = changedAnswer(q, a) ? {value: a.value, pending: true} : a?.stale ? {value: a.stale.value} : null;
    if (!old) return;
    const box = el('div', 'stale'), title = el('div', 'stale-title');
    title.append(icon('alert'), el('span', '', old.pending ? '题目内容已变化' : '题目变化前的旧答案'));
    box.append(title, el('div', 'stale-value', showValue(old.value) || '（空）'));
    box.append(el('p', '', old.pending
      ? '旧答案不会自动填入，请重新作答。'
      : '仅供参考，不会自动填入。'));
    staleBox.append(box);
  };
  paintStale();
  const saved = draft.answers[q.id];
  const cur = saved && saved.signature === q.signature ? saved.value : null;
  const update = value => {
    const a = editable(q, draft);
    a.value = value;
    draft.answers[q.id] = a;
    markDirty(key, q.id);
    paintStale();
    updateProgress();
  };

  if (isUnsupported(q)) {
    art.append(notice('warn', 'alert', '题型未识别', ['请在学习通作答，下方可记草稿。']));
    const input = el('textarea', 'textarea short');
    input.placeholder = '草稿';
    input.value = Array.isArray(cur) ? cur.join('\n') : cur || '';
    input.setAttribute('aria-label', `第 ${index + 1} 题草稿`);
    input.oninput = () => update(input.value);
    art.append(input);
  } else if (['single', 'multiple', 'judgment'].includes(q.type)) {
    art.append(choiceInput(q, index, cur, update));
  } else if (q.type === 'blank') {
    art.append(blankInput(q, index, cur, update));
  } else if (q.type === 'essay') {
    art.append(essayInput(q, index, cur, update));
  }
  art.append(filesBlock(q, draft, key, q.type === 'upload', paintStale));
  const template = window.HomeworkAI?.templateCard(q, draft, key);
  if (template) art.append(template);
  return art;
}
function choiceInput(q, index, cur, update) {
  const multi = q.type === 'multiple', judge = q.type === 'judgment';
  const wrap = el('div', 'options' + (judge && q.options.length <= 2 ? ' judge-row' : ''));
  wrap.setAttribute('role', multi ? 'group' : 'radiogroup');
  wrap.setAttribute('aria-label', `第 ${index + 1} 题选项`);
  const chosen = new Set(multi ? (Array.isArray(cur) ? cur.map(String) : []) : (hasValue(cur) ? [String(Array.isArray(cur) ? cur[0] : cur)] : []));
  const summary = el('div', 'choice-summary');
  const textOf = v => { const o = q.options.find(o => String(o.value) === v); return judge ? (o?.text || v) : v; };
  const paint = () => {
    const values = [...wrap.querySelectorAll('input:checked')].map(x => x.value);
    summary.replaceChildren();
    if (!values.length) { summary.append(document.createTextNode(multi ? '尚未选择（可多选）' : '尚未选择')); return; }
    summary.append(document.createTextNode(multi ? '已选：' : judge ? '当前判断：' : '当前选择：'), el('b', '', values.map(textOf).join('、')));
    if (multi) summary.append(document.createTextNode(`（共 ${values.length} 项）`));
    const clear = el('button', 'link', '清除');
    clear.type = 'button';
    clear.onclick = () => { wrap.querySelectorAll('input').forEach(x => { x.checked = false; }); update(multi ? [] : ''); paint(); };
    summary.append(clear);
  };
  q.options.forEach((o, i) => {
    const label = el('label', 'option' + (multi ? ' multi' : '') + (judge ? ' judge' : ''));
    const input = el('input');
    input.type = multi ? 'checkbox' : 'radio';
    input.name = 'q-' + q.id;
    input.value = String(o.value);
    input.checked = chosen.has(String(o.value));
    const text = el('div', 'option-text');
    renderRich(text, o.html, o.text);
    const letter = /^[A-Z]$/.test(String(o.value)) ? String(o.value) : String.fromCharCode(65 + i);
    const prefixed = new RegExp('^\\s*' + letter + '\\s*[.、．:：)）\\s]').test(text.textContent || '');
    const mark = el('span', 'mark', judge || prefixed ? '' : letter);
    input.onchange = () => {
      update(multi ? [...wrap.querySelectorAll('input:checked')].map(x => x.value) : input.value);
      paint();
    };
    label.append(input, mark, text);
    wrap.append(label);
  });
  const box = el('div');
  box.append(wrap, summary);
  paint();
  return box;
}
function blankInput(q, index, cur, update) {
  const count = Math.max(1, Number(q.blank_count) || 1);
  const values = Array.isArray(cur) ? cur : hasValue(cur) ? [cur] : [];
  const box = el('div');
  const list = el('div', 'blank-list'), fields = [];
  for (let i = 0; i < count; i++) {
    const row = el('label', 'blank');
    const input = el('input', 'input');
    input.type = 'text';
    input.value = String(values[i] ?? '');
    input.placeholder = count > 1 ? `第 ${i + 1} 空` : '填写答案';
    input.setAttribute('aria-label', `第 ${index + 1} 题，第 ${i + 1} 空`);
    input.oninput = () => update(fields.map(f => f.value));
    fields.push(input);
    if (count > 1) row.append(el('span', 'blank-no', String(i + 1)));
    row.append(input);
    list.append(row);
  }
  box.append(list);
  return box;
}
function essayInput(q, index, cur, update) {
  const box = el('div');
  const input = el('textarea', 'textarea');
  input.placeholder = '填写答案';
  input.value = Array.isArray(cur) ? cur.join('\n') : cur || '';
  input.setAttribute('aria-label', `第 ${index + 1} 题答案`);
  const foot = el('div', 'field-foot'), count = el('span');
  const grow = () => {
    input.style.height = 'auto';
    input.style.height = Math.min(Math.max(input.scrollHeight + 2, 140), window.innerHeight * 0.6) + 'px';
    count.textContent = `${input.value.replace(/\s/g, '').length} 字`;
  };
  input.oninput = () => { update(input.value); grow(); };
  foot.append(count);
  box.append(input, foot);
  requestAnimationFrame(grow);
  return box;
}
function formatSize(n) {
  if (!Number.isFinite(n)) return '大小未知';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}
function filesBlock(q, draft, key, prominent, onChange) {
  const box = el('div', 'files'), list = el('div', 'files-list'), status = el('div', 'uploading'), hint = el('div', 'files-hint');
  status.hidden = true;
  const paint = () => {
    list.replaceChildren();
    const files = draft.answers[q.id]?.files || [];
    list.hidden = !files.length;
    for (const f of files) {
      const row = el('div', 'file'), fi = el('span', 'file-icon'), main = el('div', 'file-main');
      fi.append(icon('file'));
      main.append(el('span', 'file-name', f.name || '答案附件'), el('span', 'file-size', formatSize(Number(f.size))));
      const open = el('a', 'btn btn-sm btn-quiet', '打开');
      open.href = '/api/attachment?' + new URLSearchParams({key, id: f.id});
      open.target = '_blank';
      open.rel = 'noopener';
      const remove = btn('移除', 'btn-sm btn-quiet');
      remove.title = '从答案缓存中删除此附件';
      remove.onclick = async () => {
        if (remove.dataset.confirm !== '1') {
          remove.dataset.confirm = '1';
          remove.querySelector('span').textContent = '确认移除';
          setTimeout(() => { if (remove.isConnected) { remove.dataset.confirm = ''; remove.querySelector('span').textContent = '移除'; } }, 3000);
          return;
        }
        remove.disabled = true;
        const a = editable(q, draft);
        a.files = (a.files || []).filter(x => x.id !== f.id);
        draft.answers[q.id] = a;
        removeAttachmentFromRecovery(key, String(q.id), f.id);
        markDirty(key, q.id);
        paint(); onChange(); updateProgress();
        try {
          await flush(key);
          toast('已从答案缓存中删除附件', 'ok');
        } catch (e) {
          toast('附件已移除，缓存保存失败：' + e.message, 'error');
        }
      };
      row.append(fi, main, open, remove);
      list.append(row);
    }
    hint.hidden = !prominent && !files.length;
    hint.textContent = prominent
      ? '单个文件不超过 30 MB，自动填入时会尝试上传。'
      : '自动填入时会尝试上传附件。';
  };
  const upload = async fileList => {
    for (const file of [...fileList]) {
      if (file.size > MAX_FILE) { toast(`“${file.name}”超过 30 MB，未添加`, 'error'); continue; }
      if (!file.size) { toast(`“${file.name}”是空文件，未添加`, 'error'); continue; }
      status.hidden = false;
      status.replaceChildren(icon('loader'), el('span', '', `正在添加 ${file.name}…`));
      try {
        const f = await api('/api/upload?' + new URLSearchParams({key, qid: q.id, name: file.name}), file, true);
        const a = editable(q, draft);
        a.files = [...(a.files || []), f];
        draft.answers[q.id] = a;
        markDirty(key, q.id);
        await flush(key);
        toast(`已添加“${f.name}”`, 'ok');
      } catch (e) {
        toast(`“${file.name}”添加失败：${e.message}`, 'error');
      }
      paint(); onChange(); updateProgress();
    }
    status.hidden = true;
  };
  const input = el('input');
  input.type = 'file';
  input.multiple = true;
  input.onchange = async () => { const files = [...input.files]; input.value = ''; await upload(files); };
  box.append(list, status);
  if (prominent) {
    const zone = el('label', 'dropzone');
    zone.append(icon('clip'), el('strong', '', '添加答案文件'), el('span', '', '点击选择，或把文件拖到这里'), input);
    zone.ondragover = e => { e.preventDefault(); zone.classList.add('over'); };
    zone.ondragleave = () => zone.classList.remove('over');
    zone.ondrop = e => { e.preventDefault(); zone.classList.remove('over'); upload(e.dataTransfer.files); };
    box.append(zone);
  } else {
    const add = el('label', 'add-file');
    add.append(icon('clip'), el('span', '', '添加附件'), input);
    box.append(add);
  }
  box.append(hint);
  paint();
  return box;
}

/* ---------- Save indicator & dock ---------- */
function saveText(key) {
  const s = saveState.get(key) || {state: 'saved'};
  if (s.state === 'saving') return ['saving', 'loader', '正在保存…'];
  if (s.state === 'failed') return ['failed', 'alert', '保存失败，已暂存在浏览器'];
  const at = s.at || drafts.get(key)?.updated_at;
  return ['ok', 'check', at ? '已保存 · ' + hm(parseDate(at) || new Date()) : '自动保存'];
}
let lastSaveTone = '';
function renderSave() {
  if (record()?.group === 'history') { $('#top-save').hidden = true; return; }
  if (!S.key) return;
  const [tone, iconName, text] = saveText(S.key);
  const pill = $('#top-save');
  pill.hidden = false;
  pill.className = 'save-pill ' + tone;
  pill.replaceChildren(icon(iconName), el('span', '', text));
  if (tone === 'failed') {
    const retry = el('button', 'link', '重试');
    retry.type = 'button';
    retry.onclick = () => flush(S.key).then(() => toast('已保存', 'ok')).catch(e => toast('仍未保存：' + e.message + '。请检查助手是否仍在运行。', 'error'));
    pill.append(retry);
    if (lastSaveTone !== 'failed') toast('答案未能保存到本机，会自动重试。关闭窗口前请确认已保存。', 'error');
  }
  lastSaveTone = tone;
}
function fillBlocker(r) {
  if (r.group === 'history') return '这份作业已截止或已提交';
  if (r.group === 'review') return '作业信息待核实';
  if (!r.questions?.length) return '还没有读取到题目';
  if (!S.online) return '网络不可用';
  if (S.auth?.state === 'login_required') return '请先登录作业通';
  return '';
}
function renderDock(r) {
  const dock = $('#dock');
  if (r.group === 'history' || r.capabilities?.show_submission_bar === false) {
    dock.hidden = true; dock.replaceChildren(); setDock(false); return;
  }
  dock.hidden = false;
  setDock(true);
  const card = el('div', 'dock-card'), row = el('div', 'dock-row'), progress = el('div', 'dock-progress'), actions = el('div', 'dock-actions');
  const busy = S.job.busy, act = S.job.action, mine = S.jobKey === r.key;
  const ring = el('span', 'ring'), text = el('div', 'dock-text'), count = el('strong', '', r.questions?.length ? '' : '题目待读取');
  count.id = 'dock-count';
  const blocker = fillBlocker(r);
  const sub = el('span', blocker && !busy ? 'warn' : '', blocker && !busy ? '暂不能填入：' + blocker : '填入后需在学习通提交');
  text.append(count, sub);
  progress.append(ring, text);
  const reread = btn('', 'btn-ghost icon-only', () => startAction('refresh', r.key), busy && act === 'refresh' && mine ? 'loader' : 'refresh');
  reread.title = '重新读取题目';
  reread.setAttribute('aria-label', '重新读取题目');
  reread.disabled = busy || !S.online;
  const going = busy && mine && ['open', 'fill'].includes(act);
  const open = btn(going ? (act === 'fill' ? '正在填入…' : '正在打开…') : '前往交作业', 'btn-primary', () => goToAssignment(r), going ? 'loader' : 'arrow');
  open.disabled = busy || !S.online;
  actions.append(reread, open);
  const aiButton = window.HomeworkAI?.taskButton(r);
  if (aiButton) actions.insertBefore(aiButton, open);
  row.append(progress, actions);
  card.append(row);
  dock.replaceChildren(card);
  updateProgress();
  renderSave();
}
if ('ResizeObserver' in window) new ResizeObserver(() => {
  const h = $('#dock').offsetHeight;
  if (h) document.documentElement.style.setProperty('--dock-h', h + 'px');
}).observe($('#dock'));

/* ---------- Actions ---------- */
function manageItem(label, desc, iconName, onclick, cls = '') {
  const b = el('button', 'manage-item ' + cls);
  b.type = 'button';
  b.setAttribute('aria-label', label);
  const text = el('span', 'mi-text');
  text.append(el('b', '', label));
  if (desc) text.append(el('small', '', desc));
  b.append(icon(iconName), text);
  b.onclick = onclick;
  return b;
}
function openAssignmentManager(r) {
  const dialog = $('#assignment-manage-dialog'), body = $('#assignment-manage-body');
  body.replaceChildren(closeButton(dialog), el('div', 'sheet-eyebrow', r.course || '未命名课程'));
  const title = el('h2', 'sheet-title', '管理作业'); title.id = 'assignment-manage-title';
  body.append(title, el('p', 'sheet-text', r.title || '未命名作业'));
  const list = el('div', 'manage-list');
  const settings = intent => { dialog.close(); openAssignmentSettings(r, intent); };
  const run = async action => { if (await startAction(action, r.key)) dialog.close(); };
  const openItem = manageItem('查看原作业', '在学习通打开这份作业', 'external', () => run('open'));
  openItem.disabled = S.job.busy || !S.online || r.capabilities?.can_open === false;
  list.append(openItem);
  if (r.group !== 'history') {
    const reread = manageItem('重新读取题目', '题目有变化时使用', 'refresh', () => run('refresh'));
    reread.disabled = S.job.busy || !S.online;
    list.append(reread);
  }
  body.append(list, el('div', 'manage-group', '本机设置'));
  const local = el('div', 'manage-list');
  if (!r.local_settings?.completed) local.append(manageItem('标记已完成', '移入历史，并清理题目和答案', 'check', () => settings('completed')));
  local.append(manageItem('设置截止时间', r.local_settings?.deadline ? '当前：' + stamp(r.local_settings.deadline) : '平台时间不准确时使用', 'calendar', () => settings('deadline')));
  if (r.local_settings?.completed || r.local_settings?.deadline) local.append(manageItem('修改或撤销本机设置', '恢复跟随学习通的状态', 'undo', () => settings()));
  body.append(local);
  dialog.showModal();
}
async function goToAssignment(r) {
  try {
    await flushAll();
    const draft = await getDraft(r.key);
    if (canonicalKey(r.key) !== S.key) return;
    if (Object.values(draft.answers || {}).some(answered)) await openFill(r);
    else await startAction('open', r.key);
  } catch (e) { toast('答案尚未保存，请稍后重试：' + e.message, 'error'); }
}

function openAssignmentSettings(r, intent = '') {
  const dialog = $('#assignment-settings-dialog'), body = $('#assignment-settings-body');
  body.replaceChildren(closeButton(dialog), el('div', 'sheet-eyebrow', r.course || '本机设置'));
  const title = el('h2', 'sheet-title', '状态与截止时间'); title.id = 'assignment-settings-title';
  body.append(title, el('p', 'sheet-text', '只修改本机记录，不影响学习通。'));
  const form = el('form');
  const statusLabel = el('label', 'form-field'), state = el('select', 'input');
  state.id = 'assignment-completed';
  for (const [value, text] of [['platform', '跟随学习通'], ['completed', '已完成（本机标记）']]) {
    const option = el('option', '', text); option.value = value; state.append(option);
  }
  state.value = intent === 'completed' || r.local_settings?.completed ? 'completed' : 'platform';
  statusLabel.append(el('span', '', '完成状态'), state);
  const danger = notice('danger', 'alert', '标记完成会清理本机内容', ['题目、答案和附件将被删除，无法恢复。']);
  const dateLabel = el('label', 'form-field'), date = el('input', 'input');
  date.id = 'assignment-deadline'; date.type = 'datetime-local';
  date.value = r.local_settings?.deadline?.slice(0, 16) || '';
  dateLabel.append(el('span', '', '自设截止时间'), date);
  form.append(statusLabel, danger, dateLabel, el('p', 'field-foot', '留空则使用学习通的截止时间。'));
  const error = el('p', 'settings-error'); error.setAttribute('role', 'alert'); form.append(error);
  const actions = el('div', 'sheet-actions'), save = btn('保存设置', 'btn-primary'); save.type = 'submit';
  const reset = btn('撤销本机设置', 'btn-ghost', () => persist(false, null));
  reset.disabled = !r.local_settings?.completed && !r.local_settings?.deadline;
  const sync = () => {
    const destructive = state.value === 'completed' && !r.local_settings?.completed;
    danger.hidden = !destructive;
    save.className = 'btn ' + (destructive ? 'btn-danger' : 'btn-primary');
  };
  state.onchange = sync; sync();
  actions.append(reset, el('span', 'spacer'), btn('取消', '', () => dialog.close()), save); form.append(actions);
  async function persist(completed, deadline) {
    save.disabled = reset.disabled = true;
    error.textContent = '';
    try {
      await api('/api/assignment-settings', {key: r.key, completed, deadline});
      dialog.close();
      await load();
      const updated = S.records.find(x => x.key === canonicalKey(r.key));
      if (updated) { S.group = updated.group; S.sub = 'all'; renderList(); }
      toast(completed ? '已标记完成，移入历史' : deadline ? '截止时间已保存' : '已恢复跟随学习通');
    } catch (e) {
      error.textContent = e.message;
      if (!dialog.open) toast('设置已保存，但列表未刷新：' + e.message, 'error');
      save.disabled = false;
      reset.disabled = !r.local_settings?.completed && !r.local_settings?.deadline;
    }
  }
  form.onsubmit = event => { event.preventDefault(); if (form.reportValidity()) persist(state.value === 'completed', date.value || null); };
  body.append(form);
  dialog.showModal();
  (intent === 'deadline' ? date : state).focus();
}

async function startAction(name, key = null, extra = {}) {
  if (S.job.busy) { toast('当前还有操作在进行，请稍候', 'warn'); return false; }
  try {
    await flushAll();
  } catch (e) {
    toast('答案尚未保存成功：' + e.message, 'error');
    if (name === 'fill' || name === 'ai') return false;
  }
  try {
    const job = await api('/api/action', {action: name, key, ...extra});
    S.jobKey = key;
    applyJob(job);
    renderStatus(true);
    return true;
  } catch (e) {
    toast(e.message, 'error');
    return false;
  }
}
function applyJob(job) {
  if (!job) return;
  window.HomeworkAI?.jobChanged(S.job, job);
  S.job = {busy: !!job.busy, action: job.action || '', key: job.key || null, message: job.message || '', result: job.result};
  if (job.auth) {
    S.auth = job.auth;
    if (job.auth.state === 'logged_in') { try { localStorage.setItem('homework-ever-logged-in', '1'); } catch {} }
  }
  if (job.reminder) S.reminder = job.reminder;
}
function onJobDone(job, key) {
  const failed = !!job.result?.error;
  if (job.action === 'login') return loginDone(job);
  if (job.action === 'fill') return fillDone(job, key);
  if (job.action === 'ai' || job.action === 'ai-test') return window.HomeworkAI?.jobDone(job);
  if (job.action === 'open') return toast(job.message || (failed ? '打开失败' : '已打开'), failed ? 'error' : 'ok');
  if (job.action === 'refresh') {
    if (failed) return toast(job.message || '刷新失败', 'error');
    if (key) return toast('题目已重新读取', 'ok');
    const n = S.records.filter(r => r.group === 'active').length, m = S.records.filter(r => r.group === 'review').length;
    const partial = /部分/.test(job.message || '');
    toast(`${job.message || '作业已更新'} · 待完成 ${n} 项${m ? '，待核实 ' + m + ' 项' : ''}`, partial ? 'warn' : 'ok');
  }
}

/* ---------- Login ---------- */
function openLogin() {
  loginPhase = S.job.busy && S.job.action === 'login' ? 'running' : 'form';
  loginError = '';
  renderLogin();
  const d = $('#login-dialog');
  if (!d.open) d.showModal();
  $('#login-account')?.focus();
}
function titled(id, text) { const h = el('h2', 'sheet-title', text); h.id = id; return h; }
function closeButton(dialog) {
  const b = btn('', 'btn-quiet icon-only sheet-close', () => dialog.close(), 'x');
  b.setAttribute('aria-label', '关闭');
  return b;
}
function siteList(sites, logged) {
  const box = el('div', 'kv');
  for (const [id, name] of Object.entries(SITES)) {
    const ok = !!sites?.[id] || logged;
    const dd = el('dd', 'site');
    dd.append(el('span', 'dot ' + (ok ? 'ok' : 'bad')), el('span', '', ok ? '已登录' : '未登录'));
    box.append(el('dt', '', name), dd);
  }
  return box;
}
function renderLogin() {
  const body = $('#login-body'), dialog = $('#login-dialog');
  if (loginPhase === 'running' && !(S.job.busy && S.job.action === 'login')) return;
  const savedAccount = S.auth?.credentials?.username || '';
  const account = $('#login-account')?.value || savedAccount;
  body.replaceChildren(closeButton(dialog));
  body.append(el('div', 'sheet-eyebrow', '作业通'));
  const a = S.auth || {};
  if (loginPhase === 'running') {
    body.append(titled('login-title', '正在登录作业通'));
    body.append(el('p', 'sheet-text', '助手正在检查两个学习通站点，并只为尚未登录的站点补登录。这可能需要几十秒。'));
    const steps = el('ol', 'steps');
    for (const text of ['检查两个站点的登录状态', '为尚未登录的站点补登录', '确认两个站点均已登录']) {
      const step = el('li', 'step now'), mark = el('span', 'step-mark');
      mark.append(icon('loader'));
      step.append(mark, el('span', '', text));
      steps.append(step);
    }
    body.append(steps);
    const actions = el('div', 'sheet-actions');
    actions.append(btn('在后台继续', '', () => dialog.close()));
    body.append(actions);
    return;
  }
  if (loginPhase === 'success') {
    const hero = el('div', 'result-hero'), ic = el('div', 'result-icon ok');
    ic.append(icon('check'));
    const t = el('div');
    t.append(titled('login-title', '登录成功'));
    hero.append(ic, t);
    body.append(hero, siteList(a.sites, true));
    const actions = el('div', 'sheet-actions');
    actions.append(btn('完成', '', () => dialog.close()));
    actions.append(Object.assign(btn('刷新作业', 'btn-primary', () => { dialog.close(); startAction('refresh'); }, 'refresh'), {disabled: S.job.busy || !S.online}));
    body.append(actions);
    return;
  }
  if (loginPhase === 'failed') {
    const hero = el('div', 'result-hero'), ic = el('div', 'result-icon fail');
    ic.append(icon('alert'));
    const t = el('div');
    t.append(titled('login-title', '登录未完成'), el('p', 'sheet-text', '请检查账号密码和网络，然后重新输入。'));
    hero.append(ic, t);
    body.append(hero);
    if (loginError) body.append(el('div', 'form-error', loginError));
    body.append(siteList(a.sites, false));
    const actions = el('div', 'sheet-actions');
    actions.append(btn('关闭', '', () => dialog.close()), btn('重新输入', 'btn-primary', () => { loginPhase = 'form'; loginError = ''; renderLogin(); $('#login-account').value = account; $('#login-password').focus(); }));
    body.append(actions);
    return;
  }
  body.append(titled('login-title', a.state === 'logged_in' ? '重新登录作业通' : '登录作业通'));
  if (a.state === 'logged_in') body.append(notice('soft', 'info', '', ['重新登录将替换已保存的账号。']));
  const form = el('form');
  form.autocomplete = 'on';
  const f1 = el('label', 'form-field'), user = el('input', 'input');
  user.id = 'login-account'; user.name = 'username'; user.autocomplete = 'username'; user.required = true; user.value = account;
  f1.append(el('span', '', '账号'), user);
  const f2 = el('label', 'form-field'), wrap = el('div', 'pw-wrap'), pw = el('input', 'input');
  const savedPassword = !!a.credentials?.saved && account === savedAccount;
  pw.id = 'login-password'; pw.name = 'password'; pw.type = 'password'; pw.autocomplete = 'current-password';
  pw.required = !savedPassword;
  if (savedPassword) pw.placeholder = '已保存，留空继续使用';
  const eye = btn('', 'btn-quiet icon-only pw-toggle', () => { pw.type = pw.type === 'password' ? 'text' : 'password'; eye.setAttribute('aria-pressed', String(pw.type === 'text')); }, 'eye');
  eye.setAttribute('aria-label', '显示或隐藏密码');
  wrap.append(pw, eye);
  f2.append(el('span', '', '密码'), wrap);
  form.append(f1, f2);
  if (loginError) form.append(el('div', 'form-error', loginError));
  const hint = el('div', 'form-hint');
  hint.append(icon('lock'), el('span', '', savedPassword ? '登录信息已加密保存在这台电脑上。' : '账号密码只加密保存在这台电脑上。'));
  form.append(hint);
  const actions = el('div', 'sheet-actions'), submit = el('button', 'btn btn-primary', '登录作业通');
  submit.type = 'submit';
  submit.disabled = S.job.busy || !S.online;
  actions.append(btn('取消', '', () => dialog.close()), submit);
  form.append(actions);
  if (S.job.busy) form.append(el('p', 'sheet-text', '当前有其他操作在进行，完成后才能登录。'));
  else if (!S.online) form.append(el('p', 'sheet-text', '网络不可用，联网后才能登录。'));
  form.onsubmit = async e => {
    e.preventDefault();
    const username = user.value.trim(), password = pw.value;
    const reuseSaved = !!a.credentials?.saved && username === savedAccount && !password;
    if (!username || (!password && !reuseSaved)) { loginError = '请输入账号和密码'; renderLogin(); $('#login-account').value = username; return; }
    pw.value = '';
    submit.disabled = true;
    const ok = await startAction('login', null, {username, password, saved: reuseSaved});
    if (ok) { loginPhase = 'running'; renderLogin(); }
    else { submit.disabled = false; }
  };
  body.append(form);
}
function loginDone(job) {
  const ok = !job.result?.error && S.auth?.state === 'logged_in';
  loginPhase = ok ? 'success' : 'failed';
  loginError = ok ? '' : job.message || '登录失败，请重试';
  if ($('#login-dialog').open) renderLogin();
  else toast(ok ? '作业通已登录' : loginError, ok ? 'ok' : 'error');
}

/* ---------- Fill ---------- */
function analyze(r, draft) {
  const ready = [], skipped = [];
  (r.questions || []).forEach((q, i) => {
    const a = draft.answers[q.id];
    const item = {index: i, q};
    if (!a || (!hasValue(a.value) && !a.files?.length)) skipped.push({...item, kind: isUnsupported(q) ? 'unsupported' : 'empty'});
    else if (a.signature !== q.signature) skipped.push({...item, kind: 'changed'});
    else if (isUnsupported(q) && !a.files?.length) skipped.push({...item, kind: 'unsupported'});
    else ready.push(item);
  });
  return {ready, skipped};
}
const REASON = {
  changed: ['warn', '题目内容已变化', '请重新核对答案。'],
  unsupported: ['info', '题型未识别', '请在学习通作答。'],
  empty: ['', '答案为空', ''],
  upload: ['warn', '附件需手动上传', ''],
  other: ['danger', '未能填入', ''],
};
function classify(reason) {
  if (/变化|校验信息/.test(reason)) return 'changed';
  if (/附件/.test(reason)) return 'upload';
  if (/题型/.test(reason)) return 'unsupported';
  if (/没有保存答案/.test(reason)) return 'empty';
  return 'other';
}
function reasonRows(rows) {
  const out = [], empty = rows.filter(x => x.kind === 'empty' && x.i >= 0);
  for (const x of rows) {
    if (x.kind === 'empty' && x.i >= 0) {
      if (x === empty[0]) out.push(reasonRow(empty.length > 1 ? `${empty.length} 道题` : `第 ${x.i + 1} 题`, 'empty', empty.length > 1 ? `第 ${empty.map(e => e.i + 1).join('、')} 题还没有保存答案。` : ''));
      continue;
    }
    out.push(reasonRow(x.i >= 0 ? `第 ${x.i + 1} 题` : '页面题目', x.kind, x.reason, x.files));
  }
  return out;
}
function reasonRow(label, kind, detail, files) {
  const [tone, tag, fallback] = REASON[kind];
  const li = el('li', 'reason'), main = el('div', 'reason-main');
  main.append(el('span', 'reason-tag ' + tone, tag), document.createTextNode(detail || fallback));
  if (files?.length) main.append(el('span', 'reason-files', '文件：' + files.map(f => `${f.name || '答案附件'}（${formatSize(Number(f.size))}）`).join('、')));
  li.append(el('span', 'reason-q', label), main);
  return li;
}
async function openFill(r) {
  try { await flush(r.key); } catch (e) { toast('答案尚未保存成功，暂不能填入：' + e.message, 'error'); return; }
  fillPhase = 'check';
  fillResult = null;
  renderFill();
  $('#fill-dialog').showModal();
}
function renderFill() {
  const r = record(), draft = drafts.get(S.key), dialog = $('#fill-dialog'), body = $('#fill-body');
  if (!r || !draft) { dialog.close(); return; }
  body.replaceChildren(closeButton(dialog), el('div', 'sheet-eyebrow', '自动填入'));
  const noSubmit = () => { const n = el('div', 'no-submit'); n.append(icon('lock'), el('span', '', NO_SUBMIT)); return n; };
  if (fillPhase === 'check') {
    const {ready, skipped} = analyze(r, draft);
    body.append(titled('fill-title', '填入前检查'));
    const stats = el('div', 'stats');
      stats.append(statCard(ready.length, '将填入', 'ok'), statCard(skipped.length, '将跳过', skipped.length ? 'warn' : ''), statCard(r.questions.length, '共计题目'));
    body.append(stats);
    if (skipped.length) {
      body.append(el('div', 'section-label', '以下题目不会自动填入'));
      const ul = el('ul', 'reasons');
      ul.append(...reasonRows(skipped.map(x => ({i: x.index, kind: x.kind, files: x.files, reason: x.kind === 'upload' && x.text ? '文字答案也需手动填写。' : ''}))));
      body.append(ul);
    }
    if (r.content_complete === false) body.append(notice('warn', 'alert', '', ['题目未读全，其余题目需在学习通完成。']));
    if (fillBlocker(r)) body.append(notice('warn', 'info', '暂不能填入', [fillBlocker(r)]));
    body.append(noSubmit());
    const actions = el('div', 'sheet-actions'), go = btn(ready.length ? `开始填入 ${ready.length} 题` : '没有可以自动填入的答案', 'btn-primary', async () => {
      go.disabled = true;
      if (await startAction('fill', r.key)) { fillPhase = 'running'; renderFill(); } else go.disabled = false;
    }, 'pen');
    go.disabled = !ready.length || S.job.busy || !!fillBlocker(r);
    actions.append(btn('取消', '', () => dialog.close()), Object.assign(btn('仅打开作业网页', '', async () => { if (await startAction('open', r.key)) dialog.close(); }, 'external'), {disabled: S.job.busy || !S.online}), go);
    body.append(actions);
    return;
  }
  if (fillPhase === 'running') {
    body.append(titled('fill-title', '正在自动填入'));
    body.append(el('p', 'sheet-text', '填入期间请勿操作学习通窗口。'));
    const live = el('div', 'live');
    live.append(icon('loader'), document.createTextNode(' ' + (S.job.message || '正在处理…')));
    body.append(live, noSubmit());
    const actions = el('div', 'sheet-actions');
    actions.append(btn('在后台等待', '', () => dialog.close()));
    body.append(actions);
    return;
  }
  const res = fillResult || {};
  const filled = res.filled || [], skipped = res.skipped || [];
  const failed = !!res.error, notRun = !failed && !filled.length && !skipped.length;
  const hero = el('div', 'result-hero'), ic = el('div', 'result-icon ' + (failed ? 'fail' : filled.length ? 'ok' : 'warn'));
  ic.append(icon(failed ? 'alert' : filled.length ? 'check' : 'info'));
  const t = el('div');
  const title = failed ? '自动填入失败' : notRun ? '未执行填入' : filled.length ? `已填入 ${filled.length} 题` : '没有题目被填入';
  t.append(titled('fill-title', title));
  hero.append(ic, t);
  body.append(hero);
  if (res.message) body.append(el('p', 'sheet-text', res.message));
  if (!failed && !notRun) {
    const indexOf = id => r.questions.findIndex(q => String(q.id) === String(id));
    const reported = new Set([...filled.map(String), ...skipped.map(s => String(s.id))]);
    const rows = skipped.map(s => {
      const i = indexOf(s.id), kind = classify(s.reason || '');
      return {i, kind, reason: s.reason, files: kind === 'upload' && i >= 0 ? draft.answers[r.questions[i].id]?.files : null};
    });
    // An entry without id means the page-level check stopped the whole fill.
    if (!skipped.some(s => !s.id)) r.questions.forEach((q, i) => { if (!reported.has(String(q.id))) rows.push({i, kind: isUnsupported(q) ? 'unsupported' : 'empty', reason: ''}); });
    rows.sort((a, b) => (a.i < 0 ? 1e9 : a.i) - (b.i < 0 ? 1e9 : b.i));
    const uploads = rows.filter(x => x.kind === 'upload').length;
    const stats = el('div', 'stats');
      stats.append(statCard(filled.length, '已填入', 'ok'), statCard(rows.length, '已跳过', rows.length ? 'warn' : ''), statCard(uploads, '需手动上传', uploads ? 'warn' : ''));
    body.append(stats);
    if (rows.length) {
      body.append(el('div', 'section-label', '跳过的题目与原因'));
      const ul = el('ul', 'reasons');
      ul.append(...reasonRows(rows));
      body.append(ul);
    }
  }
  body.append(noSubmit());
  const actions = el('div', 'sheet-actions');
  if (failed || notRun) actions.append(Object.assign(btn('重新检查', '', () => { fillPhase = 'check'; renderFill(); }), {disabled: S.job.busy}));
  actions.append(btn('知道了', 'btn-dark', () => dialog.close()));
  body.append(actions);
}
function fillDone(job, key) {
  fillPhase = 'result';
  fillResult = {...(job.result || {}), message: job.message || job.result?.message};
  if (key && key !== S.key) { toast(job.message || '自动填入已结束', job.result?.error ? 'error' : 'ok'); return; }
  renderFill();
  const d = $('#fill-dialog');
  if (!d.open) d.showModal();
}

/* ---------- Reminder & exit ---------- */
function reminderSlots(r) {
  const now = new Date();
  return (r?.times || []).map(value => {
    const [hour, minute] = value.split(':').map(Number);
    const target = new Date(now);
    target.setHours(hour, minute, 0, 0);
    if (target <= now) target.setDate(target.getDate() + 1);
    return {value, target, day: dayLabel(target)};
  }).sort((a, b) => a.target - b.target);
}
function reminderNextText(r) {
  const slots = reminderSlots(r);
  if (!slots.length) return '今天的提醒时间已过 · 尚未设置下一次提醒';
  const next = slots[0];
  const todayCount = slots.filter(slot => slot.day === '今天').length;
  const prefix = next.day === '今天' ? '今天' : '明天';
  const passed = todayCount === 0 ? '今天的提醒时间已过；' : '';
  return `${passed}${prefix} ${next.value} · ${left(next.target - Date.now())} · 今天还会提醒 ${todayCount} 次`;
}
function reminderSlotText(slot) {
  return `${slot.day === '今天' ? '今天' : '明天'} · ${left(slot.target - Date.now())}`;
}
async function saveReminder(next, success = '') {
  try {
    S.reminder = await api('/api/reminder', next);
    if (success) toast(success, 'ok');
    renderReminder();
    renderReminderRow();
    return true;
  } catch (e) {
    toast('提醒设置未更改：' + e.message, 'error');
    renderReminder();
    return false;
  }
}
function renderReminder() {
  const body = $('#reminder-body'), dialog = $('#reminder-dialog'), r = S.reminder;
  body.replaceChildren(closeButton(dialog), el('div', 'sheet-eyebrow', '本机提醒'));
  body.append(titled('reminder-title', '作业提醒'));
  if (!r) { body.append(el('p', 'sheet-text', '正在读取提醒状态…')); return; }
  const row = el('div', 'switch-row'), text = el('div', 'sr-text'), sw = el('button', 'switch');
  text.append(el('strong', '', r.enabled ? '提醒已开启' : '提醒已关闭'));
  text.append(el('small', '', '助手运行期间有效：关掉页面仍会提醒；退出助手后停止'));
  sw.type = 'button';
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', String(r.enabled));
  sw.setAttribute('aria-label', '作业提醒');
  sw.onclick = async () => {
    sw.disabled = true;
    await saveReminder({enabled: !r.enabled, times: r.times}, r.enabled ? '作业提醒已关闭' : '作业提醒已开启');
  };
  row.append(text, sw);
  body.append(row);
  if (!r.enabled) return;

  const slots = reminderSlots(r);
  const schedule = el('section', 'reminder-schedule'), head = el('div', 'reminder-section-head');
  head.append(el('strong', '', '提醒时间'), el('span', '', `${(r.times || []).length} / ${r.max_times || 12}`));
  schedule.append(head);
  const list = el('div', 'reminder-time-list');
  (r.times || []).forEach((value, index) => {
    const item = el('div', 'reminder-time-item'), input = el('input', 'input');
    input.type = 'time'; input.value = value; input.setAttribute('aria-label', `提醒时间 ${index + 1}`);
    input.onchange = async () => {
      const times = [...r.times]; times[index] = input.value;
      await saveReminder({enabled: true, times}, '提醒时间已保存');
    };
    const meta = el('span', 'reminder-time-meta', reminderSlotText(slots.find(slot => slot.value === value) || {day: '今天', target: new Date()}));
    const remove = btn('', 'btn-quiet icon-only', async () => {
      const times = r.times.filter((_, i) => i !== index);
      await saveReminder({enabled: true, times}, '提醒时间已删除');
    }, 'trash');
    remove.setAttribute('aria-label', `删除 ${value}`);
    item.append(input, meta, remove); list.append(item);
  });
  if (!(r.times || []).length) list.append(el('p', 'reminder-empty', '还没有设置提醒时间。'));
  schedule.append(list);
  const add = btn('添加时间', 'btn-sm', async () => {
    const used = new Set(r.times || []), candidates = ['08:30', '12:00', '19:00', '21:00'];
    const value = candidates.find(item => !used.has(item)) || Array.from({length: 24}, (_, hour) => `${pad(hour)}:00`).find(item => !used.has(item));
    if (!value) return;
    await saveReminder({enabled: true, times: [...(r.times || []), value]}, '提醒时间已添加');
  }, 'plus');
  add.disabled = (r.times || []).length >= (r.max_times || 12);
  schedule.append(add);
  body.append(schedule);

  const next = el('div', 'reminder-next-card');
  next.append(el('strong', '', '下次提醒'), el('span', '', reminderNextText(r)));
  body.append(next);
}

async function exitApp() {
  const confirmBtn = $('#exit-confirm');
  confirmBtn.disabled = true;
  try {
    await flushAll();
  } catch (e) {
    confirmBtn.disabled = false;
    toast('答案尚未保存成功，已取消退出：' + e.message, 'error');
    return;
  }
  try {
    await api('/api/exit', {});
  } catch (e) {
    confirmBtn.disabled = false;
    toast(e.message, 'error');
    return;
  }
  const end = el('div', 'ended'), inner = el('div');
  inner.append(spark(), el('h1', '', '助手已退出'), el('p', '', '答案已保存，提醒已停止。'));
  end.append(inner);
  document.body.replaceChildren(end);
  S.exited = true;
}

/* ---------- Load & poll ---------- */
async function load() {
  await flushAll().catch(() => {});
  const data = await api('/api/state');
  S.records = data.assignments || [];
  S.aliases = data.record_aliases || {};
  for (const r of S.records) if (r.group === 'history') discardHistoryDraft(r.key);
  S.lastSuccess = data.last_success;
  S.warnings = data.last_stats?.warnings || [];
  S.loaded = true;
  S.loadError = null;
  applyJob(data.job);
  const prev = S.key;
  S.key = S.key ? canonicalKey(S.key) : null;
  for (const k of [...drafts.keys()]) if (!dirty.has(k) && !saving.has(k) && !(k === S.key && k === prev)) drafts.delete(k);
  renderList();
  renderStatus(true);
  if (S.key) {
    const r = record();
    if (!r) {
      S.key = null;
      setView('list');
      renderWelcome();
      toast('这份作业已随列表更新，请重新选择', 'warn');
    } else if (S.key !== prev || !drafts.has(S.key) || fingerprint(r) !== S.printed) {
      await select(S.key, true);
    } else {
      renderDock(r);
      renderReading(r);
    }
  } else {
    renderWelcome();
  }
  return data;
}
async function poll() {
  if (S.exited) return;
  try {
    const job = await api('/api/job');
    S.serverLost = false;
    applyJob(job);
    if (S.revision !== job.revision) {
      const first = S.revision === -1, key = S.jobKey;
      S.revision = job.revision;
      if (!first) {
        S.jobKey = null;
        await load().catch(e => toast(e.message, 'error'));
        onJobDone(job, key);
      }
    }
  } catch {
    S.serverLost = true;
  }
  renderStatus();
  setTimeout(poll, S.job.busy ? 1000 : 2000);
}
async function boot() {
  S.loadError = null;
  renderList();
  try {
    const data = await load();
    S.revision = data.job.revision;
  } catch (e) {
    S.loadError = e.message;
    S.serverLost = !!e.offline;
    renderList();
    renderWelcome();
    renderStatus(true);
  }
}

/* ---------- Wiring ---------- */
document.querySelectorAll('[data-icon]').forEach(n => n.replaceWith(icon(n.dataset.icon)));
$('#image-close').append(icon('x'));
document.querySelectorAll('.tab').forEach(t => t.onclick = () => { S.group = t.dataset.group; S.sub = 'all'; S.progressFilter = S.dueFilter = 'all'; renderList(); });
$('#home').onclick = goHome;
$('#home-top').onclick = goHome;
$('#progress-filter').onchange = e => { S.progressFilter = e.target.value; renderList(); };
$('#due-filter').onchange = e => { S.dueFilter = e.target.value; renderList(); };
$('#search').oninput = renderList;
$('#search').onkeydown = e => { if (e.key === 'Escape') { e.target.value = ''; renderList(); } };
$('#refresh').onclick = () => startAction('refresh');
$('#back').onclick = () => setView('list');
$('#account-login-button').onclick = () => { closeAccountMenu(); openLogin(); };
$('#reminder-button').onclick = () => { closeAccountMenu(); renderReminder(); $('#reminder-dialog').showModal(); };
$('#account-exit-button').onclick = () => { closeAccountMenu(); $('#exit-dialog').showModal(); };
$('#exit-cancel').onclick = () => $('#exit-dialog').close();
$('#exit-confirm').onclick = exitApp;
$('#image-close').onclick = () => $('#image-dialog').close();
$('.viewer-stage').onclick = e => { if (e.target.tagName === 'IMG') e.currentTarget.classList.toggle('actual'); else $('#image-dialog').close(); };
$('#detail').addEventListener('click', e => {
  const act = e.target.closest('[data-act]');
  if (act && S.key) { startAction(act.dataset.act === 'open' ? 'open' : 'refresh', S.key); return; }
  const img = e.target.closest('.rich img');
  if (img && !img.closest('a')) {
    e.preventDefault();
    $('#image-large').src = img.src;
    $('.viewer-stage').classList.remove('actual');
    $('#image-dialog').showModal();
  }
});
$('#detail').addEventListener('scroll', () => document.querySelector('.qnav')?.classList.toggle('stuck', $('#detail').scrollTop > 180), {passive: true});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && accountMenuOpen && !document.querySelector('dialog[open]')) {
    closeAccountMenu();
    $('#account-button')?.focus();
    return;
  }
  if (e.key === '/' && !e.ctrlKey && !e.metaKey && !/^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName) && !document.querySelector('dialog[open]')) {
    e.preventDefault();
    setView('list');
    $('#search').focus();
  }
});
document.addEventListener('click', e => {
  if (accountMenuOpen && !e.target.closest('.side-foot')) closeAccountMenu();
});
window.addEventListener('online', () => { S.online = true; renderStatus(); });
window.addEventListener('offline', () => { S.online = false; renderStatus(); });
window.addEventListener('beforeunload', e => { if (dirty.size) { e.preventDefault(); e.returnValue = ''; } });
window.addEventListener('pagehide', () => {
  for (const key of dirty.keys()) {
    try {
      fetch('/api/draft', {method: 'POST', keepalive: true, credentials: 'same-origin', headers: {'X-App-Token': token, 'Content-Type': 'application/json'}, body: JSON.stringify({key, draft: draftPayload(key)})});
    } catch {}
  }
});
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flushAll().catch(() => {}); });
setInterval(() => {
  if (!S.exited && !S.job.busy) {
    renderReminderRow();
    // Local API only: update deadline groups and summaries without crawling.
    load().catch(() => { if (!S.key) renderWelcome(); });
  }
}, 60000);
boot().finally(poll);
