"""分月护理工时供给推演。

输出口径是"未来各业务月、各院区、各资质可提供的护理工时（分钟）"，
而不是在册人数或离职预测。两层结果并存：

* ``baseline``：只按已登记岗位、合同工时和证书有效期计算的确定性供给，
  带教占用在课程覆盖月份直接扣减带教者的一线工时；
* ``expected``：在 baseline 的人员起点上叠加有证据的留存曲线与
  返聘参与度，给出期望工时与不确定性；证据不足处不给区间、只给标记。

硬约束在两层中都生效：
* 证书到期当月起不再计入对应资质（半开区间）；
* 合同有固定到期日的岗位到期后不自动续期；
* 同一人同一月只归属一个院区的一个岗位，绝不重复计入两个院区；
* 只有登记日期的记录照常计入确定性供给，但逐人打 ``registration_only`` 标记，
  且不参与留存时间估计。
"""

from .evidence import MIN_EVIDENCE_PEOPLE, rehire_evidence, retention_at, retention_curves
from .months import format_month, iter_months, month_first, months_between
from .timeline import (
    FRONTLINE_ROLE,
    appointment_at,
    care_episodes,
    flow_ledger,
    history_quality,
    valid_skill_set,
)

MENTOR_ROLE_DEDUCTION_LIMIT = 0


def _courses_active(store, month):
    first = month_first(month)
    result = []
    for course in store.all('courses'):
        if month_first(course['start']) <= first < month_first(course['end']):
            result.append(course)
    return result


def _mentor_deduction(store, person_id, month):
    """该人员当月带教占用的分钟数（仅当其当月为一线护理岗才实际扣减）。"""
    total = 0
    courses = []
    for course in _courses_active(store, month):
        if course['mentor'] == person_id:
            total += int(course['mentor_minutes'])
            courses.append(course['id'])
    return total, courses


def baseline_supply(store, anchor, horizon_months):
    """确定性供给：合同 × 有效证书 − 带教占用，逐月逐院区逐资质。"""
    anchor_first = month_first(anchor)
    months = iter_months(anchor_first, horizon_months)
    quality = history_quality(store)
    monthly = []
    for month in months:
        cells = {}
        people_seen = {}
        for person in store.all('people'):
            person_id = person['id']
            appt = appointment_at(store, person_id, month)
            if appt is None or appt['role'] != FRONTLINE_ROLE:
                continue
            # 人员唯一性护栏：同一人同月不能进两个院区。
            if person_id in people_seen and people_seen[person_id] != appt['site']:
                raise ValueError(f'{person_id} 在 {format_month(month)} 被计入多个院区')
            people_seen[person_id] = appt['site']
            deduction, course_ids = _mentor_deduction(store, person_id, month)
            skills = valid_skill_set(store, person_id, month)
            if not skills:
                # 证书已失效的人不产生资质供给，但若当月仍在带教，占用事实要保留在课程资质格上。
                if deduction:
                    for course_id in course_ids:
                        course = store.get('courses', course_id)
                        cell = cells.setdefault(
                            (appt['site'], course['skill']),
                            {'site': appt['site'], 'skill': course['skill'], 'minutes': 0, 'people': 0, 'flags': set(), 'person_ids': []},
                        )
                        cell['flags'].add('mentor_time_deducted')
                continue
            minutes = max(MENTOR_ROLE_DEDUCTION_LIMIT, appt['minutes_per_month'] - deduction)
            date_known = True
            note = quality.get(person_id)
            if note is not None:
                date_known = bool(note.get('effective_date_known', True))
            for skill in sorted(skills):
                key = (appt['site'], skill)
                cell = cells.setdefault(
                    key,
                    {'site': appt['site'], 'skill': skill, 'minutes': 0, 'people': 0, 'flags': set(), 'person_ids': []},
                )
                cell['minutes'] += minutes
                cell['people'] += 1
                cell['person_ids'].append(person_id)
                if not date_known:
                    cell['flags'].add('registration_only')
                if deduction:
                    cell['flags'].add('mentor_time_deducted')
                if _cert_expires_within(store, person_id, skill, month, 3):
                    cell['flags'].add('certificate_expiring_soon')
                if appt['end'] is not None:
                    cell['flags'].add('fixed_contract_end')
        monthly.append(
            {
                'month': format_month(month),
                'cells': [
                    {
                        'site': cell['site'],
                        'skill': cell['skill'],
                        'minutes': cell['minutes'],
                        'people': cell['people'],
                        'person_ids': sorted(cell['person_ids']),
                        'flags': sorted(cell['flags']),
                    }
                    for cell in (cells[key] for key in sorted(cells))
                ],
            }
        )
    return {'anchor': format_month(anchor_first), 'horizon_months': horizon_months, 'months': monthly}


