"""编制方案对比引擎。

所有方案在**相同人员起点、同一业务月份、同一观察截止日**下推演，
保证延长带教、增加进修名额、提高返聘参与度三类方案可以直接比较
工时缺口、培养产出与人工成本。

方案参数只作用于当次推演，不写回岗位与课程记录：

* ``mentor_extra_minutes``：每个课程在带教月份额外占用的带教者分钟，
  按一线岗位实时扣减当期供给，扣减不低于零；
* ``extra_learners``：课程的假设新增学员（必须是已有人员），
  学员在开课当月不获得资质，只在课程结束当月形成培养产出；
* ``rehire_rate_override``：覆盖历史返聘率的目标参与度（方案假设），
  为空时沿用有证据的历史估计。
"""

from .evidence import MIN_EVIDENCE_PEOPLE, rehire_evidence, retention_at, retention_curves
from .months import format_month, iter_months, month_first, months_between
from .supply import (
    _away_people,
    _cert_expires_within,
    _current_care_episode,
    _rehire_lag_distribution,
    training_output,
)
from .timeline import FRONTLINE_ROLE, appointment_at, history_quality, valid_skill_set


def _mentor_minutes(store, person_id, month, scenario):
    total = 0
    course_ids = []
    first = month_first(month)
    for course in store.all('courses'):
        if not (month_first(course['start']) <= first < month_first(course['end'])):
            continue
        if course['mentor'] != person_id:
            continue
        extra = int(scenario.get('mentor_extra_minutes', {}).get(course['id'], 0))
        total += int(course['mentor_minutes']) + extra
        course_ids.append(course['id'])
    return total, course_ids


def _retention_factor(store, curves, person_id, appt, month):
    episode = _current_care_episode(store, person_id, month_first(curves['anchor']))
    if episode is None:
        return 1.0, True
    tenure = months_between(month, episode['start'])
    site_curve = curves['by_site'].get(appt['site'], {})
    table = site_curve.get('estimate') if not site_curve.get('insufficient_evidence') else None
    fallback = table is None and curves['pooled'].get('estimate') is not None
    if table is None:
        table = curves['pooled'].get('estimate')
    if table is None:
        return 1.0, True
    retention, _, _, extrapolated = retention_at(table, tenure)
    return retention, fallback or extrapolated


def _rate_for_role(store, role):
    try:
        return store.get('rates', role)['cents_per_hour']
    except KeyError:
        return None


