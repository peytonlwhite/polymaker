import copy
from datetime import datetime, timedelta
import unittest
from unittest.mock import Mock, patch

import aibetpicks_accounting as accounting
import dashboard
import sports_paper_bettor as sports
import sports_aibetpicks as picks
from test_sports_aibetpicks import pick

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=accounting.CHICAGO)


def bet(**changes):
    return {'id': 'one', 'source': 'aibetpicks', 'mode': 'live', 'status': 'settled',
            'aibetpicks_bot_id': 'test', 'aibetpicks_bot_name': 'Test Bot',
            'placed_at': '2026-09-14T11:00:00-05:00', 'settled_at': '2026-09-14T20:00:00',
            'kalshi_ticker': 'MATCH', 'kalshi_order_side': 'no',
            'live_order': {'response': {'order_id': 'order-one'}},
            'stake': 19.32, 'contracts': 42, 'fee': .3612, 'profit': -19.68, 'payout': 0,
            'result': 'LOSS', 'settlement_side_value': 0, 'unit_size': 19.49, 'unit_count': .991,
            **changes}


def fill(**changes):
    return {'fill_id': 'fill-one', 'order_id': 'order-one', 'ticker': 'MATCH',
            'side': 'no', 'action': 'sell', 'outcome_side': 'no', 'book_side': 'ask',
            'count_fp': '42.00', 'no_price_dollars': '.4600', 'yes_price_dollars': '.5400',
            'fee_cost': '.365200', **changes}


