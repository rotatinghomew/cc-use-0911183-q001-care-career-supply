"""有证据支持的人才流向估计。

本模块只输出能从历史履历中估计的量，并明确给出证据强度：

* :func:`retention_curves` 用 Kaplan–Meier 乘积极限估计一线护理段的
  留存曲线，右删失段进入风险集而不是被当作"永不离开"；
* 只有登记日期、生效日不明的人员不进入精确时间估计；
* 按院区/资质分层时，若风险人数不足，不输出看似精确的分层数，
  回退到整体曲线并显式标注 ``insufficient_evidence``；
* :func:`rehire_rate` 用 Wilson 区间估计离岗后返聘参与度，
  样本不足时同样只给保守结论。

人数过少时（新院区、少见资质组合）不给置信区间，只返回
``estimate=None`` 与原因，避免编制会上引用虚假精度。
"""

import math

from .timeline import build_care_episode_table

# 少于该人数（去重人员）的分层不具备给出数值估计的证据。
MIN_EVIDENCE_PEOPLE = 5
# 少于该事件数时置信区间不具备参考意义。
MIN_EVENTS_FOR_INTERVAL = 3
# 置信区间使用的正态分位数（95%）。
Z_95 = 1.959963984540054


def _kaplan_meier(episodes):
    """对 (tenure, outcome) 序列做 KM 估计，返回按月的留存表与 Greenwood 方差。"""
    event_times = sorted({ep['tenure_months'] for ep in episodes if ep['outcome'] == 'event'})
    at_risk = {}
    events = {}
    for time in event_times:
        at_risk[time] = sum(1 for ep in episodes if ep['tenure_months'] >= time)
        events[time] = sum(1 for ep in episodes if ep['outcome'] == 'event' and ep['tenure_months'] == time)
    survival = 1.0
    variance_factor = 0.0
    table = [{'month': 0, 'survival': 1.0, 'ci_low': 1.0, 'ci_high': 1.0, 'at_risk': len(episodes), 'events': 0}]
    for time in event_times:
        risk = at_risk[time]
        died = events[time]
        if risk == 0:
            continue
        survival *= (risk - died) / risk
        if risk - died > 0:
            variance_factor += died / (risk * (risk - died))
        if variance_factor > 0 and events_total(episodes) >= MIN_EVENTS_FOR_INTERVAL:
            spread = Z_95 * math.sqrt(variance_factor)
            ci_low = max(0.0, survival * math.exp(-spread))
            ci_high = min(1.0, survival * math.exp(spread))
        else:
            ci_low = ci_high = None
        table.append({'month': time, 'survival': survival, 'ci_low': ci_low, 'ci_high': ci_high, 'at_risk': risk, 'events': died})
    return table


def events_total(episodes):
    return sum(1 for ep in episodes if ep['outcome'] == 'event')


def _curve_lookup(table):
    return {row['month']: row for row in table}


def retention_at(table, horizon_months):
    """取 horizon 月处的留存概率；超过观察期使用末期值并标注外推。"""
    lookup = _curve_lookup(table)
    observed_months = sorted(lookup)
    if horizon_months in lookup:
        row = lookup[horizon_months]
        return row['survival'], row['ci_low'], row['ci_high'], False
    last = lookup[observed_months[-1]]
    extrapolated = horizon_months > observed_months[-1]
    return last['survival'], last['ci_low'], last['ci_high'], extrapolated


