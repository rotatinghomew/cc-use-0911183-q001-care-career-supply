"""人员履历时间线重建。

同一个人的调院、休假、转岗、返聘由按时间排序的岗位段串成一条履历，
后续分析（风险人群、留存估计）始终以人员为单位，绝不把每段履历
当作相互独立的新员工。

``history_quality`` 记录区分两类历史：
* ``effective_date_known=true``：日期即真实生效日，可作为有时间依据的证据；
* ``effective_date_known=false``：只有人事登记日期，真实生效日不明，
  只能做区间口径的低权重处理，不能参与精确的留存时间估计。

观察截止日 ``observation_until`` 之后尚未发生的事件属于右删失：
没有结束记录不等于永远不会发生，只表示"截至该日尚未观察到"。
"""

from datetime import date

from .months import active_in_month, month_first, month_index, months_between

# 可作为一线护理供给的岗位；其余岗位（如 manager）不产生护理工时。
FRONTLINE_ROLE = 'care'


def history_quality(store):
    """按人员编号返回历史质量标记；没有记录时默认生效日已知。"""
    return {row['id']: row for row in store.all('history_quality')}


def quality_for(store, person_id):
    rows = store.all('history_quality')
    for row in rows:
        if row['id'] == person_id:
            return row
    return {
        'id': person_id,
        'effective_date_known': True,
        'observation_until': None,
    }


def person_timeline(store, person_id):
    """返回单人按开始时间排序的岗位段，并校验半开区间不重叠。"""
    rows = [row for row in store.all('appointments') if row['person'] == person_id]
    rows.sort(key=lambda row: (month_index(row['start']), row['id']))
    for earlier, later in zip(rows, rows[1:]):
        earlier_end = month_first(earlier['end']) if earlier['end'] else date.max
        if month_first(later['start']) < earlier_end:
            raise ValueError(f'{person_id} 的岗位期间重叠')
    return rows


def person_certificates(store, person_id):
    rows = [row for row in store.all('certificates') if row['person'] == person_id]
    return sorted(rows, key=lambda row: (month_index(row['start']), row['id']))


def appointment_at(store, person_id, month):
    """该月 1 日有效的岗位段；同一人同日至多一个岗位，没有则返回 None。"""
    matches = [
        row
        for row in store.all('appointments')
        if row['person'] == person_id and active_in_month(row['start'], row['end'], month)
    ]
    if len(matches) > 1:
        raise ValueError('同一人同日不能归属多个岗位')
    return matches[0] if matches else None


def valid_skill_set(store, person_id, month):
    first = month_first(month)
    return {
        row['skill']
        for row in person_certificates(store, person_id)
        if month_first(row['start']) <= first < month_first(row['end'])
    }


def _segment_episodes(timeline):
    """把岗位段切成一线护理段与非一线段，相邻同性质段保持独立（调院可区分）。"""
    episodes = []
    for row in timeline:
        episodes.append(
            {
                'appointment': row['id'],
                'site': row['site'],
                'role': row['role'],
                'start': row['start'],
                'end': row['end'],
                'minutes_per_month': row['minutes_per_month'],
            }
        )
    return episodes


def care_episodes(store, person_id):
    """单人的一线护理段列表（按时间排序，同一人可有多段，如返聘）。"""
    return [episode for episode in _segment_episodes(person_timeline(store, person_id)) if episode['role'] == FRONTLINE_ROLE]


