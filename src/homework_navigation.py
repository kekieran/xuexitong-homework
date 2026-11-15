"""Resolve a single verified homework without submitting, saving or restarting it."""
from __future__ import annotations
import hashlib
import html
import json
import re
from urllib.parse import urljoin, urlsplit, urlencode
import homework_dom as dom
import homework_shared as shared


class NotInClassError(PermissionError):
    """Raised when Chaoxing explicitly says the current account is not in this class."""


def _check_permission(markup):
    if '您可能不在此班级' in markup or '没有权限答题' in markup:
        raise NotInClassError('您可能不在此班级，没有权限读取此作业')


identities = dom.record_identities


def _serialize(node):
    if isinstance(node, str):
        return html.escape(node)
    attrs = ''.join(' ' + k + '="' + html.escape(v or '', quote=True) + '"' for k, v in node.attrs.items())
    return '<' + node.tag + attrs + '>' + ''.join(_serialize(c) for c in node.children) + '</' + node.tag + '>'


def _literal_concat(expression, values):
    """Interpret only server literals and specifically known variables; no JS eval."""
    tokens = re.split(r'''((?:"[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*'))''', expression)
    output = ''
    for token in tokens:
        if not token.strip():
            continue
        if token[0] in '\"\'':
            output += token[1:-1].replace('\\/', '/')
        else:
            for name in token.split('+'):
                name = name.strip()
                if name:
                    if name not in values:
                        raise ValueError('作业入口包含尚未支持的参数，未猜测链接')
                    output += str(values[name])
    return html.unescape(output)


