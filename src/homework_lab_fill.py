"""Fill verified native upload fields, leaving platform save/submit to the user.

The platform's own file control performs the upload.  This module never posts
answers, calls report mutation endpoints, or clicks save/submit.  Merely opening
the user-requested editor may run the platform's normal viewed-state handlers.
"""
from __future__ import annotations

from collections import Counter
import mimetypes
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

import homework_lab as lab
import homework_runtime as runtime


_FIELDS = {
    'currUploadFiles': ('上传报告', {'.pdf'}, 1),
    'uploadImages': ('上传多张图片', {'.png', '.bmp', '.jpg', '.jpeg'}, 0),
}
_MAX_BYTES = 50 * 1024 * 1024


def entry_url(record):
    target = lab._record_target(record)
    details = record.get('lab_report') or {}
    return lab._entry(target['origin'], target['fid'], target['report_id'], details.get('course_id', ''),
                      belong_type=details.get('belong_type'), is_virtual=details.get('is_virtual', False),
                      group_mode=details.get('group_mode'), report_type=details.get('type'))


def authenticated_record(context, record):
    """Use the official SSO result without changing the persisted identity."""
    from homework_lab_auth import ensure_session
    target = lab._record_target(record)
    origin = ensure_session(context, target['origin'], target['fid'])
    actual = dict(target, origin=origin)
    if lab.identity(**actual) != lab.identity(**target):
        raise RuntimeError('作业登录身份无法核实，请重新登录')
    return dict(record, lab_report=dict(record.get('lab_report') or {}, origin=origin))


def _page_matches(record, url):
    try:
        target = lab._record_target(record)
        parsed = urlsplit(url)
        route, _, query = parsed.fragment.partition('?')
        params = {k.lower(): values[0] for k, values in parse_qs(query).items() if len(values) == 1}
        if lab.trusted_origin(url) != target['origin'] or parsed.path.rstrip('/') != '/lab':
            return False
        if route == '/redirect':
            if params.get('key') != 'labTemplate/report/fill':
                return False
        elif route != '/admin/labTemplate/report/fill':
            return False
        course = str((record.get('lab_report') or {}).get('course_id') or '')
        return (params.get('reportid') == target['report_id'] and params.get('fid') == target['fid']
                and (not course or params.get('courseid') == course))
    except (ValueError, TypeError):
        return False


def _supported(record):
    details = record.get('lab_report') or {}
    return (record.get('source_kind') == lab.SOURCE_KIND and str(details.get('type')) == '2'
            and str(details.get('belong_type', details.get('belongType', '0'))) != '1'
            and not details.get('is_virtual')
            and not details.get('child_reports'))


def _fresh_reason(patch):
    from homework_fill import _deadline_reason
    if patch.get('status') != 'pending' or patch.get('content_complete') is not True or not patch.get('questions'):
        return '当前作业状态或填写要求无法核实，请在原网页检查。'
    if (patch.get('lab_report') or {}).get('attachments_verified') is False:
        return '当前作业附件状态尚未完整核实，请在原网页检查。'
    # A missing live deadline must not silently inherit an old cached date.
    return _deadline_reason(patch)


def _field_root(page, field):
    label, accepted, _ = _FIELDS[field]
    candidates = page.locator('.el-form-item')
    matches = []
    for index in range(candidates.count()):
        root = candidates.nth(index)
        labels = root.locator('.el-form-item__label')
        picker = root.locator('input[type="file"][name="files"]')
        if labels.count() != 1 or labels.inner_text().strip().rstrip('：:') != label or not root.is_visible():
            continue
        if picker.count() != 1 or picker.is_disabled():
            continue
        accept = {value.strip().lower() for value in (picker.get_attribute('accept') or '').split(',') if value.strip()}
        if accept == accepted:
            matches.append((root, picker))
    return matches[0] if len(matches) == 1 else (None, None)


def _uploaded(root, names, paths, image):
    """Require successful native UI rows, never just a populated file input."""
    items = root.locator('.el-upload-list__item')
    if items.count() != len(names):
        return False
    actual_names, actual_paths = [], []
    for index in range(items.count()):
        item = items.nth(index)
        if 'is-success' not in (item.get_attribute('class') or '').split():
            return False
        if image:
            thumbnails = item.locator('img.el-upload-list__item-thumbnail')
            if thumbnails.count() != 1:
                return False
            actual_paths.append(thumbnails.get_attribute('src') or '')
        else:
            label = item.locator('.el-upload-list__item-name')
            if label.count() != 1:
                return False
            actual_names.append(label.inner_text().strip())
    if not image:
        return Counter(actual_names) == Counter(names)
    # Picture cards do not render a filename. Match their final remote image
    # URLs against this operation's successful native upload responses instead.
    return (len(paths) == len(names) and len(set(paths)) == len(paths)
            and Counter(actual_paths) == Counter(paths))


