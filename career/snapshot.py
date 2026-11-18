"""编制会方案的不可变快照。

已保存的方案必须保留**当时的数据与假设解释**：后续补录经历后允许
重新分析，但不能悄悄改掉原来供编制会讨论的数字。做法是：

1. 保存时计算基础业务记录（people/appointments/certificates/
   courses/rates/history_quality）的内容指纹；
2. 快照整体写入 ``snapshots`` 类型记录，写后不可变，重复保存同编号
   直接报冲突，不覆盖原数字；
3. 重新读取时用当前数据重算指纹并与快照内指纹比对，不一致时显式
   标注 ``stale``，仍原样返回旧结果；新一轮分析应另存新快照。
"""

import hashlib
import json

from .scenario import compare_scenarios
from .timeline import default_anchor

FINGERPRINT_KINDS = ('people', 'appointments', 'certificates', 'courses', 'rates', 'history_quality')


def data_fingerprint(store):
    """对参与分析的基础记录做规范化哈希，与写入顺序无关。"""
    digest = hashlib.sha256()
    for kind in FINGERPRINT_KINDS:
        for row in sorted(store.all(kind), key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)):
            digest.update(kind.encode('utf-8'))
            digest.update(json.dumps(row, ensure_ascii=False, sort_keys=True).encode('utf-8'))
    return digest.hexdigest()


def save_analysis_snapshot(store, snapshot_id, scenarios, horizon_months, demand=None, anchor=None, note=None):
    if store.revision('snapshots', snapshot_id):
        raise ValueError(f'快照 {snapshot_id} 已存在，保存的编制会数字不可覆盖')
    anchor = month_anchor(store, anchor)
    result = compare_scenarios(store, anchor, horizon_months, scenarios, demand)
    snapshot = {
        'id': snapshot_id,
        'saved_at_anchor': result['anchor'],
        'horizon_months': horizon_months,
        'demand': demand,
        'note': note,
        'data_fingerprint': data_fingerprint(store),
        'scenario_parameters': [
            {'id': scenario['id'], 'parameters': result_part['parameters'], 'assumptions': result_part['assumptions']}
            for scenario, result_part in zip(scenarios, result['scenarios'])
        ],
        'result': result,
    }
    store.put('snapshots', snapshot)
    return snapshot


def month_anchor(store, anchor):
    return anchor or default_anchor(store).isoformat()


def get_snapshot(store, snapshot_id):
    snapshot = store.get('snapshots', snapshot_id)
    current = data_fingerprint(store)
    enriched = dict(snapshot)
    enriched['current_fingerprint'] = current
    enriched['stale'] = current != snapshot['data_fingerprint']
    if enriched['stale']:
        enriched['stale_reason'] = '基础履历在快照保存后发生补录或更正，快照数字仍为保存当时口径；请另存新快照做重新分析'
    return enriched


def list_snapshots(store):
    current = data_fingerprint(store)
    rows = []
    for snapshot in store.all('snapshots'):
        rows.append(
            {
                'id': snapshot['id'],
                'saved_at_anchor': snapshot['saved_at_anchor'],
                'horizon_months': snapshot['horizon_months'],
                'note': snapshot.get('note'),
                'stale': snapshot['data_fingerprint'] != current,
            }
        )
    return rows