def _cert_expires_within(store, person_id, skill, month, within):
    first = month_first(month)
    for cert in store.all('certificates'):
        if cert['person'] == person_id and cert['skill'] == skill:
            if month_first(cert['start']) <= first < month_first(cert['end']) and 0 <= months_between(cert['end'], first) <= within:
                return True
    return False


def _current_care_episode(store, person_id, anchor_first):
    for episode in reversed(care_episodes(store, person_id)):
        start = month_first(episode['start'])
        if start <= anchor_first and (episode['end'] is None or month_first(episode['end']) > anchor_first):
            return episode
    return None


def expected_supply(store, anchor, horizon_months, curves=None):
    """风险调整后的期望供给，以及逐格不确定性与证据标记。"""
    anchor_first = month_first(anchor)
    months = iter_months(anchor_first, horizon_months)
    curves = curves or retention_curves(store, anchor_first)
    pooled_table = curves['pooled'].get('estimate')
    rehire = rehire_evidence(store, anchor)
    quality = history_quality(store)
    away_pool = _away_people(store, anchor_first)
    lag_distribution = _rehire_lag_distribution(store, anchor_first)
    monthly = []
    for offset, month in enumerate(months):
        cells = {}
        for person in store.all('people'):
            person_id = person['id']
            appt = appointment_at(store, person_id, month)
            if appt is not None and appt['role'] == FRONTLINE_ROLE:
                contribution = _retained_contribution(store, person_id, appt, month, offset, curves, pooled_table, quality)
                if contribution is None:
                    continue
                for skill, payload in contribution.items():
                    cell = cells.setdefault((appt['site'], skill), _empty_cell(appt['site'], skill))
                    _accumulate(cell, payload, person_id)
        # 返聘带来的期望增量（只计证书仍有效者）。
        no_pooled_curve = pooled_table is None
        for payload in _rehire_additions(store, away_pool, rehire, lag_distribution, month, offset, anchor_first):
            key = (payload['site'], payload['skill'])
            cell = cells.setdefault(key, _empty_cell(payload['site'], payload['skill']))
            cell['expected_minutes'] += payload['expected_minutes']
            cell['ci_low'] = None if cell['ci_low'] is None or payload['ci_low'] is None else cell['ci_low'] + payload['ci_low']
            cell['ci_high'] = None if cell['ci_high'] is None or payload['ci_high'] is None else cell['ci_high'] + payload['ci_high']
            cell['flags'].update(payload['flags'])
            if no_pooled_curve:
                cell['flags'].add('no_retention_evidence')
            cell['sources'].extend(payload['sources'])
        monthly.append({'month': format_month(month), 'cells': [_finalize(cells[key]) for key in sorted(cells)]})
    return {
        'anchor': format_month(anchor_first),
        'horizon_months': horizon_months,
        'assumptions': [
            '留存概率乘在个人合同工时上；置信区间近似假设人员条件独立',
            '超过最长观察期的月份沿用末期留存率（外推）',
            '固定合同到期后不自动续期，除非方案显式假设返聘',
        ],
        'rehire_evidence': rehire,
        'months': monthly,
    }


