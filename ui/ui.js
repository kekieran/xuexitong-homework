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
  loaded: false, loadError: null, lastSuccess: null, lastCheck: null, warnings: [],
  job: {busy: false, action: '', message: ''}, jobKey: null, revision: -1,
  auth: null, reminder: null, online: navigator.onLine, serverLost: false,
  selecting: 0, printed: null, statusMemo: '', exited: false,
  // Refresh feedback: when the running job began, how the last refresh ended,
  // and which records the last refresh added or changed (key -> timestamp).
  jobStart: 0, outcome: null, fresh: null, diff: null,
  refreshUi: {pct: 0, course: '', countText: '', phase: '', finTimer: null, finLabel: ''},
  diffRibbon: null,
  firstListAnimated: false, tabDir: 0, tabSeq: 0, tabTimer: 0, tabSwapEnter: false, prevCounts: null,
  addedKeys: null, deadlineChangedKeys: null,
  waveState: '', acctRevealState: '',
  watchingSub: new Map(), subCheckBusyKey: null, subCheckSeq: 0,
  navDir: '', prevDoneDots: new Map(), aiHintKeys: new Map(),
  welcomeAnimated: false, bellRingUntil: 0, remOpenedKey: null,
  dockPct: null, filterEnter: false, prevFilterCount: -1,
  lastImage: null, prevDockKey: null, listSelBox: null,
  historyExpanded: false, historyCleanConfirm: false,
  accountRemoveConfirmId: null, accountRenameId: null, accountRenameValue: '', accountRenameBusy: false,
  loginListSig: '',
};
const HISTORY_FOLD_MS = 45 * 86400 * 1000;
function recordTimeMs(raw) {
  if (!raw || typeof raw !== 'string') return null;
  const ms = Date.parse(raw.trim().replace(' ', 'T'));
  return Number.isFinite(ms) ? ms : null;
}
function isOlderHistoryRecord(r, nowMs = Date.now()) {
  if (!r || r.group !== 'history') return false;
  const dl = recordTimeMs(r.deadline);
  if (dl && dl > nowMs) return false;
  const ref = recordTimeMs(r.deadline || r.start || r.notice_sent || r.status_checked_at || r.metadata_checked_at || r.history_cleaned_at);
  return !!(ref && (nowMs - ref) > HISTORY_FOLD_MS);
}
const GROUP_IDX = {active: 0, review: 1, history: 2};
function switchGroup(nextGroup) {
  if (nextGroup && nextGroup !== S.group) {
    S.tabDir = (GROUP_IDX[nextGroup] ?? 0) > (GROUP_IDX[S.group] ?? 0) ? 1 : -1;
    S.group = nextGroup;
  }
}
const OUTCOME_MS = 4500, FRESH_MS = 4200;
const REDUCED_MOTION = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
const drafts = new Map(), dirty = new Map(), timers = new Map(), saving = new Map(), saveState = new Map();
const draftChanges = new Map();
let loginPhase = 'form', loginError = '', loginAddMode = false;
let loginView = 'form', loginTarget = null, loginFromList = false, loginAliasDraft = '';
let fillPhase = 'check', fillResult = null;
let accountMenuOpen = false;
let warningDismissal;