def run_scenario(store, anchor, horizon_months, scenario, curves=None, rehire=None):
    anchor_first = month_first(anchor)
    months = iter_months(anchor_first, horizon_months)
    curves = curves or retention_curves(store, anchor_first)
    rehire = rehire or rehire_evidence(store, anchor_first)
    quality = history_quality(store)
    away_pool = _away_people(store, anchor_first)
    lag_distribution = _rehire_lag_distribution(store, anchor_first)
    rate_override = scenario.get('rehire_rate_override')
    monthly = []
    for offset, month in enumerate(months):
        cells = {}
        costs = {'staff_cents': 0, 'expected_rehire_cents': 0, 'rate_missing_roles': set()}

        def cell_for(site, skill):
            return cells.setdefault((site, skill), {'site': site, 'skill': skill, 'deterministic_minutes': 0, 'expected_minutes': 0.0, 'people': set(), 'flags': set()})

        for person in store.all('people'):
            person_id = person['id']
            appt = appointment_at(store, person_id, month)
            if appt is None:
                continue
            rate = _rate_for_role(store, appt['role'])
            if rate is None:
                costs['rate_missing_roles'].add(appt['role'])
            else:
                costs['staff_cents'] += (appt['minutes_per_month'] * rate + 30) // 60
            if appt['role'] != FRONTLINE_ROLE:
                continue
            skills = valid_skill_set(store, person_id, month)
            if not skills:
                continue
            deduction, _ = _mentor_minutes(store, person_id, month, scenario)
            minutes = max(0, appt['minutes_per_month'] - deduction)
            retention, fallback = _retention_factor(store, curves, person_id, appt, month)
            note = quality.get(person_id)
            date_known = True if note is None else bool(note.get('effective_date_known', True))
            for skill in sorted(skills):
                cell = cell_for(appt['site'], skill)
                cell['deterministic_minutes'] += minutes
                cell['expected_minutes'] += minutes * retention
                cell['people'].add(person_id)
                if deduction:
                    cell['flags'].add('mentor_time_deducted')
                if fallback:
                    cell['flags'].add('pooled_curve_fallback')
                if not date_known:
                    cell['flags'].add('registration_only')
                if appt['end'] is not None:
                    cell['flags'].add('fixed_contract_end')
                if _cert_expires_within(store, person_id, skill, month, 3):
                    cell['flags'].add('certificate_expiring_soon')

        # 培养产出：学员在课程结束当月才取得资质（假设性增量，单独打标）。
        graduates = _scenario_graduates(store, scenario, month)
        for graduate in graduates:
            if not graduate['has_care_appointment']:
                continue
            cell = cell_for(graduate['site'], graduate['skill'])
            minutes = graduate['minutes_per_month']
            cell['expected_minutes'] += minutes
            cell['people'].add(graduate['person'])
            cell['flags'].add('training_graduate_assumption')

        # 返聘增量：方案可显式指定目标参与度，否则沿用历史证据。
        rate, rate_flag = _effective_rehire_rate(rehire, rate_override)
        rehire_rows = _scenario_rehire(store, away_pool, rehire, lag_distribution, month, offset, rate)
        care_rate = _rate_for_role(store, FRONTLINE_ROLE)
        for row in rehire_rows:
            cell = cell_for(row['site'], row['skill'])
            cell['expected_minutes'] += row['expected_minutes']
            cell['flags'].add(rate_flag)
            if care_rate is not None:
                costs['expected_rehire_cents'] += round(row['expected_minutes'] / 60 * care_rate)
            else:
                costs['rate_missing_roles'].add(FRONTLINE_ROLE)

        monthly.append(
            {
                'month': format_month(month),
                'cells': [
                    {
                        'site': value['site'],
                        'skill': value['skill'],
                        'deterministic_minutes': value['deterministic_minutes'],
                        'expected_minutes': round(value['expected_minutes'], 1),
                        'people': len(value['people']),
                        'person_ids': sorted(value['people']),
                        'flags': sorted(value['flags']),
                    }
                    for key, value in sorted(cells.items())
                ],
                'staff_cents': costs['staff_cents'],
                'expected_rehire_cents': costs['expected_rehire_cents'],
                'total_expected_cents': costs['staff_cents'] + costs['expected_rehire_cents'],
                'rate_missing_roles': sorted(costs['rate_missing_roles']),
                'graduates': [row['person'] for row in graduates],
            }
        )
    return {
        'scenario_id': scenario['id'],
        'label': scenario.get('label', scenario['id']),
        'anchor': format_month(anchor_first),
        'horizon_months': horizon_months,
        'parameters': _public_parameters(scenario),
        'assumptions': _scenario_assumptions(scenario, rehire),
        'training_output': training_output(store, anchor_first, horizon_months),
        'months': monthly,
    }


def _public_parameters(scenario):
    return {
        'mentor_extra_minutes': dict(scenario.get('mentor_extra_minutes', {})),
        'extra_learners': {key: list(value) for key, value in scenario.get('extra_learners', {}).items()},
        'rehire_rate_override': scenario.get('rehire_rate_override'),
    }


def _scenario_assumptions(scenario, rehire):
    notes = []
    if scenario.get('mentor_extra_minutes'):
        notes.append('延长带教在课程覆盖月份扣减带教者一线工时，不改变带教者薪酬口径')
    if scenario.get('extra_learners'):
        notes.append('新增进修名额在课程结束当月才形成资质，开课当月不产生合格供给')
    if scenario.get('rehire_rate_override') is not None:
        notes.append(
            f'返聘参与度 {scenario["rehire_rate_override"]:.2%} 是方案目标假设；'
            f'历史证据为 {rehire["observed_returnees"]}/{rehire["eligible_leavers"]}'
        )
    elif rehire.get('insufficient_evidence'):
        notes.append('返聘样本不足，返聘量只能作为假设，不能引用为精确预测')
    return notes


def _effective_rehire_rate(rehire, override):
    if override is not None:
        if not 0 <= override <= 1:
            raise ValueError('返聘参与度必须位于 0 到 1 之间')
        return override, 'rehire_target_assumption'
    if rehire['rate'] is None:
        return 0.0, 'rehire_no_evidence'
    return rehire['rate'], 'rehire_rate_observed' if not rehire.get('insufficient_evidence') else 'rehire_assumption'


