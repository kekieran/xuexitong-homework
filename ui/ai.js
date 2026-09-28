'use strict';
/* AI UI uses the existing controls; requests and keys stay in the local backend. */
window.HomeworkAI = (() => {
  let config = null, selected = null, newProvider = 'openai';
  const edits = new Map();
  const settling = new Set();
  const settingsDialog = $('#ai-settings-dialog');

  function updateLabel() {
    const current = config?.profiles?.[config.active];
    $('#ai-model-label').textContent = current?.ready ? `${current.name} · ${current.model}` : '未配置';
  }
  async function openSettings() {
    try {
      config = await api('/api/ai/settings');
      selected = config.active;
      renderSettings();
      if (!settingsDialog.open) settingsDialog.showModal();
    } catch (e) { toast(e.message, 'error'); }
  }
  function renderSettings() {
    const body = $('#ai-settings-body'), saved = config.profiles[selected];
    const provider = saved?.provider || newProvider;
    const editKey = selected || 'new-' + provider;
    const current = {...(saved || {...config.presets[provider], model: '', has_key: false, name: ''}), ...edits.get(editKey)};
    body.replaceChildren(closeButton(settingsDialog), el('div', 'sheet-eyebrow', 'AI'));
    const title = el('h2', 'sheet-title', 'AI 设置'); title.id = 'ai-settings-title';
    body.append(title, el('p', 'ai-settings-subtitle', '保存常用模型，随时切换。'));
    const layout = el('div', 'ai-settings-layout'), library = el('section', 'ai-library');
    library.setAttribute('aria-label', '已保存的 AI 配置');
    const libraryHead = el('div', 'ai-library-head');
    libraryHead.append(el('h3', '', `我的配置 · ${Object.keys(config.profiles).length}`));
    const add = btn('添加配置', 'btn-sm', () => { selected = null; newProvider = 'openai'; renderSettings(); }, 'plus');
    add.id = 'ai-add-profile'; add.disabled = S.job.busy;
    libraryHead.append(add); library.append(libraryHead);
    const list = el('div', 'ai-profile-list');
    for (const [id, item] of Object.entries(config.profiles)) {
      const card = el('div', 'ai-profile-card' + (id === selected ? ' selected' : ''));
      card.dataset.profile = id;
      const edit = btn('', 'ai-profile-edit', () => { selected = id; renderSettings(); });
      edit.setAttribute('aria-label', '编辑 ' + item.name);
      edit.replaceChildren(el('strong', '', item.name), el('span', '', `${config.presets[item.provider].name} · ${item.model}`));
      edit.disabled = S.job.busy;
      const bottom = el('div', 'ai-profile-bottom');
      bottom.append(el('span', 'ai-profile-state', item.ready ? '已配置' : '待完善'));
      const use = btn(id === config.active ? '使用中' : '使用', 'btn-sm' + (id === config.active ? ' ai-current' : ''), async () => {
        use.disabled = true;
        try { config = await api('/api/ai/settings', {id, activate: true}); updateLabel(); renderSettings(); }
        catch (e) { toast(e.message, 'error'); use.disabled = false; }
      });
      use.setAttribute('aria-label', (id === config.active ? '正在使用 ' : '使用 ') + item.name);
      use.disabled = S.job.busy || !item.ready || id === config.active;
      bottom.append(use); card.append(edit, bottom); list.append(card);
    }
    if (!Object.keys(config.profiles).length) list.append(el('div', 'ai-library-empty', '还没有配置\n添加一个常用模型开始使用'));
    library.append(list);
    const editor = el('section', 'ai-config-editor');
    editor.append(el('h3', 'ai-editor-title', saved ? '编辑配置' : '添加配置'));
    layout.append(library, editor); body.append(layout);
    const providers = el('div', 'ai-providers'); providers.setAttribute('role', 'tablist'); providers.setAttribute('aria-label', 'AI 平台');
    for (const [id, item] of Object.entries(config.presets)) {
      const tab = btn(item.name, 'ai-provider', () => { newProvider = id; renderSettings(); });
      tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', String(id === provider));
      tab.disabled = !!saved || S.job.busy;
      providers.append(tab);
    }
    editor.append(providers);
    const form = el('form'); form.autocomplete = 'off';
    const field = (label, input, id) => {
      input.id = id;
      const row = el('label', 'form-field'); row.append(el('span', '', label), input); form.append(row); return input;
    };
    const name = field('配置名称', el('input', 'input'), 'ai-profile-name');
    name.value = current.name || ''; name.maxLength = 80; name.placeholder = '例如：日常作答、复杂题目（选填）';
    const model = field('模型名称', el('input', 'input'), 'ai-model');
    model.required = true; model.value = current.model; model.placeholder = '填写服务商提供的模型名称';
    const secret = field('API 密钥', el('input', 'input'), 'ai-api-key');
    secret.type = 'password'; secret.autocomplete = 'new-password'; secret.spellcheck = false;
    secret.value = current.apiKey || '';
    secret.placeholder = current.has_key ? '已保存，留空不修改' : provider === 'custom' ? '无鉴权的本机服务可留空' : '填写 API 密钥';
    secret.required = !current.has_key && provider !== 'custom';
    const protocol = field('接口类型', el('select', 'input'), 'ai-protocol');
    for (const [id, label] of Object.entries(config.protocols)) {
      const option = el('option', '', label); option.value = id; protocol.append(option);
    }
    protocol.value = current.protocol; protocol.disabled = provider !== 'custom';
    const address = field('接口地址', el('input', 'input'), 'ai-base-url');
    address.type = 'url'; address.required = true; address.value = current.base_url; address.readOnly = provider !== 'custom';
    address.placeholder = 'https://example.com/v1';
    const visionRow = el('label', 'ai-checkbox'), vision = el('input'); vision.type = 'checkbox'; vision.id = 'ai-vision'; vision.checked = current.vision;
    visionRow.append(vision, el('span', '', '模型支持识图')); form.append(visionRow);
    const note = el('div', 'ai-note'); note.append(icon('info'), el('span', '', '密钥加密保存在本机。测试连接和作答会调用 API，费用由服务商收取。'));
    form.append(note);
    const error = el('p', 'ai-config-status'); error.id = 'ai-config-message'; error.setAttribute('role', 'status'); form.append(error);
    const actions = el('div', 'sheet-actions');
    const remove = btn('删除配置', 'btn-quiet', async () => {
      if (remove.dataset.confirm !== 'yes') { remove.dataset.confirm = 'yes'; remove.querySelector('span').textContent = '确认删除'; return; }
      remove.disabled = true;
      try { config = await api('/api/ai/settings', {id: selected, remove: true}); edits.delete(editKey); selected = config.active; updateLabel(); renderSettings(); }
      catch (e) { error.classList.add('error'); error.textContent = e.message; }
      finally { remove.disabled = false; }
    });
    remove.hidden = !saved;
    const save = btn('保存并使用', 'btn-primary'); save.type = 'submit';
    const test = btn('测试连接', '', async () => {
      if (!form.reportValidity() || !(await persist())) return;
      $('#ai-config-message').className = 'ai-config-status';
      $('#ai-config-message').textContent = '正在测试连接…';
      if (await startAction('ai-test')) renderSettings();
    });
    test.title = '发送一次测试请求';
    actions.append(remove, test, save); form.append(actions); editor.append(form);
    async function persist() {
      save.disabled = test.disabled = true; error.textContent = '';
      const payload = {provider, name: name.value.trim() || model.value.trim(), protocol: protocol.value, base_url: address.value.trim(), model: model.value.trim(), api_key: secret.value.trim(), vision: vision.checked,
        ...(saved ? {id: selected} : {create: true})};
      secret.value = '';
      if (edits.has(editKey)) edits.get(editKey).apiKey = '';
      try {
        config = await api('/api/ai/settings', payload);
        edits.delete(editKey);
        selected = config.active;
        payload.api_key = '';
        updateLabel(); renderSettings();
        $('#ai-config-message').textContent = '已保存';
        $('#ai-config-message').classList.add('ok');
        return true;
      } catch (e) { error.classList.add('error'); error.textContent = e.message; return false; }
      finally { payload.api_key = ''; save.disabled = test.disabled = false; }
    }
    form.onsubmit = async e => { e.preventDefault(); if (form.reportValidity()) await persist(); };
    const keepEdit = () => edits.set(editKey, {name: name.value, model: model.value, apiKey: secret.value,
      base_url: address.value, protocol: protocol.value, vision: vision.checked});
    form.addEventListener('input', keepEdit); form.addEventListener('change', keepEdit);
    if (S.job.busy) form.querySelectorAll('button').forEach(b => b.disabled = true);
  }
  settingsDialog.addEventListener('close', () => { edits.clear(); const key = $('#ai-api-key'); if (key) key.value = ''; });

  function taskButton(r) {
    if (!r || r.group === 'history') return null;
    const writing = S.job.busy && S.job.action === 'ai' && S.job.key === r.key;
    const button = btn(writing ? 'AI 作答中…' : 'AI 作答', '', async () => {
      try {
        config = await api('/api/ai/settings'); updateLabel();
        if (!config.profiles[config.active]?.ready) { await openSettings(); return; }
        await startAction('ai', r.key);
      } catch (e) { toast(e.message, 'error'); }
    }, writing ? 'loader' : 'pen');
    button.id = 'ai-solve';
    button.disabled = S.job.busy || !S.online || !r.questions?.length;
    button.title = '只处理未作答的题目，不覆盖已有答案';
    return button;
  }

  function lockInputs(r) {
    const locked = !!r && (settling.has(r.key) || S.job.busy && S.job.action === 'ai' && S.job.key === r.key);
    $('#detail').classList.toggle('ai-locked', locked);
    for (const input of document.querySelectorAll('#detail .question input, #detail .question textarea, #detail .question button, #detail .legacy textarea')) {
      if (locked && !input.hasAttribute('data-ai-disabled')) {
        input.dataset.aiDisabled = String(input.disabled);
        input.disabled = true;
      } else if (!locked && input.hasAttribute('data-ai-disabled')) {
        input.disabled = input.dataset.aiDisabled === 'true';
        delete input.dataset.aiDisabled;
      }
    }
  }

  function templateCard(q, draft, key) {
    const template = draft.ai_templates?.[q.id];
    if (!template || q.type !== 'essay') return null;
    const box = el('details', 'ai-template'); box.open = true;
    const summary = el('summary');
    summary.append(icon('sparkle'), el('span', '', 'AI 解答模板'));
    if (template.model) summary.append(el('small', '', template.model));
    box.append(summary, el('div', 'ai-template-text', template.text));
    const actions = el('div', 'notice-actions');
    const use = btn('采用模板', 'btn-sm btn-primary', () => apply(true), 'check');
    const changed = q.signature !== template.signature, filled = answered(draft.answers[q.id]);
    use.disabled = changed || filled;
    if (changed) box.append(el('p', 'field-foot', '题目已变化，模板不能采用，可复制参考或移除。'));
    else if (filled) box.append(el('p', 'field-foot', '本题已有答案，模板仅供参考。'));
    actions.append(use, btn('复制', 'btn-sm', () => navigator.clipboard.writeText(template.text).then(() => toast('已复制')).catch(() => toast('复制失败，请手动选中文字', 'warn')), 'copy'), btn('移除', 'btn-sm btn-ghost', () => apply(false), 'trash'));
    box.append(actions);
    async function apply(adopt) {
      try {
        await flush(key);
        const latest = await api('/api/ai/template', {key, qid: String(q.id), adopt});
        drafts.set(key, latest);
        saveState.set(key, {state: 'saved', at: latest.updated_at});
        if (S.key === key) renderDetail(record(), latest, true);
        renderList();
      } catch (e) { toast(e.message, 'error'); }
    }
    return box;
  }

  async function jobDone(job) {
    if (job.action === 'ai-test') {
      const message = $('#ai-config-message');
      if (message) message.textContent = job.message;
      if (settingsDialog.open) { renderSettings(); $('#ai-config-message').textContent = job.message; $('#ai-config-message').classList.add(job.result?.error ? 'error' : 'ok'); }
      else toast(job.message, job.result?.error ? 'error' : '');
      return;
    }
    const key = canonicalKey(job.result?.key || job.key);
    try {
      await flushAll();
      drafts.delete(key);
      await load();
    } catch (e) { toast('答案刷新失败：' + e.message, 'error'); }
    finally { settling.delete(key); lockInputs(record()); }
    if (job.result?.error) { toast(job.message, 'error'); return; }
    const skipped = job.result?.skipped || [];
    const filled = job.result?.filled || [], templates = job.result?.templates || [];
    if (!skipped.length) { toast(templates.length ? job.message + '，模板在简答题下方' : job.message, 'ok'); return; }
    const dialog = $('#ai-result-dialog'), body = $('#ai-result-body');
    body.replaceChildren(closeButton(dialog), el('div', 'sheet-eyebrow', 'AI 作答'));
    const title = el('h2', 'sheet-title', '作答结果'); title.id = 'ai-result-title';
    const stats = el('div', 'stats');
    for (const [n, label, tone] of [[filled.length, '已填写', 'ok'], [templates.length, '简答模板', 'clay'], [skipped.length, '已跳过', 'warn']]) {
      const s = el('div', 'stat ' + (n ? tone : '')); s.append(el('b', '', String(n)), el('small', '', label)); stats.append(s);
    }
    body.append(title, stats, el('div', 'section-label', '跳过的题目'));
    const questions = S.records.find(r => r.key === key)?.questions || [];
    const list = el('ul', 'reasons');
    for (const item of skipped) {
      const index = questions.findIndex(q => String(q.id) === item.id);
      const row = el('li', 'reason');
      row.append(el('span', 'reason-q', index >= 0 ? `第 ${index + 1} 题` : '题目'), el('span', 'reason-main', item.reason));
      list.append(row);
    }
    const done = el('div', 'sheet-actions'); done.append(btn('完成', 'btn-primary', () => dialog.close()));
    const note = el('div', 'no-submit'); note.append(icon('lock'), el('span', '', 'AI 只写入助手，不会提交作业。请检查后再前往交作业。'));
    body.append(list, note, done);
    if (!dialog.open) dialog.showModal();
  }

  $('#ai-settings-button').onclick = openSettings;
  api('/api/ai/settings').then(value => { config = value; updateLabel(); if (record()) renderDock(record()); }).catch(() => {});
  function jobChanged(previous, next) {
    if (previous.busy && previous.action === 'ai' && !next.busy && next.action === 'ai') settling.add(canonicalKey(next.key));
  }
  return {openSettings, taskButton, templateCard, lockInputs, jobDone, jobChanged};
})();
