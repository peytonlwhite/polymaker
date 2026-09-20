from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from crypto_shadow_sizing import SizingLab, signal_features, signal_size, probability_lower
from test_crypto_shadow_expansion import candidate, quote, NOW


class SizingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = SizingLab(Path(self.temp.name)/'sizing.json',NOW)

    def enter(self, ticker='ETH-A', now=NOW, price=60, depth=20, row=None):
        row = row or candidate(ticker)
        row['close_time'] = (now+timedelta(minutes=12)).isoformat()
        self.engine.observe(row,deepcopy,
            lambda t,s:{q:{**quote(now,price), 'available_contracts':depth} for q in range(1,6)}, lambda:now)
        return self.engine.state['records'][-1] if self.engine.state['records'] else None

    def test_tiers_use_agreement_quality_and_persistence(self):
        f = signal_features(candidate(),'yes')
        self.assertEqual(signal_size(f),4)
        self.assertEqual(signal_size(dict(f,trades=60)),5)
        self.assertEqual(signal_size(dict(f,quality=.91)),1)
        self.assertEqual(signal_size(dict(f,flow60=.2,flow300=.11)),2)
        self.assertEqual(signal_size(dict(f,flow60=.28,flow300=.16)),3)

    def test_fixed_baseline_stays_three_and_signal_can_increase(self):
        r = self.enter()
        self.assertEqual(r['arms']['immediate:fixed_3']['contracts'],3)
        self.assertEqual(r['arms']['immediate:signal']['contracts'],4)
        self.assertEqual(r['arms']['immediate:confidence_bound']['status'],'no_fill')
        self.assertTrue(all(a['status']=='waiting' for k,a in r['arms'].items() if k.startswith('patient:')))

    def test_depth_reduces_dynamic_size_without_fabricating_baseline_fill(self):
        r = self.enter(depth=2)
        self.assertEqual(r['arms']['immediate:fixed_3']['status'],'no_fill')
        self.assertEqual(r['arms']['immediate:signal']['contracts'],2)

    def test_stale_quotes_never_fill_any_arm(self):
        row = candidate()
        self.engine.observe(row,deepcopy,lambda t,s:{q:quote(NOW-timedelta(seconds=3)) for q in range(1,6)},lambda:NOW)
        self.assertTrue(all(a['status']!='open' for a in self.engine.state['records'][0]['arms'].values()))

    def test_opposite_fresh_signal_cancels_capture(self):
        def refresh(row):
            row=deepcopy(row)
            for v in row['microstructure'].values():v['trade_flow_60s']=-.6;v['trade_flow_300s']=-.4
            return row
        self.engine.observe(candidate(),refresh,lambda t,s:{q:quote() for q in range(1,6)},lambda:NOW)
        self.assertEqual(self.engine.state['records'],[])

    def test_patient_arm_requires_improvement_and_preserves_missed_winner(self):
        r=self.enter()
        self.engine.settle(lambda t:{'status':'settled','result':'yes'},NOW+timedelta(minutes=13))
        self.assertEqual(r['arms']['patient:fixed_3']['status'],'no_fill')
        self.assertEqual(r['arms']['patient:fixed_3']['profit_dollars'],0)
        self.assertGreater(r['arms']['immediate:fixed_3']['profit_dollars'],0)
        s={a['arm']:a for a in self.engine.summary(NOW+timedelta(minutes=13))['arms']}
        self.assertEqual(s['patient:fixed_3']['opportunities'],1)
        self.assertEqual(s['patient:fixed_3']['no_fill'],1)
        self.assertLess(s['patient:fixed_3']['vs_immediate_fixed_delta'],0)

    def test_patient_fill_rechecks_signal_and_deadline(self):
        r=self.enter()
        row=candidate();row['yes_ask']=59
        time=NOW+timedelta(seconds=40)
        self.engine.observe(row,deepcopy,lambda t,s:{q:quote(time,59) for q in range(1,6)},lambda:time)
        self.assertEqual(r['arms']['patient:signal']['entry_price_cents'],59)
        self.assertEqual(r['arms']['patient:signal']['contracts'],4)
        self.assertEqual(len(self.engine.state['records']),1)

    def test_confirmation_after_patient_deadline_cannot_fill(self):
        r=self.enter();row=candidate();row['yes_ask']=59
        times=iter([NOW+timedelta(seconds=59),NOW+timedelta(seconds=61)])
        self.engine.observe(row,deepcopy,lambda t,s:{q:quote(NOW+timedelta(seconds=61),59) for q in range(1,6)},lambda:next(times))
        self.assertTrue(all(a['status']=='waiting' for k,a in r['arms'].items() if k.startswith('patient:')))

    def test_patient_target_improves_actual_baseline_fill_not_stale_signal(self):
        r=self.enter(price=59)
        self.assertEqual(r['signal_price_cents'],60)
        self.assertEqual(r['patient_anchor_cents'],59)
        row=candidate();row['yes_ask']=59
        time=NOW+timedelta(seconds=30)
        self.engine.observe(row,deepcopy,lambda t,s:{q:quote(time,59) for q in range(1,6)},lambda:time)
        self.assertEqual(r['arms']['patient:fixed_3']['status'],'waiting')
        row['yes_ask']=58
        self.engine.observe(row,deepcopy,lambda t,s:{q:quote(time,58) for q in range(1,6)},lambda:time)
        self.assertEqual(r['arms']['patient:fixed_3']['entry_price_cents'],58)

    def test_recovery_requires_observed_loss_and_only_adds_one_contract(self):
        first=self.enter()
        self.assertEqual(first['arms']['immediate:bounded_recovery']['contracts'],4)
        now=NOW+timedelta(minutes=13)
        self.engine.settle(lambda t:{'status':'settled','result':'no'},now)
        second=self.enter('ETH-B',now)
        arm=second['arms']['immediate:bounded_recovery']
        self.assertEqual(arm['contracts'],5)
        self.assertEqual(arm['recovery_extra_contracts'],1)
        self.assertGreater(arm['recovery_extra_cost'],0)
        self.assertLessEqual(arm['recovery_extra_cost'],5)
        self.assertEqual(second['arms']['immediate:signal']['contracts'],4)

    def test_unsettled_loss_cannot_change_sizing(self):
        self.enter()
        second=self.enter('ETH-B',NOW+timedelta(seconds=5))
        self.assertEqual(second['arms']['immediate:bounded_recovery']['contracts'],4)

    def test_drawdown_reduces_size_and_stops_all_arms_at_cap(self):
        f=signal_features(candidate(),'yes')
        risk={'cash':480,'drawdown':12,'last_loss':True,'recovery_spent':0}
        self.assertEqual(self.engine.requested_size('bounded_recovery',f,risk,None)[0],2)
        self.assertEqual(self.engine.requested_size('defensive',f,dict(risk,drawdown=22),None)[0],1)
        for policy in ('fixed_3','signal','defensive','bounded_recovery','confidence_bound'):
            self.assertEqual(self.engine.requested_size(policy,f,dict(risk,drawdown=30),.8)[0],0)
        self.assertEqual(self.engine.requested_size('bounded_recovery',f,dict(risk,drawdown=2,recovery_spent=5),None)[0],4)

    def test_daily_limit_includes_open_risk_before_accepting_new_trade(self):
        risk=self.engine.risk('immediate:fixed_3',NOW)
        risk.update(daily_loss=44.9,exposure=0)
        with patch.object(self.engine,'risk',return_value=risk):r=self.enter()
        self.assertTrue(all(a['status']!='open' for a in r['arms'].values()))

    def test_probability_bound_requires_qualified_interval_and_side_orientation(self):
        row=candidate()
        self.assertIsNone(probability_lower(row,'yes'))
        row['probability_interval']={'interval_status':'empirical_coverage_qualified','p_low':70,'p_high':80}
        self.assertEqual(probability_lower(row,'yes'),.7)
        self.assertAlmostEqual(probability_lower(row,'no'),.2)
        r=self.enter(row=row)
        self.assertGreater(r['arms']['immediate:confidence_bound']['contracts'],0)
        self.assertLessEqual(r['arms']['immediate:confidence_bound']['contracts'],5)

    def test_bound_below_cost_or_unknown_fees_prevents_fill(self):
        row=candidate();row['probability_interval']={'interval_status':'empirical_coverage_qualified','p_low':50,'p_high':70}
        r=self.enter(row=row)
        self.assertEqual(r['arms']['immediate:confidence_bound']['status'],'no_fill')
        row=candidate('ETH-B');row['fee_schedule']={}
        r=self.enter('ETH-B',row=row)
        self.assertTrue(all(a['status']!='open' for a in r['arms'].values()))

    def test_settlement_profit_uses_actual_contracts_fees_and_extra_cost(self):
        r=self.enter()
        self.engine.settle(lambda t:{'status':'settled','result':'yes'},NOW+timedelta(minutes=13))
        a=r['arms']['immediate:signal']
        self.assertAlmostEqual(a['profit_dollars'],4-a['cost_dollars'])
        self.assertAlmostEqual(a['stress_profit_dollars'],a['profit_dollars']-.04)
        before=deepcopy(r)
        self.engine.settle(lambda t:{'status':'settled','result':'no'},NOW+timedelta(minutes=14))
        self.assertEqual(r,before)

    def test_restart_keeps_registration_and_deduplicates(self):
        self.enter();self.engine.persist()
        self.engine=SizingLab(self.engine.path,NOW+timedelta(days=2))
        self.enter()
        self.assertEqual(len(self.engine.state['records']),1)
        self.assertEqual(self.engine.state['registered_at'],NOW.isoformat())

    def test_no_historical_backfill_and_corrupt_ledger_refused(self):
        self.engine=SizingLab(self.engine.path,NOW+timedelta(seconds=1))
        self.enter()
        self.assertEqual(self.engine.state['records'],[])
        self.engine.path.write_text('{invalid')
        with self.assertRaises(json.JSONDecodeError):SizingLab(self.engine.path,NOW)

    def test_reviews_are_fixed_immutable_and_never_enable_live(self):
        self.engine.reviews(NOW+timedelta(days=29))
        self.assertEqual(self.engine.state['reviews'],[])
        self.engine.reviews(NOW+timedelta(days=30))
        saved=deepcopy(self.engine.state['reviews'])
        self.engine.reviews(NOW+timedelta(days=31))
        self.assertEqual(self.engine.state['reviews'],saved)
        self.assertFalse(saved[0]['automatic_promotion'])
        self.assertEqual(saved[0]['eligible_for_manual_review'],[])


if __name__=='__main__':unittest.main()
