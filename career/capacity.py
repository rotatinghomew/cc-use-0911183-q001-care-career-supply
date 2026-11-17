from datetime import date
from .people import staff_at


def valid_skills(store, person, as_of):
    day = date.fromisoformat(as_of)
    return sorted({row['skill'] for row in store.all('certificates') if row['person'] == person and date.fromisoformat(row['start']) <= day < date.fromisoformat(row['end'])})


def capacity(store, as_of, skill):
    totals = {}
    members = []
    for person in staff_at(store, as_of):
        if person['role'] != 'care' or skill not in valid_skills(store, person['id'], as_of):
            continue
        totals[person['site']] = totals.get(person['site'], 0) + person['minutes_per_month']
        members.append(person['id'])
    return {'date': as_of, 'skill': skill, 'minutes_by_site': totals, 'people': sorted(members)}


def training_bookings(store):
    result = []
    for course in store.all('courses'):
        store.get('people', course['mentor'])
        if len(set(course['learners'])) != len(course['learners']):
            raise ValueError('学员重复')
        for person in course['learners']:
            store.get('people', person)
        result.append({'course': course['id'], 'learners': len(course['learners']), 'mentor_minutes': course['mentor_minutes'], 'ends': course['end']})
    return result
