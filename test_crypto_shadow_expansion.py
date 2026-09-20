from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from crypto_shadow_expansion import Expansion, POLICY_HASH
from crypto_shadow_challenger import digest
from crypto_shadow_worker import single_instance, spots_from_stream
from crypto_evidence import iter_jsonl

NOW = datetime(2026, 9, 8, 22, tzinfo=timezone.utc)


def candidate(ticker='ETH-A'):
    venue = {'connected':True, 'stream_age_seconds':.1, 'book_age_seconds':.1,
             'mid':100, 'trade_flow_60s':.6, 'trade_flow_300s':.4, 'trade_count_60s':50}
    return {'ticker':ticker, 'asset':'ETH', 'market_lane':'crypto_15m', 'market_kind':'above',
        'close_time':(NOW+timedelta(minutes=12)).isoformat(), 'minutes_to_close':12,
        'yes_ask':60, 'no_ask':41, 'fee_schedule':{'authoritative':True},
        'data_quality':{'score':1}, 'probability':{'market_implied_yes':60},
        'microstructure':{'coinbase':dict(venue), 'kraken':dict(venue)},
        'kalshi_microstructure':{'fresh':True, 'sequence_valid':True, 'book_consistent':True,
                                'age_seconds':.1, 'spread_yes_cents':1},
        'model':{'spot':100}}


def quote(now=NOW, price=60):
    return {'fetched_at':now.isoformat(), 'sequence_valid':True, 'source':'independent_rest',
            'full_size_entry_price_cents':price, 'available_contracts':20}


def artifact(intercept=.5, features=None):
    features = features or ['utc_time_sin']
    model = {'features':features,'means':[0]*len(features),'stds':[1]*len(features),
             'weights':[0]*len(features),'intercept':intercept}
    return {'model':model, 'model_hash':digest(model), 'frozen_at':(NOW-timedelta(minutes=1)).isoformat()}


def regime_snapshot(now=NOW, direction='up'):
    return {'generated_at':now.isoformat(),'assets':{'ETH':{'forecast_horizons':{'1h':{
        'available':True,'data_quality':1,'direction':direction,'probability_up':.6}}}}}