class AIBetPicksAccountingTests(unittest.TestCase):
    def test_exact_exchange_fee_corrects_rounding_without_changing_cash_or_size(self):
        row = bet()
        portfolio = {'balance': 1000, 'history': [row], 'bets': []}
        before = copy.deepcopy(portfolio)
        fetch = Mock(return_value={'ok': True, 'fills': [fill()]})
        changes = accounting.reconcile_fill_fees(portfolio, fetch, now=NOW)
        self.assertEqual('verified', changes[0]['status'])
        self.assertEqual(.3652, row['fee'])
        self.assertEqual(-19.69, row['profit'])
        for key in ('stake', 'contracts', 'unit_size', 'unit_count', 'result'):
            self.assertEqual(before['history'][0][key], row[key])
        self.assertEqual(1000, portfolio['balance'])
        self.assertEqual((.3652, 'exchange_order_fills'), sports.known_sports_bet_fee(row))
        self.assertEqual([], accounting.reconcile_fill_fees(portfolio, fetch, now=NOW))
        self.assertEqual(1, fetch.call_count)

    def test_exact_fills_scope_to_order_and_deduplicate_pagination(self):
        one = fill(count_fp='20', fee_cost='.17')
        two = fill(fill_id='two', count_fp='22', fee_cost='.1952')
        result = accounting.exact_fill_totals(bet(), [one, two, one, fill(order_id='unrelated')])
        self.assertEqual(2, result['fill_count'])
        self.assertEqual(.3652, result['fee'])
        self.assertEqual(19.32, result['premium'])

    def test_incomplete_malformed_or_wrong_direction_fills_never_rewrite(self):
        for changes in ({'count_fp': '41'}, {'fee_cost': None}, {'fee_cost': 'NaN'},
                        {'ticker': 'OTHER'}, {'outcome_side': 'yes'}, {'no_price_dollars': '.45'},
                        {'book_side': 'bid'}, {'fill_id': None}):
            with self.subTest(changes=changes):
                row = bet(); fetch = Mock(return_value={'ok': True, 'fills': [fill(**changes)]})
                portfolio = {'history': [row]}
                result = accounting.reconcile_fill_fees(portfolio, fetch, now=NOW)
                self.assertEqual('pending', result[0]['status'])
                self.assertEqual(-19.68, row['profit'])
                self.assertEqual(.3612, row['fee'])
                self.assertEqual([], accounting.reconcile_fill_fees(portfolio, fetch, now=NOW + timedelta(minutes=1)))
                self.assertEqual(1, fetch.call_count)

    def test_conflicting_duplicate_fills_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'conflicting_duplicate_fill'):
            accounting.exact_fill_totals(bet(), [fill(), fill(fee_cost='.9')])

    def test_legacy_buy_no_and_sell_yes_directions(self):
        for side, action in [('no', 'buy'), ('yes', 'sell')]:
            result = accounting.exact_fill_totals(bet(), [fill(outcome_side=None, book_side=None, side=side, action=action)])
            self.assertEqual(.3652, result['fee'])

    def test_zero_exact_fee_is_verified_and_overrides_estimate(self):
        row = bet(); accounting.reconcile_fill_fees({'history': [row]}, lambda *a: {'ok': True, 'fills': [fill(fee_cost='0')]}, now=NOW)
        self.assertEqual((0.0, 'exchange_order_fills'), sports.known_sports_bet_fee(row))
        self.assertEqual(-19.32, row['profit'])

    def test_fee_cache_invalidates_if_quantity_or_order_changes(self):
        row = bet(); accounting.reconcile_fill_fees({'history': [row]}, lambda *a: {'ok': True, 'fills': [fill()]}, now=NOW)
        row['contracts'] = 43
        self.assertIsNone(accounting.verified_fee(row))

    def test_open_fee_reconciliation_does_not_create_result_or_profit(self):
        row = bet(status='open', profit=None, result=None, payout=None)
        accounting.reconcile_fill_fees({'bets': [row]}, lambda *a: {'ok': True, 'fills': [fill()]}, now=NOW)
        self.assertIsNone(row['profit'])
        result = accounting.local_analytics([row], [], now=NOW)
        self.assertEqual(0, result['settled'])
        self.assertEqual(0, result['profit'])
        self.assertIsNone(result['rows'][0]['profit_units'])
        self.assertGreater(result['open_risk_units'], 1)

    def test_outage_preserves_financials_and_reconciliation_is_bounded(self):
        portfolio = {'history': [bet(id=str(i)) for i in range(12)]}
        fetch = Mock(return_value={'ok': False})
        self.assertEqual(8, len(accounting.reconcile_fill_fees(portfolio, fetch, now=NOW)))
        self.assertEqual(8, fetch.call_count)
        self.assertTrue(all(r['profit'] == -19.68 for r in portfolio['history']))

    def test_each_entry_unit_is_used_instead_of_current_unit_or_rounded_count(self):
        rows = [bet(id='win', kalshi_order_side='yes', stake=20, fee=1, payout=100, contracts=100,
                    profit=79, result='WIN', settlement_side_value=100, unit_size=10, unit_count=999),
                bet(id='loss', stake=40, fee=1, profit=-41, unit_size=20)]
        result = accounting.local_analytics([], rows, now=NOW)
        self.assertEqual(38, result['profit'])
        self.assertAlmostEqual(5.85, result['profit_units'])
        self.assertEqual((1, 1), (result['wins'], result['losses']))
        self.assertEqual(4, result['stake_units'])
        self.assertAlmostEqual(4.15, result['risk_units'])
        self.assertEqual(result['profit_units'], result['by_bot'][0]['profit_units'])
        self.assertEqual(result['profit_units'], result['daily'][0]['profit_units'])

    def test_unknown_unit_is_not_silently_replaced_by_today(self):
        row = bet(unit_size=None, unit_count=1)
        result = accounting.local_analytics([], [row], now=NOW)
        self.assertIsNone(result['profit_units'])
        self.assertEqual(1, result['missing_unit_records'])
        self.assertEqual(-19.68, result['profit'])
        self.assertFalse(result['accounting']['ok'])

    def test_void_and_partial_settlements_are_not_binary_wins_losses(self):
        rows = [bet(id='void', result='VOID', stake=20, payout=20, fee=0, profit=0, settlement_side_value=None),
                bet(id='partial', contracts=100, stake=20, payout=50, fee=1, profit=29,
                    result='WIN', settlement_side_value=50)]
        result = accounting.local_analytics([], rows, now=NOW)
        self.assertEqual((0, 0, 1, 1), (result['wins'], result['losses'], result['voids'], result['other']))
        self.assertIsNone(result['win_rate'])
        self.assertEqual(29, result['profit'])

    def test_nontrades_other_sources_and_paper_are_excluded(self):
        rows = [bet(status='review', result='UNRESOLVED'), bet(id='paper', mode='paper'),
                bet(id='manual', source='user_manual'), bet(id='good')]
        result = accounting.local_analytics([], rows, now=NOW)
        self.assertEqual(1, result['settled'])
        self.assertEqual(-19.68, result['profit'])

    def test_duplicate_records_are_flagged_without_double_counting(self):
        row = bet(); result = accounting.local_analytics([], [row, row], now=NOW)
        self.assertEqual(1, result['settled'])
        self.assertFalse(result['accounting']['ok'])

    def test_all_history_and_chicago_settlement_day(self):
        rows = [bet(id=str(i), settled_at='2026-09-15T02:00:00Z') for i in range(205)]
        result = accounting.local_analytics([], rows, now=NOW)
        self.assertEqual(205, len(result['rows']))
        self.assertEqual(205, result['daily'][0]['losses'])
        self.assertEqual('2026-09-14', result['daily'][0]['date'])

    def test_mismatched_profit_payout_and_result_are_visible(self):
        row = bet(profit=2, payout=42, result='WIN')
        result = accounting.local_analytics([], [row], now=NOW)
        reasons = {r['reason'] for r in result['accounting']['errors']}
        self.assertEqual({'net_profit_mismatch', 'settlement_payout_mismatch', 'settlement_result_mismatch'}, reasons)

    def test_shared_source_totals_include_aibetpicks(self):
        row = bet()
        result = dashboard.sports_source_analytics([], [row], now=NOW)
        self.assertEqual(-19.68, result['aibetpicks']['profit'])
        self.assertTrue(result['reconciliation']['settled_count_matches'])
        self.assertEqual(0, result['reconciliation']['profit_delta'])

    def test_generic_owner_does_not_double_count_explicit_source(self):
        rows = [bet(strategy_owner='user_bet'),
                bet(id='test', source='manual_live_test', strategy_owner='user_bet', stake=.06, profit=-.06)]
        result = dashboard.sports_source_analytics([], rows, now=NOW)
        self.assertEqual(0, result['user_live']['settled_count'])
        self.assertEqual(1, result['aibetpicks']['settled_count'])
        self.assertEqual(1, result['system_test']['settled_count'])
        self.assertTrue(result['reconciliation']['settled_count_matches'])
        self.assertEqual(0, result['reconciliation']['profit_delta'])

    def test_losing_units_match_account_precision_risk(self):
        row = bet(fee=.3652, profit=-19.69)
        result = accounting.local_analytics([], [row], now=NOW)
        self.assertEqual(result['risk_units'], -result['profit_units'])

    def test_published_roi_weights_actual_units_and_push_risk(self):
        history = {str(i): pick(pick_id=str(i), status=outcome, odds=odds, stake_units=units,
                    commence_time=(NOW - timedelta(days=1)).isoformat(), result_source='scores')
                   for i, (outcome, odds, units) in enumerate([('win', 200, 3), ('loss', -150, 1), ('push', -110, 1)])}
        result = picks.performance({'history': history}, NOW)
        self.assertEqual(5, result['profit_units'])
        self.assertEqual(5, result['risked_units'])
        self.assertEqual(100, result['roi_pct'])

    def test_settlement_path_saves_exact_fee_corrections_without_placing_orders(self):
        portfolio = {'bets': [], 'history': [bet()]}
        with patch.object(sports, 'fetch_position_fills', return_value={'ok': True, 'fills': [fill()]}), \
             patch.object(sports, 'recently_reconciled_remote_open_tickers', return_value=set()), \
             patch.object(sports, 'append_jsonl'), patch.object(sports, 'place_live_kalshi_order') as place:
            changed, settled = sports.settle_open_sports_bets(portfolio, {})
        self.assertTrue(changed)
        self.assertEqual([], settled)
        self.assertEqual(-19.69, portfolio['history'][0]['profit'])
        place.assert_not_called()


if __name__ == '__main__':
    unittest.main()