def retention_curves(store, anchor):
    """构建整体与分层留存曲线，并对证据不足的分层显式降级。"""
    table = build_care_episode_table(store, anchor)
    # 只有真实生效日明确的护理段才进入精确时间估计。
    dated = [row for row in table if row['effective_date_known']]
    result = {
        'anchor': anchor,
        'excluded_registration_only': sorted({row['person'] for row in table if not row['effective_date_known']}),
        'pooled': _curve_payload(dated),
        'by_site': {},
        'by_site_skill': {},
    }
    sites = sorted({row['site'] for row in dated})
    for site in sites:
        subset = [row for row in dated if row['site'] == site]
        result['by_site'][site] = _curve_payload(subset, fallback=result['pooled'])
    skills = _skills_for_episodes(store, dated)
    for site in sites:
        for skill in sorted({skills.get(row['appointment']) for row in dated if row['site'] == site}):
            if skill is None:
                continue
            subset = [row for row in dated if row['site'] == site and skills.get(row['appointment']) == skill]
            key = f'{site}|{skill}'
            result['by_site_skill'][key] = _curve_payload(subset, fallback=result['pooled'])
    return result


def _skills_for_episodes(store, episodes):
    mapping = {}
    for cert in store.all('certificates'):
        mapping.setdefault(cert['person'], set()).add(cert['skill'])
    appointment_skill = {}
    for row in store.all('appointments'):
        skills = mapping.get(row['person'], set())
        if len(skills) == 1:
            appointment_skill[row['id']] = next(iter(skills))
    return appointment_skill


def _curve_payload(episodes, fallback=None):
    people = len({ep['person'] for ep in episodes})
    event_count = events_total(episodes)
    if people < MIN_EVIDENCE_PEOPLE:
        return {
            'estimate': None,
            'people': people,
            'events': event_count,
            'insufficient_evidence': True,
            'reason': f'去重人员 {people} 人，低于证据下限 {MIN_EVIDENCE_PEOPLE} 人',
            'fallback': 'pooled' if fallback is not None else None,
        }
    table = _kaplan_meier(episodes)
    return {
        'estimate': table,
        'people': people,
        'events': event_count,
        'insufficient_evidence': False,
        'extrapolation_note': '超过最长观察期的月份沿用末期留存率，属于外推假设',
    }


def rehire_evidence(store, anchor):
    """离岗后返聘的历史证据：返聘人数、可观察离岗人数与 Wilson 区间。"""
    from .timeline import flow_ledger

    ledger = flow_ledger(store, anchor)
    dated = [row for row in ledger if row['effective_date_known']]
    registration_only = [row for row in ledger if not row['effective_date_known']]
    # 可观察的离岗：出现过 gap/rehire 起点，或当前处于非一线/离岗状态。
    left = {row['person'] for row in dated if row['flow'] in {'gap', 'to_manager', 'rehire'}}
    returned = {row['person'] for row in dated if row['flow'] == 'rehire'}
    n = len(left)
    k = len(returned)
    payload = {
        'eligible_leavers': n,
        'observed_returnees': k,
        'rate': None,
        'ci_low': None,
        'ci_high': None,
        'insufficient_evidence': n < MIN_EVIDENCE_PEOPLE,
        'assumption_only': n < MIN_EVIDENCE_PEOPLE,
        'registration_only_excluded': sorted(
            {row['person'] for row in registration_only if row['flow'] in {'gap', 'to_manager', 'rehire'}}
        ),
        'returnee_minutes': _returnee_minutes(store, returned),
    }
    if n > 0:
        rate = k / n
        payload['rate'] = rate
        if n >= MIN_EVIDENCE_PEOPLE:
            ci_low, ci_high = _wilson(k, n)
            payload['ci_low'] = ci_low
            payload['ci_high'] = ci_high
    return payload


def _wilson(k, n):
    denominator = 1 + Z_95 ** 2 / n
    center = (k / n + Z_95 ** 2 / (2 * n)) / denominator
    spread = Z_95 * math.sqrt((k / n) * (1 - k / n) / n + Z_95 ** 2 / (4 * n * n)) / denominator
    return max(0.0, center - spread), min(1.0, center + spread)


def _returnee_minutes(store, people):
    minutes = []
    people = set(people)
    for row in store.all('appointments'):
        if row['person'] in people and row['role'] == 'care':
            minutes.append(row['minutes_per_month'])
    if not minutes:
        return {'values': [], 'median': None}
    ordered = sorted(minutes)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {'values': ordered, 'median': median}
