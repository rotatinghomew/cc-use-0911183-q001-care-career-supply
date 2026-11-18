from pathlib import Path
import json

from .people import *
from .capacity import *
from .payroll import *
from .timeline import default_anchor, flow_ledger
from .evidence import rehire_evidence, retention_curves
from .supply import baseline_supply, expected_supply, gap_against_demand, training_output
from .scenario import compare_scenarios
from .snapshot import get_snapshot, list_snapshots, save_analysis_snapshot


class Application:
    def __init__(self, store):
        self.store = store

    def seed(self):
        directory = Path(__file__).resolve().parent.parent / 'fixtures'
        for path in sorted(directory.glob('*.json')):
            if not self.store.all(path.stem):
                self.store.put_many(path.stem, json.loads(path.read_text(encoding='utf-8')))

    def demo(self):
        return {'staff': staff_at(self.store, '2026-11-01'), 'capacity': capacity(self.store, '2026-11-01', 'bathing'), 'cost': monthly_cost(self.store, '2026-11-01'), 'training': training_bookings(self.store)}

    def check(self):
        value = self.demo()
        return {'status': 'ok', 'summary': value}

    def supply_analysis(self, payload):
        anchor = payload.get('anchor') or default_anchor(self.store).isoformat()
        horizon = int(payload.get('horizon_months', 6))
        if horizon <= 0 or horizon > 36:
            raise ValueError('推演月数须位于 1 到 36 之间')
        curves = retention_curves(self.store, anchor)
        baseline = baseline_supply(self.store, anchor, horizon)
        expected = expected_supply(self.store, anchor, horizon, curves)
        demand = payload.get('demand')
        return {
            'anchor': anchor,
            'horizon_months': horizon,
            'flow_ledger': flow_ledger(self.store, anchor),
            'retention': curves,
            'rehire_evidence': rehire_evidence(self.store, anchor),
            'baseline_supply': baseline,
            'expected_supply': expected,
            'training_output': training_output(self.store, anchor, horizon),
            'gap': gap_against_demand(expected, demand),
            'demand_note': None if demand else '未提供需求口径，缺口留空；缺口数字不会被臆造',
        }

    def scenario_comparison(self, payload):
        anchor = payload.get('anchor') or default_anchor(self.store).isoformat()
        horizon = int(payload.get('horizon_months', 6))
        scenarios = payload.get('scenarios')
        if not scenarios:
            raise ValueError('至少需要一个方案')
        return compare_scenarios(self.store, anchor, horizon, scenarios, payload.get('demand'))

    def save_snapshot(self, payload):
        if not payload.get('id') or not payload.get('scenarios'):
            raise ValueError('快照需要编号与方案列表')
        horizon = int(payload.get('horizon_months', 6))
        snapshot = save_analysis_snapshot(
            self.store,
            payload['id'],
            payload['scenarios'],
            horizon,
            demand=payload.get('demand'),
            anchor=payload.get('anchor'),
            note=payload.get('note'),
        )
        return {'id': snapshot['id'], 'stale': False, 'data_fingerprint': snapshot['data_fingerprint']}

    def call(self, method, route, payload):
        if method == 'GET' and route == 'summary':
            return self.demo()
        if method == 'POST' and route == 'appointment':
            return {'revision': save_appointment(self.store, payload)}
        if method == 'GET' and route == 'capacity':
            return capacity(self.store, payload['date'], payload['skill'])
        if method == 'GET' and route == 'cost':
            return monthly_cost(self.store, payload['date'])
        if method == 'GET' and route == 'supply':
            return self.supply_analysis(payload)
        if method == 'POST' and route == 'scenario-compare':
            return self.scenario_comparison(payload)
        if method == 'POST' and route == 'snapshot':
            return self.save_snapshot(payload)
        if method == 'GET' and route == 'snapshot':
            return get_snapshot(self.store, payload['id'])
        if method == 'GET' and route == 'snapshots':
            return {'snapshots': list_snapshots(self.store)}
        raise KeyError('接口不存在')
