"""Read the school's observed student catalog to discover report-only courses.

The one POST in this module is the catalog query implemented by the school's
public course.js getCourseList(); no course or report write route is used.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlsplit

import homework_dom as dom
from homework_lab import dispose_response
import homework_school as school


ORIGIN = 'https://mooc.istudy.szpu.edu.cn'
FID = '1860'
CATALOG_URL = ORIGIN + '/visit/interaction'
LIST_URL = ORIGIN + '/mooc-ans/visit/courselistdata'
_DEFAULT_ORIGIN = ORIGIN
_DEFAULT_FID = FID
_DEFAULT_CATALOG_URL = CATALOG_URL
_DEFAULT_LIST_URL = LIST_URL
COURSE_SCRIPT = '/mooc-ans/mooc2/js/course/course.js'
LOGIN_REQUIRED = 'LOGIN_REQUIRED:学校课程目录需要重新登录'
NETWORK_ERROR = '学校课程目录读取失败，请检查网络后重试'
FORMAT_ERROR = '学校课程目录结构已变化，已有作业已保留'
INCOMPLETE = '学校课程目录包含尚未支持的分页，部分课程可能未检查'
LIMIT_WARNING = '学校课程目录较多，本次未能检查全部课程'
_FIELDS = ('baseEducation', 'courseFolderId', 'courseFolderSize', 'superstarClass')
_ID = re.compile(r'[0-9]{1,20}\Z')
_MAX_FOLDERS = 20
_MAX_COURSES = 200


def _active_endpoints():
    if (ORIGIN != _DEFAULT_ORIGIN or FID != _DEFAULT_FID
            or CATALOG_URL != _DEFAULT_CATALOG_URL or LIST_URL != _DEFAULT_LIST_URL):
        return ORIGIN, FID, CATALOG_URL, LIST_URL
    if school.is_chaoxing_only():
        return '', '', '', ''
    origin = school.school_origin(ORIGIN)
    fid = school.school_fid(FID)
    if origin == ORIGIN:
        return ORIGIN, fid, CATALOG_URL, LIST_URL
    return origin, fid, origin + '/visit/interaction', origin + '/mooc-ans/visit/courselistdata'


def _read(context, url, form=None):
    _, _, catalog_url, _ = _active_endpoints()
    response = None
    try:
        try:
            options = {'max_redirects': 0, 'timeout': 15000, 'fail_on_status_code': False}
            if form is None:
                response = context.request.get(url, **options)
            else:
                response = context.request.post(url, form=form, headers={
                    'Referer': catalog_url or CATALOG_URL, 'X-Requested-With': 'XMLHttpRequest',
                }, **options)
        except Exception:
            raise RuntimeError(NETWORK_ERROR) from None
        if response.status in (301, 302, 303, 307, 308, 401, 403):
            raise RuntimeError(LOGIN_REQUIRED)
        if response.url != url:
            raise RuntimeError(FORMAT_ERROR)
        if response.status != 200:
            raise RuntimeError(NETWORK_ERROR)
        try:
            body = response.body()
            if len(body) > 4_000_000:
                raise ValueError()
            markup = body.decode('utf-8')
        except Exception:
            raise RuntimeError(FORMAT_ERROR) from None
        if dom.is_login(markup, url):
            raise RuntimeError(LOGIN_REQUIRED)
        return markup
    finally:
        dispose_response(response)


def _bootstrap(markup):
    origin, _, catalog_url, _ = _active_endpoints()
    origin = origin or ORIGIN
    catalog_url = catalog_url or CATALOG_URL
    root = dom.parse(markup)
    scripts = [urlsplit(urljoin(catalog_url, node.attrs.get('src', '')))
               for node in root.all('script') if node.attrs.get('src')]
    if not any(item.scheme == 'https' and item.hostname == urlsplit(origin).hostname
               and item.path == COURSE_SCRIPT and not item.username and not item.password
               for item in scripts):
        raise ValueError(FORMAT_ERROR)
    inline = '\n'.join(''.join(child for child in node.children if isinstance(child, str))
                       for node in root.all('script') if not node.attrs.get('src'))
    if (not re.search(r'''\b_CP_\s*=\s*(["'])/mooc-ans\1\s*;''', inline)
            or not re.search(r'\b_HOST_CP2_\s*=\s*_CP_\s*;', inline)):
        raise ValueError(FORMAT_ERROR)
    tabs = [node for node in root.all(cls='tab-item')
            if node.attrs.get('coursetype') == '1' and '我学的课' in dom.clean_text(node)]
    if len(tabs) != 1:
        raise ValueError(FORMAT_ERROR)
    form = {'courseType': '1'}
    for name in _FIELDS:
        controls = [node for node in root.all('input') if node.attrs.get('id') == name]
        if len(controls) != 1:
            raise ValueError(FORMAT_ERROR)
        value = controls[0].attrs.get('value', '')
        if not (name == 'superstarClass' and value == '') and not _ID.fullmatch(value):
            raise ValueError(FORMAT_ERROR)
        form[name] = value
    # The observed student-tab action resets the folder to the catalog root.
    form['courseFolderId'] = '0'
    return form


def _has_paging(root):
    for node in root.all():
        if node.tag in {'script', 'style'}:
            continue
        if 'disabled' in node.attrs or node.attrs.get('aria-disabled') == 'true':
            continue
        markers = ' '.join(node.attrs.get(key, '') for key in ('id', 'name', 'class'))
        if re.search(r'pagination|\bpager\b|pagenum|totalpage|nextpage|loadmore', markers, re.I):
            return True
        if node.tag in {'a', 'button'} and dom.clean_text(node) in {'下一页', '下页', '加载更多', '更多课程'}:
            return True
    return False


def _parse_list(markup):
    origin, fid, _, list_url = _active_endpoints()
    origin = origin or ORIGIN
    fid = fid or FID
    list_url = list_url or LIST_URL
    root = dom.parse(markup)
    containers = [node for node in root.all() if node.attrs.get('id') == 'courseList']
    if len(containers) != 1:
        raise ValueError(FORMAT_ERROR)
    courses = []
    invalid = False
    for card in containers[0].all('li', attr='courseid'):
        course_id, clazz_id = card.attrs.get('courseid', ''), card.attrs.get('clazzid', '')
        if not _ID.fullmatch(course_id) or not _ID.fullmatch(clazz_id):
            invalid = True
            continue
        matches = []
        conflict = False
        for anchor in card.all('a', attr='href'):
            try:
                target = urlsplit(urljoin(list_url, anchor.attrs['href']))
                if (target.scheme != 'https' or target.hostname != urlsplit(origin).hostname
                        or target.username or target.password or target.port not in (None, 443)
                        or target.path != '/mooc-ans/visit/stucoursemiddle'):
                    continue
                fields = parse_qs(target.query, keep_blank_values=True)
                if fields.get('courseid') == [course_id] and fields.get('clazzid') == [clazz_id]:
                    matches.append(anchor)
                else:
                    conflict = True
            except ValueError:
                conflict = True
        title_node = card.first('h3') or card.first('dt')
        title = dom.clean_text(title_node) if title_node else ''
        if not title:
            title = next((anchor.attrs.get('title') or dom.clean_text(anchor)
                          for anchor in matches if anchor.attrs.get('title') or dom.clean_text(anchor)), '')
        if conflict or not matches or not title:
            invalid = True
            continue
        courses.append({'origin': origin, 'fid': fid, 'course_id': course_id,
                        'course': title, 'title': title})
    folders = set()
    for folder in root.all('li', attr='fileid'):
        value = folder.attrs.get('fileid', '')
        if not _ID.fullmatch(value) or value == '0':
            invalid = True
            continue
        # Only follow ids exposed by the actual intoFolder control.
        actions = [node.attrs.get('onclick', '') for node in [folder] + folder.all()]
        if any(re.fullmatch(r'\s*intoFolder\(\s*' + re.escape(value) + r'\s*\)\s*;?\s*',
                            action) for action in actions):
            folders.add(value)
        else:
            invalid = True
    warnings = [FORMAT_ERROR] if invalid else []
    if _has_paging(root):
        warnings.append(INCOMPLETE)
    return courses, sorted(folders, key=int), warnings


def discover_courses(context):
    """Return (course targets, fixed warnings), without altering stored data."""
    origin, _, catalog_url, list_url = _active_endpoints()
    if not origin or not catalog_url or not list_url:
        return [], []
    targets, warnings, seen = {}, [], set()
    try:
        form = _bootstrap(_read(context, catalog_url))
    except (RuntimeError, ValueError) as exc:
        return [], [str(exc)]
    pending = ['0']
    while pending and len(seen) < _MAX_FOLDERS:
        folder = pending.pop(0)
        if folder in seen:
            continue
        seen.add(folder)
        try:
            markup = _read(context, list_url, dict(form, courseFolderId=folder))
            courses, folders, messages = _parse_list(markup)
            warnings.extend(messages)
            for target in courses:
                if target['course_id'] in targets:
                    continue
                if len(targets) >= _MAX_COURSES:
                    warnings.append(LIMIT_WARNING)
                    break
                targets[target['course_id']] = target
            pending.extend(value for value in folders if value not in seen and value not in pending)
        except (RuntimeError, ValueError) as exc:
            warnings.append(str(exc))
            if str(exc) == LOGIN_REQUIRED:
                break
    if pending:
        warnings.append(LIMIT_WARNING)
    return list(targets.values()), list(dict.fromkeys(warnings))
