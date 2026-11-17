from datetime import date


def staff_at(store, as_of):
    day = date.fromisoformat(as_of)
    result = []
    for person in store.all('people'):
        appointments = [row for row in store.all('appointments') if row['person'] == person['id'] and date.fromisoformat(row['start']) <= day and (row['end'] is None or day < date.fromisoformat(row['end']))]
        if len(appointments) > 1:
            raise ValueError('同一人同日不能归属多个岗位')
        if appointments:
            result.append({**person, **{key: appointments[0][key] for key in ['site', 'role', 'minutes_per_month']}})
    return result


def save_appointment(store, row):
    store.get('people', row['person'])
    start = date.fromisoformat(row['start'])
    end = date.fromisoformat(row['end']) if row['end'] else None
    if end is not None and end <= start:
        raise ValueError('岗位结束必须晚于开始')
    if row['minutes_per_month'] < 0:
        raise ValueError('合同分钟不能为负')
    for old in store.all('appointments'):
        if old['id'] != row['id'] and old['person'] == row['person']:
            if start < date.fromisoformat(old['end'] or '9999-12-31') and date.fromisoformat(old['start']) < (end or date.max):
                raise ValueError('岗位有效期间重叠')
    return store.put('appointments', row)