def _empty_cell(site, skill):
    return {
        'site': site,
        'skill': skill,
        'expected_minutes': 0.0,
        'ci_low': 0.0,
        'ci_high': 0.0,
        'people': 0,
        'flags': set(),
        'person_ids': [],
        'sources': [],
    }


def _accumulate(cell, payload, person_id):
    cell['expected_minutes'] += payload['expected_minutes']
    if payload['ci_low'] is None or payload['ci_high'] is None:
        cell['ci_low'] = cell['ci_high'] = None
    if cell['ci_low'] is not None:
        cell['ci_low'] += payload['ci_low']
        cell['ci_high'] += payload['ci_high']
    cell['people'] += 1
    cell['person_ids'].append(person_id)
    cell['flags'].update(payload['flags'])


def _finalize(cell):
    return {
        'site': cell['site'],
        'skill': cell['skill'],
        'expected_minutes': round(cell['expected_minutes'], 1),
        'ci_low': None if cell['ci_low'] is None else round(cell['ci_low'], 1),
        'ci_high': None if cell['ci_high'] is None else round(cell['ci_high'], 1),
        'people': cell['people'],
        'person_ids': sorted(cell['person_ids']),
        'flags': sorted(cell['flags']),
        'rehire_sources': sorted(set(cell['sources'])),
    }


def _retained_contribution(store, person_id, appt, month, offset, curves, pooled_table, quality):
    """个人当月在各有效资质上的期望工时。"""
    skills = valid_skill_set(store, person_id, month)
    if not skills:
        return None
    note = quality.get(person_id)
    date_known = True if note is None else bool(note.get('effective_date_known', True))
    anchor_first = month_first(curves['anchor'])
    episode = _current_care_episode(store, person_id, anchor_first)
    if episode is None:
        return None
    if appt['end'] is not None and month_first(appt['end']) <= month_first(month):
        return None
    tenure = months_between(month, episode['start'])
    site_curve = curves['by_site'].get(appt['site'], {})
    table = site_curve.get('estimate') if not site_curve.get('insufficient_evidence') else None
    used_fallback = table is None and pooled_table is not None
    if table is None:
        table = pooled_table
    if table is None:
        retention = ci_low = ci_high = None
    else:
        retention, ci_low, ci_high, extrapolated = retention_at(table, tenure)
        if extrapolated:
            used_fallback = True
    deduction, _ = _mentor_deduction(store, person_id, month)
    base_minutes = max(0, appt['minutes_per_month'] - deduction)
    result = {}
    for skill in sorted(skills):
        flags = set()
        if not date_known:
            flags.add('registration_only')
        if used_fallback:
            flags.add('pooled_curve_fallback')
        if appt['end'] is not None:
            flags.add('fixed_contract_end')
        if deduction:
            flags.add('mentor_time_deducted')
        if retention is None:
            flags.add('no_retention_evidence')
            expected = float(base_minutes)
            low = high = None
        else:
            expected = base_minutes * retention
            low = None if ci_low is None else base_minutes * ci_low
            high = None if ci_high is None else base_minutes * ci_high
        result[skill] = {'expected_minutes': expected, 'ci_low': low, 'ci_high': high, 'flags': flags}
    return result


def _away_people(store, anchor_first):
    """观察截止时处于离岗/非一线状态、理论上可返聘的人员及其资质。"""
    away = {}
    ledger = flow_ledger(store, anchor_first)
    returned = {row['person'] for row in ledger if row['flow'] == 'rehire'}
    for row in ledger:
        if row['flow'] in {'gap', 'to_manager', 'active_other'} and row['person'] not in returned:
            away.setdefault(row['person'], {'last_site': row['from_site']})
    # 剔除观察截止仍有有效岗位的人（active_other 是管理岗，保留在池中但标注）。
    return away


