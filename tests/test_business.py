import unittest
from career.app import Application
from career.store import Store
from career.people import staff_at, save_appointment
from career.capacity import capacity, valid_skills, training_bookings
from career.payroll import monthly_cost


class CareerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        Application(self.store).seed()

    def tearDown(self):
        self.store.close()

    def test_role_transition_uses_half_open_dates(self):
        before = {row['id']: row for row in staff_at(self.store, '2026-03-31')}
        after = {row['id']: row for row in staff_at(self.store, '2026-04-01')}
        self.assertEqual(before['p1']['role'], 'care')
        self.assertEqual(after['p1']['role'], 'manager')

    def test_rehire_gap(self):
        self.assertNotIn('p2', [row['id'] for row in staff_at(self.store, '2026-09-01')])
        self.assertIn('p2', [row['id'] for row in staff_at(self.store, '2026-10-01')])

    def test_certificate_end_is_exclusive(self):
        self.assertEqual(valid_skills(self.store, 'p2', '2026-12-01'), [])

    def test_manager_is_not_frontline_capacity(self):
        self.assertEqual(capacity(self.store, '2026-11-01', 'bathing')['minutes_by_site'], {'west': 4800})

    def test_cost_matches_people(self):
        value = monthly_cost(self.store, '2026-11-01')
        self.assertEqual(value['cents'], sum(row['cents'] for row in value['rows']))

    def test_overlap_rejected_without_write(self):
        row = dict(self.store.get('appointments', 'a1'), id='overlap')
        with self.assertRaises(ValueError):
            save_appointment(self.store, row)
        self.assertEqual(len(self.store.all('appointments')), 5)

    def test_adjacent_appointment_allowed(self):
        row = dict(self.store.get('appointments', 'a3'), id='new', start='2026-08-01', end='2026-09-01')
        self.assertEqual(save_appointment(self.store, row), 1)

    def test_unknown_person_rejected(self):
        row = dict(self.store.get('appointments', 'a1'), id='bad', person='missing')
        with self.assertRaises(KeyError):
            save_appointment(self.store, row)

    def test_training_contains_real_participants(self):
        self.assertEqual(training_bookings(self.store)[0]['learners'], 2)

    def test_empty_future_returns_no_staff(self):
        self.assertEqual(staff_at(self.store, '2020-01-01'), [])
