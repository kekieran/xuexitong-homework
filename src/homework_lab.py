"""Read laboratory reports into the ordinary assignment contract.

Only the explicitly listed GET endpoints are used here.  In particular, opening
a report must never create a student's report, refresh a Word copy, or mark it
viewed.  Callers apply returned patches only after a successful read, so cached
questions survive an expired session or an unsupported response format.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import homework_dom as dom
import homework_school as school


SOURCE_KIND = 'lab_report'
DEFAULT_ORIGIN = 'https://mooc.istudy.szpu.edu.cn'
DEFAULT_FID = '1860'
ALLOWED_HOSTS = {'mooc.istudy.szpu.edu.cn', 'jcxygl.chaoxing.com'}
READ_PATHS = {
    'list': '/labTemplate/report/my/index',
    'report': '/labTemplate/report/edit',
    'fill': '/labTemplate/report/fill/stuEdit',
    'children': '/labTemplate/report/my/getWordSubReportFillList',
    'instructions': '/labTemplate/report/fill/getDefaultFormData',
}
LOCAL_ZONE = timezone(timedelta(hours=8))
PENDING_STATUSES = {'0', '1', '2', '5'}
SUBMITTED_STATUSES = {'3', '4'}
STATUS_LABELS = {'0': '待查看', '1': '已查看', '2': '已保存', '3': '待批阅',
                 '4': '已批阅', '5': '待重做', '99': '待建组'}
NO_DEADLINE = {'无截止时间', '无截止', '不限', '不限制', '不限时', '永久', '无限制'}
_ID = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')


def _identifier(value):
    text = str(value if value is not None else '').strip()
    return text if _ID.fullmatch(text) else ''


def trusted_origin(value):
    """Pin network requests to known lab hosts, never a notice-supplied origin."""
    try:
        parsed = urlsplit(str(value or ''))
        if parsed.scheme != 'https' or parsed.hostname not in (ALLOWED_HOSTS | school.lab_hosts()):
            return ''
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            return ''
        return 'https://' + parsed.hostname
    except ValueError:
        return ''


def identity(origin, fid, report_id):
    """The school and central lab hosts are aliases of the same platform."""
    fid, report_id = _identifier(fid), _identifier(report_id)
    if not trusted_origin(origin) or not fid or not report_id:
        return ''
    return f'chaoxing-lab:{fid}:report:reportid:{report_id}'


def _datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            stamp = value / 1000 if abs(value) >= 100_000_000_000 else value
            parsed = datetime.fromtimestamp(stamp, LOCAL_ZONE)
        except (ValueError, OSError, OverflowError):
            return None
    else:
        text = str(value or '').strip()
        if not text:
            return None
        if re.fullmatch(r'\d{10}|\d{13}', text):
            return _datetime(int(text))
        text = text.replace('年', '-').replace('月', '-').replace('日', '').replace('/', '-')
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not 2000 <= parsed.year <= 2200:
        return None
    return parsed.astimezone(LOCAL_ZONE).replace(tzinfo=None) if parsed.tzinfo else parsed


def _stamp(value):
    parsed = _datetime(value)
    return parsed.isoformat(timespec='seconds') if parsed else None


def _now(value=None):
    return _datetime(value) or datetime.now(LOCAL_ZONE).replace(tzinfo=None)


def _first(mapping, *names):
    return next((mapping[name] for name in names if mapping.get(name) not in (None, '')), None)


def _deadline(value, explicit_none=False):
    stamp = _stamp(value)
    if stamp:
        return stamp, 'known'
    if explicit_none or str(value or '').strip() in NO_DEADLINE:
        return None, 'none'
    return None, 'unknown'


def _status(value):
    code = str(value if value is not None else '')
    if code in SUBMITTED_STATUSES:
        return 'submitted'
    if code in PENDING_STATUSES:
        return 'pending'
    return 'unknown'


def _entry(origin, fid, report_id, course_id='', *, belong_type=None, is_virtual=False, group_mode=None, report_type=None):
    route = 'labTemplate/report/group/groupFill' if str(belong_type) == '1' else 'labTemplate/report/fill'
    if str(report_type) == '3':
        # The native parent opens a child-report dialog on the list page.
        route = 'labTemplate/report/my'
    if is_virtual:
        route = {'2': 'labTemplate/report/group/leader', '3': 'labTemplate/report/group/faceToFace',
                 '4': 'labTemplate/report/group/faceToFace'}.get(str(group_mode), 'labTemplate/report/my')
    params = {'key': route, 'reportId': report_id, 'fid': fid}
    if course_id:
        params['courseId'] = course_id
    if is_virtual and group_mode is not None:
        params['groupMode'] = group_mode
    return origin + '/lab/#/redirect?' + urlencode(params)


def target_from_url(value):
    """Extract stable identity fields; redirect keys are not identity inputs."""
    try:
        parsed = urlsplit(html.unescape(str(value or '')))
        origin = trusted_origin(value)
        if not origin or not parsed.path.startswith('/lab/'):
            return None
        fields = parse_qs(parsed.query)
        fields.update(parse_qs(parsed.fragment.partition('?')[2]))
        lowered = {k.casefold(): v[0] for k, v in fields.items() if v}
        fid = _identifier(lowered.get('fid') or lowered.get('deptid'))
        report_id = _identifier(lowered.get('reportid') or lowered.get('id'))
        if not fid or not report_id:
            return None
        return {'origin': origin, 'fid': fid, 'report_id': report_id}
    except ValueError:
        return None


def parse_notice(notice):
    """Parse lab-specific rich text without trusting its historical task status."""
    if not isinstance(notice, dict):
        return None
    root = dom.parse(str(notice.get('rtf_content') or notice.get('content') or ''))
    text = dom.clean_text(root)
    labels = r'课程名称|实验报告名称|报告名称|作答截止时间|截止时间|发布时间|发布于|作答开始时间|开始时间'

    def field(names):
        pattern = r'(?:' + names + r')\s*[：:]\s*(.*?)(?=\r?\n|(?:' + labels + r')\s*[：:]|$)'
        match = re.search(pattern, text)
        return match.group(1).strip() if match else ''

    title, course = field('实验报告名称|报告名称'), field('课程名称')
    if not title or not course:
        return None
    links = [html.unescape(node.attrs.get('href', '')) for node in root.all('a')]
    for link in links:
        target = target_from_url(link)
        if target:
            break
    else:
        return None
    deadline, deadline_kind = _deadline(field('作答截止时间|截止时间'))
    published = _stamp(field('发布时间|发布于'))
    return {
        'source_kind': SOURCE_KIND, 'course': course, 'title': title,
        'identities': [identity(**target)], 'entry_url': link,
        'lab_report': target, 'status': 'unknown',
        'start': _stamp(field('作答开始时间|开始时间')),
        'published_at': published, 'deadline': deadline, 'deadline_kind': deadline_kind,
        'deadline_source': 'notice' if deadline_kind != 'unknown' else None,
        'notice_sent': _stamp(notice.get('sendTime')),
        'source_notice_ids': [str(notice['idCode'])] if notice.get('idCode') else [],
    }


def candidate_from_row(row, origin=DEFAULT_ORIGIN, fid=DEFAULT_FID, now=None):
    if not isinstance(row, dict):
        return None
    origin = trusted_origin(origin)
    report_id = _identifier(row.get('reportId'))
    fid = _identifier(_first(row, 'fid', 'deptId') or fid)
    canonical = identity(origin, fid, report_id)
    if not canonical or not row.get('reportName'):
        return None
    status_code = str(row.get('status') if row.get('status') is not None else '')
    # The native list renders a configured start + an explicitly empty end as
    # “无期限”. An absent end field (or an unconfigured schedule) is unknown.
    open_ended = ('reportEndTime' in row and row['reportEndTime'] in (None, '')
                  and _stamp(row.get('reportStartTime')) is not None)
    deadline, deadline_kind = _deadline(row.get('reportEndTime'),
                                        row.get('deadlineKind') == 'none' or open_ended)
    target = {'origin': origin, 'fid': fid, 'report_id': report_id,
              'type': row.get('type'), 'status_code': status_code,
              'status_label': STATUS_LABELS.get(status_code, ''),
              'belong_type': row.get('belongType'),
              'is_virtual': row.get('isVirtual') in (True, 1, '1'),
              'group_mode': row.get('groupMode'),
              'course_id': _identifier(row.get('moocCourseId'))}
    stamp = _now(now).isoformat(timespec='seconds')
    return {
        'source_kind': SOURCE_KIND, 'course': str(_first(row, 'moocCourseName', 'courseName') or ''),
        'title': str(row['reportName']), 'identities': [canonical],
        'entry_url': _entry(origin, fid, report_id, target['course_id'], belong_type=target['belong_type'],
                            is_virtual=target['is_virtual'], group_mode=target['group_mode'],
                            report_type=target['type']), 'lab_report': target,
        'status': _status(status_code), 'status_checked_at': stamp,
        'metadata_checked_at': stamp, 'start': _stamp(row.get('reportStartTime')),
        # A template creation date, course start, or reminder is not publication.
        'published_at': _stamp(_first(row, 'reportPublishTime', 'publishTime', 'publishedAt')),
        'deadline': deadline, 'deadline_kind': deadline_kind,
        'deadline_source': 'detail' if deadline_kind != 'unknown' else None,
    }


def should_import(candidate, now=None, existing=False):
    if not isinstance(candidate, dict) or candidate.get('source_kind') != SOURCE_KIND:
        return False
    if not candidate.get('identities'):
        return False
    if existing:
        return True
    now = _now(now)
    published = _datetime(candidate.get('published_at'))
    deadline = _datetime(candidate.get('deadline'))
    if published and now - timedelta(days=30) <= published <= now:
        return True
    if deadline and deadline > now:
        return True
    return candidate.get('deadline_kind') == 'none' and candidate.get('status') == 'pending'


def dispose_response(response):
    """Safely release a Playwright APIResponse handle without raising."""
    if response is not None:
        try:
            response.dispose()
        except Exception:
            pass


def _get(context, origin, endpoint, params):
    origin = trusted_origin(origin)
    if not origin or endpoint not in READ_PATHS:
        raise ValueError('不支持的作业读取地址')
    params = dict(params)
    fid = _identifier(params.get('deptId') or params.get('fid'))
    if fid:
        # The platform's request wrapper injects both names for every GET.
        params.update(deptId=fid, fid=fid)
    url = origin + '/lab/api' + READ_PATHS[endpoint]
    response = None
    try:
        try:
            response = context.request.get(url, params=params, timeout=18000, max_redirects=0,
                headers={'Referer': origin + '/lab/', 'X-Requested-With': 'XMLHttpRequest', 'server': 'true'})
        except Exception:
            # Request exceptions can contain cookies and signed URLs. Never expose them.
            raise RuntimeError('作业读取失败，请检查网络或重新登录') from None
        if response.status in (401, 403) or 300 <= response.status < 400:
            raise RuntimeError('LOGIN_REQUIRED:' + urlsplit(origin).hostname)
        if response.status >= 400:
            raise RuntimeError(f'作业读取失败（HTTP {response.status}）')
        try:
            payload = response.json()
        except Exception:
            raise RuntimeError('作业读取失败，请检查网络或重新登录') from None
        if not isinstance(payload, dict):
            raise RuntimeError('作业接口返回格式无法识别')
        if str(payload.get('code')) == '1005':
            raise RuntimeError('LOGIN_REQUIRED:' + urlsplit(origin).hostname)
        if str(payload.get('code')) != '1000':
            raise RuntimeError('作业读取未成功，请在原网页检查登录和访问权限')
        return payload.get('data')
    finally:
        dispose_response(response)


def collect_reports(context, targets, *, now=None):
    """Read both pending/submitted lists without fetching any report details."""
    candidates, warnings, known_targets = {}, [], set()
    for target in targets:
        origin, fid = trusted_origin(target.get('origin')), _identifier(target.get('fid'))
        course_id = _identifier(target.get('course_id'))
        if not origin or not fid or (origin, fid, course_id) in known_targets:
            continue
        known_targets.add((origin, fid, course_id))
        for status_filter in ('2', '1'):
            signatures = set()
            try:
                for page in range(1, 21):
                    # Public table filters are an array of objects. Axios's
                    # serializer uses bracketed nested keys, not dotted keys.
                    params = {'cpage': page, 'pageSize': 50, 'deptId': fid,
                              'search[0][field]': 'type', 'search[0][val]': status_filter,
                              'search[0][operator]': 'eq'}
                    # Native global lists explicitly filter by course 0; their
                    # filter helper deliberately retains zero-valued filters.
                    params.update({'search[1][field]': 'moocCourseId',
                                   'search[1][val]': course_id or '0', 'search[1][operator]': 'eq'})
                    data = _get(context, origin, 'list', params)
                    if not isinstance(data, dict) or not isinstance(data.get('list'), list):
                        raise RuntimeError('作业列表结构无法识别，已有内容已保留')
                    rows = data['list']
                    signature = tuple(str(row.get('reportId')) for row in rows if isinstance(row, dict))
                    if rows and signature in signatures:
                        warnings.append('作业列表分页重复，本次已停止，已有内容已保留')
                        break
                    signatures.add(signature)
                    for row in rows:
                        candidate = candidate_from_row(row, origin, fid, now)
                        if candidate:
                            canonical = candidate['identities'][0]
                            # A just-submitted report can be in both filters during sync.
                            previous = candidates.get(canonical)
                            if not previous or candidate['status'] == 'submitted' or previous['status'] != 'submitted':
                                candidates[canonical] = candidate
                    total = data.get('total')
                    try:
                        exhausted = int(total) <= page * 50 if total is not None else False
                    except (TypeError, ValueError):
                        exhausted = False
                    if len(rows) < 50 or exhausted:
                        break
                    if page == 20:
                        warnings.append('作业列表达到分页上限，本次未读取全部记录，下次可继续刷新')
            except Exception as exc:
                message = str(exc) if isinstance(exc, RuntimeError) else '作业列表暂时无法读取'
                warnings.append(message)
    return list(candidates.values()), list(dict.fromkeys(warnings))


def _record_target(record):
    details = record.get('lab_report') or {}
    origin = trusted_origin(details.get('origin'))
    fid, report_id = _identifier(details.get('fid')), _identifier(details.get('report_id'))
    if not identity(origin, fid, report_id):
        raise ValueError('缺少可核对的作业身份，请重新同步')
    return {'origin': origin, 'fid': fid, 'report_id': report_id}


def _file_url(value, origin):
    value = str(value or '').strip()
    if not value or '\\' in value or any(ord(char) < 32 for char in value):
        return ''
    if value.startswith(('https://', 'http://')):
        try:
            parsed = urlsplit(value)
            return value if parsed.hostname and not parsed.username and not parsed.password else ''
        except ValueError:
            return ''
    if ':' in value or value.startswith('//') or '..' in value.split('/'):
        return ''
    return origin + '/lab/fileUpload/' + value.lstrip('/')


def _files(raw, origin, role):
    """Normalize only the verified current-file shape, not submission history."""
    if raw in (None, ''):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError('附件信息格式无法识别') from None
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError('附件信息格式无法识别')
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('附件信息格式无法识别')
        url = _file_url(item.get('filePath'), origin)
        if not url:
            raise ValueError('附件缺少可打开的地址')
        entry = {'name': str(item.get('fileName') or '附件'), 'url': url, 'role': role}
        preview = _file_url(item.get('filePreview'), origin)
        if preview:
            entry['preview_url'] = preview
        if isinstance(item.get('fileSize'), (int, float)):
            entry['size'] = item['fileSize']
        result.append(entry)
    return result


def _upload_question(report_id, upload_type):
    if str(upload_type) == '2':
        field, accept, maximum = 'currUploadFiles', ['.pdf'], 1
        text = '请上传 PDF 格式的作业文件，仅限 1 个，文件需小于 50 MB。'
    elif str(upload_type) == '1':
        field, accept, maximum = 'uploadImages', ['.png', '.bmp', '.jpg', '.jpeg'], 0
        text = '请上传作业图片，支持 PNG、BMP、JPG、JPEG，每个文件需小于 50 MB。'
    else:
        return None
    target = {'kind': 'file', 'field': field, 'report_id': report_id,
              'accept': accept, 'max_files': maximum, 'max_bytes': 50 * 1024 * 1024}
    signature = hashlib.sha256(json.dumps(target, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]
    return {'id': f'lab-{report_id}-{field}', 'source_kind': SOURCE_KIND,
            'type': 'upload', 'type_code': '', 'label': dom.TYPE_LABELS['upload'],
            'text': text, 'html': '<p>' + html.escape(text) + '</p>', 'options': [],
            'blank_names': [], 'blank_count': 0, 'signature': signature, 'lab_field': target}


def _children(data, target, course_id=''):
    if not isinstance(data, dict) or not isinstance(data.get('subReports'), list):
        raise RuntimeError('组合报告列表结构无法识别，已有内容已保留')
    result = []
    for item in data['subReports']:
        if not isinstance(item, dict) or not _identifier(item.get('id')):
            raise RuntimeError('组合报告身份无法核对，已有内容已保留')
        code = str(item.get('status') if item.get('status') is not None else '')
        child_id = _identifier(item['id'])
        # Native WordSubReportDialog displays description as text, not v-html.
        # Preserve that meaning even if the teacher types HTML-looking content.
        description = str(item.get('description') or '')
        description_html = dom.safe_html(dom.parse('<p>' + html.escape(description).replace('\n', '<br>') + '</p>'))
        child = {'report_id': child_id, 'title': str(item.get('name') or ''),
                 'status': _status(code), 'status_code': code,
                 'status_label': STATUS_LABELS.get(code, ''),
                 'description_text': description, 'description_html': description_html}
        if item.get('isVirtual') in (True, 1, '1') or str(item.get('belongType')) == '1':
            child['entry_url'] = _entry(target['origin'], target['fid'], child_id, course_id,
                belong_type=item.get('belongType'), is_virtual=item.get('isVirtual') in (True, 1, '1'),
                group_mode=item.get('groupMode'))
        else:
            params = {'key': 'labTemplate/report/stuWordFill', 'reportId': child_id, 'fid': target['fid']}
            if course_id:
                params['courseId'] = course_id
            child['entry_url'] = target['origin'] + '/lab/#/redirect?' + urlencode(params)
        result.append(child)
    return result


def _form_prefill_instructions(data):
    """Read the two description fields in the native form's precast data.

    These are edittextarea values, so HTML-looking characters remain text.
    Their authorship is not exposed by this endpoint; a nonempty default does
    not verify that the teacher's full instructions have been retrieved.
    Identity defaults, answer fields and unknown components are never included.
    Both aliases must be present to distinguish an empty value from a partial
    response; an absent field cannot erase a previously read requirement.
    """
    if not isinstance(data, list):
        raise ValueError('作业要求格式无法识别')
    expected = {'ContentandRequirements': '内容与要求', 'Assessmentcriteria': '考核标准'}
    values = {}
    for item in data:
        if not isinstance(item, dict):
            raise ValueError('作业要求格式无法识别')
        alias = item.get('alias')
        if not isinstance(alias, str) or alias not in expected:
            continue
        value = item.get('val')
        if (alias in values or item.get('compt') != 'edittextarea'
                or not isinstance(value, list) or len(value) != 1
                or not isinstance(value[0], str)):
            raise ValueError('作业要求格式无法识别')
        values[alias] = value[0]
    if not values:
        return None
    if set(values) != set(expected):
        raise ValueError('作业要求字段不完整')
    text_parts, html_parts = [], []
    for alias, label in expected.items():
        value = values[alias].strip()
        if not value:
            continue
        text_parts.append(label + '\n' + value)
        markup = '<h4>' + label + '</h4><p>' + html.escape(value).replace('\n', '<br>') + '</p>'
        html_parts.append(dom.safe_html(dom.parse(markup)))
    return {'instructions_text': '\n\n'.join(text_parts), 'instructions_html': ''.join(html_parts)}


def read_report(context, record):
    """Return current metadata and genuine fields, without mutating cached content.

    Form and WPS fields need their own verified identity mapping. They are not
    converted to fictional essays; type 2's native upload control is known.
    """
    target = _record_target(record)
    data = _get(context, target['origin'], 'report', {'id': target['report_id'], 'deptId': target['fid']})
    if not isinstance(data, dict):
        raise RuntimeError('作业详情结构无法识别，已有内容已保留')
    reported_id = _first(data, 'reportId', 'id')
    if reported_id is not None and str(reported_id) != target['report_id']:
        raise RuntimeError('作业详情身份不一致，已有内容已保留')
    reported_fid = _first(data, 'fid', 'deptId')
    if reported_fid is not None and str(reported_fid) != target['fid']:
        raise RuntimeError('作业所属学校不一致，已有内容已保留')
    fill = _get(context, target['origin'], 'fill', {'reportId': target['report_id'], 'deptId': target['fid']})
    if not isinstance(fill, dict):
        raise RuntimeError('当前作答信息无法识别，已有内容已保留')
    if fill.get('reportId') is not None and str(fill['reportId']) != target['report_id']:
        raise RuntimeError('当前作答身份不一致，已有内容已保留')
    row = dict(data, reportId=target['report_id'])
    row.setdefault('reportName', data.get('name') or record.get('title'))
    # Detail and list endpoints use different names for the same boundaries.
    if 'endTime' in data and 'reportEndTime' not in row:
        row['reportEndTime'] = data['endTime']
    if 'startTime' in data and 'reportStartTime' not in row:
        row['reportStartTime'] = data['startTime']
    if fill.get('status') is not None:
        row['status'] = fill['status']
    candidate = candidate_from_row(row, target['origin'], target['fid'])
    if not candidate:
        raise RuntimeError('作业详情缺少必要字段，已有内容已保留')
    # A detail endpoint may omit fields available on the list. Absence is not an
    # instruction to clear a previously verified deadline or current state.
    if candidate['deadline_kind'] == 'unknown':
        for field in ('deadline', 'deadline_kind', 'deadline_source'):
            candidate.pop(field, None)
    if row.get('status') is None:
        candidate.pop('status', None)
        candidate.pop('status_checked_at', None)
    for field in ('course', 'start', 'published_at'):
        if not candidate.get(field):
            candidate.pop(field, None)
    details = dict(record.get('lab_report') or {})
    details.update({key: value for key, value in candidate['lab_report'].items() if value not in (None, '')})
    if data.get('uploadType') is not None:
        details['upload_type'] = data['uploadType']
    details['read_at'] = candidate['metadata_checked_at']
    # Reading an upload widget or child summaries is not a claim that a teacher's
    # experiment instructions, Word template, or external form have been read.
    details['requirements_verified'] = False
    candidate['lab_report'] = details
    candidate['entry_url'] = _entry(target['origin'], target['fid'], target['report_id'], details.get('course_id', ''),
                                    belong_type=details.get('belong_type'), is_virtual=details.get('is_virtual'),
                                    group_mode=details.get('group_mode'), report_type=details.get('type'))
    candidate['answer_url'] = candidate['entry_url']
    warnings = []
    if str(details.get('type')) in {'1', '2'}:
        try:
            defaults = _get(context, target['origin'], 'instructions', {
                'reportId': target['report_id'], 'deptId': target['fid'], 'isTemplateAnswer': 'false'})
            instructions = _form_prefill_instructions(defaults)
            if instructions is not None:
                details['instructions_source'] = 'form_prefill'
                details['instructions_read_at'] = details['read_at']
                candidate.update(instructions)
                if not instructions['instructions_text']:
                    warnings.append('表单预填说明为空，具体作业要求请在原网页或课程资料中核对')
            elif record.get('instructions_text') or record.get('instructions_html'):
                warnings.append('表单预填说明尚未返回，已保留上次读取的说明，请在原网页核对')
        except (RuntimeError, ValueError):
            # Optional default data must not suppress a verified current status
            # or erase cached requirements when only this request fails.
            warnings.append('表单预填说明尚未更新，已保留上次读取的说明，请在原网页核对')
    details['attachments_verified'] = True
    required_file_field = {'1': 'uploadImages', '2': 'currUploadFiles'}.get(str(details.get('upload_type'))) \
        if str(details.get('type')) == '2' else None
    for name, keys, role in [('student_attachments', ('currUploadFiles', 'uploadImages'), 'student'),
                             ('review_attachments', ('correctionFiles',), 'review')]:
        present = [key for key in keys if key in fill]
        if name == 'student_attachments' and (not present or (required_file_field and required_file_field not in fill)):
            # A partial status response is not proof that the upload control is
            # empty. Preserve its snapshot and refuse a potentially destructive
            # fill until the current control's field is explicitly returned.
            details['attachments_verified'] = False
            warnings.append('当前已上传文件尚未核实，已保留上次读取的附件，请刷新后再填入')
            continue
        if name == 'review_attachments' and not present:
            if details.get(name):
                warnings.append('批阅附件尚未更新，已保留上次读取的附件')
            continue
        try:
            details[name] = [file for key in present for file in _files(fill[key], target['origin'], role)]
        except ValueError as exc:
            # Keep the previous attachment list when one part cannot be decoded.
            warnings.append(str(exc) + '，已保留上次读取的附件')
            if name == 'student_attachments':
                details['attachments_verified'] = False
    if str(details.get('type')) == '3':
        sub = _get(context, target['origin'], 'children', {'reportId': target['report_id'], 'deptId': target['fid']})
        details['child_reports'] = _children(sub, target, details.get('course_id', ''))
    question = _upload_question(target['report_id'], details.get('upload_type')) if str(details.get('type')) == '2' else None
    if question:
        candidate.update(questions=[question], question_count=1, requirements=question['text'],
                         content_complete=True, content_status='ready',
                         content_last_read=details['read_at'], content_entry_checked=candidate['entry_url'])
        if not details['requirements_verified']:
            warnings.append('已识别上传字段；具体实验要求和模板请在原网页核对')
    else:
        warnings.append('已读取子项说明和状态；Word 正文与模板请在原网页核对和填写' if str(details.get('type')) == '3'
                        else '原网页的作答字段尚未识别，请打开原网页完成，已有内容已保留')
        candidate['content_status'] = warnings[-1]
    candidate['content_warnings'] = warnings
    return candidate
