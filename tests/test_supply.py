import json
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from threading import Thread

from career.app import Application
from career.evidence import MIN_EVIDENCE_PEOPLE, rehire_evidence, retention_curves
from career.months import add_months, format_month, iter_months, month_index, months_between
from career.scenario import compare_scenarios, run_scenario
from career.snapshot import data_fingerprint, get_snapshot, list_snapshots, save_analysis_snapshot
from career.store import Store
from career.supply import baseline_supply, expected_supply, gap_against_demand, training_output
from career.timeline import (
    build_care_episode_table,
    care_episodes,
    default_anchor,
    flow_ledger,
    person_timeline,
)
from career.web import handler_for

OPERATIONS = Path(__file__).resolve().parent.parent / 'examples' / 'operations.json'


class SeededTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        Application(self.store).seed()
        self.anchor = '2026-11-01'

    def tearDown(self):
        self.store.close()

    def test_month_helpers_are_consistent(self):
        self.assertEqual(format_month((2026, 11)), '2026-11')
        self.assertEqual(month_index('2026-11-01'), month_index('2026-11'))
        self.assertEqual(add_months((2026, 12), 1), (2027, 1))
        self.assertEqual(months_between('2027-03-01', '2026-11-01'), 4)
        self.assertEqual([format_month(m) for m in iter_months('2026-11-01', 3)], ['2026-11', '2026-12', '2027-01'])

    def test_timeline_links_episodes_per_person_not_independent_rows(self):
        timeline = person_timeline(self.store, 'p1')
        self.assertEqual([row['id'] for row in timeline], ['a1', 'a2'])
        episodes = care_episodes(self.store, 'p1')
        self.assertEqual(len(episodes), 1)
        # p2 的两段护理之间存在真实离岗月，后一段是返聘而不是新员工。
        flows = [row['flow'] for row in flow_ledger(self.store, self.anchor) if row['person'] == 'p2']
        self.assertIn('rehire', flows)

    def test_open_appointments_are_right_censored_not_never_leaving(self):
        table = build_care_episode_table(self.store, self.anchor)
        outcomes = {(row['person'], row['appointment']): row['outcome'] for row in table}
        self.assertEqual(outcomes[('p2', 'a4')], 'censored')
        self.assertEqual(outcomes[('p3', 'a5')], 'censored')
        # p1 的护理段在观察截止前结束，是事件而不是删失。
        self.assertEqual(outcomes[('p1', 'a1')], 'event')

    def test_baseline_supply_is_skill_and_site_specific_minutes(self):
        result = baseline_supply(self.store, self.anchor, 2)
        first = result['months'][0]
        cells = {(c['site'], c['skill']): c for c in first['cells']}
        # 管理岗 p1 不计；p2  west bathing 4800；p3 east basic 8400。
        self.assertEqual(cells[('west', 'bathing')]['minutes'], 4800)
        self.assertEqual(cells[('east', 'basic')]['minutes'], 8400)
        self.assertNotIn(('east', 'bathing'), cells)

    def test_certificate_expiry_removes_skill_from_future_months(self):
        result = baseline_supply(self.store, self.anchor, 3)
        # p2 的 bathing 证书在 2026-12-01 到期（半开），12 月起 west/bathing 消失。
        december = {(c['site'], c['skill']): c for c in result['months'][1]['cells']}
        self.assertNotIn(('west', 'bathing'), december)
        # p3 的 basic 证书 2027-01-01 到期。
        january = {(c['site'], c['skill']): c for c in result['months'][2]['cells']}
        self.assertNotIn(('east', 'basic'), january)

    def test_small_sample_does_not_fabricate_precision(self):
        curves = retention_curves(self.store, self.anchor)
        # 只有 3 名有日期依据的护理人员，低于证据下限，不能给精确曲线。
        self.assertTrue(curves['pooled']['insufficient_evidence'])
        self.assertIsNone(curves['pooled']['estimate'])
        expected = expected_supply(self.store, self.anchor, 2, curves)
        for month in expected['months']:
            for cell in month['cells']:
                self.assertIn('no_retention_evidence', cell['flags'])
                self.assertIsNone(cell['ci_low'])
                self.assertIsNone(cell['ci_high'])

    def test_registration_only_records_are_flagged_and_excluded_from_survival(self):
        self.store.put('history_quality', {'id': 'p3', 'effective_date_known': False, 'observation_until': self.anchor})
        curves = retention_curves(self.store, self.anchor)
        self.assertIn('p3', curves['excluded_registration_only'])
        # 确定性供给仍包含 p3，但逐格打标。
        baseline = baseline_supply(self.store, self.anchor, 1)
        east_basic = next(cell for cell in baseline['months'][0]['cells'] if cell['site'] == 'east')
        self.assertIn('registration_only', east_basic['flags'])
        self.assertEqual(east_basic['minutes'], 8400)

    def test_training_output_only_qualifies_at_course_end(self):
        output = training_output(self.store, self.anchor, 3)
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]['month'], '2027-01')
        self.assertEqual(output[0]['deployable_count'], 1)
        # 开课当月（2026-11）没有任何结业产出。
        self.assertFalse(any(row['month'] == '2026-11' for row in output))

    def test_scenarios_share_common_basis_and_mentor_time_reduces_supply(self):
        self.store.put('courses', {'id': 'c2', 'mentor': 'p2', 'learners': ['p3'], 'start': '2026-11-01', 'end': '2027-01-01', 'mentor_minutes': 500, 'skill': 'bathing'})
        scenarios = [{'id': 'base'}, {'id': 'extended', 'mentor_extra_minutes': {'c2': 300}}]
        comparison = compare_scenarios(self.store, self.anchor, 1, scenarios)
        row = next(row for row in comparison['comparison'] if row['site'] == 'west' and row['skill'] == 'bathing')
        cells = {alternative['scenario_id']: alternative for alternative in row['alternatives']}
        self.assertEqual(cells['base']['deterministic_minutes'], 4300)
        self.assertEqual(cells['extended']['deterministic_minutes'], 4000)
        # 延长带教的假设被显式记录。
        self.assertTrue(comparison['scenarios'][1]['assumptions'])

    def test_extra_learners_do_not_qualify_in_opening_month(self):
        scenarios = [{'id': 'base'}, {'id': 'more_training', 'extra_learners': {'course1': ['p3']}}]
        # p3 在结业月会取得 bathing，但其 basic 证书仍有效；该方案在首月不改变供给。
        comparison = compare_scenarios(self.store, self.anchor, 1, scenarios)
        base = comparison['scenarios'][0]['months'][0]
        trained = comparison['scenarios'][1]['months'][0]
        self.assertEqual(
            [(c['site'], c['skill'], c['expected_minutes']) for c in base['cells']],
            [(c['site'], c['skill'], c['expected_minutes']) for c in trained['cells']],
        )

    def test_duplicate_scenario_ids_rejected(self):
        with self.assertRaises(ValueError):
            compare_scenarios(self.store, self.anchor, 1, [{'id': 'x'}, {'id': 'x'}])

    def test_rehire_rate_override_validated(self):
        with self.assertRaises(ValueError):
            run_scenario(self.store, self.anchor, 1, {'id': 'bad', 'rehire_rate_override': 1.5})

    def test_cost_flag_when_rate_missing_and_cost_present_when_known(self):
        result = run_scenario(self.store, self.anchor, 1, {'id': 'base'})
        self.assertFalse(result['months'][0]['rate_missing_roles'])
        self.assertGreater(result['months'][0]['staff_cents'], 0)
        self.store.connection.execute("DELETE FROM records WHERE kind='rates'")
        result = run_scenario(self.store, self.anchor, 1, {'id': 'base'})
        self.assertIn('care', result['months'][0]['rate_missing_roles'])

    def test_gap_is_empty_without_demand_but_computed_with_demand(self):
        curves = retention_curves(self.store, self.anchor)
        expected = expected_supply(self.store, self.anchor, 1, curves)
        without = gap_against_demand(expected, None)
        self.assertTrue(all(row['gap_minutes'] is None for row in without))
        demand = {'west': {'bathing': 99999}}
        with_gap = gap_against_demand(expected, demand)
        west = next(row for row in with_gap if row['site'] == 'west')
        self.assertEqual(west['gap_minutes'], 99999 - west['expected_minutes'])

    def test_snapshot_is_immutable_and_goes_stale_after_data_change(self):
        scenarios = [{'id': 'base'}]
        snapshot = save_analysis_snapshot(self.store, 'snap-1', scenarios, 2, anchor=self.anchor, note='11月编制会')
        with self.assertRaises(ValueError):
            save_analysis_snapshot(self.store, 'snap-1', scenarios, 2, anchor=self.anchor)
        before = get_snapshot(self.store, 'snap-1')
        self.assertFalse(before['stale'])
        self.store.put('people', {'id': 'p9', 'label': '补录'})
        after = get_snapshot(self.store, 'snap-1')
        self.assertTrue(after['stale'])
        # 旧数字原样保留。
        self.assertEqual(after['result']['totals'], snapshot['result']['totals'])
        self.assertEqual([row['id'] for row in list_snapshots(self.store)], ['snap-1'])


class OperationsDatasetTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        with OPERATIONS.open(encoding='utf-8') as handle:
            data = json.load(handle)
        self.store.put_many('people', data['people'])
        for kind in ('appointments', 'certificates', 'history_quality', 'courses'):
            self.store.put_many(kind, data[kind])
        self.anchor = '2026-11-01'

    def tearDown(self):
        self.store.close()

    def test_default_anchor_matches_observation_cutoff(self):
        self.assertEqual(default_anchor(self.store).isoformat(), self.anchor)

    def test_dated_evidence_supports_km_but_registration_only_is_excluded(self):
        curves = retention_curves(self.store, self.anchor)
        self.assertGreaterEqual(curves['pooled']['people'], MIN_EVIDENCE_PEOPLE)
        self.assertFalse(curves['pooled']['insufficient_evidence'])
        self.assertEqual(len(curves['excluded_registration_only']), 5)
        # 少见资质组合（west|basic 仅 1 人）不产出精确分层估计。
        self.assertTrue(curves['by_site_skill']['west|basic']['insufficient_evidence'])
        self.assertEqual(curves['by_site_skill']['west|basic']['fallback'], 'pooled')

    def test_rehire_evidence_has_wilson_interval_and_is_person_based(self):
        evidence = rehire_evidence(self.store, self.anchor)
        self.assertEqual(evidence['eligible_leavers'], 10)
        self.assertEqual(evidence['observed_returnees'], 5)
        self.assertAlmostEqual(evidence['rate'], 0.5)
        self.assertLess(evidence['ci_low'], evidence['rate'] < evidence['ci_high'])
        # 同一个返聘者只计一次，不按岗位段重复。
        ledger = flow_ledger(self.store, self.anchor)
        rehires = [row for row in ledger if row['flow'] == 'rehire']
        self.assertEqual(len({row['person'] for row in rehires}), len(rehires))

    def test_person_never_counted_in_two_sites_in_same_month(self):
        result = baseline_supply(self.store, self.anchor, 6)
        for month in result['months']:
            seen = {}
            for cell in month['cells']:
                for person_id in cell['person_ids']:
                    self.assertNotIn(person_id, seen)
                    seen[person_id] = cell['site']

    def test_mentor_deduction_appears_in_course_months_only(self):
        result = baseline_supply(self.store, self.anchor, 6)
        course_months = {'2026-12', '2027-01', '2027-02'}
        for month in result['months']:
            west = next((c for c in month['cells'] if c['site'] == 'west' and c['skill'] == 'bathing'), None)
            if west is None:
                continue
            if month['month'] in course_months:
                self.assertIn('mentor_time_deducted', west['flags'])
            elif month['month'] == '2026-11':
                # 课程 2026-12 才开课，开课之前的月份没有带教占用。
                self.assertNotIn('mentor_time_deducted', west['flags'])

    def test_three_policy_scenarios_compare_on_same_month_and_people(self):
        scenarios = [
            {'id': 'base', 'label': '现状'},
            {'id': 'longer_mentoring', 'mentor_extra_minutes': {'winter': 600}},
            {'id': 'more_seats', 'extra_learners': {'winter': ['worker012', 'worker015']}},
            {'id': 'rehire_push', 'rehire_rate_override': 0.8},
        ]
        comparison = compare_scenarios(self.store, self.anchor, 6, scenarios)
        self.assertEqual(len(comparison['scenarios']), 4)
        for result in comparison['scenarios']:
            self.assertEqual(len(result['months']), 6)
            self.assertEqual(result['anchor'], '2026-11')
        totals = {row['scenario_id']: row for row in comparison['totals']}
        # 延长带教减少当期供给；提高返聘目标增加期望供给。
        self.assertLess(totals['longer_mentoring']['total_expected_minutes'], totals['base']['total_expected_minutes'])
        self.assertGreater(totals['rehire_push']['total_expected_minutes'], totals['base']['total_expected_minutes'])
        # 增加进修在结业月形成可部署产出。
        self.assertGreaterEqual(totals['more_seats']['graduate_count'], totals['base']['graduate_count'])

    def test_rehire_override_is_marked_assumption_while_history_is_evidence(self):
        comparison = compare_scenarios(self.store, self.anchor, 2, [{'id': 'base'}, {'id': 'push', 'rehire_rate_override': 0.7}])
        base_flags = {flag for month in comparison['scenarios'][0]['months'] for cell in month['cells'] for flag in cell['flags']}
        push_flags = {flag for month in comparison['scenarios'][1]['months'] for cell in month['cells'] for flag in cell['flags']}
        self.assertIn('rehire_rate_observed', base_flags)
        self.assertIn('rehire_target_assumption', push_flags)

    def test_snapshot_fingerprint_covers_analysed_record_kinds(self):
        first = data_fingerprint(self.store)
        self.store.put('people', {'id': 'worker-new', 'label': '新入职'})
        self.assertNotEqual(first, data_fingerprint(self.store))


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        Application(self.store).seed()
        self.server = HTTPServer(('127.0.0.1', 0), handler_for(Application(self.store)))
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.store.close()

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode('utf-8'))

    def _post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode('utf-8'))

    def test_supply_endpoint_returns_evidence_and_assumptions(self):
        value = self._get('/api/supply?anchor=2026-11-01&horizon_months=3')
        self.assertIn('baseline_supply', value)
        self.assertIn('expected_supply', value)
        self.assertIn('retention', value)
        self.assertIn('flow_ledger', value)

    def test_snapshot_round_trip_and_immutability(self):
        payload = {'id': 'meeting-1', 'anchor': '2026-11-01', 'horizon_months': 2, 'scenarios': [{'id': 'base'}]}
        saved = self._post('/api/snapshot', payload)
        self.assertEqual(saved['id'], 'meeting-1')
        loaded = self._get('/api/snapshot?id=meeting-1')
        self.assertFalse(loaded['stale'])
        rejected = self._post('/api/snapshot', payload)
        self.assertEqual(rejected[0], 400)

    def test_bad_horizon_rejected(self):
        rejected = self._get('/api/supply?horizon_months=0')
        self.assertEqual(rejected[0], 400)

    def test_scenario_endpoint_requires_scenarios(self):
        rejected = self._post('/api/scenario-compare', {'horizon_months': 2})
        self.assertEqual(rejected[0], 400)


if __name__ == '__main__':
    unittest.main()