def fill_page(page, record, questions, answers, localfiles=None, timeout=30):
    """Fill a freshly verified record; usable with synthetic browser fixtures."""
    from homework_fill import _deadline_reason
    result = {'filled': [], 'skipped': []}
    reason = _deadline_reason(record)
    if not _page_matches(record, page.url):
        reason = '作业网页身份无法核实，未填入'
    elif not _supported(record):
        reason = '当前网页填写方式尚未验证，请在原网页完成'
    elif (record.get('lab_report') or {}).get('attachments_verified') is False:
        reason = '当前网页附件状态无法核实，请在原网页检查'
    elif (record.get('lab_report') or {}).get('student_attachments'):
        # The native page can render its empty picker before its asynchronous
        # saved-answer request finishes. The fresh server read also guards this
        # interval, so an existing attachment is never replaced during loading.
        reason = '网页已有附件，未覆盖，请在原网页核对'
    if reason:
        result['skipped'].append({'id': '', 'reason': reason})
        return result
    ids = [str(question.get('id', '')) for question in questions]
    if len(ids) != len(set(ids)):
        result['skipped'].append({'id': '', 'reason': '题目编号重复，已停止填入'})
        return result
    by_id = dict(zip(ids, questions))
    localfiles = localfiles or {}
    for qid, saved in answers.items():
        qid = str(qid)
        question = by_id.get(qid)
        mapping = question.get('lab_field', {}) if question else {}
        field = mapping.get('field')
        reason = None
        if (not isinstance(saved, dict) or not question or not question.get('signature')
                or saved.get('signature') != question.get('signature')):
            reason = '题目或上传要求发生变化，请重新核对答案'
        elif (question.get('type') != 'upload' or mapping.get('kind') != 'file' or field not in _FIELDS
              or str(mapping.get('report_id')) != str(record['lab_report']['report_id'])):
            reason = '未识别到可核对的附件位置，请在原网页上传'
        elif not saved.get('files'):
            reason = '尚未添加答案附件，请在原网页完成'
        if reason:
            result['skipped'].append({'id': qid, 'reason': reason})
            continue
        try:
            files = saved['files']
            local = localfiles.get(qid, saved.get('_local_files', []))
            _, extensions, limit = _FIELDS[field]
            names = [str(item.get('name') or '') for item in files]
            paths = [Path(item['path']) for item in local]
            if len(paths) != len(files) or not all(path.is_file() for path in paths):
                reason = '本机附件不可用，请重新添加附件'
            elif limit and len(files) > limit:
                reason = '网页只接受一个答案附件，请先核对'
            elif any(Path(name).suffix.lower() not in extensions for name in names):
                reason = '附件格式与网页要求不一致，请先核对'
            elif any(path.stat().st_size >= _MAX_BYTES for path in paths):
                reason = '附件必须小于 50 MB，请先核对'
            root, picker = _field_root(page, field)
            if not reason and root is None:
                reason = '未找到唯一可用的附件控件，请在原网页上传'
            if not reason and (root.locator('.el-upload-list__item').count() or picker.evaluate('e => e.files.length')):
                reason = '网页已有附件，未覆盖，请在原网页核对'
            if not reason:
                uploaded_paths = []
                origin = record['lab_report']['origin']
                endpoint = '/lab/api/base/upload/' + ('multiFile' if field == 'uploadImages' else 'multiFileToYunPan')

                def response_received(response):
                    try:
                        parsed = urlsplit(response.url)
                        if (lab.trusted_origin(response.url) != origin or parsed.path != endpoint
                                or response.request.method != 'POST' or not response.ok):
                            return
                        data = response.json()
                        for item in data.get('data', []) if isinstance(data, dict) else []:
                            value = item.get('path') if isinstance(item, dict) else None
                            if isinstance(value, str) and value:
                                uploaded_paths.append(value if value.startswith(('https://', 'http://'))
                                                      else origin + '/lab/fileUpload/' + value)
                    except Exception:
                        pass

                page.on('response', response_received)
                try:
                    payloads = [{'name': name, 'mimeType': mimetypes.guess_type(name)[0] or 'application/octet-stream',
                                 'buffer': path.read_bytes()} for name, path in zip(names, paths)]
                    picker.set_input_files(payloads)
                    deadline = time.monotonic() + timeout
                    while not _uploaded(root, names, uploaded_paths, field == 'uploadImages'):
                        if time.monotonic() >= deadline:
                            reason = '附件上传尚未确认成功，请在原网页检查；未重复上传'
                            break
                        page.wait_for_timeout(100)
                finally:
                    page.remove_listener('response', response_received)
        except Exception as exc:
            reason = '附件控件操作失败（' + type(exc).__name__ + '），请在原网页检查'
        if reason:
            result['skipped'].append({'id': qid, 'reason': reason})
        else:
            result['filled'].append(qid)
    return result


