'use strict';
// View-independent local-draft helpers. Mirrors homework_workspace.py's v1
// progress/dashboard contract; UI designers can reuse these without DOM access.
window.HomeworkWorkspace = (() => {
  const labels = {unstarted: '未作答', partial: '部分作答', complete: '已写完', unread: '题目待读取'};
  const hasValue = v => Array.isArray(v) ? v.some(hasValue) : v !== null && v !== undefined && String(v).trim() !== '';
  function questionComplete(q, a) {
    const values = Array.isArray(a.value) ? a.value : [a.value];
    if (['single', 'multiple', 'judgment'].includes(q.type)) {
      const options = new Set((q.options || []).map(o => String(o.value)));
      return !!values.length && values.every(v => hasValue(v) && options.has(String(v))) && (q.type === 'multiple' || values.length === 1);
    }
    if (q.type === 'blank') return values.length === Math.max(1, Number(q.blank_count) || 1) && values.every(hasValue);
    if (q.type === 'upload') return !!a.files?.length;
    if (q.type === 'essay') return hasValue(a.value) || !!a.files?.length;
    return false;
  }
  function progress(r, draft = {}) {
    const answers = draft.answers || {}, questions = r.questions || [];
    let answered = 0, started = 0, needs_review = 0;
    for (const q of questions) {
      const a = answers[q.id] || {};
      if (!hasValue(a.value) && !a.files?.length) continue;
      started++;
      if (!q.signature || a.signature !== q.signature) needs_review++;
      else if (questionComplete(q, a)) answered++;
      else if (!['single', 'multiple', 'judgment', 'blank', 'upload', 'essay'].includes(q.type)) needs_review++;
    }
    const total = questions.length;
    const anySaved = Object.values(answers).some(a => hasValue(a.value) || a.files?.length) || hasValue(draft.legacy_text);
    const complete = total > 0 && answered === total && r.content_complete !== false;
    return {state: !total ? 'unread' : complete ? 'complete' : anySaved ? 'partial' : 'unstarted',
      total, answered, started, needs_review, content_complete: r.content_complete !== false,
      percentage: total ? Math.round(answered * 100 / total) : 0, updated_at: draft.updated_at || null};
  }
  function dashboard(records, now = new Date()) {
    const keys = Object.fromEntries(['active', 'review', 'history', 'today', 'within_3_days', 'within_7_days', ...Object.keys(labels)].map(k => [k, []]));
    const endToday = new Date(now); endToday.setHours(24, 0, 0, 0);
    let answered = 0, total = 0;
    for (const r of [...records].sort((a,b) => (a.deadline || '9999').localeCompare(b.deadline || '9999'))) {
      keys[r.group].push(r.key);
      if (r.group !== 'active') continue;
      keys[r.progress.state].push(r.key);
      answered += r.progress.answered; total += r.progress.total;
      const deadline = new Date(r.deadline || NaN);
      if (!Number.isFinite(+deadline) || deadline <= now) continue;
      if (deadline < endToday) keys.today.push(r.key);
      for (const days of [3, 7]) if (deadline - now <= days * 864e5) keys[`within_${days}_days`].push(r.key);
    }
    return {version: 1, generated_at: now.toISOString(), counts: Object.fromEntries(Object.entries(keys).map(([k,v]) => [k,v.length])), questions: {answered,total}, keys};
  }
  return Object.freeze({labels, questionComplete, progress, dashboard});
})();