def _school_entry(context, record, markup, url, fetch_html):
    root = dom.parse(markup)
    anchors = root.all('a', cls='inspectTask')
    if not anchors:
        return None
    course, clazz = dom.course_class_pair(url)
    host = dom.platform_host(urlsplit(url).hostname)
    expected = identities(record)
    prefix = f'{host}:{course}:{clazz}:'
    wanted = {x.split(':')[-1] for x in expected if x.startswith(prefix) and x.split(':')[-2] in {'taskrefid', 'workid'}}
    rows = {}
    for a in anchors:
        task = a.attrs.get('data')
        if task not in wanted:
            continue
        row = a.parent
        while row is not None and row.tag not in {'li', 'tr'}:
            row = row.parent
        if row is None:
            raise ValueError('目标作业缺少独立列表行，无法核实')
        rows[id(row)] = (row, a)
    if len(rows) != 1:
        raise ValueError('课程列表中无法唯一定位目标作业，未读取其他作业')
    row, anchor = next(iter(rows.values()))
    task = anchor.attrs['data']
    row_anchors = row.all('a', cls='inspectTask')
    if any(a.attrs.get('data') != task for a in row_anchors):
        raise ValueError('作业列表行包含多个作业标识，已停止')
    row_titles = {shared.collapse_whitespace(a.attrs.get('title', '')) for a in row_anchors if a.attrs.get('title', '').strip()}
    if len(row_titles) > 1:
        raise ValueError('目标作业行含多个不同标题，已停止')
    if row_titles:
        record['title'] = next(iter(row_titles))
    redit = anchor.attrs.get('data3')
    if redit == '4':
        # A redo action is forbidden, but an exact list row may still prove
        # that this old assignment has expired or was already submitted.
        row_text = dom.clean_text(row)
        row_status = dom.explicit_status(row_text)
        view_url = None
        for link in row.all('a'):
            href = html.unescape((link.attrs.get('href') or '').strip())
            if not href or href.startswith('javascript:'):
                continue
            cand = urljoin(url, href)
            if dom.platform_url(cand) and re.search(r'selectworkquestion|/work/view', cand, re.I):
                c_ids = set(dom.url_identity(cand))
                if not c_ids or c_ids & expected or dom.course_class_pair(cand) == (course, clazz):
                    view_url = cand
                    break
        metadata_url = view_url or next((record.get(k) for k in ('entry_url', 'work_url', 'answer_url')
            if record.get(k) and dom.platform_url(record[k]) and set(dom.url_identity(record[k])) & expected), None)
        import homework_engine as engine
        if metadata_url and not re.search(r'/(?:getAllWork|task-list|courselistdata)(?:/|$)', urlsplit(metadata_url).path, re.I):
            engine.update_metadata(record, _serialize(row), metadata_url)
            record['deadline_source'] = 'list-row'
        elif row_status == 'submitted':
            start, deadline = engine.time_fields(row_text, record.get('start'))
            if start:
                record['start'] = start
            if deadline:
                record.update(deadline=deadline, deadline_kind='known', deadline_source='list-row')
                record.pop('metadata_needs_check', None)
            record['metadata_checked_at'] = engine.now_iso()
        if row_status == 'submitted':
            record['status'] = 'submitted'
            record['identities'] = sorted(expected | {prefix + 'taskrefid:' + task})
            if view_url:
                record['work_url'] = view_url
            return ('_SUBMITTED_', view_url or metadata_url or url, _serialize(row))
    if redit not in {'0', '1'}:
        raise ValueError('此入口需要重做或不支持安全读取，请在原网页处理')
    # Use only the click handler shipped with this very course list.
    handler = re.search(r'''\$\("\.inspectTask"\)\.click\(function\(\)\s*\{([\s\S]*?)\n\s*\}\);\s*\n\s*\}\);''', markup)
    if not handler:
        raise ValueError('课程作业入口结构已变化，无法安全读取')
    script = handler.group(1)
    for name, expected_value in [('courseId', course), ('classId', clazz)]:
        found = re.search(r'var\s+' + name + r'\s*=\s*[\"\']?(\d+)', script)
        if not found or found.group(1) != expected_value:
            raise ValueError('课程列表与作业入口身份不一致')
    cp = re.search(r'''_CP_\s*=\s*['"]([^'"]+)['"]''', markup)
    cpi = re.search(r'var\s+cpi\s*=\s*[\"\']?(\d+)', script)
    if not cp or not cpi or '"/work/isExpire"' not in script:
        raise ValueError('未识别到平台作业资格检查入口')
    params = {'classId': clazz, 'courseId': course, 'workRelationId': task, 'cpi': cpi.group(1)}
    gate_url = urljoin(url, cp.group(1) + '/work/isExpire?' + urlencode(params))
    gate_markup, gate_final = fetch_html(context, gate_url)
    if urlsplit(gate_final).path != urlsplit(gate_url).path:
        raise ValueError('作业资格检查发生意外跳转')
    try:
        gate = json.loads(gate_markup)
    except (ValueError, TypeError):
        raise ValueError('平台未返回有效作业资格信息') from None
    if str(gate.get('status')) != '0':
        raise ValueError('平台尚未允许进入该作业，请在原网页检查课程要求')
    expressions = re.findall(r'url\s*=\s*([^;]+);', script)
    expressions = [x for x in expressions if '/work/doHomeWorkNew?' in x and ('&reEdit=1' in x) == (redit == '1')]
    if len(expressions) != 1:
        raise ValueError('未找到唯一的服务器作业入口')
    values = {'_HOST_CP2_': cp.group(1), 'courseId': course, 'classId': clazz,
              'workRelationId': task, 'workRelationAnswerId': anchor.attrs.get('data2', '0')}
    entry = urljoin(url, _literal_concat(expressions[0], values) + str(gate.get('standardEnc') or ''))
    if not dom.platform_url(entry):
        raise ValueError('作业入口不属于已支持的平台')
    # This matched row is the only source allowed for list metadata.
    import homework_engine as engine
    engine.update_metadata(record, _serialize(row), entry)
    record['deadline_source'] = 'list-row'
    if re.search(r'待做', dom.clean_text(row)):
        record['status'] = 'pending'
    record['identities'] = sorted(expected | {prefix + 'taskrefid:' + task, prefix + 'workid:' + task})
    return entry



