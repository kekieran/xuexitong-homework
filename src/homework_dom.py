"""Read-only HTML parsing and safe, structured homework content (standard library)."""
from __future__ import annotations
import hashlib
import html
import re
import json
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, parse_qs

VOID = {'img', 'input', 'br', 'hr', 'meta', 'link', 'source', 'wbr', 'area', 'base', 'embed', 'param', 'col'}
BLOCK = {'div', 'p', 'li', 'h1', 'h2', 'h3', 'h4', 'tr', 'br', 'section'}

class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def all(self, tag=None, cls=None, attr=None):
        result = []
        for n in self.children:
            if isinstance(n, Node):
                if (tag is None or n.tag == tag) and (cls is None or cls in n.attrs.get('class', '').split()) and (attr is None or attr in n.attrs):
                    result.append(n)
                result.extend(n.all(tag, cls, attr))
        return result

    def first(self, tag=None, cls=None, attr=None):
        return next(iter(self.all(tag, cls, attr)), None)

    def text(self):
        if self.tag in {'script', 'style', 'noscript'}:
            return ''
        content = ''.join(c.text() if isinstance(c, Node) else c for c in self.children)
        return content + ('\n' if self.tag in BLOCK else '')

class Parser(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == 'li' and self.stack[-1].tag == 'li':
            self.stack.pop()
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

def parse(markup):
    return Parser(markup).root

def clean_text(node):
    return re.sub(r'[ \t\xa0]+', ' ', node.text()).strip()

def fields(root):
    return {n.attrs.get('name') or n.attrs.get('id'): n.attrs.get('value', '') for n in root.all('input')}

def platform_url(url):
    try:
        u = urlsplit(url)
        return u.scheme in {'https', 'http'} and (u.hostname == 'chaoxing.com' or (u.hostname or '').endswith('.chaoxing.com') or u.hostname in {'mooc.istudy.szpu.edu.cn', 'passport.istudy.szpu.edu.cn'})
    except ValueError:
        return False

def url_identity(url):
    """Taskref and work IDs are aliases, not interchangeable numbers."""
    try:
        u = urlsplit(url or '')
        q = {k.lower(): v[0] for k, v in parse_qs(u.query).items() if v}
        host = 'chaoxing' if (u.hostname or '').endswith('.chaoxing.com') else u.hostname or ''
        course, clazz = q.get('courseid', ''), q.get('classid', q.get('clazzid', ''))
        return [f'{host}:{course}:{clazz}:{k}:{q[k]}' for k in ('taskrefid', 'workid') if q.get(k) and course and clazz]
    except ValueError:
        return []


# Keep the candidate URL order and identity inputs identical for navigation and
# answer filling. Legacy field names remain part of the record contract.
RECORD_URL_FIELDS = ('entry_url', 'answer_url', 'work_url', 'url', 'list_url')

def record_urls(record):
    return [record.get(field) for field in RECORD_URL_FIELDS if record.get(field)]

def record_identities(record):
    result = set(record.get('identities') or [])
    for url in record_urls(record):
        result.update(url_identity(url))
    return result

ALLOWED = set('p div span br strong b em i u sub sup ol ul li table thead tbody tr td th blockquote pre code h3 h4 img a audio video source math mi mn mo mrow mfrac msqrt mroot msub msup msubsup munder mover munderover mtable mtr mtd mtext semantics annotation'.split())
DROP = {'script', 'style', 'input', 'textarea', 'button', 'iframe', 'object', 'embed', 'select'}

def safe_html(node, base_url='', asset=None):
    if isinstance(node, str):
        return html.escape(node)
    if node.tag == 'iframe':
        src = urljoin(base_url, node.attrs.get('src') or '')
        if src.startswith(('https://', 'http://')):
            return f'<a href="{html.escape(src, quote=True)}" target="_blank" rel="noopener noreferrer">打开题目中的附件或媒体</a>'
        return ''
    if node.tag in DROP:
        return ''
    inside = ''.join(safe_html(c, base_url, asset) for c in node.children)
    if node.tag not in ALLOWED:
        return inside
    attrs = ''
    if node.tag == 'img':
        src = node.attrs.get('data-original') or node.attrs.get('data-src') or node.attrs.get('src') or ''
        src = urljoin(base_url, src)
        if not src.startswith(('https://', 'http://', 'data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/gif;base64,')):
            return html.escape(node.attrs.get('alt', '[图片]'))
        if asset and not src.startswith('data:'):
            src = asset(src)
        attrs += f' src="{html.escape(src, quote=True)}" loading="lazy"'
        attrs += f' alt="{html.escape(node.attrs.get("alt") or "题目原图", quote=True)}"'
    elif node.tag == 'a':
        href = urljoin(base_url, node.attrs.get('href') or '')
        if href.startswith(('https://', 'http://')):
            attrs += f' href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer"'
    elif node.tag in {'audio', 'video', 'source'}:
        src = urljoin(base_url, node.attrs.get('src') or '')
        if src.startswith(('https://', 'http://')):
            attrs += f' src="{html.escape(src, quote=True)}"'
        if node.tag != 'source':
            attrs += ' controls preload="none"'
    for key in ('colspan', 'rowspan'):
        value = node.attrs.get(key, '')
        if value.isdigit():
            attrs += f' {key}="{value}"'
    if node.tag == 'annotation':
        attrs += ' encoding="application/x-tex"'
    return f'<{node.tag}{attrs}>' + ('' if node.tag in VOID else inside + f'</{node.tag}>')

# Codes above 8 include compound, matching, ordering and language exercises.
# They must never silently become a free-text answer with the wrong encoding.
TYPE_MAP = {'0': 'single', '1': 'multiple', '2': 'blank', '3': 'judgment', '4': 'essay', '5': 'essay', '6': 'essay', '7': 'essay', '8': 'essay'}
TYPE_LABELS = {'single': '单选题', 'multiple': '多选题', 'blank': '填空题', 'judgment': '判断题', 'essay': '简答 / 论述题', 'upload': '上传题', 'unsupported': '特殊题型'}

def question_nodes(root):
    candidates = [n for n in root.all() if set(n.attrs.get('class', '').split()) & {'singleQuesId', 'questionLi', 'TiMu'}]
    # Some PC skins wrap questionLi in TiMu. Keep the innermost actual question.
    return [n for n in candidates if not any(child in candidates for child in n.all())]


def question_id(n):
    f = fields(n)
    choices = [n.attrs.get('data'), n.attrs.get('data-questionid'), n.attrs.get('data-question-id'), f.get('questionId')]
    choices += [m.group(1) for k in f if (m := re.fullmatch(r'(?:answer)?type(\d+)', k or ''))]
    return next((str(v) for v in choices if re.fullmatch(r'\d+', str(v or ''))), '')


def _content_fingerprint(node, base_url=''):
    """Ignore layout wrappers; include original media, not just its alt text."""
    text = re.sub(r'\s+', ' ', clean_text(node)).strip()
    text = re.sub(r'^\d+\s*[.、．]\s*', '', text)
    text = re.sub(r'^[(（【]?(?:单选题|多选题|判断题|填空题|简答题|论述题|计算题)[)）】]?[\s:：]*', '', text)
    media = [urljoin(base_url, x.attrs.get('data-original') or x.attrs.get('data-src') or x.attrs.get('src') or '') for x in node.all() if x.tag in {'img', 'iframe', 'audio', 'video', 'source'}]
    maths = [safe_html(x, base_url) for x in node.all('math')]
    links = [urljoin(base_url, x.attrs.get('href', '')) for x in node.all('a') if x.attrs.get('href')]
    return [text, media, maths, links]

def parse_questions(markup, base_url='', asset=None):
    root = parse(markup)
    result = []
    for n in question_nodes(root):
        f = fields(n)
        qid = question_id(n)
        stem = n.first(cls='workTextWrap') or n.first(cls='timuStyle') or n.first(cls='mark_name') or n.first(cls='Zy_TItle')
        if not stem or not qid:
            continue
        text = clean_text(stem)
        if not text and not any(stem.all(tag) for tag in ('img', 'math', 'audio', 'video', 'iframe')):
            continue
        code = f.get('type' + qid, f.get('answertype' + qid, f.get('questionType', f.get('questionDataType', ''))))
        kind = TYPE_MAP.get(code, 'unsupported')
        if not code:
            heading = n.first(cls='mark_name') or n.first(cls='titType') or stem
            type_text = n.attrs.get('typename', '') + ' ' + clean_text(heading)[:70]
            for label, value in [('多选', 'multiple'), ('单选', 'single'), ('判断', 'judgment'), ('填空', 'blank'), ('简答', 'essay'), ('论述', 'essay'), ('计算', 'essay')]:
                if label in type_text:
                    kind = value
                    code = {'single': '0', 'multiple': '1', 'blank': '2', 'judgment': '3', 'essay': '4'}[kind]
                    break
        options = []
        candidates = n.all(cls='answerBg') or n.all(cls='answerList')
        # PC: ul.mark_letter > li; phone: div.answerList.radio / .check.
        if not candidates:
            for container in n.all(cls='mark_letter') + n.all(cls='stem_answer'):
                candidates.extend(container.all('li'))
        if not candidates and kind in {'single', 'multiple', 'judgment'}:
            candidates = n.all('li')
        for opt in candidates:
            opt_text = clean_text(opt)
            radio = next((x for x in opt.all('input') if x.attrs.get('type', '').lower() in {'radio', 'checkbox'}), None)
            label = (radio.attrs.get('value') if radio else None) or opt.attrs.get('data') or opt.attrs.get('data-value') or ''
            marker = opt.first(cls='num_option') or opt.first(cls='num_option_dx')
            if not label and marker:
                label = marker.attrs.get('data') or marker.attrs.get('data-value') or clean_text(marker)
            prefix = re.match(r'^\s*([A-Z])\s*[.．、:：\s]', opt_text)
            if not label and prefix:
                label = prefix.group(1)
            label = str(label or '').strip()
            if kind == 'judgment' and label.lower() in {'true', 'false'}:
                label = label.lower()
            if not re.fullmatch(r'[A-Z]|true|false|0|1', label):
                continue
            if any(o['value'] == label for o in options):
                continue
            content = opt.first(cls='after') or opt.first(cls='answer_p') or opt.first(cls='optionContent') or opt
            options.append({'value': label, 'text': clean_text(content), 'html': safe_html(content, base_url, asset), '_fingerprint': _content_fingerprint(content, base_url)})
        if kind == 'judgment' and not options:
            options = [{'value': 'true', 'text': '正确', 'html': '正确'}, {'value': 'false', 'text': '错误', 'html': '错误'}]
        # 'answertype{qid}' is platform metadata, never an answer blank.
        # Match supported answer field shapes for this exact question instead
        # of treating every field beginning with 'answer' as an editor.
        answer_pattern = re.compile(r'(?:answers?' + re.escape(qid) + r'(?:[_-][\w-]+)?|answerEditor' + re.escape(qid) + r'(?:[_-]?\d+)?|answerEditor)')
        def answer_name(control):
            return next((control.attrs.get(key) for key in ('name', 'id')
                         if answer_pattern.fullmatch(control.attrs.get(key, ''))), None)
        answers = [x for x in n.all() if x.tag in {'textarea', 'input'}
                   and x.attrs.get('type', '').lower() not in {'radio', 'checkbox'} and answer_name(x)]
        blank_names = list(dict.fromkeys(answer_name(x) for x in answers
                                        if re.match(r'answers?\d+[_-]', answer_name(x))))
        if kind == 'blank' and not blank_names:
            blank_names = list(dict.fromkeys(answer_name(x) for x in answers
                                            if answer_name(x) != 'answer' + qid))
        # The school PC page explicitly declares the number of UEditor blanks.
        # Prefer those editors over any hidden aggregate or mirror field.
        school_count = f.get('tiankongsize' + qid, '')
        if kind == 'blank' and str(school_count).isdigit() and 0 < int(school_count) <= 200:
            exact = ['answerEditor' + qid + str(i) for i in range(1, int(school_count) + 1)]
            if all(any(answer_name(x) == name for x in answers) for name in exact):
                blank_names = exact
        # Phone blankNum lists the exact suffix for each blank; do not infer by order.
        suffixes = f.get('blankNum' + qid, '')
        if kind == 'blank' and suffixes:
            exact = ['answer' + qid + s for s in suffixes.split(',') if s]
            if exact and all(any(x.attrs.get('name') == name or x.attrs.get('id') == name for x in answers) for name in exact):
                blank_names = exact
        # Upload-only questions are commonly reported with an unknown type
        # code, but their question container exposes a native file input.  Keep
        # them distinct so saved attachments can be filled automatically.
        has_file_input = any(x.tag == 'input' and x.attrs.get('type', '').lower() == 'file' for x in n.all())
        if kind == 'unsupported' and has_file_input:
            kind = 'upload'
        if kind in {'single', 'multiple'} and not options:
            kind = 'unsupported'
        fingerprint = [qid, code or kind, _content_fingerprint(stem, base_url), [(o['value'], o.pop('_fingerprint', o['text'])) for o in options], blank_names]
        signature = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:24]
        result.append({'id': qid, 'type': kind, 'type_code': code, 'label': TYPE_LABELS[kind], 'text': text,
                       'html': safe_html(stem, base_url, asset), 'options': options, 'blank_names': blank_names,
                       'blank_count': len(blank_names) or (1 if kind == 'blank' else 0), 'signature': signature})
    return result