def build_care_episode_table(store, anchor):
    """构建以人员为单位的一线护理段结局表。

    结局口径：
    * ``event``：在观察截止前观察到该护理段结束（转管理、休假、离职都算离开一线风险池）；
    * ``censored``：观察截止时仍在岗，或结束日晚于观察截止——右删失，
      不能当成永不离开。

    只有登记日期（effective_date_known=false）的人员单独打标，
    不进入精确留存时间估计。
    """
    anchor_first = month_first(anchor)
    quality = history_quality(store)
    table = []
    for person in store.all('people'):
        person_id = person['id']
        note = quality.get(person_id)
        date_known = True if note is None else bool(note.get('effective_date_known', True))
        for episode in care_episodes(store, person_id):
            start = month_first(episode['start'])
            if start > anchor_first:
                # 观察截止之后才开始的段不是历史证据，属于排程/假设数据。
                continue
            if episode['end'] is None or month_first(episode['end']) > anchor_first:
                outcome = 'censored'
                end_month = anchor_first
            else:
                outcome = 'event'
                end_month = month_first(episode['end'])
            tenure = max(0, months_between(end_month, start))
            table.append(
                {
                    'person': person_id,
                    'appointment': episode['appointment'],
                    'site': episode['site'],
                    'start_month': start.isoformat(),
                    'tenure_months': tenure,
                    'outcome': outcome,
                    'effective_date_known': date_known,
                }
            )
    return table


def flow_ledger(store, anchor):
    """汇总有记录支持的人员流向，每条都标注证据强度。

    流向类型：transfer（调院）、to_manager（转管理）、gap（离岗/休假）、
    rehire（离岗后返聘）、active_care（观察截止仍在一线）、
    active_other（观察截止在非一线岗）。
    """
    anchor_first = month_first(anchor)
    quality = history_quality(store)
    ledger = []
    for person in store.all('people'):
        person_id = person['id']
        timeline = person_timeline(store, person_id)
        date_known = True
        note = quality.get(person_id)
        if note is not None:
            date_known = bool(note.get('effective_date_known', True))
        previous = None
        saw_gap = False
        for episode in _segment_episodes(timeline):
            if previous is not None:
                has_time_gap = previous['end'] is None or month_first(episode['start']) > month_first(previous['end'])
                if has_time_gap and episode['role'] == FRONTLINE_ROLE:
                    flow = 'rehire'
                    saw_gap = True
                elif episode['role'] != FRONTLINE_ROLE and previous['role'] == FRONTLINE_ROLE:
                    flow = 'to_manager' if episode['role'] == 'manager' else 'gap'
                elif episode['role'] == FRONTLINE_ROLE and previous['role'] != FRONTLINE_ROLE:
                    flow = 'rehire'
                    saw_gap = True
                elif has_time_gap:
                    flow = 'gap'
                    saw_gap = True
                elif episode['site'] != previous['site'] and episode['role'] == FRONTLINE_ROLE:
                    flow = 'transfer'
                else:
                    flow = 'gap'
                    saw_gap = True
                ledger.append(
                    {
                        'person': person_id,
                        'flow': flow,
                        'from_site': previous['site'],
                        'to_site': episode['site'],
                        'month': month_first(episode['start']).isoformat(),
                        'effective_date_known': date_known,
                        'evidence': 'dated' if date_known else 'registration_only',
                    }
                )
            previous = episode
        if timeline:
            last = timeline[-1]
            open_ended = last['end'] is None or month_first(last['end']) > anchor_first
            if open_ended:
                flow = 'active_care' if last['role'] == FRONTLINE_ROLE else 'active_other'
                ledger.append(
                    {
                        'person': person_id,
                        'flow': flow,
                        'from_site': last['site'],
                        'to_site': last['site'],
                        'month': anchor_first.isoformat(),
                        'effective_date_known': date_known,
                        'evidence': 'dated' if date_known else 'registration_only',
                    }
                )
            elif not saw_gap and len(timeline) == 1:
                ledger.append(
                    {
                        'person': person_id,
                        'flow': 'gap',
                        'from_site': last['site'],
                        'to_site': None,
                        'month': month_first(last['end']).isoformat(),
                        'effective_date_known': date_known,
                        'evidence': 'dated' if date_known else 'registration_only',
                    }
                )
    return ledger


def default_anchor(store):
    """默认分析基准月：取历史质量记录中最大的观察截止日，否则取今天所在月。"""
    cutoff = [row['observation_until'] for row in store.all('history_quality') if row.get('observation_until')]
    if cutoff:
        return month_first(max(cutoff, key=lambda value: month_index(value)))
    return month_first(date.today())
