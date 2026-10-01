"""Fill saved answers into verified questions, leaving final submission to the user.

This module never calls a submit/save HTTP endpoint, clicks a submit button, or
uses arbitrary page scripts to invent an editor protocol. Unsupported layouts
remain open for the user to handle. Playwright's CDP connection is disconnected,
not closed, so the review window remains available.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime
from pathlib import Path

import homework_dom as dom


def _normalized(value):
    return re.sub(r'\s+', ' ', str(value or '').replace('\xa0', ' ')).strip()


def _plain(markup):
    return _normalized(dom.clean_text(dom.parse(markup or '')))


def _root_for(page, qid):
    if not re.fullmatch(r'\d+', str(qid)):
        return None
    candidates = page.locator(
        f'.singleQuesId[data="{qid}"], .singleQuesId[data-questionid="{qid}"], '
        f'.questionLi[data="{qid}"], .questionLi[data-questionid="{qid}"], '
        f'.singleQuesId:has(input[name="type{qid}"]), '
        f'.questionLi:has(input[name="type{qid}"]), .TiMu:has(input[name="type{qid}"])'
    )
    indices = candidates.evaluate_all("els => els.map((e,i)=>({i,leaf:!els.some(n=>n!==e&&e.contains(n))})).filter(x=>x.leaf).map(x=>x.i)")
    return candidates.nth(indices[0]) if len(indices) == 1 else None


def _visible_text_controls(root, name=None):
    controls = root.locator('textarea, input:not([type]), input[type="text"], input[type="number"]')
    result = []
    for i in range(controls.count()):
        control = controls.nth(i)
        if name and name not in {control.get_attribute('name'), control.get_attribute('id')}:
            continue
        if control.is_visible() and control.is_editable():
            result.append(control)
    return result


def _editor_info(root, name=None):
    # Reading only. UE instances are matched by DOM ownership, not array order.
    return root.evaluate("""(root,name) => {
        const candidates = [...Object.values(window.UE?.instants || {}), ...Object.values(window.UE?.instances || {})];
        if (window.ueditor) candidates.push(window.ueditor);
        const editors = [...new Set(candidates)].filter(e => e && e.body &&
            ((e.container && root.contains(e.container)) || (e.iframe && root.contains(e.iframe))) &&
            (!name || [e.key,e.textarea?.id,e.textarea?.name].includes(name)));
        return editors.map(e => ({id:e.key || e.uid || '', names:[e.key,e.textarea?.id,e.textarea?.name].filter(Boolean), html:e.getContent(), text:e.getContentTxt(),
            ready:!!(e.body.isContentEditable && typeof e.setContent==='function')}));
    }""", name)


def _rich_media(markup):
    root = dom.parse(markup or '')
    return any(root.all(tag) for tag in ('img', 'iframe', 'audio', 'video', 'object', 'a'))


def _fill_essay(root, value, editor_name=None):
    editors = _editor_info(root, editor_name)
    if len(editors) == 1 and editors[0]['ready']:
        current = editors[0]
        if _rich_media(current['html']):
            return '网页已有图片或附件答案，请自行核对，未覆盖'
        if _normalized(current['text']) and _normalized(current['text']) != _normalized(value):
            return '网页已有不同答案，未覆盖'
        if _normalized(current['text']) == _normalized(value):
            return None
        markup = '<p>' + html.escape(value).replace('\n', '</p><p>') + '</p>'
        result = root.evaluate("""(root, args) => {
            const all=[...Object.values(window.UE?.instants||{}),...Object.values(window.UE?.instances||{})];
            if(window.ueditor) all.push(window.ueditor);
            const found=[...new Set(all)].filter(e=>e&&e.body&&((e.container&&root.contains(e.container))||(e.iframe&&root.contains(e.iframe)))&&(!args.name||[e.key,e.textarea?.id,e.textarea?.name].includes(args.name)));
            if(found.length!==1 || !found[0].body.isContentEditable) return false;
            const e=found[0];
            // Recheck in the same operation so a just-entered online answer is never overwritten.
            if(e.getContentTxt().trim()) return false;
            e.setContent(args.markup);
            e.fireEvent('contentchange');
            e.fireEvent('blur');
            if(typeof e.sync==='function') e.sync();
            return true;
        }""", {'markup': markup, 'name': editor_name})
        if not result:
            return '网页答案刚发生变化，未覆盖'
        verified = _editor_info(root, editor_name)
        if len(verified) != 1 or _normalized(verified[0]['text']) != _normalized(value):
            return '编辑器回读未通过，请在网页检查'
        # Mobile editors mirror into a hidden form field through contentchange.
        # Inspect the mirror if present; never assign hidden fields ourselves.
        mirrors = root.locator('input#answerEditor')
        if mirrors.count() == 1 and _plain(mirrors.input_value()) != _normalized(value):
            return '可视编辑器已填入，但平台表单未同步，请在网页检查'
        return None
    if editor_name:
        return '未识别到对应空位的可验证编辑器'

    # Some Chaoxing skins expose the answer editor as a contenteditable body
    # inside an iframe without registering a UEditor instance.  Treat one
    # visible editable frame as an essay editor, but never guess when there are
    # multiple frames or when the frame already contains rich media.
    frames = root.locator('iframe')
    frame_editors = []
    for i in range(frames.count()):
        try:
            body = frames.nth(i).content_frame.locator('body[contenteditable="true"]')
            if body.count() == 1 and body.is_visible() and body.is_editable():
                frame_editors.append(body)
        except Exception:
            continue
    if len(frame_editors) == 1:
        editor = frame_editors[0]
        if _rich_media(editor.inner_html()):
            return '网页已有图片或附件答案，请自行核对，未覆盖'
        current = _normalized(editor.inner_text())
        expected = _normalized(value)
        if current and current != expected:
            return '网页已有不同答案，未覆盖'
        if current == expected:
            return None
        editor.fill(value)
        editor.dispatch_event('input')
        editor.dispatch_event('change')
        editor.dispatch_event('blur')
        return None if _normalized(editor.inner_text()) == expected else '答案回读未通过'

    controls = _visible_text_controls(root)
    if len(controls) == 1:
        control = controls[0]
        current = _normalized(control.input_value())
        if current and current != _normalized(value):
            return '网页已有不同答案，未覆盖'
        if not current:
            control.fill(value)
            control.dispatch_event('change')
        return None if _normalized(control.input_value()) == _normalized(value) else '答案回读未通过'
    # Standard contenteditable with explicit answer mapping is supported too.
    editable = root.locator('[contenteditable="true"]')
    visible_editable = [editable.nth(i) for i in range(editable.count()) if editable.nth(i).is_visible() and editable.nth(i).is_editable()]
    if len(visible_editable) == 1:
        editable = visible_editable[0]
        if _rich_media(editable.inner_html()):
            return '网页已有图片或附件答案，请自行核对，未覆盖'
        current = _normalized(editable.inner_text())
        if current and current != _normalized(value):
            return '网页已有不同答案，未覆盖'
        if not current:
            editable.fill(value)
            editable.dispatch_event('change')
        return None if _normalized(editable.inner_text()) == _normalized(value) else '答案回读未通过'
    return '未识别到可验证的文本编辑器，请在原网页填写'


def _fill_attachments(root, files, local_files=None):
    """Select saved local files in the question's native upload control.

    The platform owns the upload request and validation.  We only use the
    question's native file input or its UEditor attachment dialog, so no
    upload endpoint or page script is invented by the assistant.
    """
    if not files:
        return None
    local_files = local_files or []
    paths = [str(item.get('path', '')) for item in local_files if isinstance(item, dict) and item.get('path')]
    if len(paths) != len(files):
        return '本机附件不可用，请重新添加附件'
    inputs = root.locator('input[type="file"]')
    if inputs.count():
        try:
            if inputs.count() == 1:
                inputs.first.set_input_files(paths)
            elif inputs.count() == len(paths):
                for i, path in enumerate(paths):
                    inputs.nth(i).set_input_files(path)
            else:
                return '附件上传控件数量不匹配，请在原网页上传'
        except Exception:
            return '附件上传控件操作失败，请在原网页上传'
        return None

    # Chaoxing's essay/upload questions use UEditor.  Its attachment picker is
    # created only after clicking the toolbar button and lives in a dialog
    # iframe outside the question's file-input subtree.
    toolbar = root.locator('.edui-for-attachment_new')
    visible_toolbar = [toolbar.nth(i) for i in range(toolbar.count()) if toolbar.nth(i).is_visible()]
    if len(visible_toolbar) != 1:
        return '未找到附件上传控件，请在原网页上传'
    try:
        page = root.page
        visible_toolbar[0].click()
        dialog = page.locator('.edui-dialog.edui-for-attachment_new:visible').last
        iframe = dialog.locator('iframe')
        if iframe.count() != 1:
            return '未打开附件上传窗口，请在原网页上传'
        frame = iframe.content_frame

        # Some Chaoxing skins use the newer WebUploader dialog.  It starts
        # uploading as soon as files are selected, writes the returned file
        # into the editor from its uploadSuccess callback, and has no separate
        # upload/confirm button.  Detect this picker before falling back to
        # the legacy queue dialog so the file is selected only once.
        modern_picker = frame.locator('#pickfiles input[type=file]')
        try:
            modern_picker.first.wait_for(state='attached', timeout=5000)
        except Exception:
            pass
        if modern_picker.count() != 1:
            modern_picker = frame.locator('#pickfiles2 input[type=file]')
        if modern_picker.count() == 1:
            modern_picker.set_input_files(paths)
            expected = len(paths)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    uploaded = frame.locator('body').evaluate("""(body, expected) => {
                        const value = typeof fileArr !== 'undefined' ? fileArr : [];
                        return Array.isArray(value) ? value.length : 0;
                    }""", expected)
                except Exception:
                    # addFileToEditor() closes this iframe after inserting the
                    # attachment, so a detached frame is itself a possible
                    # success signal.  Verify the editor before accepting it.
                    current = _editor_info(root, None)
                    if len(current) == 1 and _rich_media(current[0]['html']):
                        return None
                    uploaded = 0
                if int(uploaded or 0) >= expected:
                    break
                page.wait_for_timeout(200)
            else:
                return '附件上传超时，请在原网页检查'

            # addFileToEditor() in this skin closes its own dialog after
            # inserting the attachment.  Older variants leave it open, so
            # close it only when it is still visible.
            try:
                dialog_visible = dialog.is_visible()
            except Exception:
                dialog_visible = False
            if dialog_visible:
                close_button = dialog.locator('.edui-dialog-closebutton')
                if close_button.count() != 1:
                    return '未找到附件窗口关闭按钮，请在原网页检查'
                close_button.click()
                page.wait_for_timeout(300)
            current = _editor_info(root, None)
            if len(current) != 1 or not _rich_media(current[0]['html']):
                return '附件已上传但编辑器未回读，请在原网页检查'
            return None

        picker = frame.locator('#filePickerBtn input[type="file"]')
        try:
            picker.wait_for(state='attached', timeout=5000)
        except Exception:
            pass
        if picker.count() != 1:
            picker = frame.locator('input[type="file"]').first
        if picker.count() != 1:
            return '未找到附件选择控件，请在原网页上传'
        picker.set_input_files(paths)

        upload = frame.locator('#queueList .btns .uploadBtn')
        if upload.count() != 1:
            return '未找到附件上传按钮，请在原网页上传'
        deadline = time.monotonic() + 2
        while 'disabled' in (upload.get_attribute('class') or '').split() and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        if 'disabled' in (upload.get_attribute('class') or '').split():
            return '附件格式不受支持或未进入上传队列'
        upload.click()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = upload.get_attribute('class') or ''
            if 'state-finish' in state.split():
                break
            page.wait_for_timeout(200)
        else:
            return '附件上传超时，请在原网页检查'
        ok = dialog.locator('.edui-okbutton .edui-button-body')
        if ok.count() != 1:
            return '未找到附件确认按钮，请在原网页检查'
        ok.click()
        page.wait_for_timeout(300)
        return None
    except Exception:
        return '附件上传控件操作失败，请在原网页上传'


def _fill_blanks(root, question, value):
    names = question.get('blank_names') or []
    if not isinstance(value, list) or not names or len(names) != len(value):
        return '填空数量或空位标识不一致，未填入'
    controls = []
    for name, answer in zip(names, value):
        found = _visible_text_controls(root, name)
        if not isinstance(answer, str):
            return '部分空位没有唯一可编辑控件，未填入此题'
        if len(found) == 1:
            current = _normalized(found[0].input_value())
            control = found[0]
        else:
            editors = _editor_info(root, name)
            if len(editors) != 1 or not editors[0]['ready'] or _rich_media(editors[0]['html']):
                return '部分空位没有唯一可编辑控件，未填入此题'
            current = _normalized(editors[0]['text'])
            control = name
        if current and current != _normalized(answer):
            return '网页某一空已有不同答案，此题未覆盖'
        controls.append((control, answer, current))
    for control, answer, current in controls:
        if not current and answer:
            if isinstance(control, str):
                reason = _fill_essay(root, answer, control)
                if reason:
                    return reason
            else:
                control.fill(answer)
                control.dispatch_event('change')
    for control, answer, _ in controls:
        actual = _editor_info(root, control)[0]['text'] if isinstance(control, str) else control.input_value()
        if _normalized(actual) != _normalized(answer):
            return '部分空位回读未通过'
    return None


def _fill_choice(root, question, value):
    multiple = question['type'] == 'multiple'
    selected = value if isinstance(value, list) else [value]
    if not multiple and len(selected) != 1:
        return '单选或判断题需要唯一答案'
    expected = set(str(v) for v in selected)
    allowed = {str(o['value']) for o in question.get('options', [])}
    if not expected or not expected <= allowed:
        return '草稿选项不属于当前题目'
    control_type = 'checkbox' if multiple else 'radio'
    controls = root.locator(f'input[type="{control_type}"]')
    if controls.count() == 0:
        return _fill_platform_choice(root, question, expected, allowed)
    pairs = []
    for i in range(controls.count()):
        control = controls.nth(i)
        label = control.get_attribute('value') or ''
        if label in allowed:
            pairs.append((label, control))
    if len(pairs) != len(allowed) or len({v for v, _ in pairs}) != len(pairs):
        return '选项控件未能唯一对应，请在原网页选择'
    current = {v for v, control in pairs if control.is_checked()}
    if current and current != expected:
        return '网页已选择不同答案，未覆盖'
    if current == expected:
        return None
    # Prepare every target before changing any control.
    targets = []
    for value, control in pairs:
        if value not in expected:
            continue
        if control.is_disabled():
            return '选项不可编辑，请确认作业状态'
        if control.is_visible():
            targets.append((control, True))
            continue
        identifier = control.get_attribute('id') or ''
        label = root.locator('label').filter(has=control)
        if not (label.count() == 1 and label.is_visible()) and identifier:
            labels = root.locator('label')
            matches = [labels.nth(i) for i in range(labels.count()) if labels.nth(i).get_attribute('for') == identifier]
            label = matches[0] if len(matches) == 1 else None
        if label is None or label.count() != 1 or not label.is_visible():
            return '选项只暴露隐藏字段，未模拟填入'
        targets.append((label, False))
    for target, native in targets:
        target.check() if native else target.click()
    actual = {v for v, control in pairs if control.is_checked()}
    return None if actual == expected else '选项回读未通过，请在网页检查'


def _fill_platform_choice(root, question, expected, allowed):
    """PC's real addChoice/addMultipleChoice widgets, not hidden input writes.

    The fixture's own handler sets the visible selection class AND the form
    field. Both must agree after a normal click; unknown handlers are skipped.
    """
    qid = question['id']
    multiple = question['type'] == 'multiple'
    marker_class = 'num_option_dx' if multiple else 'num_option'
    checked_class = 'check_answer_dx' if multiple else 'check_answer'
    handler = 'addMultipleChoice' if multiple else 'addChoice'
    candidates = root.locator(f'.answerBg[qid="{qid}"]')
    pairs = []
    for i in range(candidates.count()):
        option = candidates.nth(i)
        onclick = option.get_attribute('onclick') or ''
        if not re.fullmatch(r'\s*' + handler + r'\s*\(\s*this\s*\)\s*;?\s*', onclick):
            return '选项行为尚未验证，请在原网页选择'
        marker = option.locator('.' + marker_class)
        if marker.count() != 1:
            return '选项标识无法唯一对应'
        value = marker.get_attribute('data') or ''
        if value not in allowed or not option.is_visible():
            return '选项值或可见状态无法核实'
        pairs.append((value, option, marker))
    if {v for v, _, _ in pairs} != allowed or len(pairs) != len(allowed):
        return '未识别到完整的可点击选项'
    mirrors = root.locator(f'input[name="answer{qid}"], input[id="answer{qid}"]')
    if mirrors.count() != 1:
        return '未识别到唯一的选项表单字段'
    def visible_selected():
        return {v for v, _, m in pairs if checked_class in (m.get_attribute('class') or '').split()}
    def form_selected():
        value = mirrors.input_value().strip()
        return set(value) if multiple else ({value} if value else set())
    current = visible_selected()
    if current != form_selected():
        return '网页选项与表单不同步，请先在原网页核对'
    if current and current != expected:
        return '网页已选择不同答案，未覆盖'
    if current == expected:
        return None
    for value, option, _ in pairs:
        if value in expected:
            option.click()
    return None if visible_selected() == expected and form_selected() == expected else '选项填入后未同步，请在网页检查'


def _read_only_url(url):
    path = url.split('?', 1)[0].lower().rstrip('/')
    return bool(re.search(r'/work/(?:view(?:work|answer|report)?|workreport)(?:/|$)', path) or any(part in path for part in ('viewwork', 'workreport', 'viewanswer')))


def fill_page(page, questions, answers):
    """Fill only matching saved answers in this already-verified document.

    Kept independent of disk/network for fixture-based browser regression tests.
    The caller must verify assignment identity, status and deadline first.
    """
    filled, skipped = [], []
    ids = [str(q.get('id', '')) for q in questions]
    if len(ids) != len(set(ids)):
        return {'filled': [], 'skipped': [{'id': '', 'reason': '题目编号重复，已停止填入'}]}
    by_id = {str(q['id']): q for q in questions}
    for qid, saved in answers.items():
        qid = str(qid)
        reason = None
        question = by_id.get(qid)
        if not isinstance(saved, dict):
            reason = '旧草稿没有题目校验信息，请在助手中重新保存'
        elif not question:
            reason = '当前网页未找到此题'
        elif saved.get('signature') != question.get('signature'):
            reason = '题目或选项发生变化，请重新核对并保存答案'
        elif question['type'] == 'unsupported':
            if not saved.get('files'):
                reason = '此题型暂不支持自动填入，请在原网页完成'
        elif saved.get('value') in (None, '', []) and not saved.get('files'):
            reason = '没有保存答案'
        if reason:
            skipped.append({'id': qid, 'reason': reason})
            continue
        root = _root_for(page, qid)
        if root is None:
            skipped.append({'id': qid, 'reason': '未找到唯一题目容器'})
            continue
        value = saved.get('value')
        try:
            if question['type'] in {'single', 'multiple', 'judgment'}:
                reason = _fill_choice(root, question, value)
            elif question['type'] == 'blank':
                reason = _fill_attachments(root, saved['files'], saved.get('_local_files')) if not value and saved.get('files') else _fill_blanks(root, question, value)
            elif question['type'] == 'essay':
                if value not in (None, '', []) and not isinstance(value, str):
                    reason = '答案格式与题型不一致'
                else:
                    reason = _fill_essay(root, value) if value else None
                    if not reason and saved.get('files'):
                        reason = _fill_attachments(root, saved['files'], saved.get('_local_files'))
            elif question['type'] == 'upload':
                reason = _fill_attachments(root, saved.get('files'), saved.get('_local_files'))
            elif question['type'] == 'unsupported' and saved.get('files'):
                reason = _fill_attachments(root, saved.get('files'), saved.get('_local_files'))
            else:
                reason = '答案格式与题型不一致'
            if not reason and saved.get('files') and question['type'] not in {'blank', 'essay', 'upload', 'unsupported'}:
                reason = _fill_attachments(root, saved['files'], saved.get('_local_files'))
        except Exception as exc:
            # Playwright exceptions include URLs and request logs. Never expose
            # those, browser credentials, or content of an online answer.
            reason = '页面控件操作失败（' + type(exc).__name__ + '），请在网页检查'
        if reason:
            skipped.append({'id': qid, 'reason': reason})
        else:
            filled.append(qid)
    return {'filled': filled, 'skipped': skipped}


def _backend():
    import homework_engine as b
    return b


def _record(key):
    b = _backend()
    state = b.load_state()
    records = state.get('assignments', state.get('items', [])) if isinstance(state, dict) else state
    if isinstance(records, dict):
        record = records.get(key)
        if record is None:
            record = next((r for r in records.values() if isinstance(r, dict) and r.get('key') == key), None)
    else:
        record = next((r for r in records if isinstance(r, dict) and r.get('key') == key), None)
    if not record:
        raise ValueError('没有找到这份作业，请刷新列表')
    return record


def _entry(record):
    for url in dom.record_urls(record):
        if url and dom.platform_url(url):
            return url
    raise ValueError('尚未取得可验证的作答入口，请刷新此作业')


def _identities(record):
    return dom.record_identities(record)


def _identity_matches(record, markup, url):
    expected = _identities(record)
    actual = set(dom.page_identity(markup, url))
    return bool(expected and actual and expected & actual)


def _deadline_reason(record):
    if record.get('availability') == 'closed':
        return '平台已关闭此作业，未填入'
    status = record.get('status', '')
    if status in {'submitted', '已交', '已提交', '已完成'}:
        return '此作业已提交，未填入'
    if status not in {'pending', '未交', '未提交', '未完成'}:
        return '作业提交状态尚未核实，请先刷新'
    deadline = record.get('deadline')
    if not deadline:
        if any(record.get(key) in {'none', 'no_deadline', 'unlimited'} for key in ('deadline_kind', 'deadline_state', 'deadline_status')):
            return None
        return '截止时间尚未核实，请先刷新'
    try:
        dt = datetime.fromisoformat(str(deadline).replace('Z', '+00:00'))
        current = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        if dt <= current:
            return '此作业已截止，未填入'
    except (ValueError, TypeError):
        return '截止时间格式无法核实，请先刷新'
    return None


def open_assignment(key):
    b = _backend()
    with b.browser_operation():
        record = _record(key)
        from playwright.sync_api import sync_playwright
        from homework_navigation import resolve_work_page
        b.ensure_chrome(_entry(record), app=False)
        with sync_playwright() as p:
            context = b.open_context(p, headless=False)
            try:
                _, final = resolve_work_page(context, record, b.fetch_html)
                b.ensure_chrome(final, app=False)
            finally:
                b.close_context(context)
    return {'message': '已打开作业'}


def fill_assignment(key):
    b = _backend()
    with b.browser_operation():
        return _fill_assignment(key)


def _local_attachment_files(b, files):
    """Resolve saved attachment records to files inside this project's data dir."""
    root = (Path(b.DATA_DIR) / 'answer_attachments').resolve()
    resolved = []
    for info in files or []:
        if not isinstance(info, dict):
            continue
        file_id = str(info.get('id') or '')
        name = Path(str(info.get('path') or '')).name
        if not file_id or Path(name).stem != file_id:
            continue
        path = (root / name).resolve()
        if path.is_relative_to(root) and path.is_file():
            resolved.append({'path': str(path), 'name': info.get('name') or name})
    return resolved