_ALIAS_TARGET_PATHS = {
    '/mooc-ans/mooc2/work/view',
    '/mooc-ans/mooc2/work/dowork',
    '/mooc-ans/work/dohomeworknew',
    '/mooc-ans/work/phone/task-work',
    '/work/phone/task-work',
    '/mooc-ans/work/phone/dohomework',
    '/work/phone/dohomework',
    '/mooc-ans/work/phone/selectworkquestionyipiyue',
    '/work/phone/selectworkquestionyipiyue',
    '/mooc-ans/work/selectworkquestionyipiyue',
    '/work/selectworkquestionyipiyue',
}


def _redirect_aliases(record, entry, final, markup):
    """An observed official task entry redirect may prove a task/work alias.

    Require matching scope and agreement between final URL and form; numeric
    equality alone is never used as evidence.
    """
    start = urlsplit(entry)
    end = urlsplit(final)
    q = dom.query_params(entry)
    start_path = start.path.lower()
    valid_source = (
        (start_path.endswith('/examapi/intoexamorwork') and q.get('workorexam') == 'work')
        or (start_path.endswith('/android/mtaskmsgspecial') and q.get('type') == 'work')
        or (start_path.endswith('/work/phone/task-work') and bool(q.get('taskrefid')))
    )
    if not valid_source:
        return set()
    if end.path.lower() not in _ALIAS_TARGET_PATHS:
        return set()
    known = identities(record)
    source = set(dom.url_identity(entry)) & known
    source = {x for x in source if ':taskrefid:' in x}
    targets = {x for x in dom.url_identity(final) if ':workid:' in x}
    if len(source) != 1 or len(targets) != 1:
        return set()
    task, work = next(iter(source)), next(iter(targets))
    scope = task.rsplit(':', 2)[0]
    if work.rsplit(':', 2)[0] != scope:
        return set()
    f = {str(k).lower(): v for k,v in dom.fields(dom.parse(markup)).items() if k}
    parts = work.split(':')
    if (f.get('courseid'), f.get('classid',f.get('clazzid')), f.get('workid')) != (parts[1],parts[2],parts[4]):
        return set()
    if any(x.rsplit(':',1)[0] == work.rsplit(':',1)[0] and x != work for x in known):
        return set()
    record['identity_source'] = 'official-entry-redirect-and-form'
    return {task, work}


def _update_verified_title(record, markup):
    root = dom.parse(markup)
    nodes = root.all('h2', cls='mark_title')
    if not nodes:
        for top in root.all('div', cls='CyTop'):
            for nav in top.all('ul', cls='ul01'):
                nodes.extend(nav.all('a'))
    titles = {shared.collapse_whitespace(dom.clean_text(n)) for n in nodes if dom.clean_text(n).strip()}
    if len(titles) == 1:
        record['title'] = next(iter(titles))


def _direct_work_priority(url, known_ids):
    """Rank verified direct do-homework URLs ahead of slow redirect/list URLs."""
    path = urlsplit(url).path.lower()
    if '/view' in path or 'selectworkquestion' in path:
        return None
    if '/dohomeworknew' not in path and '/dohomework' not in path:
        return None
    url_ids = set(dom.url_identity(url))
    if url_ids and not (url_ids & known_ids):
        return None
    return 0 if '/dohomeworknew' in path else (2 if '/phone/' in path else 1)


def _prioritized_work_urls(urls, known_ids):
    ranked = []
    fallback = []
    for url in urls:
        prio = _direct_work_priority(url, known_ids)
        if prio is not None:
            ranked.append((prio, url))
        else:
            fallback.append(url)
    ranked.sort(key=lambda item: item[0])
    return [u for _, u in ranked] + fallback


