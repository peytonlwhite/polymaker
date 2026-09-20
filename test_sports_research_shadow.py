import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sports_itf_shadow import CENTRAL, stamp
from sports_research_shadow import (STATE, REPORT, Research, candidate_signal, confirmed_fill,
                                    fee_rates, metrics, reward_score)
from sports_research_worker import Reader, itf_signals, manual_signals, partial_signals, write_view

NOW = datetime(2026, 9, 17, 13, tzinfo=CENTRAL)


def book(yes=.49, no=.49, size=100):
    return {'orderbook_fp': {'yes_dollars': [[str(yes),str(size)]], 'no_dollars': [[str(no),str(size)]]}}


def market(ticker='KXITFMATCH-26SEP17AB-A', **kw):
    return {'ticker':ticker,'event_ticker':ticker.rsplit('-',1)[0], 'market_type':'binary',
            'notional_value_dollars':'1.0000','status':'active','price_level_structure':'linear_cent',
            'rules_primary':'Test rules','rules_secondary':'Test settlement',**kw}


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engine = Research(self.root,register=True,now=NOW,implementation_hash='frozen')
        self.rates = fee_rates({'fee_type':'quadratic_with_maker_fees','fee_multiplier':1})

    def enter(self, strategy='core_moneyline', **kw):
        return self.engine.enter(strategy,'game',market(),'yes',book(),book(),self.rates,NOW,**kw)

    def test_explicit_registration_missing_ledger_and_corruption_fail_closed(self):
        with self.assertRaises(ValueError):
            Research(self.root,now=NOW)
        self.engine.persist()
        path = self.root/STATE
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            Research(self.root,now=NOW,implementation_hash='changed')
        self.assertEqual(original,path.read_bytes())
        path.write_text('{broken')
        with self.assertRaises(json.JSONDecodeError):
            Research(self.root,register=True,now=NOW)

    def test_compressed_ledger_resume_and_missing_state_does_not_reset_report(self):
        import gzip
        with gzip.open(str(self.root/STATE)+'.gz','wt',encoding='utf-8') as f:
            json.dump(self.engine.state,f)
        loaded=Research(self.root,now=NOW,implementation_hash='frozen')
        self.assertEqual(loaded.state['started_at'],stamp(NOW))
        Path(str(self.root/STATE)+'.gz').unlink()
        (self.root/REPORT).write_text('{}')
        with self.assertRaises(ValueError):
            Research(self.root,register=True,now=NOW)

    def test_confirmed_depth_not_last_price_or_disappearing_liquidity(self):
        self.assertIsNone(confirmed_fill(book(size=100),book(size=1),'yes',10,.07))
        self.assertIsNone(confirmed_fill(book(),book(no=.48),'yes',10,.07))
        fill=confirmed_fill(book(),book(),'yes',10,.07)
        self.assertLessEqual(fill['cost'],10)
        self.assertGreaterEqual(fill['cost'],9)
        self.assertGreater(fill['fee'],0)

    def test_duplicate_event_and_fixed_budget_no_loss_recovery(self):
        first=self.enter()
        self.assertIsNotNone(first)
        self.assertIsNone(self.enter())
        self.assertLessEqual(first['cost'],10)
        second=self.engine.enter('core_moneyline','second',market(),'yes',book(),book(),self.rates,NOW)
        self.assertEqual(first['cost'],second['cost'])

    def test_binary_and_scalar_settlement_with_no_complement_fees(self):
        row=self.engine.enter('manual_tennis','game',market(),'no',book(),book(),self.rates,NOW)
        self.engine.settle({row['ticker']:market(status='finalized',result='yes',settlement_value_dollars='1.0000')},NOW+timedelta(hours=1))
        self.assertEqual(row['profit'],-row['cost'])
        other=self.engine.enter('manual_tennis','other',market(),'yes',book(),book(),self.rates,NOW)
        self.engine.settle({other['ticker']:market(status='finalized',result='scalar',settlement_value_dollars='.5000')},NOW+timedelta(hours=1))
        self.assertAlmostEqual(other['profit'],round(other['contracts']*.5-other['cost'],4))

    def test_wrong_ticker_and_non_final_prices_never_settle(self):
        row=self.enter()
        self.engine.settle({row['ticker']:market(ticker='KXITFMATCH-OTHER-A',status='finalized',result='yes')},NOW)
        self.assertEqual(row['status'],'open')
        self.engine.settle({row['ticker']:market(status='closed',last_price_dollars='1.0')},NOW)
        self.assertEqual(row['status'],'open')

    def maker(self):
        return self.enter('pregame_maker',maker=True,expiry=NOW+timedelta(seconds=180))

    def trade(self, **kw):
        return {'trade_id':'a','ticker':market()['ticker'],'created_time':stamp(NOW+timedelta(seconds=5)),
                'yes_price_dollars':'.48','no_price_dollars':'.52','count_fp':'2.00',
                'taker_outcome_side':'no','is_block_trade':False,**kw}

    def test_maker_trade_through_direction_time_volume_and_duplicates(self):
        row=self.maker()
        trades=[self.trade(),self.trade(),self.trade(trade_id='b',yes_price_dollars='.49'),
                self.trade(trade_id='c',taker_outcome_side='yes'),self.trade(trade_id='d',is_block_trade=True),
                self.trade(trade_id='e',created_time=stamp(NOW-timedelta(seconds=1)))]
        self.engine.process_trades(row,trades,NOW+timedelta(seconds=30))
        self.assertEqual(row['contracts'],2)
        self.engine.process_trades(row,trades,NOW+timedelta(seconds=60))
        self.assertEqual(row['contracts'],2)
        self.engine.process_trades(row,[],NOW+timedelta(seconds=181),complete=False)
        self.assertEqual(row['status'],'open')
        self.assertEqual(row['remaining'],0)

    def test_missing_block_flag_and_truncated_pages_do_not_fake_fills(self):
        row=self.maker()
        trade=self.trade()
        trade.pop('is_block_trade')
        self.engine.process_trades(row,[trade],NOW+timedelta(seconds=30))
        self.assertEqual(row['contracts'],0)
        self.engine.process_trades(row,[self.trade()],NOW+timedelta(seconds=60),complete=False)
        self.assertEqual(row['status'],'unfilled')
        self.assertEqual(row['cost'],0)

    def test_restart_gap_never_backfills_maker_fills(self):
        row=self.maker()
        self.engine.process_trades(row,[self.trade()],NOW+timedelta(minutes=10))
        self.assertEqual(row['status'],'unfilled')
        self.assertTrue(row['fill_data_gap'])

    def test_reward_estimates_never_become_trading_profit(self):
        program={'start_date':stamp(NOW),'end_date':stamp(NOW+timedelta(hours=1)),
                 'target_size_fp':'100','discount_factor_bps':5000,'period_reward':1000000}
        row=self.enter('liquidity_rewards',maker=True,expiry=NOW+timedelta(seconds=180),reward=program)
        self.engine.accrue_reward(row,book(),NOW)
        self.engine.accrue_reward(row,book(),NOW+timedelta(seconds=30))
        self.assertGreater(row['reward_estimate'],0)
        report=metrics([row],NOW)
        self.assertEqual(report['profit'],0)
        self.assertIsNone(report['roi_pct'])
        before=row['reward_estimate']
        self.engine.accrue_reward(row,book(),NOW+timedelta(seconds=150))
        self.assertEqual(before,row['reward_estimate'])
        self.assertEqual(reward_score(book(size=20),'yes',.49,20,100,.5),0)

    def test_time_window_stops_entries_but_settles_existing_positions(self):
        row=self.enter()
        self.assertFalse(self.engine.can_enter('core_total','new',NOW+timedelta(days=30)))
        self.assertEqual(self.engine.summary(NOW+timedelta(days=31))['status'],'maturing')
        self.engine.settle({row['ticker']:market(status='finalized',result='no',settlement_value_dollars='0.0000')},NOW+timedelta(days=31))
        self.assertEqual(self.engine.summary(NOW+timedelta(days=31))['status'],'complete')

    def test_statistics_keep_unfilled_open_and_correlated_events(self):
        row=self.enter()
        rows=[]
        for i in range(10):
            rows.append({**row,'id':str(i),'event':'same','status':'settled','settled_at':stamp(NOW),'profit':1})
        out=metrics(rows,NOW)
        self.assertEqual(out['unique_events'],1)
        self.assertEqual(out['without_best_event'],0)
        self.assertIsNone(out['exploratory_event_bootstrap_roi_95'])
        self.assertTrue(all(not x['automatic_promotion'] for x in self.engine.summary(NOW)['strategies']))

    def test_manual_baseline_and_naive_chicago_timestamp_are_prospective(self):
        old={'source':'manual','status':'open','kalshi_ticker':'KXWTAMATCH-OLD-A','placed_at':'2026-09-17T12:59:00','order_side':'yes'}
        self.assertEqual(manual_signals(self.engine,{'bets':[old]},NOW),[])
        new={**old,'kalshi_ticker':'KXWTAMATCH-NEW-A','placed_at':'2026-09-17T13:00:15'}
        signals=manual_signals(self.engine,{'bets':[old,new]},NOW+timedelta(seconds=30))
        self.assertEqual(len(signals),1)
        self.assertFalse(signals[0]['evidence']['reason_available'])
        self.assertEqual(manual_signals(self.engine,{'bets':[new]},NOW+timedelta(minutes=4)),[])

    def test_source_quality_fresh_exact_families_not_confidence(self):
        candidate={'market_type':'moneyline','order_side':'yes','game_started':False,'commence_time':stamp(NOW+timedelta(hours=2)),
                   'kalshi_ticker':market()['ticker'],'game_key':'game','pricing_v2':{'consensus':{
                       'ok':True,'lower_probability':60,'observations':[
                           {'family':name,'probability':62,'all_exact_lines':True,'contributing_updates':[stamp(NOW)]} for name in ['a','b']]}}}
        self.assertIsNotNone(candidate_signal(candidate,stamp(NOW),NOW))
        candidate['pricing_v2']['consensus']['observations'][1]['contributing_updates']=[stamp(NOW-timedelta(minutes=5))]
        self.assertIsNone(candidate_signal(candidate,stamp(NOW),NOW))
        self.assertIsNone(candidate_signal(candidate,stamp(NOW+timedelta(seconds=1)),NOW))

    def test_partial_ladder_uses_same_event_scope_and_neighbor_quotes(self):
        markets=[]; books={}
        for strike,bid in [(2.5,.69),(3.5,.40),(4.5,.49)]:
            m=market(ticker='KXMLBF5TOTAL-E-'+str(int(strike+.5)),strike_type='greater',floor_strike=strike,
                     rules_primary=f'If teams score more than {strike} runs in first 5 innings then Yes.')
            markets.append(m);books[m['ticker']]=book(yes=bid,no=round(1-bid-.02,2))
        signals=partial_signals(markets,books)
        self.assertEqual(len(signals),1)
        self.assertEqual(signals[0]['side'],'yes')
        markets[2]['rules_secondary']='Different postponement rules'
        self.assertEqual(partial_signals(markets,books),[])

    def test_itf_requires_prospective_anchor_confirmed_momentum_and_two_books(self):
        a=market(volume_fp='200');b=market(ticker='KXITFMATCH-26SEP17AB-B',volume_fp='200')
        phases={a['event_ticker']:{'phase':'pregame','at':stamp(NOW)}}
        pair={a['event_ticker']:[a,b]}
        books={a['ticker']:book(.64,.34),b['ticker']:book(.34,.64)}
        out=itf_signals(self.engine,pair,books,phases,NOW)
        self.assertEqual(out[0][0],'itf_favorite')
        self.assertTrue(self.engine.state['itf_anchors'])
        phases[a['event_ticker']]={'phase':'live','at':stamp(NOW+timedelta(minutes=6))}
        books={a['ticker']:book(.44,.54),b['ticker']:book(.54,.44)}
        out=itf_signals(self.engine,pair,books,phases,NOW+timedelta(minutes=6))
        self.assertFalse(any(x[0]=='itf_favorite_rebound' for x in out))
        self.assertEqual(itf_signals(self.engine,pair,{a['ticker']:books[a['ticker']]},phases,NOW+timedelta(minutes=6)),[])

    def test_public_reader_rejects_account_and_mutation_endpoints(self):
        api=Reader()
        api.session.get=Mock(side_effect=AssertionError('network should not run'))
        for path in ['/portfolio/orders','/portfolio/balance','/markets/../portfolio/orders','https://evil.test']:
            with self.assertRaises(ValueError):
                api.get(path)
        self.assertFalse(api.session.trust_env)
        self.assertFalse(api.session.get.called)

    def test_unknown_fee_schedules_fail_closed_and_maker_exceptions_apply(self):
        self.assertEqual(fee_rates({'fee_type':'quadratic','fee_multiplier':.5})['maker'],0)
        self.assertEqual(fee_rates({'fee_type':'quadratic_with_maker_fees','fee_multiplier':.5})['maker'],.00875)
        with self.assertRaises(ValueError):
            fee_rates({'fee_type':'unknown','fee_multiplier':1})

    def test_view_escapes_remote_content(self):
        report=self.engine.summary(NOW)
        report['coverage']={'remote':'<script>alert(1)</script>'}
        write_view(self.root,report)
        doc=(self.root/'archives'/'sports_research'/'index.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>',doc)
        self.assertIn('&lt;script&gt;',doc)


if __name__=='__main__':
    unittest.main()