def spots(now=NOW, mid=100):
    return {'ETH':{'mid':mid,'observed_at':now.isoformat()}}


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Expansion(Path(self.temp.name)/'new_research.json',NOW)
        self.engine.install_model(artifact(),NOW)

    def enter_model(self, row=None, confirmation=None, refresh=None):
        self.engine.challenger(row or candidate(), refresh or (lambda r:deepcopy(r)),
            confirmation or (lambda *_:quote()), now_fn=lambda:NOW)

    def enter_eth(self, row=None):
        self.engine.eth_entry(row or candidate(),lambda r:deepcopy(r),lambda *_:quote(),now_fn=lambda:NOW)

    def test_challenger_first_decision_fees_and_official_settlement(self):
        self.enter_model()
        self.enter_model()
        self.assertEqual(len(self.engine.state['forecasts']),1)
        self.assertEqual(len(self.engine.state['records']),1)
        self.assertAlmostEqual(self.engine.state['records'][0]['cost_dollars'],1.86)
        after = NOW+timedelta(minutes=13)
        self.engine.settle(lambda _: {'status':'closed','result':'yes'},after)
        self.assertEqual(self.engine.state['records'][0]['status'],'open')
        self.engine.settle(lambda _: {'status':'finalized','result':'yes'},after)
        self.assertAlmostEqual(self.engine.summary(after)['challenger']['profit_dollars'],1.14)
        self.assertAlmostEqual(self.engine.summary(after)['challenger']['stress_profit_dollars'],1.11)
        self.assertIsNotNone(self.engine.summary(after)['challenger']['brier_improvement'])

    def test_stale_confirmation_records_forecast_without_fake_fill_or_retry_selection(self):
        self.enter_model(confirmation=lambda *_:quote(NOW-timedelta(seconds=3)))
        self.enter_model()
        self.assertEqual(len(self.engine.state['forecasts']),1)
        self.assertEqual(self.engine.state['records'],[])
        self.assertEqual(self.engine.state['rejections']['challenger:confirmation_quote_stale'],1)

    def test_missing_features_are_not_zero_imputed_for_live_observation(self):
        self.engine.state['model']=None
        self.engine.install_model(artifact(features=['btc_relative_return_60s_bps']),NOW)
        self.enter_model()
        self.assertEqual(self.engine.state['forecasts'],[])
        self.assertTrue(any('model_feature_missing' in k for k in self.engine.state['rejections']))

    def test_reconfirmation_recomputes_probability_and_rejects_disappeared_edge(self):
        def refresh(row):
            row=deepcopy(row);row['probability']['market_implied_yes']=20
            return row
        self.enter_model(refresh=refresh)
        self.assertEqual(self.engine.state['records'],[])
        self.assertIn('challenger:edge_below_2c_after_fees_and_stress',self.engine.state['rejections'])

    def test_refreshed_interval_binds_to_training_features_without_mutating_input(self):
        self.engine.state['model']=None
        self.engine.install_model(artifact(features=['probability_low_yes']),NOW)
        row=candidate()
        row['probability']['p_low']=10
        row['probability_interval']={'p_yes':60,'p_low':53,'p_high':67}
        self.enter_model(row)
        forecast=self.engine.state['forecasts'][0]
        self.assertEqual(forecast['research_features']['values']['probability_low_yes'],53)
        self.assertEqual(row['probability']['p_low'],10)
        self.assertEqual(len(self.engine.state['records']),1)

    def test_portfolio_caps_apply_before_confirmation(self):
        self.engine.state['records']=[{'captured_at':NOW.isoformat(),'status':'open',
            'cost_dollars':1,'close_time':candidate()['close_time']} for _ in range(4)]
        def forbidden(*_):self.fail('quote callback must not be invoked at capacity')
        self.enter_model(confirmation=forbidden)
        self.assertIn('challenger:virtual_portfolio_limit',self.engine.state['rejections'])

    def test_model_is_frozen_and_pre_registration_cannot_enter(self):
        with self.assertRaisesRegex(ValueError,'cannot be replaced'):
            self.engine.install_model(artifact(intercept=.9),NOW)
        self.engine.challenger(candidate(),lambda r:r,lambda *_:quote(),now_fn=lambda:NOW-timedelta(seconds=1))
        self.assertEqual(self.engine.state['forecasts'],[])
        invalid=artifact();invalid['model']['weights'][0]=3
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            self.engine.install_model(invalid,NOW)

    def test_eth_wait_requires_observed_price_improvement(self):
        self.enter_eth()
        later=NOW+timedelta(seconds=20)
        row=candidate();row['yes_ask']=59
        self.engine.advance_eth([row],lambda r:deepcopy(r),lambda *_:quote(later,59),now_fn=lambda:later)
        pair=self.engine.state['eth_pairs'][0]
        self.assertEqual(pair['wait']['status'],'open')
        self.assertEqual(pair['polls'],1)
        self.engine.settle(lambda _: {'status':'settled','result':'yes'},NOW+timedelta(minutes=13))
        s=self.engine.summary()['eth_entry']
        self.assertEqual(s['filled'],1)
        self.assertAlmostEqual(s['paired_stress_delta'],.03)

    def test_eth_no_fill_keeps_missed_winners_in_paired_denominator(self):
        self.enter_eth()
        later=NOW+timedelta(seconds=61)
        def forbidden(*_):self.fail('cannot fill after deadline')
        self.engine.advance_eth([candidate()],forbidden,forbidden,now_fn=lambda:later)
        self.engine.settle(lambda _: {'status':'settled','result':'yes'},NOW+timedelta(minutes=13))
        s=self.engine.summary()['eth_entry']
        self.assertEqual((s['settled'],s['missed_winners'],s['filled']),(1,1,0))
        self.assertEqual(s['wait_profit'],0)
        self.assertLess(s['paired_stress_delta'],0)

    def test_eth_confirmation_finishing_after_deadline_never_fills(self):
        self.enter_eth()
        row=candidate();row['yes_ask']=59
        clock=iter([NOW+timedelta(seconds=s) for s in [58,58,58,61]])
        self.engine.advance_eth([row],lambda r:r,lambda *_:quote(NOW+timedelta(seconds=61),59),now_fn=lambda:next(clock))
        self.assertEqual(self.engine.state['eth_pairs'][0]['wait']['status'],'waiting')
        self.assertIn('eth_wait:confirmation_after_wait_deadline',self.engine.state['rejections'])

    def test_regime_nonoverlap_and_same_sample_comparators(self):
        self.engine.regime(regime_snapshot(),spots(),NOW)
        t=NOW+timedelta(minutes=59)
        self.engine.regime(regime_snapshot(t),spots(t),t)
        self.assertEqual(len(self.engine.state['regime']),1)
        t=NOW+timedelta(hours=1)
        self.engine.regime({},spots(t,99),t)
        row=self.engine.state['regime'][0]
        self.assertTrue(row['reversal_correct'])
        self.assertFalse(row['original_correct'])
        self.assertFalse(row['always_up_correct'])
        self.assertNotIn('profit_dollars',row)
        self.assertEqual(self.engine.summary(t)['regime']['settled'],1)

    def test_regime_late_exit_is_unscored_not_backfilled(self):
        self.engine.regime(regime_snapshot(),spots(),NOW)
        t=NOW+timedelta(hours=1,seconds=61)
        self.engine.regime({},spots(t,99),t)
        row=self.engine.state['regime'][0]
        self.assertEqual(row['status'],'unscored')
        self.assertNotIn('result',row)
        self.assertEqual(self.engine.summary(t)['regime']['settled'],0)

    def test_regime_exit_spot_must_be_after_due_not_just_recent(self):
        self.engine.regime(regime_snapshot(),spots(),NOW)
        t=NOW+timedelta(hours=1)
        self.engine.regime({},spots(t-timedelta(seconds=1),99),t)
        self.assertEqual(self.engine.state['regime'][0]['status'],'open')

    def test_regime_history_is_not_imported(self):
        self.engine.regime(regime_snapshot(NOW-timedelta(seconds=1)),spots(),NOW)
        self.assertEqual(self.engine.state['regime'],[])

    def test_fixed_reviews_are_immutable_and_never_enable_trading(self):
        self.engine.reviews(NOW+timedelta(days=29))
        self.assertEqual(self.engine.state['reviews'],[])
        self.engine.reviews(NOW+timedelta(days=30))
        before=deepcopy(self.engine.state['reviews'])
        self.engine.reviews(NOW+timedelta(days=31))
        self.assertEqual(self.engine.state['reviews'],before)
        self.assertFalse(before[0]['automatic_promotion'])
        self.assertTrue(all(r['status']=='continue_shadow' for r in before[0]['studies'].values()))

    def test_lossless_event_archive_and_restart_preserve_rows_and_deduplication(self):
        sentinel=Path(self.temp.name)/'crypto_prospective_shadow.json'
        sentinel.write_text('original research must remain unchanged')
        self.enter_model()
        self.engine.reject('example',['test'],NOW,raw_evidence={'value':17})
        self.engine.persist()
        other=Expansion(self.engine.path,NOW+timedelta(days=1))
        self.assertEqual(other.state['registered_at'],NOW.isoformat())
        self.assertEqual(len(other.state['records']),1)
        archives=[Path(self.temp.name)/p for p in other.state['evidence_archive_files']]
        rows=list(iter_jsonl(archives))
        self.assertEqual(rows[0]['record']['raw_evidence']['value'],17)
        self.assertEqual(sentinel.read_text(),'original research must remain unchanged')

    def test_corrupt_ledger_fails_closed(self):
        self.engine.path.write_text('{bad json')
        with self.assertRaises(json.JSONDecodeError):Expansion(self.engine.path,NOW)
        self.assertEqual(self.engine.path.read_text(),'{bad json')

    def test_source_book_age_is_preserved_in_regime_price_timestamp(self):
        class Stream:
            def snapshot(self,asset):return {'connected':True,'mid':100,'book_age_seconds':1.5}
        values,_=spots_from_stream(Stream(),NOW)
        self.assertEqual(values['ETH']['observed_at'],(NOW-timedelta(seconds=1.5)).isoformat())

    def test_worker_lock_prevents_duplicate_writer(self):
        path=Path(self.temp.name)/'worker.lock'
        first=single_instance(path)
        self.assertIsNotNone(first)
        try:self.assertIsNone(single_instance(path))
        finally:first.close()
        second=single_instance(path)
        self.assertIsNotNone(second)
        second.close()


if __name__=='__main__':
    unittest.main()
