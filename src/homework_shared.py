"""集中存放原本分散重复的通用原子操作（只保留有生产调用点的函数）。

设计约束：

* 只用标准库，不导入 playwright，也不导入项目内任何模块，避免循环依赖。
* 导入本模块没有任何副作用：不读写文件、不打印、不启动进程。
* 每个函数都与既有实现确定等价，细微差异写在各自 docstring 里。

本模块只收录**已经被生产代码调用**的原子操作。曾经的四个候选
（``nonempty_text`` / ``safe_file_name`` / ``write_file_atomic`` / ``retry_call``）
因为找不到"先接线再删旧实现"的安全路径，已连同其测试一并移出，避免制造新的
"有测试但无人使用"的死代码。仍待处理的重复实现记录如下，供后续按需合并：

* 非空文本守卫 ``not isinstance(x, str) or not x.strip()``：``homework_ai``、
  ``homework_auth.status``、``homework_app`` 共 5 处，但其中 password 只判真假、
  不 strip，统一抽象容易改变校验语义。
* 取路径末段 ``Path(...).name[:180]``：``homework_app``(2)、``homework_fill``、
  ``homework_retention`` 共 4 处；这只是"取末段+截断"，真正的越界与符号链接校验
  另有独立实现，不可合并。
* 原子写盘：``homework_engine.write_json`` 已走本模块；``homework_auth``
  保存凭据（DPAPI 加密字节流 + ``with_suffix('.tmp')``）仍保留自有实现，属安全
  关键路径，未改动。
* 固定次数重试：``homework_engine.request``（2 次 / 0.5s）与
  ``scripts/homework_bootstrap.move_environment``（5 次 / 0.25·2ⁿ / 仅重试
  winerror 5、32、33）。两者都涉及超时与失败语义，按"不改变行为"的约束未合并。
* 附件路径包含性校验有三处：``homework_retention.safe_file``、
  ``homework_app.attachment_path``、``homework_fill._local_attachment_files``。
  三者对符号链接和越界的拒绝强度不同，``retention.safe_file`` 的 docstring 已明确
  要求不得放宽，因此不抽取公共实现。
* ``homework_dom.clean_text`` 折叠的是 ``[ \\t\\xa0]+`` 且保留换行（通知与题目正文
  依赖换行分栏），与 ``collapse_whitespace`` 的 ``\\s+`` 折叠语义不同。
* 时间解析有三套时区策略：``homework_engine.parse_datetime``（统一转本地 naive）、
  ``homework_fill`` 的截止时间判断（接受尾部 Z、保持 aware）、
  ``homework_workspace.dashboard``（只规范化 aware 值）。
* 长度上限常量（180/512/2000/12000/45000 等）含义各不相同，不抽成统一截断函数。
* ``homework_auth._protect``（凭据加密）、``homework_engine.process_transaction``
  （Windows 互斥锁）都是单点实现，不抽象。
* 日志写入只有 ``homework_engine.log`` 一处（文件追加行 + PROGRESS 回调），无重复。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    'collapse_whitespace',
    'iso_timestamp',
    'read_json_file',
    'write_json_file',
]

# ``homework_fill._normalized`` / ``homework_dom._content_fingerprint`` /
# ``homework_navigation`` 两处用的都是同一个表达式。
WHITESPACE_RUN = re.compile(r'\s+')


def collapse_whitespace(value: Any) -> str:
    """把任意值转成字符串，把连续空白折叠成一个半角空格，再去掉首尾空白。

    参数：
        value: 任意值；假值（``None``/``''``/``0``/``False``/``[]``）先变成空串，
            与 ``str(value or '')`` 的既有写法一致。

    返回：
        折叠后的字符串；``None`` 等假值返回 ``''``。

    异常行为：不抛异常（``str()`` 对任意对象都成立）。

    与既有实现的差异：``homework_fill._normalized`` 在折叠前额外做了
    ``.replace('\\xa0', ' ')``。Python 的 ``\\s`` 在字符串模式下本身就匹配
    ``\\xa0`` 与全角空格，实测两者结果完全一致，因此本函数省略该替换。
    ``homework_dom.clean_text`` 不是等价替代：它只折叠 ``[ \\t\\xa0]+``，保留换行。
    """
    return WHITESPACE_RUN.sub(' ', str(value or '')).strip()


def iso_timestamp(value: datetime | None = None, *, timespec: str = 'seconds') -> str:
    """按固定精度格式化本地时间戳，用于落盘字段。

    参数：
        value: 要格式化的 ``datetime``；``None``（或假值）表示取 ``datetime.now()``。
        timespec: 传给 ``datetime.isoformat`` 的精度，例如 ``'seconds'``、
            ``'minutes'``、``'microseconds'``。

    返回：
        ISO 8601 字符串，例如 ``'2026-09-28T12:00:00'``。

    异常行为：``value`` 不是 ``datetime`` 时由 ``datetime.isoformat`` 抛
    ``AttributeError``；``timespec`` 非法时抛 ``ValueError``。本函数不做解析。

    依据：``homework_engine.now_iso``（``datetime.now().isoformat(timespec='seconds')``）、
    ``homework_workspace.dashboard`` 的 ``generated_at``、以及若干 ``timespec='minutes'``
    的截止时间写法。

    注意：只格式化、不解析，也不做时区换算；aware 值会原样带上偏移量。
    """
    return (value or datetime.now()).isoformat(timespec=timespec)


def read_json_file(path: str | Path, fallback: Any, *,
                   encoding: str = 'utf-8-sig', corrupted: str = 'raise') -> Any:
    """读取 JSON 文件；文件不存在时返回回退值，损坏时的策略可配置。

    参数：
        path: 目标文件路径。
        fallback: 文件不存在（以及 ``corrupted='fallback'`` 时解析失败）返回的值；
            返回的是该对象本身，不做深拷贝。
        encoding: 文本编码，默认 ``'utf-8-sig'``，可容忍带 BOM 的文件。
        corrupted: 解析失败时的策略。``'raise'``（默认）原样抛出异常，
            与 ``homework_engine.read_json`` 一致；``'fallback'`` 返回 ``fallback``，
            与 ``scripts.homework_bootstrap.environment_ready`` 的容错读法一致。
            其它取值立刻抛 ``ValueError``。

    返回：
        解析出的 JSON 值，或 ``fallback``。

    异常行为：``corrupted='raise'`` 时，空文件、坏 JSON、编码错误都会抛出
    ``json.JSONDecodeError`` / ``UnicodeDecodeError``（两者都是 ``ValueError`` 的子类），
    与 ``homework_engine.read_json`` 完全一致；文件存在但读不动（``PermissionError``
    等 ``OSError``）同样照原样抛出，不吞掉。
    """
    if corrupted not in ('raise', 'fallback'):
        raise ValueError(f'未知的损坏处理策略: {corrupted!r}')
    target = Path(path)
    if not target.exists():
        return fallback
    try:
        return json.loads(target.read_text(encoding=encoding))
    except ValueError:
        if corrupted == 'raise':
            raise
        return fallback


def write_json_file(path: str | Path, value: Any, *, indent: int | None = 2,
                    ensure_ascii: bool = False) -> Path:
    """把值序列化为 JSON 并原子落盘（先写同目录临时文件，再整体替换）。

    参数：
        path: 目标文件路径。
        value: 可 JSON 序列化的对象。
        indent: 缩进，默认 2；``None`` 表示紧凑输出。
        ensure_ascii: 默认 ``False``，中文按原字符写入（与既有实现一致）。

    返回：
        目标文件路径（``Path``）。

    异常行为：``TypeError``（不可序列化，未传 ``default``）、``OSError``（写入失败、
    父目录缺失）原样抛出。不追加结尾换行。失败时临时文件可能残留（既有实现同样如此）。

    注意：临时文件名为 ``path.name + f'.{os.getpid()}.{threading.get_ident()}.tmp'``，
    与 ``homework_engine.write_json`` 的既有命名逐字符一致；缩进与 ``ensure_ascii``
    的组合也等同该处的 ``json.dumps(value, ensure_ascii=False, indent=2)``。
    本函数**不加锁**：``homework_engine.STATE_LOCK`` 等写锁仍由调用方持有。
    """
    target = Path(path)
    temporary = target.with_name(target.name + f'.{os.getpid()}.{threading.get_ident()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=ensure_ascii, indent=indent), encoding='utf-8')
    temporary.replace(target)
    return target