def _walk_work_entry(context, record, fetch_html, entry, urls, seen):
    for _ in range(6):
        if entry in seen:
            raise ValueError('作业页面发生循环跳转，未读取其他作业')
        seen.add(entry)
        markup, final = fetch_html(context, entry)
        if not dom.platform_url(final) or dom.is_login(markup, final):
            raise ValueError('作业登录或平台地址校验未通过')
        _check_permission(markup)
        school = _school_entry(context, record, markup, final, fetch_html)
        if isinstance(school, tuple) and school[0] == '_SUBMITTED_':
            _, resolved_url, row_markup = school
            return row_markup, resolved_url
        if school:
            entry = school
            continue
        if '/getallwork' in urlsplit(final).path.lower():
            raise ValueError('课程列表没有唯一可验证的作业入口')
        actual = set(dom.page_identity(markup, final))
        if not actual.intersection(identities(record)):
            aliases = _redirect_aliases(record, entry, final, markup)
            if not aliases:
                raise ValueError('网页作业标识与目标不一致，已停止读取')
            record['identities'] = sorted(identities(record) | aliases)
        _update_verified_title(record, markup)
        record['identities'] = sorted(identities(record) | actual)
        if dom.parse_questions(markup, final) and '/phone/' not in urlsplit(final).path:
            return markup, final
        # An already observed PC URL is preferred to incomplete mobile paging.
        pc = next((u for u in urls if u not in seen and '/dohomeworknew' in urlsplit(u).path.lower() and set(dom.url_identity(u)) & identities(record)), None)
        if pc:
            entry = pc
            continue
        phone = dom.phone_entry(markup, final)
        if phone and phone not in seen and dom.platform_url(phone):
            entry = phone
            continue
        if '/selectworkquestionyipiyue' in urlsplit(final).path.lower():
            report_detail = dom.phone_report_detail(markup, final)
            if report_detail and report_detail not in seen and dom.platform_url(report_detail):
                entry = report_detail
                continue
        return markup, final
    raise ValueError('作业入口跳转次数过多，已停止读取')


def resolve_work_page(context, record, fetch_html):
    """Return (markup, final_url) for this record only; fail closed on ambiguity.

    School list metadata is applied from the unique matched row. Identity aliases
    are added only when that row and its server handler prove the relationship.
    """
    urls = [u for u in dom.record_urls(record) if dom.platform_url(u)]
    if not urls or not identities(record):
        raise ValueError('尚未取得可验证的作业入口')
    ordered = _prioritized_work_urls(urls, identities(record))
    seen = set()
    try:
        return _walk_work_entry(context, record, fetch_html, ordered[0], urls, seen)
    except NotInClassError:
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        if ordered[0] == urls[0] or str(exc) == '作业登录或平台地址校验未通过' or urls[0] in seen:
            raise
        return _walk_work_entry(context, record, fetch_html, urls[0], urls, seen)


_STATUS_ONLY_RE = re.compile(r'^(?:未交|未提交|已交|已提交|已完成|待批阅|已批阅|已过期|未开始|待作答)$')


def _input_by_id_or_name(markup, key):
    root = dom.parse(markup)
    for node in root.all('input'):
        if node.attrs.get('id') == key or node.attrs.get('name') == key:
            val = (node.attrs.get('value') or '').strip()
            if val:
                return val
    return ''


def _extract_cpi(record, work_url, work_markup):
    for candidate_url in [work_url, *dom.record_urls(record)]:
        cpi = dom.query_params(candidate_url).get('cpi')
        if cpi and cpi.isdigit():
            return cpi
    val = _input_by_id_or_name(work_markup, 'cpi')
    if val.isdigit():
        return val
    found = re.search(r'\bvar\s+cpi\s*=\s*["\']?(\d+)', work_markup)
    return found.group(1) if found else ''


def _mooc2_card_title(li):
    for cls_name in ('overHidden2', 'work-name', 'tit'):
        node = li.first(cls=cls_name)
        if node is not None:
            txt = shared.collapse_whitespace(dom.clean_text(node))
            if txt and not _STATUS_ONLY_RE.fullmatch(txt):
                return txt
    for p in li.all('p'):
        txt = shared.collapse_whitespace(dom.clean_text(p))
        if txt and not _STATUS_ONLY_RE.fullmatch(txt):
            return txt
    return ''


def _clean_task_url(raw):
    text = html.unescape(str(raw or ''))
    return text.replace('\u200b', '').replace('\ufeff', '').strip()


