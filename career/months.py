"""月份口径工具。

供给推演以业务月份为最小粒度，统一使用每月 1 日作为归属日，
岗位与证书的半开区间语义与既有 :func:`career.people.staff_at` 保持一致。
"""

from datetime import date


def month_first(value):
    """返回 ISO 日期、YYYY-MM 字符串或 (年, 月) 元组对应月份的 1 日。"""
    if isinstance(value, tuple):
        return date(value[0], value[1], 1)
    if hasattr(value, 'year'):
        day = value
    else:
        text = str(value)
        day = date.fromisoformat(text if len(text) > 7 else text + '-01')
    return date(day.year, day.month, 1)


def month_key(value):
    day = month_first(value)
    return (day.year, day.month)


def month_index(value):
    year, month = month_key(value)
    return year * 12 + (month - 1)


def format_month(key):
    year, month = month_key(key)
    return f'{year:04d}-{month:02d}'


def add_months(key, delta):
    index = key[0] * 12 + (key[1] - 1) + delta
    return (index // 12, index % 12 + 1)


def iter_months(start, count):
    start = month_key(start)
    return [add_months(start, delta) for delta in range(max(0, count))]


def months_between(later, earlier):
    return month_index(later) - month_index(earlier)


def active_in_month(start, end, month):
    """半开区间 [start, end) 在该月 1 日是否有效；end 为空表示未结束。"""
    first = month_first(month)
    if month_first(start) > first:
        return False
    return end is None or first < month_first(end)
