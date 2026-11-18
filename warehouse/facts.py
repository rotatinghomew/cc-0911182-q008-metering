"""营业日用能事实。

把表计证据区间的消耗按实际经过时间分配到园区营业日。营业日从当地
06:00 开始，使用园区时区；夏令时过渡日按真实长短（23/25 小时）处理，
不假设固定二十四小时。跨营业日或跨归属的份额属于按时间比例的分配，
不是观测，事实行会标明。证据不足（读数回退且无量程或交接依据）的区间
保留缺口，不按负差值猜测消耗。

事实完全由当前业务表推导，不写回仓库：补录中间读数后重算即替换原先
估分。每个区间的份额之和恒等于区间消耗（舍入差由末段吸收），因此总量
始终可以核回原始区间。
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .storage import records

UTC = timezone.utc

DAY_START = time(6, 0)
QUANT = Decimal('0.000001')
ZERO = Decimal('0')
NO_OBSERVATION = '无观测'
MISSING_PLACEMENT = '归属缺失'
UNKNOWN_CAMPUS = '园区未登记'


def _parse(stamp):
    return datetime.fromisoformat(stamp)


def business_day(tz, stamp):
    """时刻所在的营业日：返回 (日期, 开始, 结束)。

    边界按当地时间 06:00 确定，再统一换算为 UTC 返回：同一 tzinfo 的
    两个时间相减是墙钟语义，夏令时过渡日会被错算成固定二十四小时，
    因此所有经过时长必须在 UTC 下计算。
    """
    local = stamp.astimezone(tz)
    day = local.date()
    start = datetime.combine(day, DAY_START, tzinfo=tz)
    if local < start:
        day -= timedelta(days=1)
        start = datetime.combine(day, DAY_START, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), DAY_START, tzinfo=tz)
    return day, start.astimezone(UTC), end.astimezone(UTC)


def _classify(interval, modulus):
    """单个证据区间定性：正常、量程翻转或证据不足的缺口。"""
    delta = interval['end_value'] - interval['start_value']
    if delta >= 0:
        return 'ok', delta, None
    if (interval['kind'] == 'reading' and modulus is not None
            and interval['start_value'] < modulus and interval['end_value'] < modulus):
        consumption = modulus - interval['start_value'] + interval['end_value']
        if consumption >= 0:
            return 'rollover', consumption, None
    reason = '读数回退证据不足' if interval['kind'] == 'reading' else '交接读数冲突'
    return 'gap', None, reason


def evidence_intervals(connection, meter_id):
    """表计的证据区间：相邻读数区间加上换表/回零交接边界区间，逐段定性。

    交接（含旧表换新表与表计回零，回零即新旧表相同的交接）发生的时点会
    截断跨越它的读数区间，改由档案中的交接读数形成边界区间；量程翻转依
    据表计量程参数折算。除此之外的负差值不按猜测补量，保留为缺口。
    """
    meter_rows = records(connection, 'SELECT * FROM meters WHERE meter_id=?', [meter_id])
    modulus = meter_rows[0]['modulus'] if meter_rows else None
    if modulus is not None and modulus <= 0:
        modulus = None
    readings = records(connection, 'SELECT * FROM readings WHERE meter_id=?', [meter_id])
    readings.sort(key=lambda row: (_parse(row['observed_at']), row['reading_id']))
    handovers = records(connection,
        'SELECT * FROM handovers WHERE old_meter=? OR new_meter=? ORDER BY occurred_at,handover_id',
        [meter_id, meter_id])
    cuts = sorted({_parse(row['occurred_at']) for row in handovers})
    intervals = []
    for left, right in zip(readings, readings[1:]):
        t0, t1 = _parse(left['observed_at']), _parse(right['observed_at'])
        if any(t0 < cut < t1 for cut in cuts):
            continue  # 交接点截断，由交接边界区间取代
        intervals.append({'meter_id': meter_id, 'kind': 'reading',
            'from': left['observed_at'], 'to': right['observed_at'],
            'start_value': left['value'], 'end_value': right['value'],
            'left': left['reading_id'], 'right': right['reading_id'],
            'handover_id': None})
    for row in handovers:
        cut = _parse(row['occurred_at'])
        if row['old_meter'] == meter_id:
            before = [item for item in readings if _parse(item['observed_at']) < cut]
            if before and not any(_parse(before[-1]['observed_at']) < other < cut for other in cuts):
                intervals.append({'meter_id': meter_id, 'kind': 'handover_out',
                    'from': before[-1]['observed_at'], 'to': row['occurred_at'],
                    'start_value': before[-1]['value'], 'end_value': row['old_value'],
                    'left': before[-1]['reading_id'], 'right': None,
                    'handover_id': row['handover_id']})
        if row['new_meter'] == meter_id:
            after = [item for item in readings if _parse(item['observed_at']) > cut]
            if after and not any(cut < other < _parse(after[0]['observed_at']) for other in cuts):
                intervals.append({'meter_id': meter_id, 'kind': 'handover_in',
                    'from': row['occurred_at'], 'to': after[0]['observed_at'],
                    'start_value': row['new_value'], 'end_value': after[0]['value'],
                    'left': None, 'right': after[0]['reading_id'],
                    'handover_id': row['handover_id']})
    for interval in intervals:
        interval['seconds'] = int((_parse(interval['to']) - _parse(interval['from'])).total_seconds())
        if interval['seconds'] <= 0:
            interval.update(status='duplicate', consumption=None, reason='重复时点')
            continue
        status, consumption, reason = _classify(interval, modulus)
        interval.update(status=status, consumption=consumption, reason=reason)
    intervals.sort(key=lambda row: (_parse(row['from']), _parse(row['to'])))
    return intervals


def _placement_segments(placements, t0, t1):
    """把 [t0, t1) 按归属有效区间切开；无归属的片段保留为 None。"""
    segments = []
    cursor = t0
    for placement in placements:
        start = max(t0, placement['from'])
        end = min(t1, placement['to'])
        if start >= end:
            continue
        if start < cursor:
            raise ValueError('同一时点表计归属重叠')
        if start > cursor:
            segments.append((cursor, start, None))
        segments.append((start, end, placement))
        cursor = end
    if cursor < t1:
        segments.append((cursor, t1, None))
    return segments


def _split_days(segments, campuses):
    """把归属片段按园区营业日边界切开，返回 (起, 讫, 归属|None) 片段。"""
    pieces = []
    for start, end, placement in segments:
        if placement is None:
            pieces.append((start, end, None))
            continue
        tz = campuses.get(placement['campus_id'])
        if tz is None:
            pieces.append((start, end, UNKNOWN_CAMPUS))
            continue
        cursor = start
        while cursor < end:
            day, _, boundary = business_day(tz, cursor)
            nxt = min(boundary, end)
            pieces.append((cursor, nxt, {
                'campus_id': placement['campus_id'],
                'building': placement['building'],
                'business_date': day.isoformat()}))
            cursor = nxt
    return pieces


def _allocate(consumption, pieces):
    """按实际经过秒数分配区间消耗；末段吸收舍入差，份额之和恒等于总量。"""
    total = sum(int((end - start).total_seconds()) for start, end, _ in pieces)
    shares = []
    accrued = ZERO
    for index, (start, end, _) in enumerate(pieces):
        if index == len(pieces) - 1:
            share = consumption - accrued
        else:
            seconds = int((end - start).total_seconds())
            share = (consumption * seconds / total).quantize(QUANT)
            accrued += share
        shares.append(share)
    return shares


def _subtract(spans, cut_start, cut_end):
    result = []
    for start, end in spans:
        if cut_end <= start or cut_start >= end:
            result.append((start, end))
            continue
        if start < cut_start:
            result.append((start, cut_start))
        if cut_end < end:
            result.append((cut_end, end))
    return result


def _label_gaps(meter_id, start, end, gap_spans):
    """给未覆盖片段标注原因：落在问题区间内的用区间原因，其余为无观测。"""
    labeled = []
    cursor = start
    relevant = sorted((max(start, s), min(end, e), reason)
        for meter, s, e, reason in gap_spans if meter == meter_id and s < end and start < e)
    for span_start, span_end, reason in relevant:
        if cursor < span_start:
            labeled.append((cursor, span_start, NO_OBSERVATION))
        labeled.append((span_start, span_end, reason))
        cursor = span_end
    if cursor < end:
        labeled.append((cursor, end, NO_OBSERVATION))
    return labeled


def daily_facts(connection):
    """推导营业日用能事实、覆盖核对与总量核回。

    返回：
    - facts：建筑×营业日×表计的用能事实，区分观测与分配份额；
    - coverage：园区×营业日的覆盖核对，complete 才可供计费采用，
      缺口带原因，不会被当作节能；
    - unattributed：无归属片段及其分摊能量，不静默丢弃；
    - problem_intervals：证据不足或重复的区间，供运维核对；
    - reconciliation：按表计的总量核回（归属 + 未归属 == 区间消耗）。
    """
    campuses = {row['campus_id']: ZoneInfo(row['timezone'])
        for row in records(connection, 'SELECT * FROM campuses')}
    placements = {}
    for row in records(connection, 'SELECT * FROM placements'):
        placements.setdefault(row['meter_id'], []).append({
            'campus_id': row['campus_id'], 'building': row['building'],
            'from': _parse(row['valid_from']), 'to': _parse(row['valid_to'])})
    for rows in placements.values():
        rows.sort(key=lambda row: row['from'])

    facts = {}
    unattributed = []
    problem_intervals = []
    reconciliation = []
    touched = {}
    for (meter_id,) in connection.execute('SELECT meter_id FROM meters ORDER BY meter_id').fetchall():
        meter_placements = placements.get(meter_id, [])
        consumption_total = ZERO
        attributed_total = ZERO
        unattributed_total = ZERO
        for interval in evidence_intervals(connection, meter_id):
            t0, t1 = _parse(interval['from']), _parse(interval['to'])
            if interval['status'] in ('ok', 'rollover'):
                consumption_total += interval['consumption']
                pieces = _split_days(_placement_segments(meter_placements, t0, t1), campuses)
                shares = _allocate(interval['consumption'], pieces)
                basis = '观测' if len(pieces) == 1 and pieces[0][2] not in (None, UNKNOWN_CAMPUS) else '分配'
                for (start, end, target), share in zip(pieces, shares):
                    seconds = int((end - start).total_seconds())
                    if target is None or target == UNKNOWN_CAMPUS:
                        reason = MISSING_PLACEMENT if target is None else UNKNOWN_CAMPUS
                        unattributed.append({'meter_id': meter_id, 'from': start.isoformat(),
                            'to': end.isoformat(), 'seconds': seconds, 'energy': share,
                            'reason': reason})
                        unattributed_total += share
                        continue
                    attributed_total += share
                    key = (target['campus_id'], target['building'], target['business_date'], meter_id)
                    row = facts.setdefault(key, {'campus_id': target['campus_id'],
                        'building': target['building'], 'business_date': target['business_date'],
                        'meter_id': meter_id, 'energy': ZERO, 'observed_energy': ZERO,
                        'allocated_energy': ZERO, 'seconds': 0, 'sources': []})
                    row['energy'] += share
                    row['observed_energy' if basis == '观测' else 'allocated_energy'] += share
                    row['seconds'] += seconds
                    source = {'kind': interval['kind'], 'from': interval['from'], 'to': interval['to']}
                    if source not in row['sources']:
                        row['sources'].append(source)
                    touched.setdefault((target['campus_id'], target['business_date']),
                        {'pieces': [], 'gaps': []})['pieces'].append((meter_id, start, end))
            else:
                problem_intervals.append({'meter_id': meter_id, 'kind': interval['kind'],
                    'from': interval['from'], 'to': interval['to'],
                    'seconds': interval['seconds'], 'reason': interval['reason']})
                if interval['status'] != 'gap':
                    continue
                for start, end, placement in _placement_segments(meter_placements, t0, t1):
                    seconds = int((end - start).total_seconds())
                    if placement is None or placement['campus_id'] not in campuses:
                        reason = interval['reason'] + '，' + (
                            MISSING_PLACEMENT if placement is None else UNKNOWN_CAMPUS)
                        unattributed.append({'meter_id': meter_id, 'from': start.isoformat(),
                            'to': end.isoformat(), 'seconds': seconds, 'energy': None,
                            'reason': reason})
                        continue
                    tz = campuses[placement['campus_id']]
                    cursor = start
                    while cursor < end:
                        day, _, boundary = business_day(tz, cursor)
                        nxt = min(boundary, end)
                        touched.setdefault((placement['campus_id'], day.isoformat()),
                            {'pieces': [], 'gaps': []})['gaps'].append(
                                (meter_id, cursor, nxt, interval['reason']))
                        cursor = nxt
        reconciliation.append({'meter_id': meter_id, 'consumption': consumption_total,
            'attributed': attributed_total, 'unattributed': unattributed_total,
            'balanced': attributed_total + unattributed_total == consumption_total})

    coverage = []
    for campus_id, business_date in sorted(touched):
        tz = campuses[campus_id]
        day = date.fromisoformat(business_date)
        day_start = datetime.combine(day, DAY_START, tzinfo=tz).astimezone(UTC)
        day_end = datetime.combine(day + timedelta(days=1), DAY_START, tzinfo=tz).astimezone(UTC)
        state = touched[(campus_id, business_date)]
        expected_seconds = 0
        covered_seconds = 0
        gaps = []
        for other_meter, rows in sorted(placements.items()):
            for placement in rows:
                if placement['campus_id'] != campus_id:
                    continue
                start = max(day_start, placement['from'])
                end = min(day_end, placement['to'])
                if start >= end:
                    continue
                expected_seconds += int((end - start).total_seconds())
                spans = [(start, end)]
                for piece_meter, piece_start, piece_end in state['pieces']:
                    if piece_meter == other_meter:
                        spans = _subtract(spans, piece_start, piece_end)
                uncovered = sum(int((span_end - span_start).total_seconds())
                    for span_start, span_end in spans)
                covered_seconds += int((end - start).total_seconds()) - uncovered
                for gap_start, gap_end in spans:
                    for part_start, part_end, reason in _label_gaps(
                            other_meter, gap_start, gap_end, state['gaps']):
                        gaps.append({'meter_id': other_meter, 'building': placement['building'],
                            'from': part_start.isoformat(), 'to': part_end.isoformat(),
                            'seconds': int((part_end - part_start).total_seconds()),
                            'reason': reason})
        energy = sum(row['energy'] for row in facts.values()
            if row['campus_id'] == campus_id and row['business_date'] == business_date)
        coverage.append({'campus_id': campus_id, 'business_date': business_date,
            'expected_seconds': expected_seconds, 'covered_seconds': covered_seconds,
            'complete': not gaps and expected_seconds > 0, 'energy': energy, 'gaps': gaps})

    fact_rows = sorted(facts.values(),
        key=lambda row: (row['campus_id'], row['business_date'], row['building'], row['meter_id']))
    unattributed.sort(key=lambda row: (row['meter_id'], row['from']))
    return {'facts': fact_rows, 'coverage': coverage, 'unattributed': unattributed,
        'problem_intervals': problem_intervals, 'reconciliation': reconciliation}
