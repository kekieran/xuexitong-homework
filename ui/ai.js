'use strict';
/* AI UI uses the existing controls; requests and keys stay in the local backend. */
window.HomeworkAI = (() => {
  const BRANDS = {
    openai: {cls: 'gpt', glyph: 'G', name: 'GPT'},
    claude: {cls: 'cla', glyph: 'C', name: 'Claude'},
    gemini: {cls: 'gem', glyph: '✦', name: 'Gemini'},
    deepseek: {cls: 'ds', glyph: 'D', name: 'DeepSeek'},
    custom: {cls: 'cus', glyph: '⚙', name: '自定义'},
  };
  let config = null, selected = null, newProvider = 'openai', lastProvRect = null, provObserver = null;
  let advOpen = false, showKey = false, testStartAt = 0;
  let shellRefs = null, drawerRefs = null, drawerCloseTimer = 0, prevProvidersWidth = 0;
  const deleteTimers = new Map();
  const lastTestById = new Map();
  const settling = new Set();
  const settingsDialog = $('#ai-settings-dialog');

  function getTestState(id) {
    if (!id || id === '__new__') return {state: 'idle', text: '未测试', sub: '测试连接会自动先保存配置，再发送探测请求', latencyMs: null};
    return lastTestById.get(id) || {state: 'idle', text: '未测试', sub: '测试连接会探测模型响应与延迟', latencyMs: null};
  }
  function syncProvPill(providers, animate) {
    const active = providers?.querySelector('.ai-provider[aria-selected="true"]');
    const pill = providers?.querySelector('.prov-pill');
    if (!providers || !active || !pill) return;
    const target = {left: active.offsetLeft, top: active.offsetTop, width: active.offsetWidth, height: active.offsetHeight};
    const setRect = r => {
      providers.style.setProperty('--px', r.left + 'px');
      providers.style.setProperty('--py', r.top + 'px');
      providers.style.setProperty('--pw', r.width + 'px');
      providers.style.setProperty('--ph', r.height + 'px');
    };
    const canAnimate = animate && lastProvRect && providers.dataset.pillReady === '1';
    if (canAnimate) {
      providers.classList.add('pill-instant');
      setRect(lastProvRect);
      void providers.offsetWidth;
      providers.classList.remove('pill-instant');
      setRect(target);
    } else {
      providers.classList.add('pill-instant');
      setRect(target);
      void providers.offsetWidth;
      providers.classList.remove('pill-instant');
    }
    providers.dataset.pillReady = '1';
    lastProvRect = {...target};
  }
  function updateLabel() {
    const current = config?.profiles?.[config.active];
    $('#ai-model-label').textContent = current?.ready ? `${current.name} · ${current.model}` : '未配置';
  }
  function clearDrawerCloseTimer() {
    if (drawerCloseTimer) { clearTimeout(drawerCloseTimer); drawerCloseTimer = 0; }
  }
  function destroyDrawerDom() {
    clearDrawerCloseTimer();
    if (provObserver) { provObserver.disconnect(); provObserver = null; }
    if (drawerRefs) {
      drawerRefs.scrim?.remove();
      drawerRefs.sheet?.remove();
      drawerRefs = null;
    }
    lastProvRect = null;
    prevProvidersWidth = 0;
  }
  function closeDrawer() {
    if (selected === null) return;
    selected = null;
    showKey = false;
    renderSettings();
  }
  function openDrawer(id) {
    if (selected === id && drawerRefs && !drawerRefs.closing) return;
    selected = id;
    if (id === '__new__') newProvider = 'openai';
    showKey = false;
    const prov = id === '__new__' ? newProvider : config?.profiles?.[id]?.provider;
    advOpen = prov === 'custom';
    renderSettings();
  }
  async function openSettings() {
    try {
      config = await api('/api/ai/settings');
      selected = null;
      showKey = false;
      destroyDrawerDom();
      shellRefs = null;
      $('#ai-settings-body').replaceChildren();
      renderSettings();
      if (typeof openSheet === 'function') openSheet(settingsDialog);
      else if (!settingsDialog.open) settingsDialog.showModal();
    } catch (e) { toast(e.message, 'error'); }
  }

  function ensureShell() {
    const body = $('#ai-settings-body');
    if (shellRefs && shellRefs.wrap.isConnected) return shellRefs;
    body.replaceChildren();
    const wrap = el('div', 'a1');
    wrap.append(closeButton(settingsDialog), el('div', 'sheet-eyebrow', 'AI 模型'));
    const head = el('div', 'a1-head');
    const headLeft = el('div', 'a1-head-text');
    const title = el('h2', 'sheet-title', '模型配置'); title.id = 'ai-settings-title';
    const sub = el('p', 'a1-sub', '');
    headLeft.append(title, sub);
    const addBtn = btn('添加配置', 'btn-primary btn-sm', () => openDrawer('__new__'), 'plus');
    addBtn.id = 'ai-add-profile';
    head.append(headLeft, addBtn);
    wrap.append(head);

    const library = el('section', 'a1-sec');
    library.setAttribute('aria-label', '已保存的 AI 配置');
    const grid = el('div', 'a1-grid');
    const addCard = el('button', 'pcard pcard-add');
    addCard.type = 'button';
    addCard.append(icon('plus'), el('strong', '', '新建配置'), el('span', '', '添加 GPT / Claude / Gemini / DeepSeek 或本地接口'));
    addCard.onclick = () => openDrawer('__new__');
    grid.append(addCard);
    library.append(grid);
    wrap.append(library);
    body.append(wrap);
    shellRefs = {body, wrap, sub, addBtn, grid, addCard, cards: new Map()};
    return shellRefs;
  }

  function buildCard(id, item, active) {
    const card = el('article', 'pcard');
    card.dataset.profile = id;
    card.onclick = e => {
      if (e.target.closest('button') || S.job.busy) return;
      openDrawer(id);
    };
    const top = el('div', 'pcard-top');
    const mark = el('span', 'mark');
    const statusBadge = el('span');
    top.append(mark, statusBadge);

    const bodyBox = el('div', 'pcard-meta');
    const h4 = el('h4');
    const modelLine = el('p', 'pcard-model');
    bodyBox.append(h4, modelLine);

    const tags = el('div', 'pcard-tags');
    const tagProto = el('span', 'ptag');
    const tagVision = el('span', 'ptag');
    const tagKey = el('span', 'ptag');
    tags.append(tagProto, tagVision, tagKey);

    const foot = el('div', 'pcard-foot');
    const useBtn = btn('使用', 'btn-sm', async e => {
      e.stopPropagation();
      useBtn.disabled = true;
      try {
        config = await api('/api/ai/settings', {id: card.dataset.profile, activate: true});
        const cur = config?.profiles?.[card.dataset.profile];
        updateLabel();
        toast(`已切换为「${cur?.name || '模型'}」`, 'ok');
        renderSettings();
      } catch (err) { toast(err.message, 'error'); useBtn.disabled = false; }
    });
    const editBtn = btn('编辑', 'btn-sm btn-ghost', e => { e.stopPropagation(); openDrawer(card.dataset.profile); });
    const removeBtn = btn('', 'btn-quiet btn-sm icon-only pcard-del', async e => {
      e.stopPropagation();
      const pid = card.dataset.profile;
      const curItem = config?.profiles?.[pid];
      const key = 'card-' + pid;
      if (removeBtn.dataset.confirm !== 'yes') {
        removeBtn.dataset.confirm = 'yes';
        removeBtn.setAttribute('aria-label', '确认删除 ' + (curItem?.name || ''));
        removeBtn.replaceChildren(el('span', '', '确认删除'));
        const timer = setTimeout(() => {
          if (!removeBtn.isConnected) return;
          removeBtn.dataset.confirm = '';
          const latest = config?.profiles?.[pid];
          removeBtn.setAttribute('aria-label', '删除 ' + (latest?.name || ''));
          removeBtn.replaceChildren(icon('trash'));
          deleteTimers.delete(key);
        }, 3000);
        deleteTimers.set(key, timer);
        return;
      }
      removeBtn.disabled = true; clearTimeout(deleteTimers.get(key)); deleteTimers.delete(key);
      try {
        config = await api('/api/ai/settings', {id: pid, remove: true});
        if (selected === pid) selected = null;
        lastTestById.delete(pid);
        updateLabel();
        toast('配置已删除', 'ok');
        renderSettings();
      } catch (err) { toast(err.message, 'error'); removeBtn.disabled = false; }
    }, 'trash');
    removeBtn.title = '删除配置';
    foot.append(useBtn, editBtn, removeBtn);
    card.append(top, bodyBox, tags, foot);
    card._refs = {top, mark, statusBadge, h4, modelLine, tagProto, tagVision, tagKey, foot, useBtn, editBtn, removeBtn};
    patchCard(card, id, item, active);
    return card;
  }

  function patchCard(card, id, item, active) {
    const r = card._refs;
    const brand = BRANDS[item.provider] || BRANDS.custom;
    card.className = 'pcard' + (active ? ' active' : '') + (selected === id ? ' editing' : '');
    r.mark.className = 'mark ' + brand.cls;
    r.mark.textContent = brand.glyph;
    r.statusBadge.className = active ? 'tag-use' : 'tag-idle';
    r.statusBadge.textContent = active ? '使用中' : brand.name;
    r.h4.textContent = item.name || brand.name;
    r.modelLine.textContent = item.model || '未填写模型';
    r.tagProto.textContent = config.protocols?.[item.protocol] || item.protocol;
    const supportsVision = item.provider !== 'custom' || item.vision;
    r.tagVision.className = 'ptag' + (supportsVision ? ' ok' : '');
    r.tagVision.textContent = supportsVision ? '支持识图' : '仅文字';
    r.tagKey.textContent = item.has_key ? '已设密钥' : '未设密钥';

    if (active) {
      if (r.useBtn.parentNode === r.foot) {
        if (document.activeElement === r.useBtn) r.editBtn.focus({preventScroll: true});
        r.useBtn.remove();
      }
    } else {
      if (r.useBtn.parentNode !== r.foot) r.foot.prepend(r.useBtn);
      r.useBtn.setAttribute('aria-label', '使用 ' + item.name);
      r.useBtn.disabled = S.job.busy || !item.ready;
    }
    r.editBtn.setAttribute('aria-label', '编辑 ' + item.name);
    r.editBtn.disabled = S.job.busy;
    if (r.removeBtn.dataset.confirm === 'yes') r.removeBtn.setAttribute('aria-label', '确认删除 ' + item.name);
    else r.removeBtn.setAttribute('aria-label', '删除 ' + item.name);
    r.removeBtn.disabled = S.job.busy;
  }

  function ensureDrawerDom(body) {
    if (drawerRefs && drawerRefs.sheet.isConnected) {
      if (drawerRefs.closing) {
        clearDrawerCloseTimer();
        drawerRefs.closing = false;
        drawerRefs.scrim.classList.remove('closing');
        drawerRefs.sheet.classList.remove('closing');
      }
      return drawerRefs;
    }
    clearDrawerCloseTimer();
    const scrim = el('div', 'scrim on');
    scrim.onclick = () => { if (!S.job.busy) closeDrawer(); };

    const sheet = el('section', 'side-sheet on');
    sheet.id = 'ai-profile-editor';
    sheet.setAttribute('role', 'region');

    const shHead = el('div', 'sh-head');
    const shTitle = el('div', 'sh-title');
    const shHeading = el('h3', '', '');
    const shBrand = el('small', '', '');
    shTitle.append(shHeading, shBrand);
    const shClose = btn('', 'btn-quiet btn-sm icon-only sh-close', closeDrawer, 'x');
    shClose.setAttribute('aria-label', '关闭编辑抽屉');
    shHead.append(shTitle, shClose);

    const form = el('form', 'sh-form');
    form.autocomplete = 'off';
    const shBody = el('div', 'sh-body');

    const providers = el('div', 'ai-providers prov-seg has-pill');
    providers.setAttribute('role', 'tablist');
    providers.setAttribute('aria-label', 'AI 平台');
    providers.append(el('span', 'prov-pill'));
    const tabButtons = new Map();
    for (const [providerId, item] of Object.entries(config.presets)) {
      const tab = btn(item.name, 'ai-provider', () => {
        if (drawerRefs?.saved || S.job.busy || newProvider === providerId) return;
        newProvider = providerId;
        advOpen = providerId === 'custom';
        patchDrawer(selected, null, true);
      });
      tab.dataset.provider = providerId;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', 'false');
      providers.append(tab);
      tabButtons.set(providerId, tab);
    }
    shBody.append(providers);

    const field = (label, input, idName, extra = '') => {
      input.id = idName;
      const row = el('label', 'form-field ' + extra);
      row.append(el('span', '', label), input);
      return row;
    };

    const name = el('input', 'input');
    name.maxLength = 80; name.placeholder = '例如：日常作答';
    const model = el('input', 'input');
    model.required = true; model.placeholder = '服务商提供的模型名称';
    const row2 = el('div', 'sh-row2');
    row2.append(field('配置名称', name, 'ai-profile-name'), field('模型名称', model, 'ai-model'));
    shBody.append(row2);

    const secret = el('input', 'input');
    secret.id = 'ai-api-key';
    secret.type = 'password';
    secret.autocomplete = 'new-password';
    secret.spellcheck = false;
    const eyeBtn = btn('', 'inp-eye', () => {
      showKey = !showKey;
      secret.type = showKey ? 'text' : 'password';
      eyeBtn.setAttribute('aria-label', showKey ? '隐藏密钥' : '显示密钥');
      eyeBtn.setAttribute('aria-pressed', String(showKey));
    }, 'eye');
    const inpWrap = el('div', 'inp-wrap');
    inpWrap.append(secret, eyeBtn);
    const keyField = el('div', 'form-field');
    const keyLbl = el('label', '', 'API 密钥'); keyLbl.setAttribute('for', 'ai-api-key');
    keyField.append(keyLbl, inpWrap);
    shBody.append(keyField);

    const vision = el('input');
    vision.id = 'ai-vision';
    vision.type = 'checkbox';
    const visionRow = el('label', 'ai-checkbox');
    visionRow.append(vision, el('span', '', '模型支持识图（可读取题目截图）'));

    const protocol = el('select', 'input');
    for (const [protocolId, label] of Object.entries(config.protocols)) {
      const option = el('option', '', label);
      option.value = protocolId;
      protocol.append(option);
    }
    const address = el('input', 'input');
    address.type = 'url'; address.required = true;
    address.placeholder = 'https://example.com/v1';

    const advWrap = el('div', 'adv');
    const advHead = el('button', 'adv-head');
    advHead.type = 'button';
    const advSum = el('span', 'adv-sum', '');
    advHead.append(el('strong', '', '高级选项'), advSum, icon('chevron', 'adv-chev'));
    const advBody = el('div', 'collapse');
    const advInner = el('div', 'adv-inner');
    advInner.append(field('接口类型', protocol, 'ai-protocol'), field('接口地址', address, 'ai-base-url'));
    advBody.append(advInner);
    advHead.onclick = () => {
      advOpen = !advOpen;
      advWrap.classList.toggle('open', advOpen);
      advHead.setAttribute('aria-expanded', String(advOpen));
    };
    advWrap.append(advHead, advBody);
    shBody.append(advWrap);

    const conn = el('div', 'conn');
    const connIc = el('span', 'conn-ic');
    const connInfo = el('div', 'conn-info');
    const connMsg = el('strong', 'conn-msg', '');
    connMsg.id = 'ai-config-message';
    connMsg.setAttribute('role', 'status');
    const connSub = el('p', 'conn-sub', '');
    connInfo.append(connMsg, connSub);
    const meters = el('div', 'meters');
    const latMeter = el('div', 'meter'), latValEl = el('b', '', '--');
    latMeter.append(el('small', '', '延迟'), latValEl);
    const visMeter = el('div', 'meter'), visValEl = el('b', '', '支持');
    visMeter.append(el('small', '', '识图'), visValEl);
    meters.append(latMeter, visMeter);
    vision.onchange = () => { visValEl.textContent = vision.checked ? '支持' : '仅文字'; };
    conn.append(connIc, connInfo, meters);
    shBody.append(conn);

    const errBox = el('p', 'settings-error');
    errBox.setAttribute('role', 'alert');
    shBody.append(errBox);

    const shFoot = el('div', 'sh-foot');
    const delWrap = el('div', 'sh-foot-l');
    const shDel = btn('删除', 'btn-sm btn-quiet del', async () => {
      const targetId = drawerRefs?.sessionKey;
      if (!targetId || targetId === '__new__') return;
      const k = 'drawer-' + targetId;
      if (shDel.dataset.confirm !== 'yes') {
        shDel.dataset.confirm = 'yes';
        shDel.textContent = '确认删除';
        const timer = setTimeout(() => {
          if (!shDel.isConnected) return;
          shDel.dataset.confirm = '';
          shDel.textContent = '删除';
          deleteTimers.delete(k);
        }, 3000);
        deleteTimers.set(k, timer);
        return;
      }
      shDel.disabled = true; clearTimeout(deleteTimers.get(k)); deleteTimers.delete(k);
      try {
        config = await api('/api/ai/settings', {id: targetId, remove: true});
        lastTestById.delete(targetId);
        selected = null;
        updateLabel();
        toast('配置已删除', 'ok');
        renderSettings();
      } catch (e) { errBox.textContent = e.message; shDel.disabled = false; }
    });
    const shActs = el('div', 'sh-foot-r');
    const testBtn = btn('测试连接', 'btn', async () => {
      if (!validateWithShake() || !(await persist(false))) return;
      testStartAt = Date.now();
      lastTestById.set(selected, {state: 'testing', text: '正在测试连接…', sub: '已保存配置，正在发送探测请求', latencyMs: null});
      renderSettings();
      await startAction('ai-test');
    });
    const saveBtn = btn('保存并使用', 'btn-primary');
    saveBtn.type = 'submit';
    shActs.append(testBtn, saveBtn);
    shFoot.append(delWrap, shActs);

    form.append(shBody, shFoot);
    sheet.append(shHead, form);

    const validateWithShake = () => {
      if (form.checkValidity()) return true;
      if (!advOpen && ((address.validity && !address.validity.valid) || (protocol.validity && !protocol.validity.valid))) {
        advOpen = true;
        advWrap.classList.add('open');
        advHead.setAttribute('aria-expanded', 'true');
      }
      form.querySelectorAll('.form-field').forEach(f => {
        const inp = f.querySelector('input, select');
        if (inp && !inp.validity.valid) { f.classList.remove('shake'); void f.offsetWidth; f.classList.add('shake'); }
      });
      return form.reportValidity();
    };

    async function persist(closeOnSave = true) {
      saveBtn.disabled = testBtn.disabled = true;
      errBox.textContent = '';
      const activeSaved = drawerRefs?.saved || null;
      const activeId = drawerRefs?.sessionKey;
      const activeProvider = activeSaved?.provider || newProvider;
      const payload = {
        provider: activeProvider,
        name: name.value.trim() || model.value.trim(),
        protocol: protocol.value,
        base_url: address.value.trim(),
        model: model.value.trim(),
        api_key: secret.value.trim(),
        vision: vision.checked,
        ...(activeSaved && activeId && activeId !== '__new__' ? {id: activeId} : {create: true}),
      };
      secret.value = '';
      try {
        config = await api('/api/ai/settings', payload);
        if (closeOnSave) {
          selected = null;
        } else {
          selected = config.active;
          if (drawerRefs) drawerRefs.sessionKey = selected;
        }
        updateLabel();
        if (closeOnSave) toast(`已保存并启用「${config.profiles[config.active]?.name || '模型'}」`, 'ok');
        renderSettings();
        return true;
      } catch (e) {
        errBox.textContent = e.message;
        return false;
      } finally {
        payload.api_key = '';
        saveBtn.disabled = testBtn.disabled = false;
      }
    }

    form.onsubmit = async e => { e.preventDefault(); if (validateWithShake()) await persist(true); };

    body.append(scrim, sheet);
    prevProvidersWidth = providers.clientWidth;
    if (window.ResizeObserver) {
      if (provObserver) provObserver.disconnect();
      provObserver = new ResizeObserver(() => {
        const w = providers.clientWidth;
        if (!w || w === prevProvidersWidth) return;
        prevProvidersWidth = w;
        syncProvPill(providers, false);
      });
      provObserver.observe(providers);
    }

    drawerRefs = {
      scrim, sheet, shHeading, shBrand, shClose, providers, tabButtons, shBody,
      name, model, keyField, secret, eyeBtn, visionRow, vision,
      protocol, address, advWrap, advHead, advSum,
      conn, connIc, connIconName: '', connMsg, connSub, latValEl, visValEl,
      errBox, delWrap, shDel, testBtn, saveBtn,
      sessionKey: null, provider: null, saved: null, closing: false,
    };
    return drawerRefs;
  }

  function patchDrawer(id, saved, fromTabSwitch = false) {
    const d = drawerRefs;
    if (!d) return;
    const prevKey = d.sessionKey;
    const prevProv = d.provider;
    const sameSession = prevKey === id;
    const provider = saved?.provider || newProvider;
    const current = {...(saved || {...config.presets[provider], model: '', has_key: false, name: ''})};

    d.saved = saved;
    d.sheet.setAttribute('aria-label', saved ? `编辑 ${saved.name}` : '新建 AI 配置');
    d.shHeading.textContent = saved ? saved.name : '新建模型配置';
    d.shBrand.textContent = BRANDS[provider]?.name || '自定义接口';

    for (const [providerId, tab] of d.tabButtons.entries()) {
      tab.setAttribute('aria-selected', String(providerId === provider));
      tab.disabled = !!saved || S.job.busy;
    }

    if (!sameSession) {
      d.name.value = current.name || '';
      d.model.value = current.model || '';
      d.secret.value = '';
      d.vision.checked = provider !== 'custom' || !!current.vision;
      d.protocol.value = current.protocol;
      d.address.value = current.base_url || '';
      d.errBox.textContent = '';
      d.shDel.dataset.confirm = '';
      d.shDel.textContent = '删除';
    } else if (fromTabSwitch && prevProv !== provider) {
      d.vision.checked = provider !== 'custom' || !!current.vision;
      d.protocol.value = current.protocol;
      d.address.value = current.base_url || '';
    }

    d.secret.type = showKey ? 'text' : 'password';
    d.secret.placeholder = current.has_key ? '已保存，留空不修改' : provider === 'custom' ? '本机服务可留空' : '填写 API 密钥';
    d.secret.required = !current.has_key && provider !== 'custom';
    d.eyeBtn.setAttribute('aria-label', showKey ? '隐藏密钥' : '显示密钥');
    d.eyeBtn.setAttribute('aria-pressed', String(showKey));

    if (provider === 'custom') {
      if (d.visionRow.parentNode !== d.shBody) d.keyField.after(d.visionRow);
    } else if (d.visionRow.parentNode === d.shBody) {
      d.visionRow.remove();
    }

    d.protocol.disabled = provider !== 'custom';
    d.address.readOnly = provider !== 'custom';
    d.advWrap.classList.toggle('open', advOpen);
    d.advHead.setAttribute('aria-expanded', String(advOpen));
    const protoVal = d.protocol.value || current.protocol;
    const hostPreview = (d.address.value || current.base_url || '').replace(/^https?:\/\//, '').split('/')[0] || '自定义地址';
    d.advSum.textContent = `${config.protocols?.[protoVal] || protoVal} · ${hostPreview}`;

    const tst = getTestState(id);
    const isTesting = (S.job.busy && S.job.action === 'ai-test') || tst.state === 'testing';
    d.conn.className = 'conn' + (isTesting ? ' testing' : tst.state === 'ok' ? ' ok' : tst.state === 'error' ? ' err' : '');
    const iconName = isTesting ? 'loader' : tst.state === 'ok' ? 'check' : tst.state === 'error' ? 'alert' : 'sparkle';
    if (d.connIconName !== iconName) {
      d.connIconName = iconName;
      d.connIc.replaceChildren(icon(iconName));
    }
    d.connMsg.textContent = isTesting ? '正在测试连接…' : tst.text;
    d.connSub.textContent = isTesting ? '已保存配置，正在向模型发送探测请求…' : tst.sub;
    d.latValEl.textContent = isTesting ? '…' : tst.latencyMs != null ? `${tst.latencyMs}ms` : '--';
    d.visValEl.textContent = d.vision.checked ? '支持' : '仅文字';

    if (saved) {
      if (d.shDel.parentNode !== d.delWrap) d.delWrap.append(d.shDel);
      d.shDel.disabled = S.job.busy;
    } else if (d.shDel.parentNode === d.delWrap) {
      d.shDel.remove();
    }

    d.sheet.querySelectorAll('button, input, select').forEach(control => {
      if (control.classList.contains('ai-provider')) {
        control.disabled = !!saved || S.job.busy;
      } else if (control === d.protocol) {
        control.disabled = provider !== 'custom' || S.job.busy;
      } else {
        control.disabled = S.job.busy;
      }
    });

    d.sessionKey = id;
    d.provider = provider;
    syncProvPill(d.providers, (fromTabSwitch || sameSession) && prevProv !== provider);
  }

  function renderSettings() {
    if (!config) return;
    const s = ensureShell();
    const profiles = Object.entries(config?.profiles || {});
    s.sub.textContent = `集中管理多个模型与接口（${profiles.length} 项）· 密钥仅存本机。`;
    s.addBtn.disabled = S.job.busy;
    s.addCard.disabled = S.job.busy;

    const prevScroll = s.wrap.scrollTop;
    const seenIds = new Set();
    let idx = 0;
    for (const [id, item] of profiles) {
      seenIds.add(id);
      const active = id === config.active;
      let card = s.cards.get(id);
      if (card) patchCard(card, id, item, active);
      else { card = buildCard(id, item, active); s.cards.set(id, card); }
      const cursor = s.grid.children[idx];
      if (card !== cursor) s.grid.insertBefore(card, cursor || s.addCard);
      idx++;
    }
    for (const [id, card] of s.cards.entries()) {
      if (!seenIds.has(id)) { card.remove(); s.cards.delete(id); }
    }
    if (s.wrap.scrollTop !== prevScroll) s.wrap.scrollTop = prevScroll;

    if (selected !== null) {
      const saved = selected === '__new__' ? null : config?.profiles?.[selected] || null;
      if (selected !== '__new__' && !saved) {
        selected = null;
      } else {
        ensureDrawerDom(s.body);
        patchDrawer(selected, saved, false);
        return;
      }
    }
    if (drawerRefs && !drawerRefs.closing) {
      if (!settingsDialog.open || (typeof REDUCED_MOTION === 'function' && REDUCED_MOTION())) {
        destroyDrawerDom();
      } else {
        drawerRefs.closing = true;
        drawerRefs.sessionKey = null;
        drawerRefs.saved = null;
        if (provObserver) { provObserver.disconnect(); provObserver = null; }
        drawerRefs.scrim.classList.add('closing');
        drawerRefs.sheet.classList.add('closing');
        drawerCloseTimer = setTimeout(destroyDrawerDom, 155);
      }
    }
  }
  settingsDialog.addEventListener('cancel', e => {
    if (selected !== null) { e.preventDefault(); closeDrawer(); }
  });
  settingsDialog.addEventListener('close', () => {
    selected = null; lastProvRect = null; showKey = false;
    if (provObserver) { provObserver.disconnect(); provObserver = null; }
    const key = $('#ai-api-key'); if (key) key.value = '';
  });

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
    const detail = $('#detail');
    detail.classList.toggle('ai-locked', locked);
    const col = detail.querySelector('.column');
    const existingBox = col?.querySelector('.ai-solving-box');
    if (locked && col) {
      if (!existingBox) {
        const box = el('div', 'ai-solving-box'), head = el('div', 'ai-solving-head');
        head.append(icon('sparkle'), el('span', '', S.job.message || 'AI 正在思考并填写答案…'));
        box.append(head, Object.assign(el('div', 'ai-sk-bar'), {style: 'width:92%'}), Object.assign(el('div', 'ai-sk-bar'), {style: 'width:70%'}));
        const qnav = col.querySelector('.qnav');
        if (qnav) qnav.after(box);
        else col.querySelector('.hero')?.after(box);
      }
    } else if (existingBox) {
      existingBox.remove();
    }
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
    const textNode = el('div', 'ai-template-text', template.text);
    const hint = S.aiHintKeys?.get(key);
    if (hint && Date.now() - hint.at < 2400 && hint.templates?.has(String(q.id))) {
      hint.templates.delete(String(q.id));
      const fullText = String(template.text || '');
      if (fullText.length > 1) {
        let pos = 0;
        const step = Math.max(2, Math.ceil(fullText.length / 22));
        textNode.replaceChildren(document.createTextNode(''), el('span', 'ai-caret'));
        const timer = setInterval(() => {
          pos = Math.min(fullText.length, pos + step);
          if (!textNode.isConnected || pos >= fullText.length) {
            clearInterval(timer);
            textNode.textContent = fullText;
          } else {
            textNode.replaceChildren(document.createTextNode(fullText.slice(0, pos)), el('span', 'ai-caret'));
          }
        }, 24);
      }
    }
    box.append(summary, textNode);
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
      const targetId = selected && selected !== '__new__' ? selected : config?.active;
      const failed = !!job.result?.error;
      const latency = job.result?.latency_ms ?? (testStartAt ? Math.max(1, Date.now() - testStartAt) : null);
      if (targetId) {
        lastTestById.set(targetId, {
          state: failed ? 'error' : 'ok',
          text: job.message || (failed ? '连接失败' : '连接成功'),
          sub: failed ? '请检查 API 密钥、模型名称或接口地址' : '模型返回格式正确 · 已就绪可用于作答',
          latencyMs: latency,
        });
      }
      if (settingsDialog.open) renderSettings();
      else toast(job.message, failed ? 'error' : 'ok');
      return;
    }
    const key = canonicalKey(job.result?.key || job.key);
    settling.add(key); lockInputs(record());
    try {
      await flush(key);
      const latest = await api('/api/draft?key=' + encodeURIComponent(key));
      latest.answers ??= {};
      drafts.set(key, latest);
      saveState.set(key, {state: 'saved', at: latest.updated_at});
      const filledIds = job.result?.filled || [], templateIds = job.result?.templates || [];
      if (filledIds.length || templateIds.length) {
        S.aiHintKeys.set(key, {filled: new Set(filledIds.map(String)), templates: new Set(templateIds.map(String)), at: Date.now()});
      }
      if (S.key === key) renderDetail(record(), latest, true);
      await load();
    } catch (e) { toast('答案刷新失败：' + e.message, 'error'); return; }
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
      stats.append(statCard(n, label, n ? tone : ''));
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
    body.append(list, noSubmitNote('AI 只写入助手，不会提交作业。请检查后再前往交作业。'), done);
    if (!dialog.open) dialog.showModal();
  }

  settingsDialog.addEventListener('close', () => {
    selected = null;
    showKey = false;
    destroyDrawerDom();
  });
  $('#ai-settings-button').onclick = () => { closeAccountMenu(); openSettings(); };
  api('/api/ai/settings').then(value => { config = value; updateLabel(); if (record()) renderDock(record()); }).catch(() => {});
  function jobChanged(previous, next) {
    if (previous.busy && previous.action === 'ai' && !next.busy && next.action === 'ai') settling.add(canonicalKey(next.key));
    if (previous.busy !== next.busy && settingsDialog.open && config) renderSettings();
  }
  return {openSettings, taskButton, templateCard, lockInputs, jobDone, jobChanged};
})();