def _safe_request_text(context, url, referer=''):
    import homework_lab as lab
    resp = None
    headers = {'Referer': referer} if referer else {}
    try:
        resp = context.request.get(url, headers=headers, timeout=15000)
        if resp.status >= 400:
            raise RuntimeError(f'服务器返回 HTTP {resp.status}')
        return resp.text(), resp.url
    finally:
        lab.dispose_response(resp)


def _fetch_renwu_task(context, clean_url, record, card_title, card_text, asset=None):
    import homework_engine as engine
    q = dom.query_params(clean_url)
    host = urlsplit(clean_url).hostname or ''
    course_id = q.get('courseid') or dom.course_class_pair(clean_url)[0]
    class_id = q.get('classid') or q.get('clazzid') or dom.course_class_pair(clean_url)[1]
    active_id = q.get('activeid') or ''
    fid = q.get('fid') or ''
    if not host or not active_id or not course_id or not class_id:
        return None
    api_url = f'https://{host}/v2/apis/task/getTaskActiveInfo?' + urlencode({
        'courseId': course_id,
        'classId': class_id,
        'activeId': active_id,
        'fid': fid,
    })
    raw_json, _ = _safe_request_text(context, api_url, referer=clean_url)
    try:
        payload = json.loads(raw_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or str(payload.get('result')) != '1' or not isinstance(payload.get('data'), dict):
        return None
    data = payload['data']
    title = shared.collapse_whitespace(data.get('name') or card_title or record.get('title') or '实践任务')
    raw_content = str(data.get('content') or '')
    content_root = dom.parse(raw_content)
    cleaned_html = dom.safe_html(content_root, clean_url, asset)
    body_text = dom.clean_text(content_root)

    mode_note = '分组任务（仅组长可提交，请在原网页查看小组成员与提交要求）' if data.get('onlyLeaderSubmit') else '分组/实践任务（需在学习通原网页完成提交）'
    full_text = f'{title}\n{mode_note}' + (f'\n{body_text}' if body_text else '')
    full_html = f'<p><strong>{html.escape(mode_note)}</strong></p>' + (
        cleaned_html or '<p>请点击下方「前往交作业」在学习通原页面查看任务详情并提交。</p>'
    )

    attachments = {}
    for anchor in dom.parse(cleaned_html).all('a'):
        href = anchor.attrs.get('href') or ''
        if href.startswith(('https://', 'http://')) and not dom.platform_page_url(href):
            attachments.setdefault(href, {'name': dom.clean_text(anchor) or '任务附件', 'url': href})
    for key_name in ('attachments', 'fileList', 'files'):
        items = data.get(key_name)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            href = str(item.get('url') or item.get('download') or '').strip()
            if href.startswith(('https://', 'http://')) and not dom.platform_page_url(href):
                name = shared.collapse_whitespace(item.get('name') or item.get('fileName') or '任务附件')
                attachments.setdefault(href, {'name': name, 'url': href})

    start = engine.standard_time(data.get('starttimeStr'), record.get('start'))
    deadline = engine.standard_time(data.get('endtimeStr'), start or record.get('start'))
    card_status = dom.explicit_status(card_text)
    if card_status == 'unknown' and re.search(r'待做', card_text):
        card_status = 'pending'

    qid = f'renwu_{active_id}'
    sig = hashlib.sha256(
        json.dumps([qid, 'renwu_task', title, body_text, clean_url], ensure_ascii=False, separators=(',', ':')).encode()
    ).hexdigest()[:24]
    question = {
        'id': qid,
        'type': 'essay',
        'type_code': '4',
        'label': '实践/分组任务',
        'text': full_text,
        'html': full_html,
        'options': [],
        'blank_names': [],
        'blank_count': 0,
        'signature': sig,
    }
    return {
        'mode': 'renwu_task',
        'answer_url': clean_url,
        'title': title,
        'start': start,
        'deadline': deadline,
        'card_status': card_status,
        'attachments': list(attachments.values()),
        'question': question,
        'renwu_task': {
            'active_id': active_id,
            'course_id': course_id,
            'class_id': class_id,
            'fid': fid,
            'host': host,
            'only_leader_submit': bool(data.get('onlyLeaderSubmit')),
            'group_type': data.get('groupType'),
            'if_commit': data.get('ifcommit'),
        },
        'identity': f'{dom.platform_host(host)}:{course_id}:{class_id}:activeid:{active_id}',
    }


def resolve_empty_work_fallback(context, record, work_url, work_markup, fetch_html, asset=None):
    """Resolve empty legacy doHomeWorkNew pages via mooc2/work/list (read-only)."""
    _check_permission(work_markup)
    course_id, class_id = dom.course_class_pair(work_url)
    host = urlsplit(work_url).hostname or ''
    if not (course_id and class_id and host):
        for candidate_url in dom.record_urls(record):
            c, cl = dom.course_class_pair(candidate_url)
            h = urlsplit(candidate_url).hostname or ''
            if c and cl and h:
                course_id, class_id, host = c, cl, h
                break
    if not (course_id and class_id and host):
        return None
    cpi = _extract_cpi(record, work_url, work_markup)
    sc_url = f'https://{host}/mooc-ans/mycourse/studentcourse?courseId={course_id}&clazzid={class_id}'
    if cpi:
        sc_url += f'&cpi={cpi}'
    sc_url += '&ut=s&v=2&fromMiddle=1'
    if not dom.platform_url(sc_url):
        return None
    try:
        sc_markup, sc_final = fetch_html(context, sc_url)
    except Exception:
        return None
    work_enc = _input_by_id_or_name(sc_markup, 'workEnc')
    if not work_enc:
        return None
    cpi = cpi or _input_by_id_or_name(sc_markup, 'cpi') or dom.query_params(sc_final).get('cpi', '')
    wl_url = f'https://{host}/mooc-ans/mooc2/work/list?' + urlencode({
        'courseId': course_id,
        'classId': class_id,
        'cpi': cpi,
        'ut': 's',
        'enc': work_enc,
    })
    try:
        wl_markup, wl_final = _safe_request_text(context, wl_url, referer=sc_final)
    except Exception:
        return None
    if not wl_markup.strip() or dom.is_login(wl_markup, wl_final):
        return None

    cards = []
    for li in dom.parse(wl_markup).all('li', attr='data'):
        raw_url = _clean_task_url(li.attrs.get('data'))
        if not raw_url:
            continue
        clean_url = urljoin(wl_final, raw_url)
        cards.append({
            'url': clean_url,
            'title': _mooc2_card_title(li),
            'card_text': dom.clean_text(li),
            'ids': set(dom.url_identity(clean_url)),
        })

    expected_ids = identities(record)
    id_matches = [c for c in cards if c['ids'] & expected_ids]
    matched = id_matches[0] if len(id_matches) == 1 else None
    if matched is None:
        target_title = shared.collapse_whitespace(record.get('title') or '').lower()
        if target_title:
            title_matches = [c for c in cards if c['title'] and shared.collapse_whitespace(c['title']).lower() == target_title]
            if len(title_matches) == 1:
                matched = title_matches[0]
    if matched is None:
        return None

    clean_url = matched['url']
    if not dom.platform_url(clean_url):
        return None
    parsed = urlsplit(clean_url)
    q = dom.query_params(clean_url)
    if '/page/renwu' in (parsed.path or '').lower() or q.get('activeid'):
        try:
            return _fetch_renwu_task(context, clean_url, record, matched['title'], matched['card_text'], asset=asset)
        except Exception:
            return None

    try:
        m2, f2 = fetch_html(context, clean_url)
    except Exception:
        return None
    _check_permission(m2)
    fb_status = dom.explicit_status(matched['card_text'])
    if fb_status == 'unknown' and re.search(r'待做', matched['card_text']):
        fb_status = 'pending'
    return {'mode': 'html', 'markup': m2, 'final': f2, 'card_status': fb_status}