def question_diagnostics(markup, questions=None):
    """Report partial/ambiguous parses without claiming a phone page is the full work."""
    root = parse(markup)
    questions = parse_questions(markup) if questions is None else questions
    f = fields(root)
    counts = [int(f[k]) for k in ('questionCount', 'totalQuestion', 'totalQuestionCount', 'questionNum', 'totalCount') if str(f.get(k, '')).isdigit()]
    # Section headings such as 单选题（共10题） are not the whole work.
    # Read only explicitly named totals outside question bodies.
    question_roots = set(question_nodes(root))
    def summary_text(node):
        if isinstance(node, str):
            return node
        if node in question_roots or node.tag in {'script', 'style'}:
            return ''
        return ''.join(summary_text(child) for child in node.children) + ('\n' if node.tag in BLOCK else '')
    text = summary_text(root)
    if not counts:
        counts = [int(v) for v in re.findall(r'(?:总题数|题目总数|题量)\s*[：:]?\s*(\d+)\s*(?:题|道)?', text)]
    expected = max(counts) if counts else None
    ids = [q['id'] for q in questions]
    reasons = []
    if len(ids) != len(set(ids)):
        reasons.append('页面出现重复题目编号，不能安全对应答案')
    nodes = question_nodes(root)
    if len(questions) < len(nodes):
        reasons.append('部分题目未能识别，请在原网页核对')
    if expected is not None and len(questions) != expected:
        reasons.append(f'页面标示 {expected} 题，目前只识别到 {len(questions)} 题')
    phone = bool(root.all(cls='singleQuesId')) and 'getTheNextQuestion' in markup
    # Single-question mobile navigation does not expose a trustworthy total on
    # every platform version. Do not mistake that page for the complete work.
    if phone and expected is None:
        reasons.append('当前为手机分页题目，尚未核实作业全部题数')
    return {'expected_count': expected, 'parsed_count': len(questions), 'complete': bool(questions) and not reasons, 'warnings': reasons}