def _scenario_graduates(store, scenario, month):
    """方案新增进修名额在结业月的产出；既有课程学员已在基线人员循环中，不重复计。"""
    first = month_first(month)
    rows = []
    extras = scenario.get('extra_learners', {})
    for course in store.all('courses'):
        if month_first(course['end']) != first:
            continue
        learners = list(extras.get(course['id'], []))
        if len(set(learners)) != len(learners):
            raise ValueError(f'{course["id"]} 的新增学员在方案中重复')
        for learner in sorted(set(learners)):
            store.get('people', learner)
            if course['skill'] in valid_skill_set(store, learner, month):
                raise ValueError(f'{learner} 在 {course["id"]} 结业月已具备 {course["skill"]} 资质，不能作为新增培养产出重复计')
            appt = appointment_at(store, learner, month)
            rows.append(
                {
                    'person': learner,
                    'skill': course['skill'],
                    'site': appt['site'] if appt else None,
                    'has_care_appointment': appt is not None and appt['role'] == FRONTLINE_ROLE,
                    'minutes_per_month': appt['minutes_per_month'] if appt else 0,
                }
            )
    return rows


def _scenario_rehire(store, away_pool, rehire, lag_distribution, month, offset, rate):
    median_minutes = rehire['returnee_minutes']['median']
    if median_minutes is None or not away_pool or rate <= 0:
        return []
    if lag_distribution and rehire['eligible_leavers'] >= MIN_EVIDENCE_PEOPLE:
        share = sum(1 for lag in lag_distribution if lag == offset) / len(lag_distribution)
        flag = 'rehire_lag_observed'
    else:
        share = 1.0 / max(1, 12)
        flag = 'rehire_lag_assumed'
    rows = []
    for person_id, info in sorted(away_pool.items()):
        for skill in sorted(valid_skill_set(store, person_id, month)):
            rows.append(
                {
                    'site': info['last_site'],
                    'skill': skill,
                    'expected_minutes': median_minutes * rate * share,
                    'flag': flag,
                }
            )
    return rows


def compare_scenarios(store, anchor, horizon_months, scenarios, demand=None):
    """在共同起点上运行多个方案并输出并排对比。"""
    anchor_first = month_first(anchor)
    curves = retention_curves(store, anchor_first)
    rehire = rehire_evidence(store, anchor_first)
    ids = [scenario['id'] for scenario in scenarios]
    if len(set(ids)) != len(ids):
        raise ValueError('方案编号重复，无法在同一口径下比较')
    results = [run_scenario(store, anchor_first, horizon_months, scenario, curves, rehire) for scenario in scenarios]
    comparison = []
    for offset in range(horizon_months):
        month = results[0]['months'][offset]['month']
        keys = set()
        indexed = []
        for result in results:
            mapping = {(cell['site'], cell['skill']): cell for cell in result['months'][offset]['cells']}
            indexed.append(mapping)
            keys.update(mapping)
        for key in sorted(keys):
            site, skill = key
            wanted = None
            if demand and site in demand and skill in demand[site]:
                wanted = demand[site][skill]
            row = {'month': month, 'site': site, 'skill': skill, 'demand_minutes': wanted, 'alternatives': []}
            for scenario, mapping in zip(scenarios, indexed):
                cell = mapping.get(key)
                expected = 0.0 if cell is None else cell['expected_minutes']
                deterministic = 0 if cell is None else cell['deterministic_minutes']
                row['alternatives'].append(
                    {
                        'scenario_id': scenario['id'],
                        'deterministic_minutes': deterministic,
                        'expected_minutes': round(expected, 1),
                        'gap_minutes': None if wanted is None else round(wanted - expected, 1),
                        'flags': [] if cell is None else cell['flags'],
                    }
                )
            comparison.append(row)
    totals = [
        {
            'scenario_id': result['scenario_id'],
            'total_staff_cents': sum(month['staff_cents'] for month in result['months']),
            'total_expected_cents': sum(month['total_expected_cents'] for month in result['months']),
            'total_expected_minutes': round(
                sum(cell['expected_minutes'] for month in result['months'] for cell in month['cells']), 1
            ),
            'graduate_count': sum(item.get('deployable_count', 0) for item in result['training_output']),
        }
        for result in results
    ]
    return {
        'anchor': format_month(anchor_first),
        'horizon_months': horizon_months,
        'common_basis': '同一批人员履历、同一观察截止日、同一业务月份；方案参数不写回业务记录',
        'scenarios': results,
        'comparison': comparison,
        'totals': totals,
    }
