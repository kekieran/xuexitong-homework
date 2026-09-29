'use strict';
/* AI UI uses the existing controls; requests and keys stay in the local backend. */
window.HomeworkAI = (() => {
  let config = null, selected = null, newProvider = 'openai', settingsNotice = null;
  const deleteTimers = new Map();
  const settling = new Set();
  const settingsDialog = $('#ai-settings-dialog');

  function updateLabel() {
    const current = config?.profiles?.[config.active];
    $('#ai-model-label').textContent = current?.ready ? `${current.name} · ${current.model}` : '未配置';
  }
  async function openSettings() {
    try {
      config = await api('/api/ai/settings');
      selected = null;
      settingsNotice = null;
      renderSettings();
      if (!settingsDialog.open) settingsDialog.showModal();
    } catch (e) { toast(e.message, 'error'); }
  }
  function renderSettings() {
    const body = $('#ai-settings-body'), editingNew = selected === '__new__', saved = !editingNew && selected ? config.profiles[selected] : null;
    body.replaceChildren(closeButton(settingsDialog), el('div', 'sheet-eyebrow', 'AI'));
    const title = el('h2', 'sheet-title', 'AI 设置'); title.id = 'ai-settings-title';
    body.append(title, el('p', 'ai-settings-subtitle', '保存常用模型，随时切换。'));
    const library = el('section', 'ai-config-list-page'); library.setAttribute('aria-label', '已保存的 AI 配置');
    const libraryHead = el('div', 'ai-library-head');
    libraryHead.append(el('h3', '', `我的配置 · ${Object.keys(config.profiles).length}`));
    const add = btn('添加配置', 'btn-sm', () => { selected = '__new__'; newProvider = 'openai'; settingsNotice = null; renderSettings(); }, 'plus');
    add.id = 'ai-add-profile'; add.disabled = S.job.busy;
    libraryHead.append(add); library.append(libraryHead);
    const list = el('div', 'ai-config-list');
    if (editingNew) list.append(profileCard('__new__', null));
    for (const [id, item] of Object.entries(config.profiles)) list.append(profileCard(id, item));
    if (!list.children.length) list.append(el('div', 'ai-library-empty', '还没有配置\n添加一个常用模型开始使用'));
    library.append(list); body.append(library);
    function profileCard(id, item) {
      const expanded = id === selected, card = el('article', 'ai-profile-card' + (expanded ? ' expanded' : id === config.active ? ' active' : ''));
      card.dataset.profile = id;
      const head = el('div', 'ai-card-head'), toggle = btn('', 'ai-card-toggle', () => { selected = selected === id ? null : id; settingsNotice = null; renderSettings(); });
      toggle.disabled = S.job.busy;
      const main = el('div', 'ai-card-main');
      if (item) main.append(el('strong', '', item.name), el('span', '', `${config.presets[item.provider]?.name || '自定义'} · ${item.model || '未填写模型'}`));
      else main.append(el('strong', '', '新建 AI 配置'), el('span', '', '填写后保存并使用'));
      toggle.setAttribute('aria-label', item ? '编辑 ' + item.name : '编辑新配置');
      toggle.append(main); head.append(toggle);
      const actions = el('div', 'ai-card-actions');
      if (item && id === config.active) actions.append(el('span', 'ai-active-badge', '使用中'));
      else if (item) {
        const use = btn('使用', 'btn-sm', async () => {
          use.disabled = true;
          try { config = await api('/api/ai/settings', {id, activate: true}); updateLabel(); settingsNotice = {text: '已切换使用配置', tone: 'ok'}; renderSettings(); }
          catch (e) { toast(e.message, 'error'); use.disabled = false; }
        });
        use.setAttribute('aria-label', '使用 ' + item.name);
        use.disabled = S.job.busy || !item.ready; actions.append(use);
      }
      if (item) {
        const remove = btn('', 'btn-quiet icon-only ai-delete', async () => {
          if (remove.dataset.confirm !== 'yes') {
            remove.dataset.confirm = 'yes'; remove.replaceChildren(el('span', '', '确认删除'));
            const timer = setTimeout(() => { remove.dataset.confirm = ''; remove.replaceChildren(icon('trash')); deleteTimers.delete(id); }, 3000);
            deleteTimers.set(id, timer); return;
          }
          remove.disabled = true; clearTimeout(deleteTimers.get(id)); deleteTimers.delete(id);
          try { config = await api('/api/ai/settings', {id, remove: true}); selected = null; updateLabel(); settingsNotice = {text: '配置已删除', tone: 'ok'}; renderSettings(); }
          catch (e) { toast(e.message, 'error'); remove.disabled = false; }
        }, 'trash');
        remove.setAttribute('aria-label', '删除 ' + item.name); remove.title = '删除配置'; actions.append(remove);
      } else {
        actions.append(btn('取消', 'btn-sm', () => { selected = null; renderSettings(); }));
      }
      head.append(actions); card.append(head);
      if (expanded) card.append(profileEditor(id, item));
      return card;
    }
    function profileEditor(id, saved) {
      const provider = saved?.provider || newProvider;
      const current = {...(saved || {...config.presets[provider], model: '', has_key: false, name: ''})};
      const editor = el('div', 'ai-inline-editor');
      const providers = el('div', 'ai-providers'); providers.setAttribute('role', 'tablist'); providers.setAttribute('aria-label', 'AI 平台');
      for (const [providerId, item] of Object.entries(config.presets)) {
        const tab = btn(item.name, 'ai-provider', () => { newProvider = providerId; renderSettings(); });
        tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', String(providerId === provider)); tab.disabled = !!saved || S.job.busy; providers.append(tab);
      }
      editor.append(providers);
      const form = el('form', 'ai-inline-form'); form.autocomplete = 'off';
      const field = (label, input, idName, extra = '') => { input.id = idName; const row = el('label', 'form-field ' + extra); row.append(el('span', '', label), input); return row; };
      const name = el('input', 'input'); name.maxLength = 80; name.value = current.name || ''; name.placeholder = '例如：日常作答';
      const model = el('input', 'input'); model.required = true; model.value = current.model || ''; model.placeholder = '服务商提供的模型名称';
      const secret = el('input', 'input'); secret.type = 'password'; secret.autocomplete = 'new-password'; secret.spellcheck = false; secret.placeholder = current.has_key ? '已保存，留空不修改' : provider === 'custom' ? '本机服务可留空' : '填写 API 密钥'; secret.required = !current.has_key && provider !== 'custom';
      const protocol = el('select', 'input'); for (const [protocolId, label] of Object.entries(config.protocols)) { const option = el('option', '', label); option.value = protocolId; protocol.append(option); } protocol.value = current.protocol; protocol.disabled = provider !== 'custom';
      const address = el('input', 'input'); address.type = 'url'; address.required = true; address.value = current.base_url || ''; address.readOnly = provider !== 'custom'; address.placeholder = 'https://example.com/v1';
      const grid = el('div', 'ai-form-grid'); grid.append(field('模型名称', model, 'ai-model'), field('API 密钥', secret, 'ai-api-key'), field('配置名称', name, 'ai-profile-name'), field('接口类型', protocol, 'ai-protocol'), field('接口地址', address, 'ai-base-url', 'ai-form-wide')); form.append(grid);
      const visionRow = el('label', 'ai-checkbox'); const vision = el('input'); vision.type = 'checkbox'; vision.checked = !!current.vision; visionRow.append(vision, el('span', '', '模型支持识图')); form.append(visionRow);
      const note = el('div', 'ai-note'); note.append(icon('info'), el('span', '', '密钥只保存在本机，测试连接会调用你填写的接口。')); form.append(note);
      const status = el('p', 'ai-config-status' + (settingsNotice?.tone ? ' ' + settingsNotice.tone : '')); status.id = 'ai-config-message'; status.setAttribute('role', 'status'); if (settingsNotice) status.textContent = settingsNotice.text; form.append(status);
      const actions = el('div', 'ai-form-actions'), test = btn('测试连接', '', async () => { if (!form.reportValidity() || !(await persist())) return; settingsNotice = {text: '正在测试连接…', tone: ''}; renderSettings(); if (await startAction('ai-test')) {} }), save = btn('保存并使用', 'btn-primary'); save.type = 'submit';
      actions.append(test, save); form.append(actions); editor.append(form);
      async function persist() {
        save.disabled = test.disabled = true; settingsNotice = null;
        const payload = {provider, name: name.value.trim() || model.value.trim(), protocol: protocol.value, base_url: address.value.trim(), model: model.value.trim(), api_key: secret.value.trim(), vision: vision.checked, ...(saved ? {id} : {create: true})};
        secret.value = '';
        try { config = await api('/api/ai/settings', payload); selected = config.active; updateLabel(); settingsNotice = {text: '已保存', tone: 'ok'}; renderSettings(); return true; }
        catch (e) { settingsNotice = {text: e.message, tone: 'error'}; status.textContent = e.message; status.classList.add('error'); return false; }
        finally { payload.api_key = ''; save.disabled = test.disabled = false; }
      }
      form.onsubmit = async e => { e.preventDefault(); if (form.reportValidity()) await persist(); };
      if (S.job.busy) form.querySelectorAll('button, input, select').forEach(control => control.disabled = true);
      return editor;
    }
  }
  settingsDialog.addEventListener('close', () => { selected = null; settingsNotice = null; const key = $('#ai-api-key'); if (key) key.value = ''; });

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
      settingsNotice = {text: job.message, tone: job.result?.error ? 'error' : 'ok'};
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

  $('#ai-settings-button').onclick = () => { closeAccountMenu(); openSettings(); };
  api('/api/ai/settings').then(value => { config = value; updateLabel(); if (record()) renderDock(record()); }).catch(() => {});
  function jobChanged(previous, next) {
    if (previous.busy && previous.action === 'ai' && !next.busy && next.action === 'ai') settling.add(canonicalKey(next.key));
  }
  return {openSettings, taskButton, templateCard, lockInputs, jobDone, jobChanged};
})();
