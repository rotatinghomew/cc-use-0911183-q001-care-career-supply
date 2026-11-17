from pathlib import Path
import json

from .people import *
from .capacity import *
from .payroll import *


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

    def call(self, method, route, payload):
        if method == 'GET' and route == 'summary':
            return self.demo()
        if method == 'POST' and route == 'appointment':
            return {'revision': save_appointment(self.store, payload)}
        if method == 'GET' and route == 'capacity':
            return capacity(self.store, payload['date'], payload['skill'])
        if method == 'GET' and route == 'cost':
            return monthly_cost(self.store, payload['date'])
        raise KeyError('接口不存在')