def _fill_assignment(key):
    b = _backend()
    record = _record(key)
    if b.effective_record(record).get('local_settings', {}).get('completed'):
        return {'filled': [], 'skipped': [], 'message': '此作业已在本机标记完成，请先撤销该标记再填入。'}
    draft = b.load_draft(key)
    answers = draft.get('answers') or {}
    if not isinstance(answers, dict) or not any(isinstance(v, dict) and (v.get('value') not in (None, '', []) or v.get('files')) for v in answers.values()):
        return {'filled': [], 'skipped': [], 'message': '请先在助手里保存每道题的答案。'}
    url = _entry(record)
    if not _identities(record):
        return {'filled': [], 'skipped': [], 'message': '作业身份尚未核实，请先刷新。'}
    from playwright.sync_api import sync_playwright
    b.ensure_chrome(url, app=False)
    with sync_playwright() as p:
        context = b.open_context(p, headless=False)
        try:
            # Never create a disposable browser whose closure would discard the
            # filled answers. The dedicated Chrome must remain connected via CDP.
            if hasattr(b, 'CONNECTED_BROWSER') and b.CONNECTED_BROWSER is None:
                return {'filled': [], 'skipped': [], 'message': '尚未连接到专用学习通窗口，请重新打开后再填入。'}
            # Fresh detail metadata is checked before any answer operation. A
            # cached pending status/deadline is insufficient if the teacher closed
            # the assignment since the assistant's last refresh.
            from homework_navigation import resolve_work_page
            detail, detail_final = resolve_work_page(context, record, b.fetch_html)
            url = detail_final
            if not _identity_matches(record, detail, detail_final):
                return {'filled': [], 'skipped': [], 'message': '作业详情身份不一致，已停止填入。'}
            if _read_only_url(detail_final):
                return {'filled': [], 'skipped': [], 'message': '作业详情为只读查看页面，未填入。'}
            b.update_metadata(record, detail, detail_final)
            reason = _deadline_reason(record)
            if reason:
                return {'filled': [], 'skipped': [], 'message': reason}
            matches = [existing for existing in context.pages if existing.url == url]
            page = matches[-1] if matches else context.new_page()
            if not matches:
                page.goto(url, wait_until='domcontentloaded', timeout=25000)
            page.bring_to_front()
            markup = page.content()
            if dom.is_login(markup, page.url):
                return {'filled': [], 'skipped': [], 'message': '学习通登录已失效，请在打开的窗口登录，再重试填入。'}
            if not _identity_matches(record, markup, page.url):
                return {'filled': [], 'skipped': [], 'message': '作业身份与已保存答案不一致，已停止填入。'}
            b.update_metadata(record, markup, page.url)
            reason = _deadline_reason(record)
            if reason:
                return {'filled': [], 'skipped': [], 'message': reason}
            # A report/review page must never receive answer writes.
            root = dom.parse(markup)
            displayed_status = ' '.join(dom.clean_text(n) for n in root.all() if set(n.attrs.get('class', '').split()) & {'workStatus', 'status', 'statusText'})
            if _read_only_url(page.url) or dom.explicit_status(displayed_status) == 'submitted':
                return {'filled': [], 'skipped': [], 'message': '网页显示此作业已提交或处于查看模式，未填入。'}
            questions = dom.parse_questions(markup, page.url)
            if not questions:
                entry = dom.phone_entry(markup, page.url)
                if entry and dom.platform_url(entry):
                    page.goto(entry, wait_until='domcontentloaded', timeout=25000)
                    markup = page.content()
                    if dom.is_login(markup, page.url) or _read_only_url(page.url) or not _identity_matches(record, markup, page.url):
                        return {'filled': [], 'skipped': [], 'message': '作答页面的登录或作业身份校验未通过，未填入。'}
                    b.update_metadata(record, markup, page.url)
                    reason = _deadline_reason(record)
                    if reason:
                        return {'filled': [], 'skipped': [], 'message': reason}
                    questions = dom.parse_questions(markup, page.url)
            if not questions:
                return {'filled': [], 'skipped': [], 'message': '未识别到可填写的题目，已保留原网页供你处理。'}
            diagnostics = dom.question_diagnostics(markup, questions)
            if len({q['id'] for q in questions}) != len(questions):
                return {'filled': [], 'skipped': [], 'message': '页面题目编号重复，已停止填入。'}
            # UEditor initializes asynchronously after DOMContentLoaded.
            try:
                page.wait_for_function("""() => {
                    if(document.querySelector('#ananas-editor-answer')) return !!window.ueditor?.body;
                    const areas=[...document.querySelectorAll('.questionLi textarea[id^=answer]')];
                    return areas.every(a=>a.offsetParent!==null) || [...Object.values(window.UE?.instants||{}),...Object.values(window.UE?.instances||{})].some(e=>e?.body);
                }""", timeout=7000)
            except Exception:
                pass
            # Page scripts can randomize option order during editor startup.
            # Reparse the actual displayed question immediately before matching.
            markup = page.content()
            questions = dom.parse_questions(markup, page.url)
            # Keep the persisted draft format unchanged.  The extra local file
            # paths only live for this fill operation and are never returned to
            # the UI or written back to disk.
            fill_answers = {}
            for qid, saved in answers.items():
                if isinstance(saved, dict) and saved.get('files'):
                    saved = dict(saved)
                    saved['_local_files'] = _local_attachment_files(b, saved.get('files'))
                fill_answers[qid] = saved
            result = fill_page(page, questions, fill_answers)
            result['message'] = f"已填入 {len(result['filled'])} 题，跳过 {len(result['skipped'])} 题。"
            if not diagnostics['complete']:
                result['message'] += ' 当前页面未确认包含全部题目，其他页未自动填入。'
            return result
        except Exception as exc:
            return {'filled': [], 'skipped': [], 'message': '打开或填入失败：' + b.safe_error(exc)}
        finally:
            b.close_context(context)