function restartAnim(node, cls) {
  if (!node) return;
  node.classList.remove(cls);
  void node.offsetWidth;
  node.classList.add(cls);
}
function animateNumber(node, target, suffix = '', fromStart = false) {
  if (!node) return;
  const end = Math.max(0, Math.round(Number(target) || 0));
  const prev = fromStart ? 0 : (node.dataset.numVal !== undefined ? Number(node.dataset.numVal) : end);
  node.dataset.numVal = String(end);
  if (REDUCED_MOTION() || prev === end) {
    node.textContent = String(end) + suffix;
    return;
  }
  const t0 = performance.now(), dur = 420;
  const tick = now => {
    const p = Math.min(1, (now - t0) / dur);
    const eased = 1 - Math.pow(1 - p, 3);
    node.textContent = String(Math.round(prev + (end - prev) * eased)) + suffix;
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}
function appendHighlighted(node, text, query) {
  const s = String(text ?? '');
  const q = (query || '').trim();
  if (!q) { node.append(document.createTextNode(s)); return; }
  const lower = s.toLowerCase(), ql = q.toLowerCase();
  let pos = 0;
  while (pos < s.length) {
    const idx = lower.indexOf(ql, pos);
    if (idx < 0) { node.append(document.createTextNode(s.slice(pos))); break; }
    if (idx > pos) node.append(document.createTextNode(s.slice(pos, idx)));
    node.append(el('mark', '', s.slice(idx, idx + q.length)));
    pos = idx + q.length;
  }
}
function animRing(ring, fromPct, toPct) {
  if (!ring) return;
  const start = Math.max(0, Math.min(100, Number(fromPct) || 0));
  const end = Math.max(0, Math.min(100, Number(toPct) || 0));
  ring._animPct = end;
  ring.classList.toggle('done', end >= 100);
  if (REDUCED_MOTION() || start === end) {
    ring.style.setProperty('--p', String(end));
    return;
  }
  const t0 = performance.now(), dur = 340;
  const tick = now => {
    if (ring._animPct !== end) return;
    const p = Math.min(1, (now - t0) / dur);
    const eased = 1 - Math.pow(1 - p, 3);
    ring.style.setProperty('--p', (start + (end - start) * eased).toFixed(2));
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

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
  xcircle: '<circle cx="12" cy="12" r="9"/><path d="M9.2 9.2l5.6 5.6M14.8 9.2l-5.6 5.6"/>',
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
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
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
  svg.setAttribute('width', '20');
  svg.setAttribute('height', '20');
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
// 统计卡片：供填入检查、填入结果与 AI 作答结果弹窗共用。
function statCard(value, label, tone = '') {
  const card = el('div', 'stat ' + tone);
  card.append(el('b', '', String(value)), el('small', '', label));
  return card;
}
function noSubmitNote(text = NO_SUBMIT) {
  const n = el('div', 'no-submit');
  n.append(icon('lock'), el('span', '', text));
  return n;
}
function externalLink(href, label, cls = 'link-chip') {
  const a = el('a', cls);
  a.href = href;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.append(icon('external'), el('span', '', label));
  return a;
}
// Long one-line messages read better as a short headline plus a detail line.
function splitMessage(text) {
  let m = text.match(/^(.{2,30}?)[：:]\s*(\S[\s\S]*)$/) || text.match(/^(.{2,}?)\s·\s(.+)$/) || text.match(/^([^。]{4,}?)。(\S[\s\S]*)$/);
  return m ? [m[1], m[2]] : [text, ''];
}
const TOAST_LIFE = {error: 8000, warn: 5500, ok: 3200, '': 3200};
function toast(message, tone = '', opts = {}) {
  const box = $('#toasts');
  // The same message twice in a row replaces the first instead of stacking.
  for (const old of box.children) if (old.dataset.message === message) old.remove();
  const [title, detail] = opts.title ? [opts.title, opts.detail || ''] : splitMessage(message);
  const t = el('div', 'toast ' + tone), mark = el('span', 'toast-mark'), body = el('div', 'toast-body');
  t.dataset.message = message;
  t.setAttribute('role', tone === 'error' ? 'alert' : 'status');
  mark.append(icon(tone === 'error' ? 'xcircle' : tone === 'warn' ? 'alert' : 'check'));
  body.append(el('div', 'toast-title', title));
  if (detail) body.append(el('div', 'toast-detail', detail));
  t.append(mark, body);
  let remaining = opts.duration || TOAST_LIFE[tone] || 3200, started = 0, timer = 0, gone = false;
  const dismiss = () => {
    if (gone) return;
    gone = true; clearTimeout(timer);
    t.style.maxHeight = t.offsetHeight + 'px';
    requestAnimationFrame(() => { t.classList.add('leaving'); t.style.maxHeight = '0px'; });
    setTimeout(() => t.remove(), 260);
  };
  if (opts.action) {
    const act = btn(opts.action.label, 'btn-sm toast-action', () => { dismiss(); opts.action.onClick(); });
    t.append(act);
  }
  if (tone === 'error' || tone === 'warn' || opts.action) {
    const x = el('button', 'toast-x');
    x.type = 'button';
    x.setAttribute('aria-label', '关闭提示');
    x.append(icon('x'));
    x.onclick = dismiss;
    t.append(x);
  }
  // The countdown hairline and the dismiss timer pause together while hovered.
  const bar = el('i', 'toast-timer');
  bar.style.animationDuration = remaining + 'ms';
  t.append(bar);
  const run = () => { started = Date.now(); timer = setTimeout(dismiss, remaining); t.classList.remove('paused'); };
  const hold = () => { if (gone || !started) return; clearTimeout(timer); remaining -= Date.now() - started; started = 0; t.classList.add('paused'); };
  t.addEventListener('mouseenter', hold);
  t.addEventListener('focusin', hold);
  t.addEventListener('mouseleave', () => { if (!gone && !started && !t.matches(':focus-within')) run(); });
  t.addEventListener('focusout', () => { if (!gone && !started && !t.matches(':hover')) run(); });
  box.append(t);
  while (box.children.length > 3) box.firstChild.remove();
  run();
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
const hasValue = HomeworkWorkspace.hasValue;
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
function aliasKeys(key) {
  return [...new Set([key, ...Object.keys(S.aliases).filter(k => canonicalKey(k) === key)])];
}
function discardHistoryDraft(key) {
  for (const old of aliasKeys(key)) {
    clearTimeout(timers.get(old)); timers.delete(old);
    dirty.delete(old); drafts.delete(old); saveState.delete(old); draftChanges.delete(old);
    try { localStorage.removeItem('homework-recovery:' + old); } catch {}
  }
}
function removeAttachmentFromRecovery(key, qid, fileId) {
  for (const recoveryKey of aliasKeys(key)) {
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
  drafts.set(key, draft);
  let recovered = false;
  try {
    const keys = aliasKeys(key);
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
    if (recovered) {
      markDirty(key);
      for (const {key: oldKey} of recoveries) if (oldKey !== key) localStorage.removeItem('homework-recovery:' + oldKey);
      toast('已恢复上次未完成保存的答案', 'warn');
    }
  } catch {}
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
// The platform editor wraps the assignment title in a link back to its own work
// page. Those navigation links are not attachments and must not be clickable
// inside the question text; file links on the platform CDN stay untouched.
const PLATFORM_PAGE = /^https?:\/\/[^/]+\/(?:mooc-ans|work|course|mycourse|exam|notice|task-list|getallwork)\//i;
function renderRich(node, html, text) {
  node.classList.add('rich');
  if (!html) {
    node.textContent = text || '';
    return;
  }
  const tmpl = document.createElement('template');
  tmpl.innerHTML = html;
  tmpl.content.querySelectorAll('script,style,iframe,object,embed,form,input,button,textarea,select').forEach(n => n.remove());
  // The platform editor leaves runs of empty paragraphs (and <p><br></p>) after
  // the real content. Each one is a full line plus margin, so they show up as a
  // large blank band between the stem and the options. Drop only blocks that
  // carry neither text nor media, so real images and tables always stay.
  tmpl.content.querySelectorAll('p').forEach(p => {
    if (!p.textContent.trim() && !p.querySelector('img,audio,video,iframe,math,svg,table')) p.remove();
  });
  tmpl.content.querySelectorAll('*').forEach(n => {
    for (const attr of [...n.attributes]) if (/^on/i.test(attr.name) || attr.name === 'style') n.removeAttribute(attr.name);
  });
  tmpl.content.querySelectorAll('a').forEach(a => {
    const href = a.getAttribute('href') || '';
    if (!/^https?:\/\//i.test(href) || PLATFORM_PAGE.test(href)) { a.removeAttribute('href'); return; }
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
  });
  tmpl.content.querySelectorAll('img').forEach(img => {
    const src = img.getAttribute('src') || '';
    if (/^https?:\/\//i.test(src)) { img.replaceWith(mediaFallback('uncached', src)); return; }
    img.decoding = 'async';
    // Big images become block only when they stand alone. A formula image that
    // sits inside a line of text must stay inline, otherwise it breaks the
    // sentence and picks up the framed look meant for pictures.
    img.onload = () => {
      const holder = img.closest('p, li, td');
      const inline = !!holder && holder.textContent.trim().length > 0;
      if (!inline && (img.naturalWidth > 160 || img.naturalHeight > 60)) img.classList.add('block');
    };
    img.onerror = () => img.replaceWith(mediaFallback('failed'));
    if (img.complete && img.naturalWidth) img.onload();
  });
  tmpl.content.querySelectorAll('audio,video').forEach(media => {
    const src = media.getAttribute('src') || media.querySelector('source')?.getAttribute('src') || '';
    if (/^https?:\/\//i.test(src)) media.replaceWith(mediaFallback('media', src));
    else media.remove();
  });
  node.replaceChildren(tmpl.content);
}

/* ---------- Sidebar ---------- */
function progressFor(r) {
  return drafts.has(r.key) ? HomeworkWorkspace.progress(r, drafts.get(r.key)) : r.progress || HomeworkWorkspace.progress(r);
}
function overview() {
  return HomeworkWorkspace.dashboard(S.records.map(r => ({...r, progress: progressFor(r)})));
}
function showWorkspaceList(group, progress = 'all', dueRange = 'all') {
  clearTimeout(S.tabTimer); S.tabTimer = 0; ++S.tabSeq; S.tabSwapEnter = false;
  $('#assignment-list')?.classList.remove('tab-leaving');
  switchGroup(group); S.sub = 'all'; S.progressFilter = progress; S.dueFilter = dueRange;
  $('#search').value = '';
  renderList(); setView('list');
}
async function goHome() {
  ++S.selecting; S.printed = null; S.dockPct = null; S.prevDockKey = null;
  $('.main').querySelector('.reading')?.remove();
  const fromDetail = !!S.key;
  try { await flushAll(); } catch {}
  if (fromDetail) snapshotView(-1);
  S.key = null; S.navDir = fromDetail ? 'left' : '';
  setView('detail'); renderList(); renderWelcome();
  try { await load(); }
  catch (e) { toast('首页显示本机数据，更新失败：' + e.message, 'warn'); }
}
function syncTabsAndFilters() {
  const tabsBar = $('.tabs');
  if (tabsBar) {
    tabsBar.classList.add('has-pill');
    tabsBar.style.setProperty('--tab-idx', String(GROUP_IDX[S.group] ?? 0));
  }
  document.querySelectorAll('.tab').forEach(t => t.setAttribute('aria-selected', String(t.dataset.group === S.group)));
  $('#home').setAttribute('aria-current', S.key ? 'false' : 'page');
  const wsOpen = S.loaded && S.group === 'active';
  const wsFold = $('#workspace-filters-fold');
  if (wsFold) {
    wsFold.classList.toggle('open', wsOpen);
    wsFold.setAttribute('aria-hidden', String(!wsOpen));
  }
  const wsBox = $('#workspace-filters');
  if (wsBox) wsBox.hidden = false;
  $('#progress-filter').value = S.progressFilter;
  $('#due-filter').value = S.dueFilter;
  $('#progress-filter').classList.toggle('on', S.progressFilter !== 'all');
  $('#due-filter').classList.toggle('on', S.dueFilter !== 'all');
  const subs = SUBS[S.group], box = $('#subfilters'), subFold = $('#subfilters-fold');
  const subOpen = !!(subs && S.loaded);
  if (subFold) {
    subFold.classList.toggle('open', subOpen);
    subFold.setAttribute('aria-hidden', String(!subOpen));
  }
  if (box) {
    box.hidden = false;
    if (subs) {
      box.replaceChildren();
      const inGroup = S.records.filter(r => r.group === S.group);
      for (const [id, label] of subs) {
        const chip = el('button', 'chip', label);
        chip.type = 'button';
        chip.append(el('span', 'n', String(inGroup.filter(r => matchesSub(r, id)).length)));
        chip.setAttribute('aria-pressed', String(S.sub === id));
        chip.onclick = () => { S.sub = id; S.filterEnter = true; renderList(); };
        box.append(chip);
      }
    }
  }
}
function transitionToGroup(next) {
  if (!next || next === S.group) return;
  const seq = ++S.tabSeq;
  clearTimeout(S.tabTimer);
  switchGroup(next); S.sub = 'all'; S.progressFilter = S.dueFilter = 'all';
  syncTabsAndFilters();
  const list = $('#assignment-list');
  if (!S.loaded || REDUCED_MOTION() || !list || !list.children.length) {
    S.tabSwapEnter = false;
    renderList();
    return;
  }
  list.classList.remove('tab-entering');
  void list.offsetWidth;
  list.classList.add('tab-leaving');
  S.tabTimer = setTimeout(() => {
    if (seq !== S.tabSeq) return;
    S.tabTimer = 0;
    S.tabSwapEnter = true;
    renderList();
  }, 105);
}
function renderList() {
  if (S.tabTimer && !S.tabSwapEnter) {
    clearTimeout(S.tabTimer);
    S.tabTimer = 0;
  }
  const entering = S.tabSwapEnter;
  S.tabSwapEnter = false;
  const prevCounts = S.prevCounts;
  const nextCounts = {};
  for (const g of Object.keys(GROUPS)) {
    const n = S.records.filter(r => r.group === g).length, count = $('#count-' + g);
    nextCounts[g] = n;
    count.textContent = n;
    count.classList.toggle('has', n > 0 && g !== 'history');
    if (S.loaded && prevCounts && prevCounts[g] !== undefined && prevCounts[g] !== n) {
      count.classList.remove('bump');
      void count.offsetWidth;
      count.classList.add('bump');
    }
  }
  if (S.loaded) S.prevCounts = nextCounts;
  syncTabsAndFilters();
  const list = $('#assignment-list');
  const prevScroll = entering ? 0 : list.scrollTop;
  const previousSel = list.querySelector('.list-sel');
  list.classList.remove('tab-leaving');
  list.replaceChildren();
  const sel = previousSel || el('div', 'list-sel');
  list.append(sel);
  if (!S.loaded) {
    sel.style.opacity = '0';
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
  const firstIn = !S.firstListAnimated;
  S.firstListAnimated = true;
  S.tabDir = 0;
  const filterEnter = S.filterEnter;
  S.filterEnter = false;
  const query = $('#search').value.trim();
  const qLower = query.toLowerCase();
  const match = r => !qLower || `${r.course || ''} ${r.title || ''}`.toLowerCase().includes(qLower);
  const dueKeys = S.group === 'active' && S.dueFilter !== 'all' ? new Set(overview().keys[S.dueFilter] || []) : null;
  const items = S.records.filter(r => r.group === S.group && matchesSub(r, S.sub) && match(r) &&
    (S.group !== 'active' || S.progressFilter === 'all' || progressFor(r).state === S.progressFilter) &&
    (!dueKeys || dueKeys.has(r.key))).sort(sorter(S.group));
  const hasFilter = !!(qLower || S.sub !== 'all' || (S.group === 'active' && (S.progressFilter !== 'all' || S.dueFilter !== 'all')));
  const metaBox = $('#search-meta');
  if (metaBox) {
    metaBox.hidden = !hasFilter;
    if (hasFilter) {
      let countEl = metaBox.querySelector('.search-count');
      if (!countEl) {
        metaBox.replaceChildren(
          countEl = el('span', 'search-count', `已找到 ${items.length} 项`),
          btn('清除筛选', 'search-clear', () => {
            $('#search').value = '';
            S.sub = 'all'; S.progressFilter = S.dueFilter = 'all';
            S.filterEnter = true;
            renderList();
            $('#search').focus();
          })
        );
      } else {
        countEl.textContent = `已找到 ${items.length} 项`;
      }
      if (S.prevFilterCount !== items.length) {
        S.prevFilterCount = items.length;
        restartAnim(countEl, 'bump');
      }
    } else {
      S.prevFilterCount = -1;
    }
  }
  if (!items.length) {
    sel.style.opacity = '0';
    const emptyNode = listEmpty(qLower, match);
    if (!entering) emptyNode.classList.add('anim-in');
    list.append(emptyNode);
    list.scrollTop = prevScroll;
    if (entering) restartAnim(list, 'tab-entering');
    return;
  }
  let section = null, idx = 0;
  if (S.group === 'history' && !hasFilter) {
    const recentItems = [], olderItems = [];
    const nowMs = Date.now();
    for (const r of items) {
      if (isOlderHistoryRecord(r, nowMs)) olderItems.push(r);
      else recentItems.push(r);
    }
    for (const r of recentItems) list.append(listItem(r, idx++, firstIn, 0, !entering && filterEnter, query));
    if (olderItems.length) {
      const fold = el('div', 'history-fold');
      const head = el('div', 'history-fold-row');
      const toggle = el('button', 'history-fold-toggle');
      toggle.type = 'button';
      toggle.setAttribute('aria-expanded', String(S.historyExpanded));
      toggle.append(
        el('span', '', S.historyExpanded ? `收起更早历史（超过 45 天，${olderItems.length} 项）` : `显示更早历史（超过 45 天，${olderItems.length} 项）`),
        icon('chevron', S.historyExpanded ? 'rotated' : '')
      );
      toggle.onclick = () => {
        S.historyExpanded = !S.historyExpanded;
        S.historyCleanConfirm = false;
        renderList();
      };
      head.append(toggle);
      if (S.historyCleanConfirm) {
        const confirmBtn = btn('确认清理', 'btn-quiet btn-xs danger', async () => {
          S.historyCleanConfirm = false;
          await cleanupOldHistory();
        });
        const cancelBtn = btn('取消', 'btn-quiet btn-xs', () => {
          S.historyCleanConfirm = false;
          renderList();
        });
        confirmBtn.disabled = S.job.busy;
        head.append(confirmBtn, cancelBtn);
      } else {
        const cleanBtn = btn('清理更早历史', 'btn-quiet btn-xs', () => {
          S.historyCleanConfirm = true;
          renderList();
        });
        cleanBtn.disabled = S.job.busy;
        head.append(cleanBtn);
      }
      fold.append(head);
      list.append(fold);
      if (S.historyExpanded) {
        for (const r of olderItems) list.append(listItem(r, idx++, false, 0, false, query));
      }
    }
  } else {
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
      list.append(listItem(r, idx++, firstIn, 0, !entering && filterEnter, query));
    }
  }
  list.scrollTop = prevScroll;
  placeListSel(sel, !entering);
  if (entering) restartAnim(list, 'tab-entering');
  if (S.job.busy && S.job.action === 'refresh' && !S.jobKey) {
    list.querySelectorAll('.item').forEach((n, i) => { n.style.setProperty('--i', String(Math.min(i, 8))); n.classList.add('wave'); });
    syncScanningRows(S.refreshUi.course);
  }
}
function placeListSel(sel, animate = true) {
  const item = $('#assignment-list .item[aria-current="true"]');
  if (!sel) return;
  if (!item) {
    sel.style.transition = 'none';
    sel.style.opacity = '0';
    sel.dataset.ready = '';
    S.listSelBox = null;
    return;
  }
  const top = item.offsetTop, height = item.offsetHeight;
  const prev = S.listSelBox;
  S.listSelBox = { top, height, group: S.group };
  if (!animate || !sel.dataset.ready || !prev || prev.group !== S.group) {
    sel.style.transition = 'none';
    sel.style.transform = `translateY(${top}px)`;
    sel.style.height = height + 'px';
    sel.style.opacity = '1';
    void sel.offsetWidth;
    sel.style.transition = '';
    sel.dataset.ready = '1';
    return;
  }
  if (prev.top !== top || prev.height !== height) {
    sel.style.transition = 'none';
    sel.style.transform = `translateY(${prev.top}px)`;
    sel.style.height = prev.height + 'px';
    sel.style.opacity = '1';
    void sel.offsetWidth;
    sel.style.transition = '';
  }
  sel.style.opacity = '1';
  sel.style.transform = `translateY(${top}px)`;
  sel.style.height = height + 'px';
}
function listItem(r, idx = 0, firstIn = false, tabDir = 0, filterEnter = false, query = '') {
  const d = due(r), b = el('button', 'item' + (r.group === 'active' && d.tone === 'urgent' ? ' urgent' : ''));
  b.type = 'button';
  b.dataset.key = r.key;
  b.dataset.course = r.course || '';
  if (r.key === S.key) b.setAttribute('aria-current', 'true');
  // New or changed since the last refresh: continue the highlight where it was
  // even if the list is rebuilt mid-animation.
  const addedAge = S.addedKeys?.has(r.key) ? Date.now() - S.addedKeys.get(r.key) : FRESH_MS;
  const age = S.fresh?.has(r.key) ? Date.now() - S.fresh.get(r.key) : FRESH_MS;
  if (addedAge < FRESH_MS) {
    b.classList.add('fresh-new');
    b.style.animationDelay = -addedAge + 'ms';
  } else if (age < FRESH_MS) {
    b.classList.add('fresh');
    b.style.animationDelay = -age + 'ms';
  } else if (firstIn && idx < 12) {
    b.classList.add('list-in');
    b.style.setProperty('--i', String(idx));
  } else if (tabDir && idx < 14) {
    b.classList.add('tab-in');
    b.style.setProperty('--dx', String(tabDir));
    b.style.setProperty('--i', String(idx));
  } else if (filterEnter && idx < 12) {
    b.classList.add('filter-in');
    b.style.setProperty('--i', String(idx));
  }
  b.title = `${r.course || '未命名课程'}\n${r.title || '未命名作业'}`;
  const titleEl = el('span', 'item-title');
  if (addedAge < FRESH_MS) titleEl.prepend(el('span', 'new-badge', '新'));
  appendHighlighted(titleEl, r.course || '未命名课程', query);
  const subEl = el('span', 'item-sub');
  appendHighlighted(subEl, r.title || '未命名作业', query);
  b.append(titleEl, subEl);
  const foot = el('span', 'item-foot');
  if (r.group === 'review') {
    foot.append(el('span', 'flag warn', statusInfo(r).label));
  } else if (r.group === 'history') {
    foot.append(el('span', 'flag', statusInfo(r).label), dueTag(r, true));
  } else {
    foot.append(dueTag(r));
    if (S.watchingSub.get(r.key)?.state === 'unsubmitted') {
      foot.append(el('span', 'flag warn warn-blink', '未提交'));
    }
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
  const chgAge = !plain && S.deadlineChangedKeys?.has(r.key) ? Date.now() - S.deadlineChangedKeys.get(r.key) : FRESH_MS;
  const d = due(r), tag = el('span', 'due ' + (plain ? '' : d.tone) + (chgAge < FRESH_MS ? ' due-chg' : ''));
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
    for (const [g, n] of others) row.append(btn(`${GROUPS[g]}中有 ${n} 项`, 'btn-sm', () => transitionToGroup(g)));
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
function userInitial(username) {
  const ch = (username || '').trim().charAt(0);
  return ch ? ch.toUpperCase() : '学';
}
function savedAccountsList() {
  const raw = Array.isArray(S.auth?.accounts) ? S.auth.accounts : [];
  return raw.filter(x => x.username || x.has_uid || x.saved_credentials || x.has_session);
}
function activeAccountInfo() {
  const a = S.auth || {};
  const list = Array.isArray(a.accounts) ? a.accounts : [];
  return list.find(x => x.id === a.active_account_id) || list.find(x => x.active) || null;
}
function renderAccount() {
  const box = $('#account');
  box.replaceChildren();
  const a = S.auth || {state: 'checking', sites: {}};
  const loggingIn = S.job.busy && S.job.action === 'login' && !S.acctRevealState;
  const checking = (loggingIn || a.state === 'checking') && !S.acctRevealState;
  const loggedIn = a.state === 'logged_in';
  const act = activeAccountInfo();
  const username = (act?.username || a.credentials?.username || '').trim();
  const alias = (act?.alias || a.account_alias || '').trim();
  const displayName = alias || (act?.label || a.account_label || '').trim() || username;
  const schoolLabel = act?.school_name || a.school_name || '作业通';
  const row = el('button', 'account-row'), avatar = el('div', 'avatar' + (loggedIn ? ' clay-ava' : ' off')),
    text = el('div', 'account-text');
  row.type = 'button';
  row.id = 'account-button';
  row.setAttribute('aria-haspopup', 'menu');
  row.setAttribute('aria-controls', 'account-menu');
  row.setAttribute('aria-expanded', String(accountMenuOpen));
  if (checking) {
    avatar.classList.remove('clay-ava');
    avatar.append(icon('loader'));
    text.append(el('strong', '', loggingIn ? '正在登录…' : '正在检查登录'));
    text.append(el('small', '', loggingIn && S.job.message !== '正在处理…' ? S.job.message : '请稍候'));
    row.disabled = true;
  } else {
    avatar.textContent = loggedIn ? userInitial(displayName || username) : '未';
    if (S.acctRevealState === 'hiding') {
      avatar.classList.add('ava-hide');
      text.classList.add('info-hide');
    } else if (S.acctRevealState === 'landing') {
      avatar.classList.add('land-soft');
      if (!avatar.querySelector('.ring-wave')) avatar.append(el('span', 'ring-wave'));
      if (!text.classList.contains('pop-ribbon')) {
        setTimeout(() => { if (text.isConnected && S.acctRevealState === 'landing') text.classList.add('pop-ribbon'); }, 120);
      }
    }
    text.append(el('strong', '', loggedIn ? (displayName || '作业通账号') : '登录作业通'));
    const sub = loggedIn
      ? (alias && username && alias !== username ? `${username} · ${schoolLabel}` : schoolLabel)
      : everLoggedIn() ? '登录已失效' : '尚未登录';
    text.append(el('small', '', sub));
    row.onclick = e => { e.stopPropagation(); toggleAccountMenu(); };
  }
  if (!checking) row.append(icon('chevron', 'account-chevron'));
  row.prepend(avatar, text);
  box.append(row);
  renderAccountMenu();
}
function renderAccountMenu() {
  const menu = $('#account-menu'), a = S.auth || {state: 'checking', sites: {}};
  const wasOpen = !menu.hidden && menu.classList.contains('open');
  menu.hidden = !accountMenuOpen;
  if (accountMenuOpen) {
    if (!wasOpen) {
      menu.classList.remove('open');
      requestAnimationFrame(() => { if (accountMenuOpen && !menu.hidden) menu.classList.add('open'); });
    }
  } else {
    menu.classList.remove('open');
  }
  menu.setAttribute('role', 'menu');
  const act = activeAccountInfo();
  const username = (act?.username || a.credentials?.username || '').trim();
  const alias = (act?.alias || a.account_alias || '').trim();
  const displayName = alias || (act?.label || a.account_label || '').trim() || username;
  $('#account-menu-name').textContent = (alias && username && alias !== username)
    ? `${alias} · ${username}`
    : (displayName || (a.state === 'logged_in' ? '作业通账号' : '作业助手'));
  $('#account-menu-state').textContent = a.state === 'logged_in' ? '作业通已登录' : everLoggedIn() ? '登录已失效' : '尚未登录';
  const meta = $('#account-manage-meta');
  if (meta) {
    const accounts = savedAccountsList();
    if (accounts.length > 1) {
      meta.textContent = `${accounts.length} 个账号 · ${a.state === 'logged_in' ? '已登录' : '待登录'}`;
    } else if (accounts.length === 1) {
      meta.textContent = a.state === 'logged_in' ? '已登录' : everLoggedIn() ? '登录已失效' : '点击重登';
    } else {
      meta.textContent = '点击登录';
    }
  }
}
async function cleanupOldHistory() {
  try {
    const res = await api('/api/history/cleanup', {days: 45});
    S.historyExpanded = false;
    S.historyCleanConfirm = false;
    await load();
    toast(res.removed_records ? `已清理 ${res.removed_records} 项更早历史` : '当前没有可清理的更早历史', 'ok');
  } catch (e) {
    toast(e.message, 'error');
  }
}
function resetDraftStateForAccountSwitch() {
  drafts.clear();
  dirty.clear();
  timers.forEach(t => clearTimeout(t));
  timers.clear();
  saving.clear();
  saveState.clear();
  draftChanges.clear();
  S.key = null;
  S.historyExpanded = false;
  S.historyCleanConfirm = false;
}
async function switchAccount(id) {
  if (!id || S.job.busy) return;
  try {
    closeAccountMenu();
    closeSheet($('#login-dialog'));
    await flushAll().catch(() => {});
    resetDraftStateForAccountSwitch();
    const res = await api('/api/account/switch', {id});
    S.auth = res.auth || S.auth;
    renderAccount();
    const data = await load();
    toast(`已切换到 ${res.account?.label || '选定账号'}`, 'ok');
    if (!data?.last_success && !S.job.busy && S.online) {
      startAction('refresh').catch(() => {});
    }
  } catch (e) {
    toast(e.message, 'error');
  }
}
async function renameAccount(id, label) {
  if (!id || S.job.busy || S.accountRenameBusy) return false;
  S.accountRenameBusy = true;
  try {
    const res = await api('/api/account/rename', {id, label});
    S.auth = res.auth || S.auth;
    S.accountRenameId = null;
    S.accountRenameValue = '';
    S.loginListSig = '';
    renderAccount();
    if ($('#login-dialog').open && loginPhase === 'form') {
      const listPane = $('#login-body .m3-view[data-pane="list"]');
      if (listPane) renderM3ListPane(listPane);
    }
    toast(res.account?.alias ? `已将备注设为「${res.account.alias}」` : '已恢复默认账号名称', 'ok');
    return true;
  } catch (e) {
    toast(e.message, 'error');
    return false;
  } finally {
    S.accountRenameBusy = false;
  }
}
function startAccountRename(acct) {
  if (!acct || S.job.busy) return;
  S.accountRemoveConfirmId = null;
  S.accountRenameId = acct.id;
  S.accountRenameValue = acct.alias || '';
  S.loginListSig = '';
  const listPane = $('#login-body .m3-view[data-pane="list"]');
  if (listPane) {
    renderM3ListPane(listPane);
    const inp = listPane.querySelector('.m3-rename-input');
    if (inp) { inp.focus({preventScroll: true}); inp.select(); }
  }
}
function cancelAccountRename() {
  if (!S.accountRenameId) return;
  S.accountRenameId = null;
  S.accountRenameValue = '';
  S.loginListSig = '';
  const listPane = $('#login-body .m3-view[data-pane="list"]');
  if (listPane) renderM3ListPane(listPane);
}
async function removeAccount(id) {
  if (!id || S.job.busy) return;
  try {
    await flushAll().catch(() => {});
    const res = await api('/api/account/remove', {id});
    if (res.switched) {
      resetDraftStateForAccountSwitch();
    }
    S.auth = res.auth || S.auth;
    S.accountRenameId = null;
    S.accountRemoveConfirmId = null;
    S.loginListSig = '';
    await load();
    renderAccount();
    if ($('#login-dialog').open && loginPhase === 'form') {
      if (savedAccountsList().length === 0 && loginView === 'list') {
        loginFromList = false;
        slideM3View('form', {addMode: false, target: null});
      } else {
        const listPane = $('#login-body .m3-view[data-pane="list"]');
        if (listPane) renderM3ListPane(listPane);
      }
    }
    toast('已移除该账号的本机数据', 'ok');
  } catch (e) {
    toast(e.message, 'error');
  }
}
function toggleAccountMenu() {
  if (accountMenuOpen) { closeAccountMenu(); return; }
  accountMenuOpen = true;
  renderAccount();
  $('#account-manage-button')?.focus({preventScroll: true});
}
function closeAccountMenu() {
  if (!accountMenuOpen) return;
  S.accountRemoveConfirmId = null;
  const menu = $('#account-menu');
  if (menu && !menu.hidden && !REDUCED_MOTION()) {
    const r = menu.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      const ghost = menu.cloneNode(true);
      ghost.classList.remove('open');
      ghost.hidden = false;
      const host = mountGhost(ghost, { left: r.left, top: r.top, width: r.width, height: r.height, z: 45, bg: 'transparent', radius: '12px' });
      host.style.overflow = 'visible';
      ghost.classList.add('account-menu-ghost');
      requestAnimationFrame(() => ghost.classList.add('closing'));
      setTimeout(() => host.remove(), 190);
    }
  }
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
function ensureRefreshDom(btnNode) {
  if (!btnNode || btnNode.dataset.rfInit) return;
  btnNode.dataset.rfInit = '1';
  const fill = el('span', 'rf-fill');
  const ic = el('span', 'rf-ic');
  const tx = el('span', 'rf-tx', '刷新全部');
  const cnt = el('span', 'rf-count');
  btnNode.replaceChildren(fill, ic, tx, cnt);
}
function parseRefreshProgress() {
  const msg = S.job.message && S.job.message !== '正在处理…' ? S.job.message : '';
  const sec = Math.max(0, Math.floor((Date.now() - (S.jobStart || Date.now())) / 1000));
  let m = msg.match(/^正在核对课程（(\d+)\/(\d+)）：(.+)$/);
  if (m) {
    const idx = Math.max(1, Number(m[1])), total = Math.max(idx, Number(m[2])), course = m[3].trim();
    const pct = Math.max(S.refreshUi.pct || 0.14, Math.min(0.86, 0.16 + 0.64 * (idx / total)));
    return {pct, course, label: '核对 ' + course, countText: `${idx}/${total}`};
  }
  m = msg.match(/^正在核对[：:](.+?)(?:\s*\/\s*(.+))?$/);
  if (m) {
    const course = m[1].trim();
    const pct = Math.max(S.refreshUi.pct || 0.68, Math.min(0.95, 0.72 + Math.min(0.22, sec * 0.015)));
    return {pct, course, label: '核对 ' + course, countText: sec + 's'};
  }
  const pct = Math.max(S.refreshUi.pct || 0.12, Math.min(0.42, 0.15 + sec * 0.025));
  return {pct, course: '', label: msg || '正在连接学习通…', countText: sec > 0 ? sec + 's' : ''};
}
function syncScanningRows(courseName) {
  const list = $('#assignment-list');
  if (!list) return;
  const target = (courseName || '').trim();
  list.querySelectorAll('.item').forEach(node => {
    const c = (node.dataset.course || '').trim();
    const match = !!target && !!c && (c === target || c.includes(target) || target.includes(c));
    node.classList.toggle('scan', match);
  });
}
function renderRefreshButton(refreshing) {
  const refresh = $('#refresh');
  if (!refresh) return;
  ensureRefreshDom(refresh);
  const fill = refresh.querySelector('.rf-fill');
  const ic = refresh.querySelector('.rf-ic');
  const tx = refresh.querySelector('.rf-tx');
  const cnt = refresh.querySelector('.rf-count');
  const badge = $('#sync-badge');

  if (refreshing) {
    if (S.refreshUi.finTimer) { clearTimeout(S.refreshUi.finTimer); S.refreshUi.finTimer = null; }
    S.refreshUi.phase = 'busy';
    const info = parseRefreshProgress();
    S.refreshUi.pct = info.pct;
    S.refreshUi.course = info.course;
    S.refreshUi.countText = info.countText;
    refresh.classList.add('is-busy');
    refresh.classList.remove('fin', 'fin-warn');
    if (fill) fill.style.transform = `scaleX(${info.pct.toFixed(3)})`;
    if (ic.dataset.icon !== 'refresh-spin') { ic.dataset.icon = 'refresh-spin'; ic.replaceChildren(icon('refresh', 'spin')); }
    if (tx.textContent !== info.label) tx.textContent = info.label;
    if (cnt.textContent !== info.countText) cnt.textContent = info.countText;
    if (badge) badge.hidden = true;
    syncScanningRows(info.course);
    return;
  }

  syncScanningRows('');
  if (S.refreshUi.phase === 'fin') {
    refresh.classList.remove('is-busy');
    refresh.classList.add('fin');
    refresh.classList.toggle('fin-warn', S.refreshUi.finTone === 'warn');
    if (fill) fill.style.transform = 'scaleX(1)';
    const iconKey = S.refreshUi.finTone === 'warn' ? 'alert' : 'check';
    if (ic.dataset.icon !== iconKey) { ic.dataset.icon = iconKey; ic.replaceChildren(icon(iconKey)); }
    if (tx.textContent !== S.refreshUi.finLabel) tx.textContent = S.refreshUi.finLabel;
    if (cnt.textContent !== '') cnt.textContent = '';
    if (badge) badge.hidden = true;
    return;
  }

  refresh.classList.remove('is-busy', 'fin', 'fin-warn');
  S.refreshUi.pct = 0;
  S.refreshUi.course = '';
  if (fill) fill.style.transform = 'scaleX(0)';
  const idleIcon = !refreshing && S.waveState === 'settle' ? 'refresh-settle' : 'refresh';
  if (ic.dataset.icon !== idleIcon) {
    ic.dataset.icon = idleIcon;
    ic.replaceChildren(icon('refresh', S.waveState === 'settle' ? 'settle' : ''));
  }
  if (tx.textContent !== '刷新全部') tx.textContent = '刷新全部';
  if (cnt.textContent !== '') cnt.textContent = '';
  if (badge) badge.hidden = false;
}
function finishRefreshButton(label, tone = 'ok', holdMs = 1900) {
  clearTimeout(S.refreshUi.finTimer);
  S.refreshUi.phase = 'fin';
  S.refreshUi.finLabel = label;
  S.refreshUi.finTone = tone;
  renderRefreshButton(false);
  S.refreshUi.finTimer = setTimeout(() => {
    if (S.refreshUi.phase === 'fin') {
      S.refreshUi.phase = '';
      S.refreshUi.finTimer = null;
      renderRefreshButton(false);
      renderSyncBadge();
    }
  }, holdMs);
}
function renderDiffRibbon() {
  const slot = $('#diff-ribbon-slot');
  if (!slot) return;
  const d = S.diffRibbon;
  if (!d) {
    slot.classList.remove('open');
    slot.replaceChildren();
    return;
  }
  const parts = [];
  if (d.added) parts.push(`+${d.added} 新增`);
  if (d.deadlineChanged) parts.push(`${d.deadlineChanged} 改期`);
  if (d.archived) parts.push(`${d.archived} 已归档`);
  if (d.otherChanged) parts.push(`${d.otherChanged} 状态更新`);
  if (d.partial && !parts.length) parts.push('部分课程同步受限');
  const box = el('div', 'ribbon on' + (d.partial ? ' warn' : ''));
  const dot = el('span', 'rib-dot');
  const txt = el('span', 'rib-txt', (d.partial ? '部分同步 · ' : '刚刚更新 · ') + (parts.join(' · ') || '列表已同步'));
  const close = btn('', 'rib-x', () => { S.diffRibbon = null; renderDiffRibbon(); }, 'x');
  close.setAttribute('aria-label', '关闭更新摘要');
  box.append(dot, txt, close);
  slot.replaceChildren(box);
  requestAnimationFrame(() => { if (slot.isConnected && S.diffRibbon) slot.classList.add('open'); });
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
  const refresh = $('#refresh'), refreshing = S.job.busy && S.job.action === 'refresh' && !S.jobKey;
  renderRefreshButton(refreshing);
  refresh.setAttribute('aria-busy', String(refreshing));
  refresh.disabled = S.job.busy || !S.online;
  refresh.title = !S.online ? '网络不可用' : refreshing ? '正在刷新作业…' : S.job.busy ? '请等待当前操作完成' : '更新所有课程的作业列表、截止时间和提交状态';
  $('#assignment-list').classList.toggle('refreshing', refreshing);
  $('#detail').classList.toggle('is-refreshing', refreshing);
  syncListWave(refreshing);
  setRail(S.job.busy);
  renderSyncBadge();
}
function syncListWave(refreshing) {
  const list = $('#assignment-list');
  if (!list) return;
  if (refreshing) {
    if (S.waveState === 'on') return;
    S.waveState = 'on';
    list.querySelectorAll('.item').forEach((n, i) => { n.style.setProperty('--i', String(Math.min(i, 8))); n.classList.add('wave'); });
    return;
  }
  if (S.waveState === 'on') {
    S.waveState = 'settle';
    list.querySelectorAll('.item.wave').forEach(n => n.classList.remove('wave'));
    setTimeout(() => {
      if (S.waveState !== 'settle') return;
      S.waveState = '';
      renderSyncBadge();
    }, 700);
  }
}

/* Thin progress rail along the top edge while any job runs. */
function setRail(on) {
  const rail = $('#rail');
  if (on) { rail.className = 'rail on'; rail.hidden = false; return; }
  if (rail.hidden || !rail.classList.contains('on')) return;
  rail.className = 'rail finish';
  setTimeout(() => { if (rail.classList.contains('finish')) rail.hidden = true; }, 600);
}
function elapsedText() {
  const s = Math.max(0, Math.floor((Date.now() - (S.jobStart || Date.now())) / 1000));
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${pad(s % 60)} 秒`;
}
// Short status next to the refresh button: running time, last result, or last update.
function renderSyncBadge() {
  const badge = $('#sync-badge');
  const refreshing = S.job.busy && S.job.action === 'refresh' && !S.jobKey;
  if (refreshing || S.refreshUi.phase === 'fin') { badge.hidden = true; return; }
  badge.hidden = false;
  const recent = S.outcome && Date.now() - S.outcome.at < (S.outcome.latest ? 1700 : OUTCOME_MS) ? S.outcome : null;
  let tone = '', iconName = '', text = '', hint = '';
  if (!S.online) { tone = 'warn'; iconName = 'offline'; text = '网络不可用'; }
  else if (recent) {
    tone = recent.tone;
    iconName = recent.tone === 'ok' ? 'check' : 'alert';
    text = recent.latest ? '已是最新' : recent.tone === 'ok' ? '已更新' : recent.tone === 'warn' ? '部分未更新' : '刷新失败';
  } else if (S.loaded && S.lastSuccess) {
    const d = parseDate(S.lastSuccess);
    if (d) { text = '更新于 ' + (dayLabel(d) === '今天' ? hm(d) : dayLabel(d)); hint = '上次更新：' + stamp(S.lastSuccess); }
  }
  const sig = tone + '|' + text;
  if (badge.dataset.sig === sig) return;
  badge.dataset.sig = sig;
  badge.className = 'sync-badge ' + tone;
  badge.title = hint;
  badge.replaceChildren(...(iconName ? [icon(iconName)] : []), ...(text ? [el('span', '', text)] : []));
}
// Keeps the "已用 N 秒" counters ticking without re-rendering anything else.
function tickElapsed() {
  if (!S.job.busy) return;
  const text = elapsedText(), slow = Date.now() - S.jobStart > 40000;
  document.querySelectorAll('[data-elapsed]').forEach(n => { n.textContent = '已用 ' + text; });
  document.querySelectorAll('.slow-hint').forEach(n => { n.hidden = !slow; });
  renderSyncBadge();
  if (S.job.action === 'refresh' && !S.jobKey) renderRefreshButton(true);
}

/* ---------- Banners ---------- */
const ACTION_LABEL = {refresh: '正在刷新作业', read: '正在读取题目', open: '正在打开学习通', fill: '正在自动填入', login: '正在登录作业通', ai: 'AI 作答中', 'ai-test': '正在测试 AI 连接'};
function banner(tone, iconName, title, text, actions = []) {
  const b = el('div', 'banner ' + tone);
  b.setAttribute('role', tone === 'danger' ? 'alert' : 'status');
  b.append(typeof iconName === 'string' ? icon(iconName) : iconName);
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
function visibleUpdateWarnings() {
  if (warningDismissal === undefined) {
    try {
      const value = JSON.parse(sessionStorage.getItem('homework-update-warning-dismissal') || 'null');
      warningDismissal = value && Array.isArray(value.warnings) ? value : null;
    } catch { warningDismissal = null; }
  }
  const warnings = (S.warnings || []).map(String);
  if (warningDismissal?.update !== (S.lastCheck || S.lastSuccess || '')) return warnings;
  return warnings.filter(w => !warningDismissal.warnings.includes(w));
}
function dismissUpdateWarnings() {
  warningDismissal = {update: S.lastCheck || S.lastSuccess || '', warnings: (S.warnings || []).map(String)};
  try { sessionStorage.setItem('homework-update-warning-dismissal', JSON.stringify(warningDismissal)); } catch {}
  renderStatus(true);
}
// Banners are reconciled by key: unchanged ones stay untouched (no flicker, an
// open "查看" list stays open), changed ones are swapped without replaying the
// entrance animation, and removed ones disappear at once.
function syncBanners(items) {
  const box = $('#banners'), old = new Map([...box.children].map(n => [n.dataset.key, n])), nodes = [];
  for (const {key, sig, build} of items) {
    let node = old.get(key);
    if (!node || node.dataset.sig !== sig) {
      const next = build();
      next.dataset.key = key;
      next.dataset.sig = sig;
      if (node) { next.classList.add('still'); node.replaceWith(next); }
      node = next;
    }
    old.delete(key);
    nodes.push(node);
  }
  old.forEach(n => n.remove());
  let cursor = box.firstChild;
  for (const node of nodes) { if (node === cursor) cursor = cursor.nextSibling; else box.insertBefore(node, cursor); }
}
function busyBanner() {
  const label = S.job.action === 'refresh' && S.jobKey ? ACTION_LABEL.read : ACTION_LABEL[S.job.action] || '正在处理';
  const detail = S.job.message && S.job.message !== '正在处理…' && S.job.message !== label ? S.job.message : '';
  const b = banner('busy', spark('icon banner-spark'), label + '…', detail);
  const slow = el('span', 'slow-hint', '学习通响应较慢，请继续等待。');
  slow.hidden = Date.now() - S.jobStart <= 40000;
  b.querySelector('.banner-text').append(slow);
  const elapsed = el('span', 'banner-meta', '已用 ' + elapsedText());
  elapsed.dataset.elapsed = '';
  b.append(elapsed);
  return b;
}
function warningsBanner(warnings) {
  const b = el('div', 'banner warn');
  b.append(icon('alert'));
  const d = el('details', 'banner-text');
  d.append(el('summary', '', `上次更新有 ${warnings.length} 项未读取成功`));
  const ul = el('ul');
  for (const w of warnings) ul.append(el('li', '', w));
  d.append(ul);
  b.append(d);
  const actions = el('div', 'banner-actions');
  const close = btn('关闭', 'btn-sm btn-ghost', () => {
    if (!REDUCED_MOTION()) {
      const r = b.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        const ghost = b.cloneNode(true);
        const host = mountGhost(ghost, { left: r.left, top: r.top, width: r.width, height: r.height, z: 32, bg: 'transparent', radius: '10px' });
        host.animate([
          { height: r.height + 'px', opacity: 1, transform: 'scaleY(1)' },
          { height: '0px', opacity: 0, transform: 'scaleY(.92)' }
        ], { duration: 240, easing: 'cubic-bezier(.4, 0, .2, 1)', fill: 'forwards' }).finished.then(() => host.remove());
      }
    }
    dismissUpdateWarnings();
  }, 'x');
  close.setAttribute('aria-label', '关闭本次读取失败提示');
  actions.append(close);
  b.append(actions);
  return b;
}
function renderBanners() {
  const items = [];
  if (S.serverLost) items.push({key: 'lost', sig: '', build: () => banner('danger', 'alert', '与助手的连接已中断', '已写的答案保存在本机，正在尝试重新连接；长时间没有恢复时，请重新打开助手。')});
  if (!S.online) items.push({key: 'offline', sig: '', build: () => banner('warn', 'offline', '网络不可用', '可以继续作答，刷新和填入需要联网。')});
  const a = S.auth;
  // Home and sidebar already show the login entry; the banner is for open assignments.
  if (S.key && a && a.state === 'login_required' && !(S.job.busy && S.job.action === 'login')) {
    const ever = everLoggedIn();
    items.push({key: 'login', sig: String(ever), build: () => banner('login', 'lock', ever ? '登录已失效' : '尚未登录', '登录后才能读取题目和填入答案。', [btn('登录作业通', 'btn-sm btn-primary', openLogin)])});
  }
  if (S.job.busy && S.job.action !== 'login' && S.job.action !== 'refresh') {
    items.push({key: 'busy', sig: [S.job.action, S.jobKey, S.job.message].join('|'), build: busyBanner});
  }
  const warnings = visibleUpdateWarnings();
  if (warnings.length && S.loaded && !S.job.busy) {
    items.push({key: 'warnings', sig: JSON.stringify(warnings), build: () => warningsBanner(warnings)});
  }
  syncBanners(items);
}
function renderStatus(force = false) {
  const memo = JSON.stringify([S.job.busy, S.job.action, S.job.message, S.jobKey, S.auth, S.reminder, S.online, S.serverLost, S.loaded, S.lastSuccess, S.lastCheck, S.warnings, warningDismissal, S.key]);
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
  if ($('#login-dialog').open) {
    const typingInLogin = loginPhase === 'form' && $('#login-body')?.contains(document.activeElement)
      && /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName || '');
    if (!typingInLogin) renderLogin();
  }
  if ($('#fill-dialog').open && fillPhase === 'running') renderFill();
  if ($('#reminder-dialog').open && !S.reminderEditing && !$('#reminder-body').contains(document.activeElement)) renderReminder();
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
  const active = S.records.filter(r => r.group === 'active').sort(sorter('active'));
  const review = S.records.filter(r => r.group === 'review').length;
  const entering = S.navDir === 'left';
  const welcomeSig = S.loaded
    ? JSON.stringify([S.loadError, S.auth?.state, S.job.busy, S.online, S.lastSuccess, review, active.map(r => [r.key, r.deadline, progressFor(r).answered, progressFor(r).total])])
    : '';
  if (!entering && welcomeSig && S.welcomeSig === welcomeSig && detail.querySelector('.welcome')) {
    S.navDir = '';
    return;
  }
  const animHome = entering || (!S.welcomeAnimated && S.loaded);
  if (S.loaded) { S.welcomeAnimated = true; S.welcomeSig = welcomeSig; }
  const w = el('div', 'welcome' + (entering ? ' page-in-left' : animHome ? '' : ' no-anim')), greet = el('div', 'greet stg');
  greet.style.setProperty('--i', '0');
  S.navDir = '';
  const mount = () => { detail.replaceChildren(w); if (entering) staggerView(w, true); };
  greet.append(spark(), el('h1', '', greeting()));
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
    const c = callout('login', 'lock', everLoggedIn() ? '登录已失效' : '登录作业通', '登录后才能读取作业和填入答案。', b);
    c.classList.add('stg'); c.style.setProperty('--i', '1');
    w.append(c);
  }
  if (!S.loaded) { mount(); return; }
  if (!S.records.length) {
    const empty = el('div', 'empty anim-in'), ic = el('div', 'empty-icon'), row = el('div', 'notice-actions');
    ic.append(icon('inbox'));
    empty.append(ic, el('h2', '', '还没有作业'), el('p', '', S.auth?.state === 'logged_in' ? '刷新后会读取学习通里的作业。' : '登录后刷新即可看到作业。'));
    if (S.auth?.state === 'logged_in') row.append(Object.assign(btn('刷新作业', 'btn-primary', () => startAction('refresh'), 'refresh'), {disabled: S.job.busy || !S.online}));
    empty.append(row);
    w.append(empty);
    mount();
    return;
  }

  const summary = overview();
  const today = active.filter(r => summary.keys.today.includes(r.key));
  if (today.length) {
    const first = today[0], d = due(first);
    const uc = callout('urgent pulse', 'clock', `今天有 ${today.length} 项作业截止`, `${first.course || '未命名课程'} · ${d.left || d.short}`, btn(today.length > 1 ? '查看' : '去完成', 'btn-primary', () => today.length > 1 ? showWorkspaceList('active', 'all', 'today') : select(first.key)));
    uc.classList.add('stg'); uc.style.setProperty('--i', '1');
    w.append(uc);
  }
  const metrics = el('div', 'metrics stg');
  metrics.style.setProperty('--i', '2');
  for (const [key, title, group, range] of [
    ['today', '今天截止', 'active', 'today'], ['within_3_days', '3 天内截止', 'active', 'within_3_days'],
    ['within_7_days', '7 天内截止', 'active', 'within_7_days'], ['review', '待核实', 'review', 'all'],
  ]) {
    const n = summary.counts[key];
    const metric = btn('', 'overview-metric' + (!n ? ' zero' : key === 'today' ? ' hot' : key === 'review' ? ' caution' : ''), () => showWorkspaceList(group, 'all', range));
    metric.dataset.overview = key;
    const strong = el('strong', '', String(n));
    if (animHome && n > 0) animateNumber(strong, n, '', true);
    metric.append(strong, el('span', '', title));
    metrics.append(metric);
  }
  w.append(metrics);

  const progress = el('section', 'panel stg'), head = el('div', 'panel-head');
  progress.style.setProperty('--i', '3');
  const {answered, total} = summary.questions, percent = total ? Math.round(answered * 100 / total) : 0;
  head.append(el('h2', '', '作答进度'), el('span', '', total ? `已写 ${answered} / ${total} 题` : '暂无已读取的题目'));
  const bar = el('div', 'big-progress'), fill = el('i');
  if (animHome && percent > 0 && !REDUCED_MOTION()) {
    fill.style.width = '0%';
    requestAnimationFrame(() => requestAnimationFrame(() => { if (fill.isConnected) fill.style.width = percent + '%'; }));
  } else {
    fill.style.width = percent + '%';
  }
  bar.append(fill);
  const meta = el('div', 'progress-meta'), pctEl = el('b', '', percent + '%');
  if (animHome && percent > 0) animateNumber(pctEl, percent, '%', true);
  meta.append(pctEl, el('span', '', '写完后仍需在学习通提交'));
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
    const panel = el('section', 'panel stg'), ph = el('div', 'panel-head'), list = el('div', 'upcoming');
    panel.style.setProperty('--i', '4');
    ph.append(el('h2', '', '近期截止'), el('span', '', `共 ${active.length} 项待完成`));
    active.slice(0, 5).forEach((r, idx) => {
      const d = due(r), row = el('button', 'up-item ' + d.tone + (animHome ? ' enter' : '')), main = el('span', 'up-main'), side = el('span', 'up-side');
      if (animHome) row.style.setProperty('--i', String(idx));
      row.type = 'button';
      main.append(el('strong', '', r.course || '未命名课程'), el('span', '', r.title || '未命名作业'));
      const p = progressFor(r);
      side.append(dueTag(r), el('span', 'flag' + (p.state === 'complete' ? ' ok' : ''), p.total ? `${p.answered}/${p.total} 题` : '题目待读取'));
      row.append(dateBadge(r), main, side);
      row.onclick = () => select(r.key);
      list.append(row);
    });
    panel.append(ph, list);
    if (active.length > 5) {
      const more = btn(`查看全部 ${active.length} 项`, 'btn-ghost btn-sm', () => showWorkspaceList('active'), 'arrow');
      more.style.marginTop = '6px';
      panel.append(more);
    }
    w.append(panel);
  }
  if (review) {
    const rc = callout('warn stg', 'alert', `${review} 项作业信息待核实`, '截止时间或提交状态不确定，核实前不会计入待完成。', btn('去核实', '', () => showWorkspaceList('review')));
    rc.style.setProperty('--i', '5');
    w.append(rc);
  }
  const foot = el('div', 'home-foot stg');
  foot.style.setProperty('--i', '5');
  foot.append(el('span', '', S.lastSuccess ? '更新于 ' + stamp(S.lastSuccess) : '尚未更新'));
  foot.append(Object.assign(btn('刷新', 'btn-ghost btn-sm', () => startAction('refresh'), 'refresh'), {disabled: S.job.busy || !S.online}));
  w.append(foot);
  mount();
}

/* ---------- Detail ---------- */
function fingerprint(r) {
  return JSON.stringify([r.questions, r.group, r.status, r.deadline, r.deadline_kind, r.content_complete, r.content_warnings, r.content_error, r.content_status, r.instructions_html, r.instructions_text, r.instructions_source, r.attachments, r.online_attachments, r.review_attachments, r.child_tasks, r.submission_note, r.title, r.course, r.local_settings]);
}
function setView(v) {
  $('#app').dataset.view = v;
  if (v === 'list') requestAnimationFrame(() => placeListSel($('#assignment-list .list-sel'), false));
}
function scrubGhost(root) {
  root.setAttribute('aria-hidden', 'true');
  root.setAttribute('inert', '');
  for (const attr of [...root.attributes]) if (attr.name.startsWith('data-')) root.removeAttribute(attr.name);
  root.querySelectorAll('[id]').forEach(n => n.removeAttribute('id'));
  root.querySelectorAll('*').forEach(n => {
    for (const attr of [...n.attributes]) if (attr.name.startsWith('data-')) n.removeAttribute(attr.name);
    n.style.animation = 'none';
    n.style.transition = 'none';
  });
}
let ghostSheet = null, ghostCssText = '';
function applyGhostStyles(shadow) {
  if (!ghostCssText) {
    const rules = [];
    for (const s of document.styleSheets) {
      try { for (const r of s.cssRules) rules.push(r.cssText); } catch {}
    }
    ghostCssText = rules.join('\n');
    if (ghostCssText && typeof CSSStyleSheet !== 'undefined' && 'replaceSync' in CSSStyleSheet.prototype) {
      try { ghostSheet = new CSSStyleSheet(); ghostSheet.replaceSync(ghostCssText); } catch { ghostSheet = null; }
    }
  }
  if (ghostSheet && 'adoptedStyleSheets' in shadow) {
    shadow.adoptedStyleSheets = [ghostSheet];
  } else if (ghostCssText) {
    const style = document.createElement('style');
    style.textContent = ghostCssText;
    shadow.append(style);
  } else {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui.css';
    shadow.append(link);
  }
}
function mountGhost(clone, { left, top, width, height, z = 35, bg = 'transparent', radius = '12px', cls = '', cloneLeft = 0, cloneTop = 0, cloneWidth = '100%', cloneHeight = '100%' }) {
  scrubGhost(clone);
  const host = document.createElement('div');
  if (cls) host.className = cls;
  Object.assign(host.style, {
    position: 'fixed', left: left + 'px', top: top + 'px', width: width + 'px', height: height + 'px',
    zIndex: String(z), pointerEvents: 'none', overflow: 'hidden', borderRadius: radius, background: bg
  });
  host.setAttribute('aria-hidden', 'true');
  host.setAttribute('inert', '');
  const shadow = host.attachShadow({ mode: 'closed' });
  applyGhostStyles(shadow);
  Object.assign(clone.style, {
    position: 'absolute',
    left: typeof cloneLeft === 'number' ? cloneLeft + 'px' : String(cloneLeft),
    top: typeof cloneTop === 'number' ? cloneTop + 'px' : String(cloneTop),
    right: 'auto',
    bottom: 'auto',
    width: typeof cloneWidth === 'number' ? cloneWidth + 'px' : String(cloneWidth),
    height: typeof cloneHeight === 'number' ? cloneHeight + 'px' : String(cloneHeight),
    margin: '0'
  });
  shadow.append(clone);
  document.body.append(host);
  return host;
}
function snapshotView(dir) {
  if (REDUCED_MOTION()) return;
  document.querySelectorAll('.view-ghost-host').forEach(n => n.remove());
  const detail = $('#detail');
  const old = detail.querySelector('.column, .welcome');
  if (!old) return;
  const detailRect = detail.getBoundingClientRect();
  const r = old.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0 || r.bottom <= detailRect.top || r.top >= detailRect.bottom) return;
  const ghost = old.cloneNode(true);
  ghost.querySelectorAll('.stg-item, .stg, .enter').forEach(n => n.classList.remove('stg-item', 'stg', 'enter'));
  ghost.classList.remove('page-in-right', 'page-in-left', 'no-anim');
  ghost.classList.add('view-ghost', dir > 0 ? 'page-out-left' : 'page-out-right');
  const host = mountGhost(ghost, {
    left: detailRect.left,
    top: detailRect.top,
    width: detailRect.width,
    height: Math.max(0, Math.min(detailRect.height, innerHeight - detailRect.top)),
    z: 4,
    bg: 'transparent',
    radius: '0',
    cls: 'view-ghost-host',
    cloneLeft: r.left - detailRect.left,
    cloneTop: r.top - detailRect.top,
    cloneWidth: r.width,
    cloneHeight: r.height
  });
  setTimeout(() => host.remove(), 360);
}
function staggerView(root, on) {
  if (!on || !root) return;
  [...root.children].slice(0, 10).forEach((n, i) => {
    if (n.matches('.reading')) return;
    n.classList.add('stg-item');
    n.style.setProperty('--si', String(i));
  });
}
function snapshotListOut(dir) {
  if (REDUCED_MOTION()) return;
  document.querySelectorAll('.list-ghost-host').forEach(n => n.remove());
  const list = $('#assignment-list');
  if (!list || !list.children.length) return;
  const r = list.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return;
  const ghost = list.cloneNode(true);
  ghost.classList.add('list-ghost', dir > 0 ? 'tab-out-left' : 'tab-out-right');
  const host = mountGhost(ghost, {
    left: r.left, top: r.top, width: r.width, height: r.height,
    z: 36, bg: 'transparent', radius: '0', cls: 'list-ghost-host'
  });
  ghost.scrollTop = list.scrollTop;
  setTimeout(() => host.remove(), 240);
}
function openSheet(dialog) {
  if (!dialog) return;
  dialog.classList.remove('closing');
  if (!dialog.open) dialog.showModal();
}
function closeSheet(dialog) {
  if (!dialog || !dialog.open) return;
  const card = dialog.querySelector('.sheet-body');
  const r = card?.getBoundingClientRect();
  if (!REDUCED_MOTION() && r && r.width > 0 && r.height > 0) {
    const scrim = document.createElement('div');
    Object.assign(scrim.style, {
      position: 'fixed', inset: '0', zIndex: '48', pointerEvents: 'none',
      background: 'rgba(31, 30, 29, .24)', backdropFilter: 'blur(2px)'
    });
    scrim.setAttribute('aria-hidden', 'true');
    document.body.append(scrim);
    scrim.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 180, easing: 'ease-out', fill: 'forwards' })
      .finished.then(() => scrim.remove(), () => scrim.remove());

    const ghost = card.cloneNode(true);
    ghost.style.maxHeight = r.height + 'px';
    const host = mountGhost(ghost, {
      left: r.left, top: r.top, width: r.width, height: r.height,
      z: 50, bg: 'transparent', radius: getComputedStyle(card).borderRadius || '20px'
    });
    host.style.boxShadow = 'var(--shadow-lg)';
    host.animate([
      { transform: 'scale(1)', opacity: 1, filter: 'blur(0)' },
      { transform: 'scale(.93) translateY(8px)', opacity: 0, filter: 'blur(4px)' }
    ], { duration: 200, easing: 'cubic-bezier(.4, 0, .6, 1)', fill: 'forwards' })
      .finished.then(() => host.remove(), () => host.remove());
  }
  dialog.classList.remove('closing');
  dialog.close();
}
function animateRemovedItems(keys) {
  for (const key of keys) {
    const node = document.querySelector(`#assignment-list .item[data-key="${CSS.escape(key)}"]`);
    if (!node) continue;
    const r = node.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) continue;
    const ghost = node.cloneNode(true);
    ghost.classList.add('item-ghost');
    const host = mountGhost(ghost, { left: r.left, top: r.top, width: r.width, height: r.height, z: 34, bg: 'var(--surface)', radius: '10px' });
    ghost.animate([
      { height: '100%', opacity: 1, transform: 'none' },
      { height: '0%', opacity: 0, transform: 'translateX(30px)' }
    ], { duration: 300, easing: 'cubic-bezier(.5,0,.75,0)', fill: 'forwards' }).finished.then(() => host.remove());
  }
}
async function select(key, keep = false) {
  const generation = ++S.selecting;
  if (S.key && S.key !== key && dirty.has(S.key)) flush(S.key).catch(() => {});
  if (!keep) { S.navDir = 'right'; S.dockPct = null; S.prevDockKey = null; }
  S.key = key;
  const selected = record();
  if (selected && S.group !== selected.group) { S.group = selected.group; S.sub = 'all'; }
  $('#home-top').hidden = false;
  setView('detail');
  renderList();
  const detail = $('#detail');
  const hadDraft = drafts.has(key);
  let snapped = false;
  let loadingTimer = null;
  if (!hadDraft) {
    loadingTimer = setTimeout(() => {
      if (generation !== S.selecting) return;
      if (!keep && !snapped) { snapped = true; snapshotView(1); }
      const loading = el('div', 'empty page-in-right');
      loading.append(icon('loader'), el('p', '', '正在打开…'));
      detail.replaceChildren(loading);
    }, 150);
  }
  try {
    const draft = await getDraft(key);
    if (loadingTimer) clearTimeout(loadingTimer);
    if (generation !== S.selecting) return;
    if (!keep && !snapped) { snapped = true; snapshotView(1); }
    renderDetail(record(), draft, keep);
  } catch (e) {
    if (loadingTimer) clearTimeout(loadingTimer);
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
    ? notice('danger', 'lock', '该课程的平台需要重新登录', ['登录后重新读取题目即可。'], [r.auth_platform === 'lab'
      ? btn('打开作业网页', 'btn-sm btn-primary', () => startAction('open', r.key))
      : btn('登录作业通', 'btn-sm btn-primary', openLogin)])
    : notice('danger', 'alert', '题目读取失败', [r.content_error], [rereadButton(r)]));
}
function renderSubWatchBar(r) {
  const watch = S.watchingSub.get(r.key);
  if (!watch || r.group === 'history') return null;
  const checking = S.subCheckBusyKey === r.key;
  const isWarn = watch.state === 'unsubmitted' && !checking;
  const bar = el('div', 'sub-watch' + (checking ? ' busy' : isWarn ? ' warn' : ''));
  bar.id = 'sub-watch-bar';
  if (checking) {
    bar.append(el('span', 'pulse-dot'), el('span', 'sub-watch-text', '正在后台核对提交状态…'));
  } else if (isWarn) {
    bar.append(icon('alert'), el('span', 'sub-watch-text', '未检测到提交（若已在网页保存，仍需在网页点击提交），作业仍保留在待完成'));
    const retry = btn('再检查', 'btn-sm', () => triggerSubmissionCheck(r.key), 'refresh');
    retry.disabled = !S.online;
    bar.append(retry);
  } else {
    bar.append(el('span', 'pulse-dot'), el('span', 'sub-watch-text', '已在学习通打开作业，提交后切回本窗口将自动核对'));
    const checkNow = btn('我已提交，立即检查', 'btn-sm', () => triggerSubmissionCheck(r.key), 'check');
    checkNow.disabled = !S.online;
    bar.append(checkNow);
  }
  return bar;
}
function renderDetail(r, draft, keep = false) {
  if (!r) return;
  const detail = $('#detail');
  const scroll = keep ? detail.scrollTop : 0;
  const animCls = keep ? ' no-anim' : S.navDir === 'right' ? ' page-in-right' : '';
  const entering = !keep && animCls === ' page-in-right';
  S.navDir = '';
  const col = el('div', 'column' + animCls);
  const live = r.group !== 'history';

  const hero = el('header', 'hero'), top = el('div', 'hero-top'), titles = el('div', 'hero-titles');
  titles.append(el('div', 'hero-course', r.title || '未命名作业'), el('h1', '', r.course || '未命名课程'));
  const tools = el('div', 'hero-tools');
  tools.append(btn('管理作业', 'btn-quiet btn-sm', () => openAssignmentManager(r), 'settings'));
  top.append(titles, tools);
  hero.append(top);
  const facts = el('div', 'facts'), st = statusInfo(r), d = due(r);
  const fact = (text, tone = '', iconName) => { const f = el('span', 'fact ' + tone); if (iconName) f.append(icon(iconName)); f.append(el('span', '', text)); return f; };
  // 未截止、未提交是待完成作业的默认状态，题目进度在题号导航里，这里只放当前作业独有的信息。
  const reviewStatus = r.group === 'review' && reviewReasons(r).includes('status');
  const showStatus = r.group === 'active' ? !!r.local_settings?.completed : r.group === 'review' ? reviewStatus : true;
  if (showStatus) facts.append(fact(r.group === 'review' ? '提交状态待核实' : st.label, st.tone));
  if (r.submission_note) facts.append(fact(r.submission_note, 'warn'));
  const local = r.deadline_source === 'local' && !r.local_settings?.completed ? ' · 自设' : '';
  const strip = r.group === 'active' && d.tone === 'urgent' && d.left;
  if (!strip) facts.append(fact(live && d.left && d.tone !== 'urgent' ? `${d.text} · ${d.left}${local}` : d.text + local, !live ? 'muted' : d.tone === 'urgent' ? 'clay' : d.tone === 'unknown' ? 'warn' : '', 'clock'));
  if (live) {
    if (!r.questions?.length) facts.append(fact('题目待读取', 'warn'));
    const meta = el('span', 'fact-meta');
    if (r.content_last_read) meta.append(el('span', '', '读取于 ' + stamp(r.content_last_read)));
    facts.append(meta);
  }
  hero.append(facts);
  col.append(hero);
  const subWatch = renderSubWatchBar(r);
  if (subWatch) col.append(subWatch);

  if (!live) {
    col.append(notice('soft', 'info', '', ['历史作业只保留基本信息。']));
    detail.replaceChildren(col);
    if (entering) staggerView(col, true);
    S.printed = fingerprint(r);
    $('#crumb').textContent = `${GROUPS[r.group]} · ${r.course || '未命名课程'}`;
    renderDock(r); renderSave(); renderReading(r);
    detail.scrollTop = scroll;
    return;
  }

  if (strip) {
    const bar = el('div', 'deadline-strip'), text = el('div');
    bar.append(icon('clock'));
    text.append(el('strong', '', d.left), el('span', '', ` · ${stamp(r.deadline)} 截止${local}`));
    bar.append(text);
    col.append(bar);
  }
  if (r.group === 'review') {
    const lines = reviewReasons(r).map(x => x === 'deadline' ? '截止时间不确定，不会计入待完成。' : '无法确认是否已提交。');
    col.append(notice('warn', 'alert', '信息待核实', lines, [
      btn('设置截止时间', 'btn-sm', () => openAssignmentSettings(r, 'deadline'), 'calendar'),
      btn('标为已完成', 'btn-sm btn-ghost', () => openAssignmentSettings(r, 'completed'), 'check'),
    ]));
  }
  const warning = el('div'); warning.id = 'content-warning'; col.append(warning);
  const issues = [...new Set([...(r.content_complete === false && r.questions?.length ? [`只读取到 ${r.questions.length} 道题，缺失的题目请在学习通完成。`] : []), ...(r.content_warnings || [])].filter(Boolean))];
  if (issues.length) col.append(notice('warn', 'alert', '题目可能不完整', issues, [rereadButton(r)]));
  else if (r.questions?.length && r.content_status && r.content_status !== 'ready') col.append(notice('info', 'info', '建议重新读取题目', [r.content_status], [rereadButton(r)]));

  if (r.instructions_html || r.instructions_text) {
    const n = notice('soft', 'info', r.instructions_source === 'form_prefill' ? '表单预填说明' : '作业要求'), body = el('div', 'q-body');
    renderRich(body, r.instructions_html, r.instructions_text);
    n.querySelector('.notice-body').append(body);
    col.append(n);
  }

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
  const links = (r.attachments || []).filter(a => /^https?:\/\//i.test(a.url || '') && !PLATFORM_PAGE.test(a.url));
  if (links.length) {
    const n = notice('soft', 'clip', '题目附件');
    const box = el('div', 'links');
    for (const a of links) box.append(externalLink(a.url, a.name || a.title || '查看附件'));
    n.querySelector('.notice-body').append(box);
    col.append(n);
  }
  for (const [label, files] of [['网页已上传文件', r.online_attachments], ['教师批阅文件', r.review_attachments]]) {
    const valid = (files || []).filter(a => /^https?:\/\//i.test(a.url || '') && !PLATFORM_PAGE.test(a.url));
    if (!valid.length) continue;
    const n = notice('soft', 'clip', label), box = el('div', 'links');
    for (const a of valid) box.append(externalLink(a.url, a.name || '查看附件'));
    n.querySelector('.notice-body').append(box);
    col.append(n);
  }
  if (r.child_tasks?.length) {
    const n = notice('soft', 'info', '作业内容');
    for (const item of r.child_tasks) {
      n.querySelector('.notice-body').append(el('p', '', `${item.title || '未命名内容'} · ${item.status_label || '状态待核实'}`));
      if (item.description_text) n.querySelector('.notice-body').append(el('p', '', item.description_text));
      if (/^https:\/\//i.test(item.entry_url || '')) n.querySelector('.notice-body').append(externalLink(item.entry_url, '打开 ' + (item.title || '该项内容')));
    }
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
    const nav = el('div', 'qnav'), label = el('div', 'qnav-label', '题号'), dots = el('div', 'qnav-dots');
    dots.setAttribute('aria-label', '题号导航');
    r.questions.forEach((q, i) => {
      const dot = el('button', 'qdot', String(i + 1));
      dot.type = 'button';
      dot.id = 'qd-' + q.id;
      dot.onclick = () => {
        const target = document.getElementById('q-' + q.id);
        if (!target) return;
        target.scrollIntoView({block: 'start'});
        restartAnim(target, 'q-flash');
      };
      dots.append(dot);
    });
    nav.append(label, dots);
    col.append(nav);
    r.questions.forEach((q, i) => col.append(questionCard(q, i, draft, r.key, r)));
    const end = el('p', 'end-note');
    end.append(spark(), document.createTextNode('全部题目到此结束'));
    col.append(end);
  }

  detail.replaceChildren(col);
  if (entering) staggerView(col, true);
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
      if (dot) {
        dot.classList.add('here');
        // Keep the active number visible without interrupting the detail's
        // smooth scroll by scrolling any of its vertical ancestors.
        const dots = dot.parentElement, bounds = dots.getBoundingClientRect(), target = dot.getBoundingClientRect();
        if (target.left < bounds.left) dots.scrollLeft += target.left - bounds.left - 2;
        else if (target.right > bounds.right) dots.scrollLeft += target.right - bounds.right + 2;
      }
    }
  }, {root: $('#detail'), rootMargin: '-20% 0px -70% 0px'});
  document.querySelectorAll('#detail .question').forEach(q => questionObserver.observe(q));
}
function renderReading(r) {
  const main = $('.main');
  const reading = S.job.busy && S.job.action === 'refresh' && S.jobKey === r.key && !(r.questions && r.questions.length) && !S.watchingSub.has(r.key);
  let overlay = main.querySelector('.reading');
  if (!reading) { overlay?.remove(); return; }
  const detail = S.job.message && S.job.message !== '正在处理…' ? S.job.message : '读取完成后会自动更新';
  if (overlay) { overlay.querySelector('.reading-sub').textContent = detail; return; }
  overlay = el('div', 'reading');
  overlay.setAttribute('role', 'status');
  const card = el('div', 'reading-card'), text = el('div', 'reading-text'), meta = el('span', 'reading-meta', '已用 ' + elapsedText());
  meta.dataset.elapsed = '';
  text.append(el('strong', '', '正在重新读取题目…'), el('span', 'reading-sub', detail));
  card.append(spark('spark reading-spark'), text, meta, el('i', 'reading-bar'));
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
  const prevPct = S.dockPct ?? percent;
  S.dockPct = percent;
  const ring = document.querySelector('.dock .ring');
  if (ring) {
    ring.classList.toggle('complete', done === total);
    animRing(ring, prevPct, percent);
  }
  if (done === total && total > 0 && prevPct < 100) {
    restartAnim(document.querySelector('.dock .dock-actions .btn-primary'), 'btn-pulse');
  }
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
    const aiHint = S.aiHintKeys.get(key);
    const isAiFilled = aiHint && Date.now() - aiHint.at < 2400 && aiHint.filled?.has(String(q.id));
    const choiceBox = choiceInput(q, index, cur, update);
    if (isAiFilled) choiceBox.querySelectorAll('input:checked').forEach(inp => inp.closest('.option')?.classList.add('ai-hint-pulse'));
    art.append(choiceBox);
  } else if (q.type === 'blank') {
    const aiHint = S.aiHintKeys.get(key);
    const isAiFilled = aiHint && Date.now() - aiHint.at < 2400 && aiHint.filled?.has(String(q.id));
    const blankBox = blankInput(q, index, cur, update);
    if (isAiFilled) blankBox.querySelectorAll('.input').forEach(inp => inp.classList.add('ai-hint-pulse'));
    art.append(blankBox);
  } else if (q.type === 'essay') {
    const aiHint = S.aiHintKeys.get(key);
    const isAiFilled = aiHint && Date.now() - aiHint.at < 2400 && aiHint.filled?.has(String(q.id));
    const essayBox = essayInput(q, index, cur, update);
    if (isAiFilled) essayBox.querySelector('.textarea')?.classList.add('ai-hint-pulse');
    art.append(essayBox);
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
  // The platform shuffles option order but keeps the original letter in the
  // radio value, so the letter shown must come from the position, never from
  // the value; the value only identifies the answer when saving and filling.
  const letterAt = i => i < 26 ? String.fromCharCode(65 + i) : String(i + 1);
  const textOf = v => {
    const i = q.options.findIndex(o => String(o.value) === String(v));
    if (judge) return (i >= 0 ? q.options[i].text : '') || String(v);
    return i >= 0 ? letterAt(i) : String(v);
  };
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
    const letter = letterAt(i);
    const prefixed = new RegExp('^\\s*' + letter + '\\s*[.、．:：)）\\s]').test(text.textContent || '');
    const mark = el('span', 'mark', judge || prefixed ? '' : letter);
    label.addEventListener('pointerdown', e => {
      if (input.disabled) return;
      const rect = label.getBoundingClientRect();
      const rip = el('span', 'opt-ripple');
      rip.style.left = (e.clientX - rect.left) + 'px';
      rip.style.top = (e.clientY - rect.top) + 'px';
      label.append(rip);
      setTimeout(() => rip.remove(), 560);
    });
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
  let justAddedId = null, dragDepth = 0;
  const paint = () => {
    list.replaceChildren();
    const files = draft.answers[q.id]?.files || [];
    list.hidden = !files.length;
    for (const f of files) {
      const wrap = el('div', 'file-wrap' + (f.id === justAddedId ? ' entering' : '')), inner = el('div');
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
        if (!REDUCED_MOTION()) {
          wrap.classList.add('removing');
          await new Promise(res => setTimeout(res, 220));
        }
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
      inner.append(row);
      wrap.append(inner);
      list.append(wrap);
    }
    justAddedId = null;
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
        justAddedId = f.id;
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
    zone.ondragenter = e => { e.preventDefault(); dragDepth++; zone.classList.add('over'); };
    zone.ondragover = e => { e.preventDefault(); zone.classList.add('over'); };
    zone.ondragleave = () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) zone.classList.remove('over'); };
    zone.ondrop = e => { e.preventDefault(); dragDepth = 0; zone.classList.remove('over'); upload(e.dataTransfer.files); };
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
  const wasHidden = pill.hidden;
  pill.hidden = false;
  pill.className = 'save-pill ' + tone + (wasHidden ? ' entering' : '');
  pill.replaceChildren(tone === 'saving' ? el('span', 'save-dot') : icon(iconName), el('span', '', text));
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
    dock.hidden = true; dock.replaceChildren(); setDock(false); S.prevDockKey = null; return;
  }
  const wasHidden = dock.hidden;
  const keyChanged = S.prevDockKey !== r.key;
  S.prevDockKey = r.key;
  if (wasHidden && !REDUCED_MOTION()) dock.classList.add('dock-pre');
  dock.hidden = false;
  setDock(true);
  const card = el('div', 'dock-card'), row = el('div', 'dock-row'), progress = el('div', 'dock-progress'), actions = el('div', 'dock-actions');
  const busy = S.job.busy, act = S.job.action, mine = S.jobKey === r.key;
  const ring = el('span', 'ring'), checkEl = el('span', 'ring-check'), text = el('div', 'dock-text'), count = el('strong', '', r.questions?.length ? '' : '题目待读取');
  checkEl.append(icon('check'));
  ring.append(checkEl);
  count.id = 'dock-count';
  const blocker = fillBlocker(r);
  const sub = el('span', blocker && !busy ? 'warn' : '', blocker && !busy ? '暂不能填入：' + blocker : '填入后需在学习通提交');
  text.append(count, sub);
  progress.append(ring, text);
  const rereading = busy && act === 'refresh' && mine;
  const reread = btn(rereading ? '读取中…' : '刷新本作业', '', () => startAction('refresh', r.key), rereading ? 'loader' : 'refresh');
  reread.title = '只重新读取这份作业的题目，不影响其他作业';
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
  if (keyChanged && S.dockPct === null) S.dockPct = progressFor(r).percentage;
  updateProgress();
  renderSave();
  if (wasHidden && !REDUCED_MOTION()) {
    void dock.offsetHeight;
    requestAnimationFrame(() => dock.classList.remove('dock-pre'));
  }
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
  const run = async action => { if (await startAction(action, r.key)) closeSheet(dialog); };
  const openItem = manageItem('查看原作业', '在学习通打开这份作业', 'external', () => run('open'));
  openItem.disabled = S.job.busy || !S.online || r.capabilities?.can_open === false;
  list.append(openItem);
  body.append(list, el('div', 'manage-group', '本机设置'));
  const local = el('div', 'manage-list');
  const done = !!r.local_settings?.completed, own = r.local_settings?.deadline;
  if (done) local.append(manageItem('撤销完成标记', '恢复跟随学习通的状态', 'undo', () => settings()));
  else local.append(manageItem('标记已完成', '移入历史，并清理题目和答案', 'check', () => settings('completed')));
  local.append(manageItem(own ? '修改截止时间' : '设置截止时间', own ? '当前自设：' + stamp(own) + '，可在其中撤销' : '平台时间不准确时使用', 'calendar', () => settings('deadline')));
  body.append(local);
  openSheet(dialog);
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
  const dangerSlot = el('div', 'danger-slot'), dangerInner = el('div');
  const danger = notice('danger', 'alert', '标记完成会清理本机内容', ['题目、答案和附件将被删除，无法恢复。']);
  dangerInner.append(danger);
  dangerSlot.append(dangerInner);
  const dateLabel = el('label', 'form-field'), date = el('input', 'input');
  date.id = 'assignment-deadline'; date.type = 'datetime-local';
  date.value = r.local_settings?.deadline?.slice(0, 16) || '';
  dateLabel.append(el('span', '', '自设截止时间'), date);
  form.append(statusLabel, dangerSlot, dateLabel, el('p', 'field-foot', '留空则使用学习通的截止时间。'));
  const error = el('p', 'settings-error'); error.setAttribute('role', 'alert'); form.append(error);
  const actions = el('div', 'sheet-actions'), save = btn('保存设置', 'btn-primary'); save.type = 'submit';
  const reset = btn('撤销本机设置', 'btn-ghost', () => persist(false, null));
  reset.disabled = !r.local_settings?.completed && !r.local_settings?.deadline;
  const sync = () => {
    const destructive = state.value === 'completed' && !r.local_settings?.completed;
    dangerSlot.classList.toggle('open', destructive);
    save.className = 'btn ' + (destructive ? 'btn-danger' : 'btn-primary');
  };
  state.onchange = sync; sync();
  actions.append(reset, el('span', 'spacer'), btn('取消', '', () => closeSheet(dialog)), save); form.append(actions);
  async function persist(completed, deadline) {
    save.disabled = reset.disabled = true;
    error.textContent = '';
    try {
      await api('/api/assignment-settings', {key: r.key, completed, deadline});
      closeSheet(dialog);
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
  openSheet(dialog);
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
    if (name === 'refresh' && !key) {
      clearTimeout(S.refreshUi.finTimer);
      S.refreshUi = {pct: 0.12, course: '', countText: '', phase: 'busy', finTimer: null, finLabel: '', finTone: ''};
      S.diffRibbon = null;
      renderDiffRibbon();
    }
    const job = await api('/api/action', {action: name, key, ...extra});
    S.jobKey = key;
    applyJob(job);
    renderStatus(true);
    schedulePoll(name === 'login' ? 280 : 180);
    return true;
  } catch (e) {
    toast(e.message, 'error');
    return false;
  }
}
function applyJob(job) {
  if (!job) return;
  window.HomeworkAI?.jobChanged(S.job, job);
  if (job.busy && !S.job.busy) S.jobStart = Date.now();
  S.job = {busy: !!job.busy, action: job.action || '', key: job.key || null, message: job.message || '', result: job.result};
  if (job.auth) {
    S.auth = job.auth;
    if (job.auth.state === 'logged_in') { try { localStorage.setItem('homework-ever-logged-in', '1'); } catch {} }
  }
  if (job.reminder) S.reminder = job.reminder;
}
function markWatchingSubmission(key) {
  if (!key) return;
  const cKey = canonicalKey(key);
  const r = S.records.find(x => x.key === cKey);
  if (!r || r.group === 'history') return;
  const now = Date.now();
  S.watchingSub.set(cKey, {
    state: 'watching',
    readyAt: now,
    nextAutoAt: now + 8000,
    blurred: document.visibilityState === 'hidden' || !document.hasFocus(),
    blurredAt: (document.visibilityState === 'hidden' || !document.hasFocus()) ? now : 0,
  });
  if (S.key === cKey && drafts.has(cKey)) renderDetail(r, drafts.get(cKey), true);
}
async function triggerSubmissionCheck(key, {auto = false} = {}) {
  const cKey = canonicalKey(key);
  if (!cKey || !S.online || S.subCheckBusyKey) return;
  if (auto && S.job.busy) return;
  const seq = ++S.subCheckSeq;
  S.subCheckBusyKey = cKey;
  if (S.key === cKey && drafts.has(cKey)) renderDetail(record(), drafts.get(cKey), true);
  try {
    const res = await api('/api/submission-check', {key: cKey, auto});
    if (S.subCheckSeq !== seq || res?.skipped) return;
    await load();
    const updatedKey = canonicalKey(res?.key || cKey);
    const updated = S.records.find(x => x.key === updatedKey) || S.records.find(x => x.key === cKey);
    if (res?.submitted || (updated && (updated.status === 'submitted' || updated.group === 'history'))) {
      S.watchingSub.delete(cKey);
      if (updatedKey) S.watchingSub.delete(updatedKey);
      renderList();
      if (updated) playSubmittedArchiveAnim(updated);
      toast('已检测到提交，已自动移入历史', 'ok');
    } else if (updated && S.watchingSub.has(cKey)) {
      const now = Date.now();
      S.watchingSub.set(cKey, {
        state: 'unsubmitted',
        readyAt: now,
        nextAutoAt: now + 30000,
        blurred: false,
        blurredAt: 0,
      });
      renderList();
      if (!auto) toast('暂未检测到提交，作业仍保留在待完成', 'warn');
    }
  } catch (e) {
    if (!auto) toast('检查提交状态失败：' + e.message, 'error');
  } finally {
    if (S.subCheckBusyKey === cKey) S.subCheckBusyKey = null;
    const cur = record();
    if (cur && canonicalKey(cur.key) === cKey && drafts.has(cKey)) renderDetail(cur, drafts.get(cKey), true);
  }
}
function playSubmittedArchiveAnim(r) {
  const hero = document.querySelector('#detail .hero');
  if (hero && S.key === r.key) {
    hero.querySelector('.submitted-stamp')?.remove();
    const stampEl = el('div', 'submitted-stamp thud', '已提交');
    hero.append(stampEl);
    restartAnim(hero, 'thud');
    document.getElementById('sub-watch-bar')?.classList.add('ok');
  }
  const srcEl = (S.key === r.key && hero) || document.querySelector(`#assignment-list .item[data-key="${CSS.escape(r.key)}"]`);
  const histTab = document.querySelector('.tab[data-group="history"]');
  if (srcEl && histTab) {
    const sRect = srcEl.getBoundingClientRect(), tRect = histTab.getBoundingClientRect();
    const card = el('div', 'fly-card');
    card.style.left = Math.max(12, sRect.left + sRect.width * 0.1) + 'px';
    card.style.top = Math.max(12, sRect.top + 8) + 'px';
    card.style.width = Math.min(250, Math.max(180, sRect.width * 0.65)) + 'px';
    const row = el('div');
    row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:6px';
    row.append(el('b', '', r.course || '未命名课程'), el('span', 'flag ok', '✓ 已提交'));
    card.append(row, el('span', '', r.title || '未命名作业'));
    document.body.append(card);
    const dx = (tRect.left + tRect.width / 2) - (sRect.left + sRect.width * 0.35);
    const dy = (tRect.top + tRect.height / 2) - (sRect.top + 24);
    card.animate([
      {transform: 'translate(0, 0) scale(1) rotate(0deg)', opacity: 1},
      {transform: `translate(${dx * 0.45}px, ${Math.min(-34, dy * 0.35 - 28)}px) scale(.72) rotate(-4deg)`, opacity: .95, offset: .45},
      {transform: `translate(${dx}px, ${dy}px) scale(.18) rotate(-8deg)`, opacity: 0},
    ], {duration: 620, easing: 'cubic-bezier(.22, 1, .36, 1)', fill: 'forwards'});
    setTimeout(() => {
      card.remove();
      const cnt = $('#count-history');
      if (cnt) { cnt.classList.remove('bump'); void cnt.offsetWidth; cnt.classList.add('bump'); }
    }, 620);
  }
}
function onJobDone(job, key) {
  const failed = !!job.result?.error;
  if (job.action === 'login') return loginDone(job);
  if (job.action === 'fill') {
    if (!failed && key) markWatchingSubmission(key);
    return fillDone(job, key);
  }
  if (job.action === 'ai' || job.action === 'ai-test') return window.HomeworkAI?.jobDone(job);
  if (job.action === 'open') {
    if (!failed && key) markWatchingSubmission(key);
    return toast(job.message || (failed ? '打开失败' : '已打开'), failed ? 'error' : 'ok');
  }
  if (job.action === 'refresh') {
    const cKey = key ? canonicalKey(key) : null;
    if (cKey && S.watchingSub.has(cKey)) {
      const r = S.records.find(x => x.key === cKey);
      if (!failed && r && (r.status === 'submitted' || r.group === 'history')) {
        S.watchingSub.delete(cKey);
        renderList();
        playSubmittedArchiveAnim(r);
        return toast('已检测到提交，已自动移入历史', 'ok');
      }
    }
    if (!key) setOutcome(failed ? 'error' : /部分/.test(job.message || '') ? 'warn' : 'ok');
    if (failed) {
      if (!key) { S.refreshUi.phase = ''; renderRefreshButton(false); }
      const msg = job.message || '刷新失败', named = /刷新失败/.test(msg);
      return toast(msg, 'error', {title: named ? msg : '刷新失败', detail: named ? '' : msg, action: {label: '重试', onClick: () => startAction('refresh', key)}});
    }
    if (key) {
      const r = S.records.find(x => x.key === key || (cKey && x.key === cKey));
      toast(r ? '《' + r.title + '》已同步最新' : '题目已同步最新', 'ok');
      return;
    }
    const d = S.diff || {added: 0, archived: 0, deadlineChanged: 0, otherChanged: 0, changed: 0};
    const partial = /部分/.test(job.message || '');
    const totalDiff = (d.added || 0) + (d.archived || 0) + (d.deadlineChanged || 0) + (d.otherChanged || 0);
    if (partial) {
      S.diffRibbon = {...d, partial: true};
      renderDiffRibbon();
      finishRefreshButton('部分未更新', 'warn', 2200);
      return;
    }
    if (!totalDiff) {
      S.outcome = {tone: 'ok', at: Date.now(), latest: true};
      renderSyncBadge();
      setTimeout(renderSyncBadge, 1800);
      finishRefreshButton('已是最新', 'ok', 1600);
      return;
    }
    S.diffRibbon = {...d, partial: false};
    renderDiffRibbon();
    finishRefreshButton(`已更新 ${totalDiff} 处`, 'ok', 2000);
    return;
  }
}
function setOutcome(tone) {
  S.outcome = {tone, at: Date.now()};
  renderSyncBadge();
  setTimeout(renderSyncBadge, OUTCOME_MS + 100);
}

/* ---------- Login ---------- */
function openAccountManager() {
  const accounts = savedAccountsList();
  loginError = '';
  S.accountRemoveConfirmId = null;
  S.accountRenameId = null;
  S.accountRenameValue = '';
  S.loginListSig = '';
  if (S.job.busy && S.job.action === 'login') {
    loginPhase = 'running';
    renderLogin();
    openSheet($('#login-dialog'));
    return;
  }
  loginPhase = 'form';
  if (accounts.length > 0) {
    loginView = 'list';
    loginAddMode = false;
    loginTarget = null;
    loginFromList = true;
    loginAliasDraft = '';
  } else {
    loginView = 'form';
    loginAddMode = false;
    loginTarget = null;
    loginFromList = false;
    loginAliasDraft = '';
  }
  renderLogin({rebuildForm: true});
  openSheet($('#login-dialog'));
  if (loginView === 'form') {
    $('#login-account')?.focus({preventScroll: true});
  } else {
    $('#m3-add-account')?.focus({preventScroll: true});
  }
}
function openLogin(opts = {}) {
  const addMode = Boolean(opts && opts.addMode === true && typeof opts.preventDefault !== 'function');
  const target = (!addMode && opts && opts.target) ? opts.target : (!addMode ? activeAccountInfo() : null);
  loginError = '';
  S.accountRemoveConfirmId = null;
  S.accountRenameId = null;
  S.accountRenameValue = '';
  S.loginListSig = '';
  if (S.job.busy && S.job.action === 'login') {
    loginPhase = 'running';
    renderLogin();
    openSheet($('#login-dialog'));
    return;
  }
  loginPhase = 'form';
  loginAddMode = addMode;
  loginTarget = target;
  loginFromList = false;
  loginView = 'form';
  loginAliasDraft = addMode ? '' : (target?.alias || S.auth?.account_alias || '');
  renderLogin({rebuildForm: true});
  openSheet($('#login-dialog'));
  const accEl = $('#login-account');
  if (accEl) {
    if (addMode) accEl.value = '';
    accEl.focus({preventScroll: true});
  }
}
function slideM3View(nextView, opts = {}) {
  const body = $('#login-body');
  const viewport = body?.querySelector('.m3-viewport');
  const track = body?.querySelector('.m3-track');
  const listPane = body?.querySelector('.m3-view[data-pane="list"]');
  const formPane = body?.querySelector('.m3-view[data-pane="form"]');
  loginView = nextView;
  loginError = '';
  S.accountRemoveConfirmId = null;
  S.accountRenameId = null;
  S.accountRenameValue = '';
  if (nextView === 'form') {
    loginAddMode = !!opts.addMode;
    loginTarget = opts.addMode ? null : (opts.target || activeAccountInfo() || null);
    loginFromList = savedAccountsList().length > 0;
    loginAliasDraft = loginAddMode ? '' : (loginTarget?.alias || '');
  }
  if (!viewport || !track || !listPane || !formPane) {
    renderLogin({rebuildForm: true});
    return;
  }
  const fromH = viewport.offsetHeight;
  S.loginListSig = '';
  renderM3ListPane(listPane);
  renderM3FormPane(formPane, {resetInputs: nextView === 'form'});
  listPane.inert = nextView !== 'list';
  formPane.inert = nextView !== 'form';
  const targetPane = nextView === 'form' ? formPane : listPane;
  const toH = targetPane.offsetHeight;
  viewport.scrollLeft = 0;
  if (!REDUCED_MOTION() && fromH > 0 && toH > 0 && fromH !== toH) {
    viewport.style.height = fromH + 'px';
    void viewport.offsetHeight;
  }
  requestAnimationFrame(() => {
    track.classList.toggle('slide-form', nextView === 'form');
    if (!REDUCED_MOTION() && toH > 0 && fromH > 0 && fromH !== toH) {
      viewport.style.height = toH + 'px';
    }
  });
  clearTimeout(viewport._m3Timer);
  viewport._m3Timer = setTimeout(() => {
    if (viewport.isConnected) {
      viewport.style.height = '';
      viewport.scrollLeft = 0;
    }
    if (nextView === 'form') {
      const accEl = $('#login-account');
      const pwEl = $('#login-password');
      if (loginAddMode && accEl) accEl.focus({preventScroll: true});
      else if (accEl && !accEl.value) accEl.focus({preventScroll: true});
      else (pwEl || accEl)?.focus({preventScroll: true});
    } else {
      $('#m3-add-account')?.focus({preventScroll: true});
    }
  }, REDUCED_MOTION() ? 0 : 280);
}
function renderM3ListPane(pane) {
  if (!pane) return;
  const a = S.auth || {};
  const accounts = savedAccountsList();
  const sig = JSON.stringify([accounts, a.state, S.job.busy, S.accountRemoveConfirmId, S.accountRenameId, S.accountRenameBusy, loginView]);
  if (sig === S.loginListSig) return;
  S.loginListSig = sig;
  pane.replaceChildren();
  pane.append(el('div', 'sheet-eyebrow', '账号管理'));
  pane.append(titled(loginView === 'list' ? 'login-title' : 'm3-list-title', '学习通账号'));
  pane.append(el('p', 'm3-sub', '每个账号的作业、草稿与提醒独立隔离，互不干扰。'));
  if (!accounts.length) {
    pane.append(el('div', 'm3-empty', '还没有已保存的学习通账号，点击下方按钮添加。'));
  } else {
    const list = el('div', 'm3-list');
    for (const acct of accounts) {
      const isAct = !!acct.active;
      const row = el('div', 'm3-row' + (isAct ? ' is-active' : ''));
      const top = el('div', 'm3-row-top');
      const displayName = (acct.alias || acct.label || acct.username || '已保存账号').trim();
      const ava = el('div', 'm3-ava', userInitial(displayName));
      const main = el('div', 'm3-row-main');
      const nameLine = el('div', 'm3-name-line');
      nameLine.append(el('strong', '', displayName));
      if (isAct) {
        nameLine.append(el('span', 'm3-badge ' + (a.state === 'logged_in' ? 'now' : 'warn'),
          a.state === 'logged_in' ? '当前' : '待重登'));
      } else if (acct.has_session) {
        nameLine.append(el('span', 'm3-badge idle', '会话有效'));
      } else if (acct.saved_credentials) {
        nameLine.append(el('span', 'm3-badge idle', '已存密码'));
      } else {
        nameLine.append(el('span', 'm3-badge warn', '需重登'));
      }
      const metaParts = [];
      if (acct.username && acct.username !== displayName) metaParts.push(acct.username);
      if (acct.school_name) metaParts.push(acct.school_name);
      if (acct.last_sync_at) metaParts.push('更新于 ' + stamp(acct.last_sync_at));
      else if (!acct.school_name && !acct.username) metaParts.push('独立隔离空间');
      main.append(nameLine);
      if (metaParts.length) main.append(el('div', 'm3-meta', metaParts.join(' · ')));
      top.append(ava, main);
      row.append(top);
      if (S.accountRenameId === acct.id) {
        const rform = el('form', 'm3-rename-form');
        const rinp = el('input', 'm3-rename-input');
        rinp.type = 'text';
        rinp.maxLength = 40;
        rinp.placeholder = '输入备注名（留空恢复默认）';
        rinp.value = S.accountRenameValue ?? '';
        rinp.setAttribute('aria-label', '账号备注名');
        rinp.oninput = () => { S.accountRenameValue = rinp.value; };
        rinp.onkeydown = e => {
          if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelAccountRename(); }
        };
        const saveBtn = el('button', 'btn btn-primary btn-xs', '保存');
        saveBtn.type = 'submit';
        saveBtn.disabled = S.job.busy || S.accountRenameBusy;
        const cancelBtn = btn('取消', 'btn-quiet btn-xs', cancelAccountRename);
        cancelBtn.disabled = S.accountRenameBusy;
        rform.onsubmit = e => {
          e.preventDefault();
          renameAccount(acct.id, rinp.value);
        };
        rform.append(rinp, saveBtn, cancelBtn);
        row.append(rform);
      } else {
        const actions = el('div', 'm3-row-actions');
        if (!isAct) {
          const sw = btn('切换', 'btn-primary btn-xs', () => switchAccount(acct.id));
          sw.disabled = S.job.busy;
          actions.append(sw);
        }
        const relog = btn('重登', 'btn-xs', () => slideM3View('form', {addMode: false, target: acct}));
        relog.disabled = S.job.busy;
        const ren = btn('备注', 'btn-quiet btn-xs', () => startAccountRename(acct), 'pen');
        ren.disabled = S.job.busy;
        actions.append(relog, ren);
        const delWrap = el('div', 'm3-row-del-wrap');
        if (S.accountRemoveConfirmId === acct.id) {
          const yes = btn('确认移除', 'btn-quiet btn-xs danger', () => {
            S.accountRemoveConfirmId = null;
            removeAccount(acct.id);
          });
          yes.disabled = S.job.busy;
          const no = btn('取消', 'btn-quiet btn-xs', () => {
            S.accountRemoveConfirmId = null;
            renderM3ListPane(pane);
          });
          delWrap.append(yes, no);
        } else {
          const del = btn('移除', 'btn-quiet btn-xs', () => {
            S.accountRemoveConfirmId = acct.id;
            renderM3ListPane(pane);
          });
          del.disabled = S.job.busy;
          delWrap.append(del);
        }
        actions.append(delWrap);
        row.append(actions);
      }
      list.append(row);
    }
    pane.append(list);
  }
  const addBtn = el('button', 'm3-add-bar');
  addBtn.id = 'm3-add-account';
  addBtn.type = 'button';
  addBtn.disabled = S.job.busy;
  addBtn.append(icon('plus'), el('span', '', '添加其他学习通账号'));
  addBtn.onclick = () => slideM3View('form', {addMode: true, target: null});
  pane.append(addBtn);
}
function renderM3FormPane(pane, opts = {}) {
  if (!pane) return;
  const dialog = $('#login-dialog');
  const a = S.auth || {};
  const accounts = savedAccountsList();
  const hasSavedList = accounts.length > 0;
  const expectedSavedUser = loginAddMode ? '' : (loginTarget?.username || a.credentials?.username || '');
  const account = opts.resetInputs ? expectedSavedUser : ($('#login-account')?.value ?? expectedSavedUser);
  const aliasVal = opts.resetInputs ? (loginAliasDraft ?? '') : ($('#login-alias')?.value ?? loginAliasDraft ?? '');
  const hasSavedCreds = !loginAddMode && (loginTarget ? !!loginTarget.saved_credentials : !!a.credentials?.saved);
  pane.replaceChildren();
  if (hasSavedList) {
    const back = btn('返回账号列表', 'btn-ghost btn-sm m3-back-btn', () => slideM3View('list'), 'back');
    back.id = 'm3-back-list';
    pane.append(back);
  }
  const inner = el('div', 'login-pane');
  inner.append(el('div', 'sheet-eyebrow', '作业通'));
  inner.append(titled(loginView === 'form' ? 'login-title' : 'm3-form-title',
    loginAddMode ? '添加学习通账号' : (loginTarget || a.state === 'logged_in' ? '重新登录账号' : '登录作业通')));
  if (loginAddMode) {
    inner.append(notice('soft', 'info', '', ['将新建独立本机数据空间，不覆盖其他账号的作业与草稿。']));
  } else if (loginTarget || a.state === 'logged_in') {
    inner.append(notice('soft', 'info', '', ['更新此账号的登录状态，原有待办、备注与草稿均会保留。']));
  }
  const form = el('form');
  form.autocomplete = 'on';
  const f1 = el('label', 'form-field' + (loginError ? ' shake' : '')), user = el('input', 'input');
  user.id = 'login-account'; user.name = 'username'; user.autocomplete = 'username'; user.required = true; user.value = account;
  f1.append(el('span', '', '账号'), user);
  const f2 = el('label', 'form-field' + (loginError ? ' shake' : '')), wrap = el('div', 'pw-wrap'), pw = el('input', 'input');
  const savedPassword = hasSavedCreds && !!expectedSavedUser && account === expectedSavedUser;
  pw.id = 'login-password'; pw.name = 'password'; pw.type = 'password'; pw.autocomplete = 'current-password';
  pw.required = !savedPassword;
  if (savedPassword) pw.placeholder = '已保存，留空继续使用';
  const eye = btn('', 'btn-quiet icon-only pw-toggle', () => { pw.type = pw.type === 'password' ? 'text' : 'password'; eye.setAttribute('aria-pressed', String(pw.type === 'text')); }, 'eye');
  eye.setAttribute('aria-label', '显示或隐藏密码');
  wrap.append(pw, eye);
  f2.append(el('span', '', '密码'), wrap);
  const f3 = el('label', 'form-field'), aliasInp = el('input', 'input');
  aliasInp.id = 'login-alias';
  aliasInp.name = 'alias';
  aliasInp.type = 'text';
  aliasInp.maxLength = 40;
  aliasInp.placeholder = '例如：大号 / 辅修号（选填，可留空）';
  aliasInp.value = aliasVal;
  aliasInp.oninput = () => { loginAliasDraft = aliasInp.value; };
  f3.append(el('span', '', '备注名（选填）'), aliasInp);
  user.oninput = () => {
    const matchSaved = hasSavedCreds && !!expectedSavedUser && user.value.trim() === expectedSavedUser;
    pw.required = !matchSaved;
    pw.placeholder = matchSaved ? '已保存，留空继续使用' : '';
  };
  form.append(f1, f2, f3);
  if (loginError) form.append(el('div', 'form-error', loginError));
  const hint = el('div', 'form-hint');
  hint.append(icon('lock'), el('span', '', savedPassword ? '登录信息与备注仅加密保存在这台电脑上。' : '账号密码只加密保存在这台电脑上。'));
  form.append(hint);
  const actions = el('div', 'sheet-actions'), submit = el('button', 'btn btn-primary login-submit', '登录作业通');
  submit.type = 'submit';
  submit.disabled = S.job.busy || !S.online;
  actions.append(btn('取消', '', () => closeSheet(dialog)), submit);
  form.append(actions);
  if (S.job.busy) form.append(el('p', 'sheet-text', '当前有其他操作在进行，完成后才能登录。'));
  else if (!S.online) form.append(el('p', 'sheet-text', '网络不可用，联网后才能登录。'));
  form.onsubmit = e => {
    e.preventDefault();
    const username = user.value.trim(), password = pw.value, alias = aliasInp.value.trim();
    const reuseSaved = hasSavedCreds && !!expectedSavedUser && username === expectedSavedUser && !password;
    if (!username || (!password && !reuseSaved)) {
      loginError = '请输入账号和密码';
      renderM3FormPane(pane, {resetInputs: false});
      const acc = $('#login-account');
      if (acc) acc.value = username;
      return;
    }
    loginAliasDraft = alias;
    pw.value = '';
    submit.disabled = true;
    const backBtn = $('#m3-back-list');
    if (backBtn) backBtn.disabled = true;
    actions.classList.add('morphing');
    submit.classList.add('morphb');
    submit.replaceChildren(icon('loader'));
    const req = startAction('login', null, {username, password, alias, saved: reuseSaved, add_account: loginAddMode});
    setTimeout(() => inner.classList.add('out'), 160);
    setTimeout(async () => {
      const ok = await req;
      if (ok) { if (loginPhase === 'form') { loginPhase = 'running'; renderLogin(); } }
      else {
        inner.classList.remove('out');
        if (backBtn) backBtn.disabled = false;
        actions.classList.remove('morphing');
        submit.classList.remove('morphb');
        submit.textContent = '登录作业通';
        submit.disabled = false;
      }
    }, 260);
  };
  inner.append(form);
  pane.append(inner);
}
function titled(id, text) { const h = el('h2', 'sheet-title', text); h.id = id; return h; }
function closeButton(dialog) {
  const b = btn('', 'btn-quiet icon-only sheet-close', () => closeSheet(dialog), 'x');
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
function loginProgressState() {
  const a = S.auth || {};
  const raw = S.job.message && S.job.message !== '正在处理…' ? S.job.message : '正在连接学习通主站…';
  let pct = 26;
  if (a.sites?.chaoxing || /主站.*已|正在识别所属学校/.test(raw)) pct = 58;
  if (a.school_name || /已识别学校|学校学习通/.test(raw)) pct = 82;
  if (a.sites?.school) pct = 95;
  const schoolName = a.school_name || (raw.match(/已识别学校[：:]\s*(\S+)/)?.[1]) || '';
  return {raw, offset: Math.round(283 * (1 - pct / 100)), schoolName};
}
function setLoginRoll(text) {
  const box = document.getElementById('login-roll');
  if (!box) return;
  const cur = box.lastElementChild;
  if (cur && cur.textContent === text) return;
  const next = el('span', 'down', text);
  box.append(next);
  void next.offsetWidth;
  next.classList.remove('down');
  if (cur) { cur.classList.add('up'); setTimeout(() => cur.remove(), 420); }
}
function buildLoginOrb(titleText = '正在登录作业通') {
  const {raw, offset, schoolName} = loginProgressState();
  const wrap = el('div', 'login-orb');
  wrap.id = 'login-orb';
  const title = titled('login-title', titleText);
  title.classList.add('sr-only');
  const halo = el('div', 'login-halo');
  halo.id = 'login-halo';
  const aura = el('div', 'aura');
  const ringspin = el('div', 'login-ringspin');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 100 100');
  svg.setAttribute('class', 'login-ring');
  svg.innerHTML = `<circle class="track" cx="50" cy="50" r="45"/><circle class="arc" id="login-arc" cx="50" cy="50" r="45" style="stroke-dashoffset:${offset}"/>`;
  ringspin.append(svg);
  const burst1 = el('span', 'burst'), burst2 = el('span', 'burst b2');
  const emblem = el('div', 'login-emblem', '作');
  emblem.id = 'login-emblem';
  halo.append(aura, ringspin, burst1, burst2, emblem);
  const roll = el('div', 'login-roll');
  roll.id = 'login-roll';
  roll.append(el('span', '', raw));
  const school = el('div', 'login-school' + (schoolName ? ' show' : ''), schoolName ? '🏫 ' + schoolName : '');
  school.id = 'login-school';
  wrap.append(title, halo, roll, school);
  return wrap;
}
function renderLogin(opts = {}) {
  const body = $('#login-body'), dialog = $('#login-dialog');
  dialog.classList.remove('a4-to-circle');
  if (loginPhase === 'running' && !(S.job.busy && S.job.action === 'login')) return;
  const savedAccount = loginAddMode ? '' : (loginTarget?.username || S.auth?.credentials?.username || '');
  const account = $('#login-account')?.value ?? savedAccount;
  const a = S.auth || {};
  if (loginPhase === 'running') {
    dialog.classList.remove('m3-mode');
    dialog.classList.add('login-running');
    if (document.getElementById('login-orb')) {
      const {raw, offset, schoolName} = loginProgressState();
      const arc = document.getElementById('login-arc');
      if (arc) arc.style.strokeDashoffset = String(offset);
      setLoginRoll(raw);
      const sc = document.getElementById('login-school');
      if (sc && schoolName) { sc.textContent = '🏫 ' + schoolName; sc.classList.add('show'); }
      return;
    }
    body.replaceChildren(closeButton(dialog));
    body.append(buildLoginOrb('正在登录作业通'));
    const actions = el('div', 'sheet-actions login-bg-actions');
    actions.append(btn('在后台继续', 'btn-ghost btn-sm', () => closeSheet(dialog)));
    body.append(actions);
    return;
  }
  dialog.classList.remove('login-running');
  $('#assignment-list')?.classList.remove('login-ghost');
  if (loginPhase === 'success' || loginPhase === 'failed') {
    dialog.classList.remove('m3-mode');
    body.replaceChildren(closeButton(dialog));
    body.append(el('div', 'sheet-eyebrow', '作业通'));
  }
  if (loginPhase === 'success') {
    const hero = el('div', 'result-hero'), ic = el('div', 'result-icon ok');
    ic.append(icon('check'));
    const t = el('div');
    t.append(titled('login-title', '登录成功'));
    hero.append(ic, t);
    body.append(hero, siteList(a.sites, true));
    const actions = el('div', 'sheet-actions');
    actions.append(btn('完成', '', () => closeSheet(dialog)));
    actions.append(Object.assign(btn('刷新作业', 'btn-primary', () => { closeSheet(dialog); startAction('refresh'); }, 'refresh'), {disabled: S.job.busy || !S.online}));
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
    actions.append(btn('关闭', '', () => closeSheet(dialog)), btn('重新输入', 'btn-primary', () => {
      loginPhase = 'form';
      loginView = 'form';
      loginError = '';
      renderLogin({rebuildForm: true});
      const acc = $('#login-account');
      if (acc) acc.value = account;
      const aliasEl = $('#login-alias');
      if (aliasEl && loginAliasDraft) aliasEl.value = loginAliasDraft;
      $('#login-password')?.focus({preventScroll: true});
    }));
    body.append(actions);
    return;
  }
  dialog.classList.add('m3-mode');
  let viewport = body.querySelector('.m3-viewport');
  let track = viewport?.querySelector('.m3-track');
  let listPane = track?.querySelector('.m3-view[data-pane="list"]');
  let formPane = track?.querySelector('.m3-view[data-pane="form"]');
  let fresh = false;
  if (!viewport || !track || !listPane || !formPane) {
    fresh = true;
    S.loginListSig = '';
    viewport = el('div', 'm3-viewport');
    track = el('div', 'm3-track');
    listPane = el('section', 'm3-view');
    listPane.dataset.pane = 'list';
    formPane = el('section', 'm3-view');
    formPane.dataset.pane = 'form';
    track.append(listPane, formPane);
    viewport.append(track);
    body.replaceChildren(closeButton(dialog), viewport);
  }
  track.classList.toggle('slide-form', loginView === 'form');
  listPane.inert = loginView !== 'list';
  formPane.inert = loginView !== 'form';
  renderM3ListPane(listPane);
  if (fresh || opts.rebuildForm) {
    renderM3FormPane(formPane, {resetInputs: !!opts.rebuildForm});
  } else {
    const subBtn = formPane.querySelector('.login-submit');
    if (subBtn) subBtn.disabled = S.job.busy || !S.online;
  }
}
function playLoginA4Ending() {
  const dialog = $('#login-dialog'), body = $('#login-body');
  if (!dialog.open) return;
  dialog.classList.remove('m3-mode');
  const act = activeAccountInfo();
  const username = (act?.username || S.auth?.credentials?.username || '').trim();
  const displayName = (act?.alias || act?.label || S.auth?.account_label || username).trim();
  const initial = userInitial(displayName || username);
  const schoolName = act?.school_name || S.auth?.school_name || '作业通';
  S.acctRevealState = 'hiding';
  $('#assignment-list')?.classList.add('login-ghost');
  renderAccount();
  if (!document.getElementById('login-orb')) {
    body.replaceChildren(closeButton(dialog), buildLoginOrb('登录成功'));
  }
  body.querySelector('.login-bg-actions')?.remove();
  const loginTitle = document.getElementById('login-title');
  if (loginTitle) loginTitle.textContent = '登录成功';
  const halo = document.getElementById('login-halo');
  halo?.classList.add('ok');
  const arc = document.getElementById('login-arc');
  if (arc) { arc.style.strokeDashoffset = '0'; arc.style.stroke = 'var(--ok)'; }
  setLoginRoll('登录成功');
  const sc = document.getElementById('login-school');
  if (sc && schoolName) { sc.textContent = '🏫 ' + schoolName; sc.classList.add('show'); }
  const emblem = document.getElementById('login-emblem');
  let flipEl = emblem;
  if (emblem) {
    const flip = el('div', 'emblem-3d'), front = el('div', 'face front'), back = el('div', 'face back', initial);
    front.append(icon('check'));
    flip.append(front, back);
    emblem.replaceWith(flip);
    flip.id = 'login-emblem';
    flipEl = flip;
  }
  setTimeout(() => {
    if (!dialog.open) { $('#assignment-list')?.classList.remove('login-ghost'); S.acctRevealState = ''; renderAccount(); return; }
    flipEl?.classList.add('flip');
  }, 420);
  setTimeout(() => {
    if (!dialog.open) { $('#assignment-list')?.classList.remove('login-ghost'); S.acctRevealState = ''; renderAccount(); return; }
    const hRect = (halo || dialog).getBoundingClientRect();
    const cx = hRect.left + hRect.width / 2 - 31, cy = hRect.top + hRect.height / 2 - 31;
    dialog.classList.add('a4-to-circle');
    setTimeout(() => {
      if (dialog.open) dialog.close();
      dialog.classList.remove('a4-to-circle');
      const targetAva = document.querySelector('#account .avatar');
      if (!targetAva) { $('#assignment-list')?.classList.remove('login-ghost'); S.acctRevealState = ''; renderAccount(); return; }
      const tRect = targetAva.getBoundingClientRect();
      const orb = el('div', 'fly-orb', initial);
      orb.style.cssText = `left:${cx}px;top:${cy}px;width:62px;height:62px;font-size:26px`;
      document.body.append(orb);
      const tx = tRect.left + tRect.width / 2 - (cx + 31);
      const ty = tRect.top + tRect.height / 2 - (cy + 31);
      const endScale = tRect.width / 62;
      orb.animate([
        { transform: 'translate(0,0) scale(1) rotate(-18deg)' },
        { transform: `translate(${tx * .52}px,${ty * .38 - 16}px) scale(.74) rotate(-6deg)`, offset: .5 },
        { transform: `translate(${tx}px,${ty}px) scale(${endScale}) rotate(0deg)` }
      ], { duration: 440, easing: 'cubic-bezier(.55,0,.25,1)', fill: 'forwards' }).finished.then(() => {
        orb.remove();
        if (!targetAva.isConnected) { $('#assignment-list')?.classList.remove('login-ghost'); S.acctRevealState = ''; renderAccount(); return; }
        S.acctRevealState = 'landing';
        renderAccount();
        setTimeout(() => {
          if (S.acctRevealState === 'landing') {
            $('#assignment-list')?.classList.remove('login-ghost');
            S.firstListAnimated = false;
            renderList();
          }
        }, 240);
        setTimeout(() => {
          if (S.acctRevealState === 'landing') S.acctRevealState = '';
        }, 1400);
      });
    }, 240);
  }, 680);
}
function loginDone(job) {
  const ok = !job.result?.error && S.auth?.state === 'logged_in';
  loginPhase = ok ? 'success' : 'failed';
  loginError = ok ? '' : job.message || '登录失败，请重试';
  if ($('#login-dialog').open) {
    if (ok) playLoginA4Ending();
    else renderLogin();
  } else {
    toast(ok ? '作业通已登录' : loginError, ok ? 'ok' : 'error');
  }
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
let lastFillPhase = '';
async function openFill(r) {
  try { await flush(r.key); } catch (e) { toast('答案尚未保存成功，暂不能填入：' + e.message, 'error'); return; }
  fillPhase = 'check';
  lastFillPhase = '';
  fillResult = null;
  renderFill();
  openSheet($('#fill-dialog'));
}
function renderFill() {
  const r = record(), draft = drafts.get(S.key), dialog = $('#fill-dialog'), body = $('#fill-body');
  if (!r || !draft) { dialog.close(); return; }
  const phaseChanged = lastFillPhase !== fillPhase;
  lastFillPhase = fillPhase;
  const stage = el('div', phaseChanged ? 'phase-enter' : '');
  body.replaceChildren(closeButton(dialog), el('div', 'sheet-eyebrow', '自动填入'), stage);
  if (fillPhase === 'check') {
    const {ready, skipped} = analyze(r, draft);
    stage.append(titled('fill-title', '填入前检查'));
    const stats = el('div', 'stats');
    const c1 = statCard(ready.length, '将填入', 'ok'), c2 = statCard(skipped.length, '将跳过', skipped.length ? 'warn' : ''), c3 = statCard(r.questions.length, '共计题目');
    stats.append(c1, c2, c3);
    stage.append(stats);
    if (phaseChanged) {
      if (ready.length) animateNumber(c1.querySelector('b'), ready.length, '', true);
      if (skipped.length) animateNumber(c2.querySelector('b'), skipped.length, '', true);
      if (r.questions.length) animateNumber(c3.querySelector('b'), r.questions.length, '', true);
    }
    if (skipped.length) {
      stage.append(el('div', 'section-label', '以下题目不会自动填入'));
      const ul = el('ul', 'reasons');
      ul.append(...reasonRows(skipped.map(x => ({i: x.index, kind: x.kind, reason: ''}))));
      stage.append(ul);
    }
    if (r.content_complete === false) stage.append(notice('warn', 'alert', '', ['题目未读全，其余题目需在学习通完成。']));
    if (fillBlocker(r)) stage.append(notice('warn', 'info', '暂不能填入', [fillBlocker(r)]));
    stage.append(noSubmitNote());
    const actions = el('div', 'sheet-actions'), go = btn(ready.length ? `开始填入 ${ready.length} 题` : '没有可以自动填入的答案', 'btn-primary', async () => {
      go.disabled = true;
      if (await startAction('fill', r.key)) { fillPhase = 'running'; renderFill(); } else go.disabled = false;
    }, 'pen');
    go.disabled = !ready.length || S.job.busy || !!fillBlocker(r);
    actions.append(btn('取消', '', () => closeSheet(dialog)), Object.assign(btn('仅打开作业网页', '', async () => { if (await startAction('open', r.key)) closeSheet(dialog); }, 'external'), {disabled: S.job.busy || !S.online}), go);
    stage.append(actions);
    return;
  }
  if (fillPhase === 'running') {
    stage.append(titled('fill-title', '正在自动填入'));
    stage.append(el('p', 'sheet-text', '填入期间请勿操作学习通窗口。'));
    const msg = S.job.message || '正在处理…';
    const stepIdx = /核|完|存|校验/.test(msg) ? 2 : /写|填|传|第/.test(msg) ? 1 : 0;
    const steps = el('div', 'steps');
    ['检查题目结构', '写入答案内容', '校对保存状态'].forEach((label, idx) => {
      const s = el('div', 'step' + (idx < stepIdx ? ' done' : idx === stepIdx ? ' active' : ''));
      const dot = el('span', 'step-dot');
      if (idx < stepIdx) dot.append(icon('check')); else dot.textContent = String(idx + 1);
      s.append(dot, el('span', '', label));
      steps.append(s);
    });
    const prog = el('div', 'fill-prog'), bar = el('div', 'fill-prog-bar');
    bar.style.width = stepIdx === 0 ? '28%' : stepIdx === 1 ? '66%' : '90%';
    prog.append(bar);
    const live = el('div', 'live');
    live.append(icon('loader'), document.createTextNode(' ' + msg));
    stage.append(steps, prog, live, noSubmitNote());
    const actions = el('div', 'sheet-actions');
    actions.append(btn('在后台等待', '', () => closeSheet(dialog)));
    stage.append(actions);
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
  stage.append(hero);
  if (res.message) stage.append(el('p', 'sheet-text', res.message));
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
    const s1 = statCard(filled.length, '已填入', 'ok'), s2 = statCard(rows.length, '已跳过', rows.length ? 'warn' : ''), s3 = statCard(uploads, '需手动上传', uploads ? 'warn' : '');
    stats.append(s1, s2, s3);
    stage.append(stats);
    if (phaseChanged) {
      if (filled.length) animateNumber(s1.querySelector('b'), filled.length, '', true);
      if (rows.length) animateNumber(s2.querySelector('b'), rows.length, '', true);
      if (uploads) animateNumber(s3.querySelector('b'), uploads, '', true);
    }
    if (rows.length) {
      stage.append(el('div', 'section-label', '跳过的题目与原因'));
      const ul = el('ul', 'reasons');
      ul.append(...reasonRows(rows));
      stage.append(ul);
    }
  }
  stage.append(noSubmitNote());
  const actions = el('div', 'sheet-actions');
  if (failed || notRun) actions.append(Object.assign(btn('重新检查', '', () => { fillPhase = 'check'; renderFill(); }), {disabled: S.job.busy}));
  actions.append(btn('知道了', 'btn-dark', () => closeSheet(dialog)));
  stage.append(actions);
}
function fillDone(job, key) {
  fillPhase = 'result';
  fillResult = {...(job.result || {}), message: job.message || job.result?.message};
  if (key && key !== S.key) { toast(job.message || '自动填入已结束', job.result?.error ? 'error' : 'ok'); return; }
  renderFill();
  openSheet($('#fill-dialog'));
}

/* ---------- Reminder (S1 Cards + 3-Step Wizard + Elevated 24h Dial) & exit ---------- */
const DEFAULT_REM_RULE = () => ({
  days: [1, 2, 3, 4, 5, 6, 7],
  skip_complete: true,
  quiet: '',
  lead_hours: [24],
  auto_refresh: false,
  notify_style: 'center',
});
const WEEK_NAMES = ['一', '二', '三', '四', '五', '六', '日'];
function normClientRule(raw) {
  const base = DEFAULT_REM_RULE();
  if (!raw || typeof raw !== 'object') return base;
  const days = Array.isArray(raw.days) && raw.days.length
    ? [...new Set(raw.days.map(Number).filter(n => n >= 1 && n <= 7))].sort((a, b) => a - b)
    : base.days;
  const leads = Array.isArray(raw.lead_hours)
    ? [...new Set(raw.lead_hours.map(Number).filter(h => h === 24 || h === 6 || h === 2))].sort((a, b) => b - a)
    : base.lead_hours;
  return {
    days: days.length ? days : base.days,
    skip_complete: raw.skip_complete !== false,
    quiet: typeof raw.quiet === 'string' ? raw.quiet : '',
    lead_hours: leads,
    auto_refresh: !!raw.auto_refresh,
    notify_style: raw.notify_style === 'banner' ? 'banner' : 'center',
  };
}
function formatDaysSummary(days) {
  const d = [...new Set(days || [1, 2, 3, 4, 5, 6, 7])].sort((a, b) => a - b);
  if (d.length === 7) return '每天';
  if (d.length === 5 && d.join(',') === '1,2,3,4,5') return '工作日';
  if (d.length === 2 && d.join(',') === '6,7') return '周末';
  return '周' + d.map(n => WEEK_NAMES[n - 1]).join('·');
}
function reminderSlots(r) {
  const now = new Date();
  const rules = r?.rules || {};
  const list = [];
  for (const value of (r?.times || [])) {
    const [hour, minute] = value.split(':').map(Number);
    const rule = rules[value] ? normClientRule(rules[value]) : null;
    const allowedDays = new Set(rule ? rule.days : [1, 2, 3, 4, 5, 6, 7]);
    for (let offset = 0; offset < 8; offset++) {
      const target = new Date(now);
      target.setDate(target.getDate() + offset);
      target.setHours(hour, minute, 0, 0);
      const isoDay = target.getDay() === 0 ? 7 : target.getDay();
      if (target > now && allowedDays.has(isoDay)) {
        list.push({value, target, day: dayLabel(target)});
        break;
      }
    }
  }
  return list.sort((a, b) => a.target - b.target);
}
function reminderNextText(r) {
  const slots = reminderSlots(r);
  if (!slots.length) return '尚未设置时间';
  const next = slots[0];
  return `下次 ${next.day} ${next.value}`;
}
function reminderPeriod(value) {
  const hour = Number(value.slice(0, 2));
  return hour < 5 || hour >= 23 ? '深夜' : hour < 11 ? '早上' : hour < 13 ? '中午' : hour < 18 ? '下午' : '晚上';
}
async function saveReminder(next) {
  if (S.reminderSaving) return false;
  S.reminderSaving = true;
  S.reminderError = '';
  renderReminder();
  try {
    S.reminder = await api('/api/reminder', next);
    S.reminderSavedAt = Date.now();
    renderReminderRow();
    return true;
  } catch (e) {
    S.reminderError = '提醒设置未更改：' + e.message;
    return false;
  } finally {
    S.reminderSaving = false;
    if ($('#reminder-dialog').open) renderReminder();
  }
}
function clearReminderWheel() {}
function reminderDraftTime(draft) {
  return `${String(draft.hour).padStart(2, '0')}:${String(draft.minute).padStart(2, '0')}`;
}
function focusReminderTime(value) {
  if (!$('#reminder-dialog').open) return;
  const target = [...document.querySelectorAll('#reminder-body .tcard-main')].find(button => button.dataset.time === value);
  (target || $('#reminder-add'))?.focus({preventScroll: true});
}
function openReminderWizard(originalValue = null) {
  if (S.reminderSaving) return;
  if (!S.reminder?.enabled && originalValue === null) return;
  const [hour, minute] = (originalValue || '08:15').split(':').map(Number);
  const rule = normClientRule(originalValue ? S.reminder?.rules?.[originalValue] : null);
  S.reminderEditing = {
    originalValue,
    step: 1,
    dialMode: 'hour',
    hour,
    minute,
    days: [...rule.days],
    skip_complete: rule.skip_complete,
    quiet: rule.quiet,
    lead_hours: [...rule.lead_hours],
    auto_refresh: rule.auto_refresh,
    notify_style: rule.notify_style,
  };
  S.reminderError = '';
  renderReminder();
  $('#reminder-hour')?.focus({preventScroll: true});
}
function cancelReminderEditor() {
  if (S.reminderSaving) return;
  const originalValue = S.reminderEditing?.originalValue;
  S.reminderEditing = null;
  S.reminderError = '';
  renderReminder();
  focusReminderTime(originalValue);
}
async function commitReminderWizard(draft) {
  if (S.reminderSaving) return;
  const value = reminderDraftTime(draft), current = S.reminder || {times: [], rules: {}, enabled: true};
  const times = [...(current.times || [])];
  const rules = {...(current.rules || {})};
  const index = draft.originalValue === null ? -1 : times.indexOf(draft.originalValue);
  if (draft.originalValue !== null && index < 0) {
    S.reminderError = '原提醒时间已更改，请返回重新选择。';
    renderReminder();
    return;
  }
  if (times.some((time, i) => time === value && i !== index)) {
    S.reminderError = `${value} 这个时间已经添加过了。`;
    renderReminder();
    ($('#reminder-hour') || $('#rem-quick-save') || $('#rem-save-btn'))?.focus({preventScroll: true});
    return;
  }
  if (index >= 0) {
    times[index] = value;
    if (draft.originalValue !== value) delete rules[draft.originalValue];
  } else {
    times.push(value);
  }
  times.sort();
  rules[value] = normClientRule({
    days: draft.days,
    skip_complete: draft.skip_complete,
    quiet: draft.quiet,
    lead_hours: draft.lead_hours,
    auto_refresh: draft.auto_refresh,
    notify_style: draft.notify_style,
  });
  S.reminderError = '';
  const ok = await saveReminder({enabled: true, times, rules});
  if (ok) {
    S.reminderEditing = null;
    if ($('#reminder-dialog').open) { renderReminder(); focusReminderTime(value); }
  } else if ($('#reminder-dialog').open && S.reminderEditing === draft) {
    ($('#rem-quick-save') || $('#rem-save-btn') || $('#reminder-hour'))?.focus({preventScroll: true});
  }
}

function buildDialTicksSvg() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 268 268');
  svg.setAttribute('class', 'gc-ticks');
  svg.setAttribute('aria-hidden', 'true');
  for (let i = 0; i < 60; i++) {
    const major = i % 5 === 0, quarter = i % 15 === 0;
    const ang = (i * 6 - 90) * Math.PI / 180;
    const r1 = quarter ? 120.5 : major ? 122 : 124.5;
    const r2 = 128;
    const line = document.createElementNS(ns, 'line');
    line.setAttribute('x1', (134 + r1 * Math.cos(ang)).toFixed(2));
    line.setAttribute('y1', (134 + r1 * Math.sin(ang)).toFixed(2));
    line.setAttribute('x2', (134 + r2 * Math.cos(ang)).toFixed(2));
    line.setAttribute('y2', (134 + r2 * Math.sin(ang)).toFixed(2));
    line.setAttribute('class', quarter ? 't-q' : major ? 't-m' : 't-s');
    svg.append(line);
  }
  return svg;
}

function renderReminderWizard(draft) {
  const wrap = el('div', 'rem-wiz');
  const top = el('div', 'wiz-top');
  const back = btn('时间卡', 'btn-ghost btn-sm', cancelReminderEditor, 'back');
  back.disabled = !!S.reminderSaving;
  const sub = el('span', 'wiz-sub', draft.originalValue ? `调整 ${draft.originalValue}` : '新建提醒时间');
  top.append(back, sub);

  const stepper = el('div', 'wiz-steps');
  stepper.setAttribute('role', 'tablist');
  for (const [n, label] of [[1, '选择时间'], [2, '周期与免打扰'], [3, '加急与通知']]) {
    const active = draft.step === n, done = draft.step > n;
    const b = el('button', 'wiz-step' + (active ? ' on' : '') + (done ? ' done' : ''));
    b.type = 'button';
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-selected', String(active));
    b.disabled = !!S.reminderSaving;
    const num = el('i', '', done ? '' : String(n));
    if (done) num.append(icon('check'));
    b.append(num, el('span', '', label));
    b.onclick = () => { draft.step = n; S.reminderError = ''; renderReminder(); };
    stepper.append(b);
  }
  wrap.append(top, stepper);

  const stage = el('div', 'wiz-stage');
  const errorEl = el('p', 'rem-error', S.reminderError || '');
  errorEl.setAttribute('role', 'alert');

  if (draft.step === 1) {
    const sec = el('div', 'wiz-pane s1');
    const readout = el('div', 'clk-readout');
    const hBtn = el('button', 'clk-seg' + (draft.dialMode === 'hour' ? ' on' : ''), pad(draft.hour));
    hBtn.type = 'button'; hBtn.id = 'reminder-hour';
    hBtn.setAttribute('role', 'spinbutton'); hBtn.setAttribute('aria-label', '小时');
    hBtn.setAttribute('aria-valuemin', '0'); hBtn.setAttribute('aria-valuemax', '23');
    hBtn.setAttribute('aria-valuenow', String(draft.hour));
    hBtn.disabled = !!S.reminderSaving;

    const mBtn = el('button', 'clk-seg' + (draft.dialMode === 'minute' ? ' on' : ''), pad(draft.minute));
    mBtn.type = 'button'; mBtn.id = 'reminder-minute';
    mBtn.setAttribute('role', 'spinbutton'); mBtn.setAttribute('aria-label', '分钟');
    mBtn.setAttribute('aria-valuemin', '0'); mBtn.setAttribute('aria-valuemax', '59');
    mBtn.setAttribute('aria-valuenow', String(draft.minute));
    mBtn.disabled = !!S.reminderSaving;

    const meta = el('div', 'clk-meta');
    const periodBadge = el('span', 'clk-period', reminderPeriod(reminderDraftTime(draft)));
    const modeHint = el('span', 'clk-mode-hint', draft.dialMode === 'hour' ? '外圈 1–12 · 内圈 13–00' : '外圈 00–55 · 支持拖拽微调');
    meta.append(periodBadge, modeHint);

    const nums = el('div', 'clk-nums');
    nums.append(hBtn, el('span', 'clk-sep', ':'), mBtn);
    readout.append(nums, meta);

    // Build Google Clock 24h Two-Ring Dial
    const dialWrap = el('div', 'gc-dial-wrap');
    const dial = el('div', 'gc-dial');
    dial.id = 'gc-dial';
    const track = el('div', 'gc-inner-track');
    const ticks = buildDialTicksSvg();
    const hand = el('div', 'gc-hand');
    const handLine = el('div', 'gc-hand-line');
    const knob = el('div', 'gc-knob');
    hand.append(handLine, knob);
    const center = el('div', 'gc-center');

    const hourLayer = el('div', 'gc-layer hours' + (draft.dialMode === 'hour' ? ' on' : ''));
    const hourButtons = new Map();
    for (let i = 0; i < 12; i++) {
      const ang = i * 30;
      const rad = ang * Math.PI / 180;
      const hOut = i === 0 ? 12 : i;
      const bOut = el('button', 'gc-num', String(hOut));
      bOut.type = 'button'; bOut.dataset.hour = String(hOut);
      bOut.setAttribute('aria-label', `${hOut} 时`);
      bOut.style.left = (50 + 38.4 * Math.sin(rad)).toFixed(2) + '%';
      bOut.style.top = (50 - 38.4 * Math.cos(rad)).toFixed(2) + '%';
      bOut.onclick = e => {
        if (e.detail === 0 && !S.reminderSaving) {
          draft.hour = hOut; S.reminderError = ''; errorEl.textContent = ''; paintDial(true);
        }
      };
      hourButtons.set(hOut, bOut);
      hourLayer.append(bOut);

      const hIn = i === 0 ? 0 : i + 12;
      const bIn = el('button', 'gc-num inner', i === 0 ? '00' : String(hIn));
      bIn.type = 'button'; bIn.dataset.hour = String(hIn);
      bIn.setAttribute('aria-label', `${hIn} 时`);
      bIn.style.left = (50 + 24.4 * Math.sin(rad)).toFixed(2) + '%';
      bIn.style.top = (50 - 24.4 * Math.cos(rad)).toFixed(2) + '%';
      bIn.onclick = e => {
        if (e.detail === 0 && !S.reminderSaving) {
          draft.hour = hIn; S.reminderError = ''; errorEl.textContent = ''; paintDial(true);
        }
      };
      hourButtons.set(hIn, bIn);
      hourLayer.append(bIn);
    }

    const minLayer = el('div', 'gc-layer minutes' + (draft.dialMode === 'minute' ? ' on' : ''));
    const minButtons = new Map();
    for (let i = 0; i < 12; i++) {
      const mVal = i * 5;
      const rad = (i * 30) * Math.PI / 180;
      const bMin = el('button', 'gc-num', pad(mVal));
      bMin.type = 'button'; bMin.dataset.minute = String(mVal);
      bMin.setAttribute('aria-label', `${pad(mVal)} 分`);
      bMin.style.left = (50 + 38.4 * Math.sin(rad)).toFixed(2) + '%';
      bMin.style.top = (50 - 38.4 * Math.cos(rad)).toFixed(2) + '%';
      bMin.onclick = e => {
        if (e.detail === 0 && !S.reminderSaving) {
          draft.minute = mVal; S.reminderError = ''; errorEl.textContent = ''; paintDial(false);
        }
      };
      minButtons.set(mVal, bMin);
      minLayer.append(bMin);
    }

    dial.append(track, ticks, hand, center, hourLayer, minLayer);
    dialWrap.append(dial);

    const presets = el('div', 'rem-chips');
    for (const [val, lbl] of [['08:00', '早八'], ['12:30', '午间'], ['18:30', '傍晚'], ['21:30', '晚间']]) {
      const c = btn(`${val} ${lbl}`, 'chip rem-preset-chip', () => {
        const [ph, pm] = val.split(':').map(Number);
        draft.hour = ph; draft.minute = pm;
        S.reminderError = ''; errorEl.textContent = '';
        paintDial(false);
      });
      c.type = 'button'; c.dataset.time = val; c.disabled = !!S.reminderSaving;
      presets.append(c);
    }

    let autoMinTimer = null;
    const paintDial = (animateSwitch = false) => {
      const h = draft.hour, m = draft.minute, mode = draft.dialMode;
      hBtn.textContent = pad(h);
      hBtn.setAttribute('aria-valuenow', String(h));
      hBtn.classList.toggle('on', mode === 'hour');
      mBtn.textContent = pad(m);
      mBtn.setAttribute('aria-valuenow', String(m));
      mBtn.classList.toggle('on', mode === 'minute');
      periodBadge.textContent = `${reminderPeriod(reminderDraftTime(draft))} · ${reminderDraftTime(draft)}`;
      modeHint.textContent = mode === 'hour' ? '外圈 1–12 · 内圈 13–00' : '外圈 00–55 · 支持拖拽微调';
      hourLayer.classList.toggle('on', mode === 'hour');
      minLayer.classList.toggle('on', mode === 'minute');
      if (mode === 'hour') {
        const isInner = h === 0 || h >= 13;
        const deg = (h % 12) * 30;
        hand.style.transform = `rotate(${deg}deg)`;
        hand.style.height = isInner ? '24.4%' : '38.4%';
        knob.style.transform = `translate(-50%, -50%) rotate(${-deg}deg)`;
        knob.textContent = h === 0 ? '00' : String(h);
        knob.classList.remove('dot');
      } else {
        const deg = m * 6;
        hand.style.transform = `rotate(${deg}deg)`;
        hand.style.height = '38.4%';
        knob.style.transform = `translate(-50%, -50%) rotate(${-deg}deg)`;
        if (m % 5 === 0) {
          knob.textContent = pad(m);
          knob.classList.remove('dot');
        } else {
          knob.textContent = '';
          knob.classList.add('dot');
        }
      }
      hourButtons.forEach((b, val) => b.classList.toggle('sel', mode === 'hour' && val === h));
      minButtons.forEach((b, val) => b.classList.toggle('sel', mode === 'minute' && val === m));
      const fullVal = reminderDraftTime(draft);
      presets.querySelectorAll('button').forEach(chip => chip.setAttribute('aria-pressed', String(chip.dataset.time === fullVal)));
      if (animateSwitch) {
        clearTimeout(autoMinTimer);
        autoMinTimer = setTimeout(() => {
          if (S.reminderEditing === draft && draft.step === 1 && draft.dialMode === 'hour') {
            draft.dialMode = 'minute';
            paintDial(false);
          }
        }, 240);
      }
    };

    const pickByPointer = (e, commit = false) => {
      if (S.reminderSaving) return;
      const rect = dial.getBoundingClientRect();
      const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
      const dx = e.clientX - cx, dy = e.clientY - cy;
      let deg = Math.atan2(dx, -dy) * 180 / Math.PI;
      if (deg < 0) deg += 360;
      if (draft.dialMode === 'hour') {
        const distRatio = Math.hypot(dx, dy) / (rect.width / 2);
        const step12 = Math.round(deg / 30) % 12;
        const inner = distRatio < 0.62;
        draft.hour = inner ? (step12 === 0 ? 0 : step12 + 12) : (step12 === 0 ? 12 : step12);
        S.reminderError = ''; errorEl.textContent = '';
        paintDial(commit);
      } else {
        const min = Math.round(deg / 6) % 60;
        draft.minute = min;
        S.reminderError = ''; errorEl.textContent = '';
        paintDial(false);
      }
    };

    let dragging = false, activePointer = null, moved = false, startX = 0, startY = 0, hitNum = null;
    dial.addEventListener('pointerdown', e => {
      if (e.button !== 0 || S.reminderSaving) return;
      clearTimeout(autoMinTimer);
      dragging = true;
      moved = false;
      hitNum = null;
      startX = e.clientX; startY = e.clientY;
      activePointer = e.pointerId;
      dial.classList.add('dragging');
      dial.setPointerCapture?.(e.pointerId);
      const nb = e.target.closest('.gc-num');
      if (nb && draft.dialMode === 'hour' && nb.dataset.hour !== undefined) {
        hitNum = {mode: 'hour', val: Number(nb.dataset.hour)};
        draft.hour = hitNum.val;
        S.reminderError = ''; errorEl.textContent = '';
        paintDial(false);
      } else if (nb && draft.dialMode === 'minute' && nb.dataset.minute !== undefined) {
        hitNum = {mode: 'minute', val: Number(nb.dataset.minute)};
        draft.minute = hitNum.val;
        S.reminderError = ''; errorEl.textContent = '';
        paintDial(false);
      } else {
        pickByPointer(e, false);
      }
    });
    dial.addEventListener('pointermove', e => {
      if (!dragging || e.pointerId !== activePointer) return;
      if (!moved && Math.hypot(e.clientX - startX, e.clientY - startY) >= 5) moved = true;
      if (moved || !hitNum) pickByPointer(e, false);
    });
    const endDialDrag = e => {
      if (!dragging || (e.pointerId !== undefined && e.pointerId !== activePointer)) return;
      dragging = false;
      activePointer = null;
      dial.classList.remove('dragging');
      if (e.type === 'pointerup') {
        if (!moved && hitNum) {
          if (hitNum.mode === 'hour') { draft.hour = hitNum.val; paintDial(true); }
          else { draft.minute = hitNum.val; paintDial(false); }
        } else {
          pickByPointer(e, draft.dialMode === 'hour');
        }
      }
      hitNum = null;
    };
    for (const ev of ['pointerup', 'pointercancel']) dial.addEventListener(ev, endDialDrag);

    hBtn.onclick = () => { clearTimeout(autoMinTimer); draft.dialMode = 'hour'; paintDial(false); };
    mBtn.onclick = () => { clearTimeout(autoMinTimer); draft.dialMode = 'minute'; paintDial(false); };
    hBtn.onkeydown = e => {
      if (S.reminderSaving) return;
      if (e.key === 'ArrowUp') { e.preventDefault(); draft.hour = (draft.hour + 1) % 24; paintDial(false); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); draft.hour = (draft.hour + 23) % 24; paintDial(false); }
      else if (e.key === 'Home') { e.preventDefault(); draft.hour = 0; paintDial(false); }
      else if (e.key === 'End') { e.preventDefault(); draft.hour = 23; paintDial(false); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); mBtn.focus(); draft.dialMode = 'minute'; paintDial(false); }
    };
    mBtn.onkeydown = e => {
      if (S.reminderSaving) return;
      if (e.key === 'ArrowUp') { e.preventDefault(); draft.minute = (draft.minute + 1) % 60; paintDial(false); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); draft.minute = (draft.minute + 59) % 60; paintDial(false); }
      else if (e.key === 'Home') { e.preventDefault(); draft.minute = 0; paintDial(false); }
      else if (e.key === 'End') { e.preventDefault(); draft.minute = 59; paintDial(false); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); hBtn.focus(); draft.dialMode = 'hour'; paintDial(false); }
    };

    paintDial(false);
    sec.append(readout, dialWrap, presets, errorEl);

    const foot = el('div', 'wiz-foot');
    const cancelBtn = btn('取消', 'btn-quiet', cancelReminderEditor);
    const quickSave = btn('直接保存', 'btn', () => commitReminderWizard(draft));
    const nextBtn = btn('下一步 · 周期设置', 'btn-primary', () => { draft.step = 2; S.reminderError = ''; renderReminder(); }, 'arrow');
    quickSave.id = 'rem-quick-save';
    nextBtn.id = 'rem-next-btn';
    cancelBtn.disabled = quickSave.disabled = nextBtn.disabled = !!S.reminderSaving;
    foot.append(cancelBtn, quickSave, nextBtn);
    stage.append(sec, foot);
  } else if (draft.step === 2) {
    const sec = el('div', 'wiz-pane s2');
    const g1 = el('div', 'wiz-card');
    g1.append(el('div', 'wiz-lbl', '重复周期'));
    const presetRow = el('div', 'wiz-chips');
    const curDays = [...draft.days].sort((a, b) => a - b).join(',');
    for (const [lbl, arr] of [['每天', [1,2,3,4,5,6,7]], ['工作日', [1,2,3,4,5]], ['周末', [6,7]]]) {
      const c = btn(lbl, 'chip', () => { draft.days = [...arr]; renderReminder(); });
      c.type = 'button';
      c.setAttribute('aria-pressed', String(curDays === arr.join(',')));
      presetRow.append(c);
    }
    const dayRow = el('div', 'day-pills');
    for (let d = 1; d <= 7; d++) {
      const on = draft.days.includes(d);
      const b = btn(WEEK_NAMES[d - 1], 'day-pill' + (on ? ' on' : ''), () => {
        if (on && draft.days.length <= 1) return;
        draft.days = on ? draft.days.filter(x => x !== d) : [...draft.days, d].sort((a, b) => a - b);
        renderReminder();
      });
      b.type = 'button'; b.setAttribute('aria-pressed', String(on));
      b.setAttribute('aria-label', `周${WEEK_NAMES[d - 1]}`);
      dayRow.append(b);
    }
    g1.append(presetRow, dayRow);

    const g2 = el('div', 'switch-row wiz-switch');
    const g2txt = el('div', 'sr-text');
    g2txt.append(el('strong', '', '本机已写完时不打扰'), el('small', '', '待提交作业在本地均已有完整草稿时跳过此次提醒'));
    const g2sw = el('button', 'switch');
    g2sw.type = 'button'; g2sw.setAttribute('role', 'switch');
    g2sw.setAttribute('aria-checked', String(!!draft.skip_complete));
    g2sw.setAttribute('aria-label', '本机已写完时不打扰');
    g2sw.onclick = () => { draft.skip_complete = !draft.skip_complete; renderReminder(); };
    g2.append(g2txt, g2sw);

    const g3 = el('div', 'wiz-card');
    g3.append(el('div', 'wiz-lbl', '免打扰时段（此期间不弹提醒）'));
    const qRow = el('div', 'wiz-chips');
    for (const [val, lbl] of [['', '不限制'], ['23:00-07:30', '深夜 23:00–07:30'], ['22:30-07:00', '休息 22:30–07:00'], ['12:30-14:00', '午休 12:30–14:00']]) {
      const c = btn(lbl, 'chip', () => { draft.quiet = val; renderReminder(); });
      c.type = 'button'; c.setAttribute('aria-pressed', String(draft.quiet === val));
      qRow.append(c);
    }
    g3.append(qRow);
    sec.append(g1, g2, g3);

    const foot = el('div', 'wiz-foot');
    const prevBtn = btn('上一步', 'btn-quiet', () => { draft.step = 1; renderReminder(); });
    const nextBtn = btn('下一步 · 加急与通知', 'btn-primary', () => { draft.step = 3; renderReminder(); }, 'arrow');
    foot.append(prevBtn, nextBtn);
    stage.append(sec, foot);
  } else {
    const sec = el('div', 'wiz-pane s3');
    const g1 = el('div', 'wiz-card');
    g1.append(el('div', 'wiz-lbl', '截止前加急提醒（独立追加通知，可多选）'));
    const leadRow = el('div', 'wiz-chips');
    for (const [h, lbl] of [[24, '截止前 24 小时'], [6, '截止前 6 小时'], [2, '截止前 2 小时']]) {
      const on = draft.lead_hours.includes(h);
      const c = btn(lbl, 'chip', () => {
        draft.lead_hours = on ? draft.lead_hours.filter(x => x !== h) : [...draft.lead_hours, h].sort((a, b) => b - a);
        renderReminder();
      });
      c.type = 'button'; c.setAttribute('aria-pressed', String(on));
      leadRow.append(c);
    }
    g1.append(leadRow);

    const g2 = el('div', 'switch-row wiz-switch');
    const g2txt = el('div', 'sr-text');
    g2txt.append(el('strong', '', '提醒前先静默刷新一次'), el('small', '', '确保最新截止时间和提交状态已从学习通同步'));
    const g2sw = el('button', 'switch');
    g2sw.type = 'button'; g2sw.setAttribute('role', 'switch');
    g2sw.setAttribute('aria-checked', String(!!draft.auto_refresh));
    g2sw.setAttribute('aria-label', '提醒前先静默刷新一次');
    g2sw.onclick = () => { draft.auto_refresh = !draft.auto_refresh; renderReminder(); };
    g2.append(g2txt, g2sw);

    const g3 = el('div', 'wiz-card');
    g3.append(el('div', 'wiz-lbl', '通知样式'));
    const seg = el('div', 'style-seg');
    for (const [val, title, subText] of [['center', '通知中心静默留存', '进入系统通知中心，不常驻遮挡屏幕'], ['banner', '桌面横幅提醒', '屏幕右下角短暂浮现并自动收起']]) {
      const on = draft.notify_style === val;
      const b = el('button', 'style-opt' + (on ? ' on' : ''));
      b.type = 'button'; b.setAttribute('aria-pressed', String(on));
      b.append(el('strong', '', title), el('small', '', subText));
      b.onclick = () => { draft.notify_style = val; renderReminder(); };
      seg.append(b);
    }
    g3.append(seg);

    const activePending = (S.records || []).filter(rec => rec.group === 'active' && (!draft.skip_complete || progressFor(rec).state !== 'complete'));
    const pv = el('div', 'rem-preview-box');
    const pvHead = el('div', 'rem-pv-head');
    pvHead.append(icon('bell'), el('strong', '', `通知预览 · ${reminderDraftTime(draft)}`), el('span', 'rem-pv-style', draft.notify_style === 'banner' ? '桌面横幅' : '通知中心'));
    const pvBody = el('div', 'rem-pv-body');
    if (activePending.length) {
      pvBody.append(el('div', 'rem-pv-sum', `${activePending.length} 项待提交 · ${formatDaysSummary(draft.days)}提醒`));
      for (const rec of activePending.slice(0, 2)) {
        pvBody.append(el('div', 'rem-pv-item', `${rec.course || '课程'} · ${rec.title || '作业'}`));
      }
    } else if ((S.records || []).some(rec => rec.group === 'active')) {
      pvBody.append(el('div', 'rem-pv-sum', '当前待办本机已全部写完（按规则将自动跳过本次打扰）'));
    } else {
      pvBody.append(el('div', 'rem-pv-sum', '示例：3 项待提交 · 今天 23:59 截止（1 项未作答）'));
    }
    pv.append(pvHead, pvBody);

    sec.append(g1, g2, g3, pv, errorEl);

    const foot = el('div', 'wiz-foot');
    const prevBtn = btn('上一步', 'btn-quiet', () => { draft.step = 2; renderReminder(); });
    const saveBtn = btn(draft.originalValue ? '保存更改' : '完成并保存', 'btn-primary', () => commitReminderWizard(draft), 'check');
    saveBtn.id = 'rem-save-btn';
    prevBtn.disabled = saveBtn.disabled = !!S.reminderSaving;
    foot.append(prevBtn, saveBtn);
    stage.append(sec, foot);
  }

  wrap.append(stage);
  return wrap;
}

function renderReminder() {
  const body = $('#reminder-body'), dialog = $('#reminder-dialog'), r = S.reminder;
  const scrollTop = body.scrollTop;
  body.replaceChildren(closeButton(dialog), el('div', 'sheet-eyebrow', '本机提醒'));
  const head = el('div', 'rem-title-row');
  head.append(titled('reminder-title', '作业提醒'));
  if (S.reminderSaving) head.append(el('span', 'rem-note', '保存中…'));
  else if (S.reminderSavedAt && Date.now() - S.reminderSavedAt < 1800) {
    const saved = el('span', 'rem-saved');
    saved.append(icon('check'), el('span', '', '已保存'));
    head.append(saved);
  }
  body.append(head);
  if (!r) { body.append(el('p', 'sheet-text', '正在读取…')); return; }

  if (S.reminderEditing) {
    body.append(renderReminderWizard(S.reminderEditing));
    body.scrollTop = 0;
    return;
  }

  body.append(el('p', 'rem-description', '卡片式管理每日定时提醒，添加时通过双圈表盘和向导配置策略。'));

  const slots = reminderSlots(r), next = slots[0], todayCount = slots.filter(slot => slot.day === '今天').length;
  const card = el('div', 'rem-status' + (r.enabled ? '' : ' off')), mark = el('span', 'rem-bell' + (r.enabled && Date.now() < S.bellRingUntil ? ' ringing' : '')), text = el('div', 'rem-status-text');
  mark.append(icon('bell'));
  if (!r.enabled) {
    text.append(el('small', '', '提醒已关闭'), el('strong', '', '不会再弹出提醒'), el('span', '', '时间卡已保留，开启后恢复'));
  } else if (!next) {
    text.append(el('small', '', '下次提醒'), el('strong', '', '尚未设置'), el('span', '', '添加时间卡后生效'));
  } else {
    text.append(el('small', '', '下次提醒'), el('strong', '', `${next.day} ${next.value}`),
      el('span', '', `${left(next.target - Date.now())}${todayCount ? ` · 今天还有 ${todayCount} 次` : ''}`));
  }
  const sw = el('button', 'switch');
  sw.id = 'reminder-switch';
  sw.type = 'button';
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', String(r.enabled));
  sw.setAttribute('aria-label', '作业提醒');
  sw.disabled = !!S.reminderSaving;
  sw.onclick = async () => {
    const turningOn = !S.reminder.enabled;
    if (turningOn) S.bellRingUntil = Date.now() + 700;
    S.reminderEditing = null;
    await saveReminder({enabled: turningOn, times: S.reminder.times, rules: S.reminder.rules});
    if (dialog.open) $('#reminder-switch')?.focus({preventScroll: true});
  };
  card.append(mark, text, sw);
  body.append(card);

  const wrap = el('div', 'rem-body' + (r.enabled ? '' : ' off'));
  const schedule = el('section', 'rem-section'), count = el('div', 'rem-head');
  const addTop = btn('添加提醒', 'btn-primary btn-sm', () => openReminderWizard(null), 'plus');
  addTop.id = 'reminder-add';
  addTop.disabled = !r.enabled || !!S.reminderSaving || (r.times || []).length >= (r.max_times || 12);
  count.append(el('strong', '', '时间卡片'), el('span', 'rem-count', `${(r.times || []).length} / ${r.max_times || 12}`), addTop);
  schedule.append(count);

  const list = el('div', 'tcards');
  list.id = 'reminder-time-list';
  (r.times || []).forEach((value, index) => {
    const rule = normClientRule(r.rules?.[value]);
    const slot = slots.find(s => s.value === value);
    const tc = el('div', 'tcard');
    const tcTop = el('div', 'tcard-top');
    const mainBtn = el('button', 'tcard-main');
    mainBtn.type = 'button';
    mainBtn.dataset.time = value;
    mainBtn.disabled = !r.enabled || !!S.reminderSaving;
    mainBtn.setAttribute('aria-label', `提醒时间 ${index + 1}，${value}`);
    const tTime = el('span', 'tcard-time', value);
    const tMeta = el('div', 'tcard-meta');
    tMeta.append(el('strong', '', `${formatDaysSummary(rule.days)} · ${reminderPeriod(value)}`),
      el('small', '', r.enabled && slot ? `下次 ${slot.day} ${slot.value}` : '已关闭'));
    mainBtn.append(tTime, tMeta);
    mainBtn.onclick = () => openReminderWizard(value);

    const ops = el('div', 'tcard-ops');
    const editBtn = btn('编辑', 'btn-sm btn-ghost', () => openReminderWizard(value));
    editBtn.setAttribute('aria-label', `编辑 ${value}`);
    editBtn.disabled = !r.enabled || !!S.reminderSaving;
    const removeBtn = btn('', 'btn-quiet btn-sm icon-only', async e => {
      e.stopPropagation();
      const nextTimes = S.reminder.times.filter(time => time !== value);
      const nextRules = {...(S.reminder.rules || {})};
      delete nextRules[value];
      await saveReminder({enabled: true, times: nextTimes, rules: nextRules});
      focusReminderTime(nextTimes[0]);
    }, 'trash');
    removeBtn.setAttribute('aria-label', `删除 ${value}`);
    removeBtn.disabled = !r.enabled || !!S.reminderSaving;
    ops.append(editBtn, removeBtn);
    tcTop.append(mainBtn, ops);

    const tags = el('div', 'tcard-tags');
    if (rule.skip_complete) tags.append(el('span', 'tcard-tag ok', '写完不打扰'));
    if (rule.quiet) tags.append(el('span', 'tcard-tag', `免打扰 ${rule.quiet}`));
    if (rule.lead_hours?.length) tags.append(el('span', 'tcard-tag warn', `截止前 ${rule.lead_hours.join('/')}h 加急`));
    if (rule.auto_refresh) tags.append(el('span', 'tcard-tag', '前先刷新'));
    tags.append(el('span', 'tcard-tag muted', rule.notify_style === 'banner' ? '桌面横幅' : '通知中心'));

    tc.append(tcTop, tags);
    list.append(tc);
  });

  if (!(r.times || []).length) {
    const empty = el('div', 'reminder-empty');
    empty.append(icon('clock'), el('strong', '', '还没有提醒时间卡'), el('span', '', '点击上方「添加提醒」，使用谷歌时钟双圈表盘设定提醒时间与免打扰策略。'));
    list.append(empty);
  }
  schedule.append(list);

  if (S.reminderError) {
    const error = el('p', 'rem-error', S.reminderError); error.setAttribute('role', 'alert'); schedule.append(error);
  }
  wrap.append(schedule);
  body.append(wrap);

  const foot = el('div', 'rem-foot rem-foot-bar');
  const infoWrap = el('span', 'rem-foot-info');
  infoWrap.append(icon('info'), el('span', '', '助手运行时生效，关闭网页仍会提醒。'));
  const testBtn = btn('发测试通知', 'btn-sm btn-ghost', async () => {
    testBtn.disabled = true;
    try {
      const res = await api('/api/reminder/test', {});
      toast(res.native ? '测试通知已发送，请查看系统通知中心' : '系统未允许弹出原生通知，可在助手内查看', res.native ? 'ok' : 'warn');
    } catch (e) {
      toast('发送测试通知失败：' + e.message, 'error');
    } finally {
      testBtn.disabled = false;
    }
  }, 'bell');
  testBtn.id = 'reminder-test';
  foot.append(infoWrap, testBtn);
  body.append(foot);
  body.scrollTop = scrollTop;
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
const recordSig = r => JSON.stringify([r.course, r.title, r.deadline, r.status, r.group, r.questions?.length || 0]);
async function load({diff = false} = {}) {
  await flushAll().catch(() => {});
  const data = await api('/api/state');
  // After a refresh, remember which records are new or changed so the list can show it.
  const before = diff && S.loaded ? new Map(S.records.map(r => [r.key, recordSig(r)])) : null;
  const beforeDeadlines = diff && S.loaded ? new Map(S.records.map(r => [r.key, r.deadline || ''])) : null;
  const beforeGroups = diff && S.loaded ? new Map(S.records.map(r => [r.key, r.group || ''])) : null;
  const flipRects = diff && S.loaded ? new Map([...document.querySelectorAll('#assignment-list .item[data-key]')].map(n => [n.dataset.key, n.getBoundingClientRect().top])) : null;
  const nextKeys = new Set((data.assignments || []).map(r => r.key));
  const removedKeys = before ? [...before.keys()].filter(k => !nextKeys.has(k)) : [];
  S.records = data.assignments || [];
  if (before) {
    const now = Date.now();
    S.fresh = new Map();
    S.addedKeys = new Map();
    S.deadlineChangedKeys = new Map();
    S.diff = {
      added: 0,
      archived: removedKeys.filter(k => beforeGroups?.get(k) !== 'history').length,
      deadlineChanged: 0,
      otherChanged: 0,
      changed: 0,
    };
    for (const r of S.records) {
      const was = before.get(r.key);
      if (was === recordSig(r)) continue;
      if (was === undefined) {
        S.diff.added++;
        S.addedKeys.set(r.key, now);
      } else {
        S.diff.changed++;
        const prevGroup = beforeGroups ? beforeGroups.get(r.key) : '';
        const movedToHistory = prevGroup !== 'history' && r.group === 'history';
        const dueChanged = beforeDeadlines && beforeDeadlines.get(r.key) !== (r.deadline || '');
        if (movedToHistory) S.diff.archived++;
        else if (dueChanged) S.diff.deadlineChanged++;
        else S.diff.otherChanged++;
        if (dueChanged) {
          S.deadlineChangedKeys.set(r.key, now);
        }
      }
      S.fresh.set(r.key, now);
    }
  }
  S.aliases = data.record_aliases || {};
  for (const r of S.records) if (r.group === 'history') discardHistoryDraft(r.key);
  S.lastSuccess = data.last_success;
  S.lastCheck = data.last_check;
  S.warnings = data.last_stats?.warnings || [];
  const prevAcctId = S.auth?.active_account_id;
  S.loaded = true;
  S.loadError = null;
  applyJob(data.job);
  const nextAcctId = S.auth?.active_account_id;
  const accountSwitched = !!(prevAcctId && nextAcctId && prevAcctId !== nextAcctId);
  const prev = S.key;
  S.key = S.key ? canonicalKey(S.key) : null;
  if (accountSwitched) {
    drafts.clear();
    dirty.clear();
    saving.clear();
    saveState.clear();
    draftChanges.clear();
    S.historyExpanded = false;
    S.historyCleanConfirm = false;
    if (S.key && !nextKeys.has(S.key)) S.key = null;
  } else {
    for (const k of [...drafts.keys()]) if (!dirty.has(k) && !saving.has(k) && !(k === S.key && k === prev)) drafts.delete(k);
  }
  if (removedKeys.length && !accountSwitched) animateRemovedItems(removedKeys);
  renderList();
  if (!accountSwitched && flipRects && flipRects.size) {
    for (const node of document.querySelectorAll('#assignment-list .item[data-key]')) {
      const oldTop = flipRects.get(node.dataset.key);
      if (oldTop === undefined) continue;
      const dy = oldTop - node.getBoundingClientRect().top;
      if (Math.abs(dy) > 4 && Math.abs(dy) < 360) {
        node.animate([{transform: `translateY(${dy}px)`}, {transform: 'translateY(0)'}], {duration: 480, easing: 'cubic-bezier(.22, 1.28, .5, 1)'});
      }
    }
  }
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
let pollTimer = null;
function schedulePoll(ms) { clearTimeout(pollTimer); pollTimer = setTimeout(poll, ms); }

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
        await load({diff: job.action === 'refresh' && !key && !job.result?.error}).catch(e => toast(e.message, 'error'));
        onJobDone(job, key);
      }
    }
  } catch {
    S.serverLost = true;
  }
  renderStatus();
  schedulePoll(S.job.busy ? (S.job.action === 'login' ? 250 : 500) : 2000);
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
document.querySelectorAll('.tab').forEach(t => t.onclick = () => transitionToGroup(t.dataset.group));
$('#home').onclick = goHome;
$('#home-top').onclick = goHome;
$('#progress-filter').onchange = e => { S.progressFilter = e.target.value; S.filterEnter = true; renderList(); };
$('#due-filter').onchange = e => { S.dueFilter = e.target.value; S.filterEnter = true; renderList(); };
$('#search').oninput = () => { S.filterEnter = true; renderList(); };
$('#search').onkeydown = e => { if (e.key === 'Escape' && e.target.value) { e.target.value = ''; S.filterEnter = true; renderList(); } };
$('#refresh').onclick = () => startAction('refresh');
$('#back').onclick = () => setView('list');
$('#account-manage-button').onclick = () => { closeAccountMenu(); openAccountManager(); };
$('#reminder-button').onclick = () => {
  closeAccountMenu();
  S.reminderEditing = null; S.reminderError = ''; S.remOpenedKey = null;
  renderReminder(); openSheet($('#reminder-dialog'));
};
$('#reminder-dialog').addEventListener('cancel', e => {
  if (S.reminderEditing) { e.preventDefault(); cancelReminderEditor(); return; }
  e.preventDefault();
  closeSheet($('#reminder-dialog'));
});
$('#reminder-dialog').addEventListener('close', () => {
  S.reminderEditing = null; S.reminderError = ''; S.remOpenedKey = null; clearReminderWheel();
});
$('#account-exit-button').onclick = () => { closeAccountMenu(); openSheet($('#exit-dialog')); };
$('#exit-cancel').onclick = () => closeSheet($('#exit-dialog'));
$('#exit-confirm').onclick = exitApp;
function openImageViewer(img) {
  const dlg = $('#image-dialog'), large = $('#image-large'), stage = $('.viewer-stage');
  const srcRect = img.getBoundingClientRect();
  S.lastImage = { el: img, rect: srcRect };
  large.src = img.src;
  stage.classList.remove('actual');
  dlg.classList.remove('v-closing', 'v-open');
  if (!dlg.open) dlg.showModal();
  requestAnimationFrame(() => {
    dlg.classList.add('v-open');
    if (REDUCED_MOTION() || !srcRect.width || !srcRect.height) return;
    const play = () => {
      const dstRect = large.getBoundingClientRect();
      if (!dstRect.width || !dstRect.height) return;
      const dx = (srcRect.left + srcRect.width / 2) - (dstRect.left + dstRect.width / 2);
      const dy = (srcRect.top + srcRect.height / 2) - (dstRect.top + dstRect.height / 2);
      const sx = Math.max(0.08, srcRect.width / dstRect.width);
      const sy = Math.max(0.08, srcRect.height / dstRect.height);
      large.animate([
        { transform: `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})`, borderRadius: '8px' },
        { transform: 'none', borderRadius: '10px' }
      ], { duration: 400, easing: 'cubic-bezier(.22, 1.18, .36, 1)' });
    };
    if (large.complete && large.naturalWidth) play();
    else requestAnimationFrame(play);
  });
}
function closeImageViewer() {
  const dlg = $('#image-dialog'), large = $('#image-large');
  if (!dlg || !dlg.open) return;
  const curRect = large.getBoundingClientRect();
  const srcEl = S.lastImage?.el;
  const targetRect = srcEl?.isConnected ? srcEl.getBoundingClientRect() : null;
  if (!REDUCED_MOTION() && curRect.width > 0 && curRect.height > 0 && large.src) {
    const scrim = document.createElement('div');
    Object.assign(scrim.style, {
      position: 'fixed', inset: '0', zIndex: '55', pointerEvents: 'none',
      background: 'rgba(20, 19, 18, .72)'
    });
    scrim.setAttribute('aria-hidden', 'true');
    document.body.append(scrim);
    scrim.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 220, easing: 'ease-out', fill: 'forwards' })
      .finished.then(() => scrim.remove(), () => scrim.remove());

    const fly = document.createElement('img');
    fly.src = large.src;
    fly.alt = '';
    fly.setAttribute('aria-hidden', 'true');
    Object.assign(fly.style, {
      position: 'fixed', left: curRect.left + 'px', top: curRect.top + 'px',
      width: curRect.width + 'px', height: curRect.height + 'px',
      zIndex: '56', pointerEvents: 'none', objectFit: 'contain', borderRadius: '10px', margin: '0'
    });
    document.body.append(fly);
    let toTransform = 'scale(.9)', toOpacity = 0.05;
    if (targetRect && targetRect.width > 0 && targetRect.height > 0) {
      const dx = (targetRect.left + targetRect.width / 2) - (curRect.left + curRect.width / 2);
      const dy = (targetRect.top + targetRect.height / 2) - (curRect.top + curRect.height / 2);
      const sx = Math.max(0.08, targetRect.width / curRect.width);
      const sy = Math.max(0.08, targetRect.height / curRect.height);
      toTransform = `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})`;
      toOpacity = 0.25;
    }
    fly.animate([
      { transform: 'none', opacity: 1 },
      { transform: toTransform, opacity: toOpacity }
    ], { duration: 230, easing: 'cubic-bezier(.4, 0, .2, 1)', fill: 'forwards' })
      .finished.then(() => fly.remove(), () => fly.remove());
  }
  dlg.classList.remove('v-open', 'v-closing');
  dlg.close();
}
$('#image-close').onclick = closeImageViewer;
$('#image-dialog').addEventListener('cancel', e => { e.preventDefault(); closeImageViewer(); });
$('.viewer-stage').onclick = e => { if (e.target.tagName === 'IMG') e.currentTarget.classList.toggle('actual'); else closeImageViewer(); };
document.querySelectorAll('dialog.sheet').forEach(dlg => {
  dlg.addEventListener('click', e => { if (e.target === dlg) closeSheet(dlg); });
  if (dlg.id !== 'reminder-dialog' && dlg.id !== 'login-dialog') {
    dlg.addEventListener('cancel', e => { e.preventDefault(); closeSheet(dlg); });
  }
});
$('#login-dialog').addEventListener('cancel', e => {
  e.preventDefault();
  if (S.accountRenameId) { cancelAccountRename(); return; }
  if (loginPhase === 'form' && loginView === 'form' && loginFromList && savedAccountsList().length > 0) {
    slideM3View('list');
    return;
  }
  closeSheet($('#login-dialog'));
});
$('#login-dialog').addEventListener('close', () => {
  S.accountRemoveConfirmId = null;
  S.accountRenameId = null;
  S.accountRenameValue = '';
  S.loginListSig = '';
  loginTarget = null;
  loginFromList = false;
  loginAliasDraft = '';
});
$('#detail').addEventListener('click', e => {
  const act = e.target.closest('[data-act]');
  if (act && S.key) { startAction(act.dataset.act === 'open' ? 'open' : 'refresh', S.key); return; }
  const img = e.target.closest('.rich img');
  if (img && !img.closest('a')) {
    e.preventDefault();
    openImageViewer(img);
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
function onWindowAway() {
  const now = Date.now();
  for (const w of S.watchingSub.values()) {
    if (!w.blurred) w.blurredAt = now;
    w.blurred = true;
  }
}
function onWindowReturn() {
  if (S.exited || S.job.busy || S.subCheckBusyKey || !S.online || !S.watchingSub.size) return;
  const now = Date.now();
  const candidates = [S.key, ...S.watchingSub.keys()].filter(Boolean);
  for (const k of candidates) {
    const w = S.watchingSub.get(k);
    const r = S.records.find(x => x.key === k);
    if (!w || !r || r.group === 'history') { S.watchingSub.delete(k); continue; }
    const awayMs = now - (w.blurredAt || w.readyAt);
    const ready = now >= (w.nextAutoAt || 0);
    if (w.blurred && ready && awayMs >= 6000) {
      w.blurred = false;
      w.readyAt = now;
      w.nextAutoAt = now + 30000;
      triggerSubmissionCheck(k, {auto: true});
      break;
    }
  }
}
window.addEventListener('blur', onWindowAway);
window.addEventListener('focus', onWindowReturn);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') { onWindowAway(); flushAll().catch(() => {}); }
  else if (document.visibilityState === 'visible') onWindowReturn();
});
setInterval(tickElapsed, 1000);
setInterval(() => {
  if (!S.exited && !S.job.busy) {
    renderReminderRow();
    // Local API only: update deadline groups and summaries without crawling.
    load().catch(() => { if (!S.key) renderWelcome(); });
  }
}, 60000);
boot().finally(poll);