def open_assignment(key):
    """Open the original page after SSO; caller owns browser_operation's lock.

    Import the saved session through the same verified dedicated CDP context
    as filling. No report detail, draft, or form field is read or written here.
    close_context only disconnects this externally owned browser connection.
    """
    import homework_fill as standard
    from playwright.sync_api import sync_playwright
    backend = standard._backend()
    record = standard._record(key)
    lab._record_target(record)
    backend.ensure_chrome(app=False)
    with sync_playwright() as playwright:
        context = None
        try:
            context = standard._open_fill_context(playwright, backend)
            authenticated = authenticated_record(context, record)
            _open_original(context, authenticated, backend=backend)
            return {'message': '已打开作业'}
        except Exception as exc:
            # A failed SSO must not silently open the unauthenticated stored URL
            # and claim success, or disclose redirect tokens in browser errors.
            raise RuntimeError(backend.safe_error(exc)) from None
        finally:
            if context is not None:
                backend.close_context(context)


def fill_assignment(key):
    """Connect to the dedicated review window and leave it open afterwards."""
    import homework_fill as standard
    backend = standard._backend()
    with backend.browser_operation():
        record = standard._record(key)
        if backend.effective_record(record).get('local_settings', {}).get('completed'):
            return {'filled': [], 'skipped': [], 'message': '此作业已在本机标记完成，请先撤销该标记再填入。'}
        answers = backend.load_draft(key).get('answers') or {}
        if _supported(record) and (not isinstance(answers, dict) or not any(
                isinstance(value, dict) and value.get('files') for value in answers.values())):
            return {'filled': [], 'skipped': [], 'message': '请先添加答案附件，再前往交作业。'}
        from playwright.sync_api import sync_playwright
        # Start/connect to the dedicated window before choosing an origin.  A
        # school link may need the official SSO bridge to the central lab host.
        backend.ensure_chrome(app=False)
        with sync_playwright() as playwright:
            context = None
            try:
                context = standard._open_fill_context(playwright, backend)
                record = authenticated_record(context, record)
                if not _supported(record):
                    _open_original(context, record, backend=backend)
                    return {'filled': [], 'skipped': [], 'message': '当前填写方式尚未验证，已打开原网页供你完成。'}
                patch = lab.read_report(context, record)
                fresh = dict(record, **patch)
                if not _supported(fresh):
                    _open_original(context, fresh, backend=backend)
                    return {'filled': [], 'skipped': [], 'message': '网页填写方式已变化，请在打开的原网页完成。'}
                reason = _fresh_reason(patch)
                if reason:
                    return {'filled': [], 'skipped': [], 'message': reason}
                matches = [page for page in context.pages if _page_matches(fresh, page.url)]
                if len(matches) > 1:
                    return {'filled': [], 'skipped': [], 'message': '打开了多个相同作业页面，请保留一个后重试。'}
                blanks = [page for page in context.pages if runtime.is_blank_or_loading_url(getattr(page, 'url', ''))]
                page = matches[0] if matches else (blanks[-1] if blanks else context.new_page())
                if not matches:
                    runtime.show_loading_splash(page, fresh.get('title') or record.get('title'))
                standard._activate_page(backend, context, page)
                if not matches:
                    page.goto(entry_url(fresh), wait_until='domcontentloaded', timeout=25000)
                page.locator('.el-form-item input[type="file"][name="files"]').first.wait_for(state='attached', timeout=10000)
                # Recheck current server state after asynchronous editor loading.
                patch = lab.read_report(context, fresh)
                fresh.update(patch)
                reason = _fresh_reason(patch)
                if reason:
                    return {'filled': [], 'skipped': [], 'message': reason}
                local = {str(qid): standard._local_attachment_files(backend, saved.get('files'))
                         for qid, saved in answers.items() if isinstance(saved, dict)}
                result = fill_page(page, fresh, patch['questions'], answers, local)
                result['message'] = f"已填入 {len(result['filled'])} 题，跳过 {len(result['skipped'])} 题。请在网页检查后自行提交。"
                return result
            except Exception as exc:
                return {'filled': [], 'skipped': [], 'message': '打开或填入失败：' + backend.safe_error(exc)}
            finally:
                if context is not None:
                    backend.close_context(context)


def _open_original(context, record, backend=None):
    """Open the canonical individual/group route without touching form controls."""
    entry = entry_url(record)
    matches = [page for page in context.pages if page.url == entry]
    blanks = [page for page in context.pages if runtime.is_blank_or_loading_url(getattr(page, 'url', ''))]
    page = matches[-1] if matches else (blanks[-1] if blanks else context.new_page())
    if not matches:
        runtime.show_loading_splash(page, record.get('title'))
    import homework_fill as standard
    standard._activate_page(backend or standard._backend(), context, page)
    if not matches:
        page.goto(entry, wait_until='domcontentloaded', timeout=25000)