def _rehire_lag_distribution(store, anchor_first):
    """历史上从离岗月到返聘月的间隔月分布（仅生效日明确的记录）。"""
    lags = []
    quality = history_quality(store)
    for person in store.all('people'):
        person_id = person['id']
        note = quality.get(person_id)
        if note is not None and not note.get('effective_date_known', True):
            continue
        episodes = care_episodes(store, person_id)
        if len(episodes) < 2:
            continue
        for earlier, later in zip(episodes, episodes[1:]):
            if earlier['end'] is not None:
                lags.append(max(0, months_between(later['start'], earlier['end'])))
    return lags


def _rehire_additions(store, away_pool, rehire, lag_distribution, month, offset, anchor_first):
    if rehire['rate'] is None or not away_pool:
        return []
    median_minutes = rehire['returnee_minutes']['median']
    if median_minutes is None:
        return []
    rate = rehire['rate']
    ci_low_rate = rehire['ci_low']
    ci_high_rate = rehire['ci_high']
    # 当月按滞后分布应发生返聘的人员比例；证据不足时均匀摊到整个观察期属于假设。
    if lag_distribution and rehire['eligible_leavers'] >= MIN_EVIDENCE_PEOPLE:
        share = sum(1 for lag in lag_distribution if lag == offset) / len(lag_distribution)
        distribution_flag = 'rehire_lag_observed'
    else:
        share = 1.0 / max(1, 12)
        distribution_flag = 'rehire_lag_assumed'
    additions = []
    for person_id, info in sorted(away_pool.items()):
        skills = valid_skill_set(store, person_id, month)
        if not skills:
            continue
        for skill in sorted(skills):
            additions.append(
                {
                    'site': info['last_site'],
                    'skill': skill,
                    'expected_minutes': median_minutes * rate * share,
                    'ci_low': None if ci_low_rate is None else median_minutes * ci_low_rate * share,
                    'ci_high': None if ci_high_rate is None else median_minutes * ci_high_rate * share,
                    'flags': {distribution_flag, 'rehire_assumption'},
                    'sources': [person_id],
                }
            )
    return additions


def training_output(store, anchor, horizon_months):
    """课程培养产出：结业当月才形成资质，开课当月不计入供给。"""
    anchor_first = month_first(anchor)
    months = iter_months(anchor_first, horizon_months)
    rows = []
    for month in months:
        graduates = []
        for course in store.all('courses'):
            if month_first(course['end']) != month_first(month):
                continue
            for learner in course['learners']:
                appt = appointment_at(store, learner, month)
                graduates.append(
                    {
                        'person': learner,
                        'skill': course['skill'],
                        'course': course['id'],
                        'has_care_appointment': appt is not None and appt['role'] == FRONTLINE_ROLE,
                        'site': appt['site'] if appt else None,
                    }
                )
        if graduates:
            rows.append(
                {
                    'month': format_month(month),
                    'graduates': graduates,
                    'graduate_count': len(graduates),
                    'deployable_count': sum(1 for row in graduates if row['has_care_appointment']),
                    'note': '资质在课程结束当月生效，开课当月不产生合格供给',
                }
            )
    return rows


def gap_against_demand(expected, demand):
    """按相同业务月对比需求与期望供给；需求缺省则缺口为空，不臆造需求。"""
    rows = []
    for monthly in expected['months']:
        for cell in monthly['cells']:
            wanted = None
            if demand and cell['site'] in demand and cell['skill'] in demand[cell['site']]:
                wanted = demand[cell['site']][cell['skill']]
            rows.append(
                {
                    'month': monthly['month'],
                    'site': cell['site'],
                    'skill': cell['skill'],
                    'demand_minutes': wanted,
                    'expected_minutes': cell['expected_minutes'],
                    'gap_minutes': None if wanted is None else wanted - cell['expected_minutes'],
                    'flags': cell['flags'],
                }
            )
    return rows