def page_identity(markup, url):
    """Only URL/form identity fields; never mine arbitrary scripts or question text."""
    result = set(url_identity(url))
    f = {str(k).lower(): v for k, v in fields(parse(markup)).items() if k}
    course = f.get('courseid') or f.get('courseidinput')
    clazz = f.get('classid') or f.get('clazzid')
    host = urlsplit(url).hostname or ''
    host = 'chaoxing' if host == 'chaoxing.com' or host.endswith('.chaoxing.com') else host
    if course and clazz:
        for kind, names in [('taskrefid', ('taskrefid', 'workrelationid')), ('workid', ('workid',))]:
            for name in names:
                if f.get(name):
                    result.add(f'{host}:{course}:{clazz}:{kind}:{f[name]}')
    return sorted(result)

def is_login(markup, url):
    path = urlsplit(url).path.casefold()
    host = urlsplit(url).hostname or ''
    if 'passport' in host or any(x in path for x in ('login', 'fanyalogin')):
        return True
    root = parse(markup)
    return any(n.attrs.get('type') == 'password' for n in root.all('input'))

def phone_entry(markup, base):
    match = re.search(r"['\"]((?:/mooc-ans)?/work/phone/doHomeWork\?[^'\"]+)", markup)
    return urljoin(base, html.unescape(match.group(1))) if match else None

def explicit_status(text):
    if re.search(r'未交|未提交|未完成|未作答|待完成', text):
        return 'pending'
    if re.search(r'已提交|已交|已完成|待批阅|已批阅|已作答', text):
        return 'submitted'
    return 'unknown'
