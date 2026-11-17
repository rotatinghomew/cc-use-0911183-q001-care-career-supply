from .people import staff_at


def monthly_cost(store, as_of):
    total = 0
    rows = []
    for person in staff_at(store, as_of):
        rate = store.get('rates', person['role'])['cents_per_hour']
        cents = (person['minutes_per_month'] * rate + 30) // 60
        rows.append({'person': person['id'], 'site': person['site'], 'cents': cents})
        total += cents
    return {'date': as_of, 'cents': total, 'rows': rows}
