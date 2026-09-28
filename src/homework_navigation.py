"""Resolve a single verified homework without submitting, saving or restarting it."""
from __future__ import annotations
import html
import json
import re
from urllib.parse import urljoin, urlsplit, parse_qs, urlencode
import homework_dom as dom


def identities(record):
    result = set(record.get('identities') or [])
    for key in ('entry_url', 'answer_url', 'work_url', 'url', 'list_url'):
        result.update(dom.url_identity(record.get(key) or ''))
    return result


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
    q = {k.lower(): v[0] for k, v in parse_qs(urlsplit(url).query).items() if v}
    course, clazz = q.get('courseid'), q.get('classid', q.get('clazzid'))
    host = urlsplit(url).hostname or ''
    host = 'chaoxing' if host == 'chaoxing.com' or host.endswith('.chaoxing.com') else host
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
    row_titles = {re.sub(r'\s+', ' ', a.attrs.get('title', '')).strip() for a in row_anchors if a.attrs.get('title', '').strip()}
    if len(row_titles) > 1:
        raise ValueError('目标作业行含多个不同标题，已停止')
    if row_titles:
        record['title'] = next(iter(row_titles))
    redit = anchor.attrs.get('data3')
    if redit == '4':
        # A redo action is forbidden, but an exact list row may still prove
        # that this old assignment has expired or was already submitted.
        metadata_url = next((record.get(k) for k in ('entry_url', 'work_url', 'answer_url')
            if record.get(k) and dom.platform_url(record[k]) and set(dom.url_identity(record[k])) & expected), None)
        if metadata_url:
            import homework_engine as engine
            engine.update_metadata(record, _serialize(row), metadata_url)
            record['deadline_source'] = 'list-row'
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



def _redirect_aliases(record, entry, final, markup):
    """An observed official task entry redirect may prove a task/work alias.

    Require matching scope and agreement between final URL and form; numeric
    equality alone is never used as evidence.
    """
    start = urlsplit(entry)
    end = urlsplit(final)
    q = {k.lower(): v[0] for k,v in parse_qs(start.query).items() if v}
    if not start.path.lower().endswith('/examapi/intoexamorwork') or q.get('workorexam') != 'work':
        return set()
    if end.path.lower() not in {'/mooc-ans/mooc2/work/view', '/mooc-ans/mooc2/work/dowork', '/mooc-ans/work/dohomeworknew'}:
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
    titles = {re.sub(r'\s+', ' ', dom.clean_text(n)).strip() for n in nodes if dom.clean_text(n).strip()}
    if len(titles) == 1:
        record['title'] = next(iter(titles))


def resolve_work_page(context, record, fetch_html):
    """Return (markup, final_url) for this record only; fail closed on ambiguity.

    School list metadata is applied from the unique matched row. Identity aliases
    are added only when that row and its server handler prove the relationship.
    """
    urls = [record.get(k) for k in ('entry_url', 'answer_url', 'work_url', 'url', 'list_url')]
    urls = list(dict.fromkeys(u for u in urls if u and dom.platform_url(u)))
    if not urls or not identities(record):
        raise ValueError('尚未取得可验证的作业入口')
    seen = set()
    entry = urls[0]
    for _ in range(6):
        if entry in seen:
            raise ValueError('作业页面发生循环跳转，未读取其他作业')
        seen.add(entry)
        markup, final = fetch_html(context, entry)
        if not dom.platform_url(final) or dom.is_login(markup, final):
            raise ValueError('作业登录或平台地址校验未通过')
        school = _school_entry(context, record, markup, final, fetch_html)
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
        return markup, final
    raise ValueError('作业入口跳转次数过多，已停止读取')
