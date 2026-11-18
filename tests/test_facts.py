
import copy
import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from warehouse.facts import business_day, daily_facts, evidence_intervals
from warehouse.ingest import ingest
from warehouse.storage import open_store

BERLIN = ZoneInfo('Europe/Berlin')


def reading(rid, meter, at, value):
    return {'reading_id': rid, 'meter_id': meter, 'observed_at': at,
            'value': value, 'received_at': at}


def placement(pid, meter, building, valid_from, valid_to='2027-01-01T00:00:00Z', campus='c1'):
    return {'placement_id': pid, 'meter_id': meter, 'campus_id': campus,
            'building': building, 'valid_from': valid_from, 'valid_to': valid_to}


def delivery(tables, batch_id='facts-batch'):
    return {'batch_id': batch_id, 'tables': tables}


class FactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = open_store(Path(self.temp.name) / 'store.duckdb')
        self.sample = json.loads(Path('samples/delivery.json').read_text())

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def load(self, payload):
        return ingest(self.connection, copy.deepcopy(payload))

    def load_sample(self):
        return self.load(self.sample)

    def facts_by_day(self, result):
        return {(row['campus_id'], row['building'], row['business_date']): row
                for row in result['facts']}

    def test_business_day_length_follows_dst(self):
        day, start, end = business_day(BERLIN, datetime.fromisoformat('2026-10-24T12:00:00+00:00'))
        self.assertEqual(day.isoformat(), '2026-10-24')
        self.assertEqual(int((end - start).total_seconds()), 25 * 3600)
        self.assertEqual(start.isoformat(), '2026-10-24T04:00:00+00:00')
        day, start, end = business_day(BERLIN, datetime.fromisoformat('2026-03-28T12:00:00+00:00'))
        self.assertEqual(int((end - start).total_seconds()), 23 * 3600)
        # 当地 06:00 之前属于前一营业日，06:00 整属于新营业日
        day, _, _ = business_day(BERLIN, datetime.fromisoformat('2026-10-25T04:59:59+00:00'))
        self.assertEqual(day.isoformat(), '2026-10-24')
        day, _, _ = business_day(BERLIN, datetime.fromisoformat('2026-10-25T05:00:00+00:00'))
        self.assertEqual(day.isoformat(), '2026-10-25')

    def test_interval_spans_dst_day_is_allocated_by_elapsed_time(self):
        self.load_sample()
        result = daily_facts(self.connection)
        facts = self.facts_by_day(result)
        first = facts[('park1', 'hall-a', '2026-10-23')]
        second = facts[('park1', 'hall-a', '2026-10-24')]
        # 26 kWh 跨 26 小时：1 小时落入前一营业日，25 小时落入夏令时过渡日
        self.assertEqual(first['energy'], Decimal('1.000000'))
        self.assertEqual(second['energy'], Decimal('25.000000'))
        # 跨营业日的份额属于分配而非观测
        self.assertEqual(first['allocated_energy'], Decimal('1.000000'))
        self.assertEqual(first['observed_energy'], Decimal('0'))
        self.assertEqual(second['allocated_energy'], Decimal('25.000000'))

    def test_handover_boundary_and_midday_building_split(self):
        self.load_sample()
        result = daily_facts(self.connection)
        facts = self.facts_by_day(result)
        # 换表交接形成 m1 的边界区间：146 -> 180，共 34 kWh / 163 小时
        intervals = {row['kind']: row for row in evidence_intervals(self.connection, 'm1')}
        self.assertEqual(intervals['handover_out']['consumption'], Decimal('34'))
        self.assertEqual(intervals['handover_out']['status'], 'ok')
        # 10-25 白天 12:00Z 移交 hall-b：7 小时归 hall-a，17 小时归 hall-b
        self.assertEqual(facts[('park1', 'hall-a', '2026-10-25')]['energy'], Decimal('1.460123'))
        self.assertEqual(facts[('park1', 'hall-b', '2026-10-25')]['energy'], Decimal('3.546012'))
        # 不能整日跟随最新归属：hall-a 在 10-25 仍有份额
        total = sum(row['energy'] for row in result['facts'])
        self.assertEqual(total, Decimal('60.000000'))

    def test_billing_coverage_and_gap_visibility(self):
        self.load_sample()
        result = daily_facts(self.connection)
        coverage = {row['business_date']: row for row in result['coverage']}
        complete = sorted(day for day, row in coverage.items() if row['complete'])
        self.assertEqual(complete, ['2026-10-24', '2026-10-25', '2026-10-26',
            '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30'])
        # 夏令时过渡日 25 小时被完整覆盖，可供计费
        self.assertEqual(coverage['2026-10-24']['expected_seconds'], 25 * 3600)
        self.assertEqual(coverage['2026-10-24']['covered_seconds'], 25 * 3600)
        # 白天移交产生两段归属，覆盖秒数不得重复计数
        self.assertEqual(coverage['2026-10-25']['expected_seconds'], 24 * 3600)
        self.assertEqual(coverage['2026-10-25']['covered_seconds'], 24 * 3600)
        for row in coverage.values():
            self.assertLessEqual(row['covered_seconds'], row['expected_seconds'])
        # 最后一日只覆盖到换表时点：缺口可见，不能看起来像节能
        last = coverage['2026-10-31']
        self.assertFalse(last['complete'])
        self.assertEqual(last['energy'], Decimal('3.963190'))
        self.assertEqual(last['gaps'][0]['reason'], '无观测')
        self.assertEqual(last['gaps'][0]['seconds'], 5 * 3600)
        # 总量核回：归属 + 未归属 == 区间消耗
        recon = {row['meter_id']: row for row in result['reconciliation']}
        self.assertEqual(recon['m1']['consumption'], Decimal('60.000000'))
        self.assertTrue(recon['m1']['balanced'])

    def test_rollover_uses_modulus_and_observed_basis(self):
        self.load(delivery({
            'campuses': [{'campus_id': 'c1', 'timezone': 'UTC', 'label': '园区'}],
            'meters': [{'meter_id': 'm1', 'modulus': '100', 'unit': 'kWh'}],
            'readings': [reading('r1', 'm1', '2026-10-24T00:00:00Z', '90'),
                         reading('r2', 'm1', '2026-10-24T12:00:00Z', '10'),
                         reading('r3', 'm1', '2026-10-24T20:00:00Z', '30')],
            'placements': [placement('p1', 'm1', 'hall', '2026-01-01T00:00:00Z')],
        }))
        intervals = evidence_intervals(self.connection, 'm1')
        self.assertEqual([row['status'] for row in intervals], ['rollover', 'ok'])
        self.assertEqual(intervals[0]['consumption'], Decimal('20'))
        facts = self.facts_by_day(daily_facts(self.connection))
        previous = facts[('c1', 'hall', '2026-10-23')]
        current = facts[('c1', 'hall', '2026-10-24')]
        self.assertEqual(previous['energy'], Decimal('10.000000'))
        self.assertEqual(current['energy'], Decimal('30.000000'))
        # 完全落在同一营业日的区间是观测，跨日部分是分配
        self.assertEqual(current['observed_energy'], Decimal('20.000000'))
        self.assertEqual(current['allocated_energy'], Decimal('10.000000'))

    def test_rollback_without_evidence_stays_gap(self):
        self.load(delivery({
            'campuses': [{'campus_id': 'c1', 'timezone': 'UTC', 'label': '园区'}],
            'meters': [{'meter_id': 'm1', 'modulus': '0', 'unit': 'kWh'}],
            'readings': [reading('r1', 'm1', '2026-10-24T00:00:00Z', '100'),
                         reading('r2', 'm1', '2026-10-24T12:00:00Z', '40')],
            'placements': [placement('p1', 'm1', 'hall', '2026-01-01T00:00:00Z')],
        }))
        result = daily_facts(self.connection)
        # 不按负差值猜消耗：没有任何事实行
        self.assertEqual(result['facts'], [])
        self.assertEqual(result['problem_intervals'][0]['reason'], '读数回退证据不足')
        # 缺口按园区营业日可见，影响园区明确
        coverage = {row['business_date']: row for row in result['coverage']}
        self.assertFalse(coverage['2026-10-24']['complete'])
        reasons = {gap['reason'] for gap in coverage['2026-10-24']['gaps']}
        self.assertIn('读数回退证据不足', reasons)
        self.assertIn('无观测', reasons)
        recon = result['reconciliation'][0]
        self.assertEqual(recon['consumption'], Decimal('0'))
        self.assertTrue(recon['balanced'])

    def test_reset_is_self_handover_not_rollback(self):
        self.load(delivery({
            'campuses': [{'campus_id': 'c1', 'timezone': 'UTC', 'label': '园区'}],
            'meters': [{'meter_id': 'm1', 'modulus': '0', 'unit': 'kWh'}],
            'readings': [reading('r1', 'm1', '2026-10-24T00:00:00Z', '80'),
                         reading('r2', 'm1', '2026-10-25T00:00:00Z', '5')],
            'placements': [placement('p1', 'm1', 'hall', '2026-01-01T00:00:00Z')],
            'handovers': [{'handover_id': 'h1', 'old_meter': 'm1', 'new_meter': 'm1',
                           'occurred_at': '2026-10-24T12:00:00Z',
                           'old_value': '90', 'new_value': '0'}],
        }))
        intervals = evidence_intervals(self.connection, 'm1')
        # 跨越回零点的读数区间被交接边界取代，不按回退处理
        self.assertEqual([row['kind'] for row in intervals], ['handover_out', 'handover_in'])
        self.assertEqual([row['consumption'] for row in intervals], [Decimal('10'), Decimal('5')])
        result = daily_facts(self.connection)
        self.assertEqual(result['problem_intervals'], [])
        facts = self.facts_by_day(result)
        self.assertEqual(facts[('c1', 'hall', '2026-10-23')]['energy'], Decimal('5.000000'))
        self.assertEqual(facts[('c1', 'hall', '2026-10-24')]['energy'], Decimal('10.000000'))

    def test_backfill_replaces_estimate_but_total_reconciles(self):
        self.load_sample()
        before = self.facts_by_day(daily_facts(self.connection))
        self.assertEqual(before[('park1', 'hall-a', '2026-10-23')]['energy'], Decimal('1.000000'))
        self.assertEqual(before[('park1', 'hall-a', '2026-10-24')]['energy'], Decimal('25.000000'))
        # 补来中间读数：原先 1/25 的估分被替换
        self.load({'batch_id': 'backfill', 'tables': {'readings': [
            reading('r1b', 'm1', '2026-10-24T15:00:00Z', '133')]}})
        after = self.facts_by_day(daily_facts(self.connection))
        self.assertEqual(after[('park1', 'hall-a', '2026-10-23')]['energy'], Decimal('1.083333'))
        self.assertEqual(after[('park1', 'hall-a', '2026-10-24')]['energy'], Decimal('24.916667'))
        # 总量仍能核回原始区间的 26 kWh
        pair = after[('park1', 'hall-a', '2026-10-23')]['energy'] + \
            after[('park1', 'hall-a', '2026-10-24')]['energy']
        self.assertEqual(pair, Decimal('26.000000'))
        recon = {row['meter_id']: row for row in daily_facts(self.connection)['reconciliation']}
        self.assertTrue(recon['m1']['balanced'])

    def test_unattributed_span_stays_visible(self):
        self.load(delivery({
            'campuses': [{'campus_id': 'c1', 'timezone': 'UTC', 'label': '园区'}],
            'meters': [{'meter_id': 'm1', 'modulus': '0', 'unit': 'kWh'}],
            'readings': [reading('r1', 'm1', '2026-10-24T00:00:00Z', '0'),
                         reading('r2', 'm1', '2026-10-25T00:00:00Z', '24')],
            'placements': [placement('p1', 'm1', 'hall', '2026-10-24T12:00:00Z')],
        }))
        result = daily_facts(self.connection)
        facts = self.facts_by_day(result)
        self.assertEqual(facts[('c1', 'hall', '2026-10-24')]['energy'], Decimal('12.000000'))
        self.assertEqual(len(result['unattributed']), 1)
        self.assertEqual(result['unattributed'][0]['energy'], Decimal('12.000000'))
        self.assertEqual(result['unattributed'][0]['reason'], '归属缺失')
        recon = result['reconciliation'][0]
        self.assertEqual(recon['consumption'], Decimal('24.000000'))
        self.assertEqual(recon['attributed'] + recon['unattributed'], Decimal('24.000000'))
        self.assertTrue(recon['balanced'])
