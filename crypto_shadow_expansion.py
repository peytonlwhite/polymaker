"""Independent forward research. Only injected read-only market callbacks exist.

No original experiment, account, settings, execution gate or model is written.
The three studies have separate estimands; their profits must not be added.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path

from crypto_15m_learning import candidate_snapshot, _predict_research_residual
from crypto_scan_intelligence import research_feature_payload
from crypto_execution_safety import confirmation_reasons
from crypto_pricing import kalshi_order_fee
from crypto_prospective_shadow import quality_reasons, hypothesis, parsed, number, local_day
from crypto_evidence import retain_records, flush_evidence_archives
from crypto_shadow_challenger import atomic_json, digest

PROTOCOL = {
    'version': 'crypto-shadow-expansion-20260908-v1',
    'automatic_promotion': False, 'historical_backfill': False,
    'challenger': {'minutes': [2, 13], 'price_cents': [35, 80], 'contracts': 3,
                   'edge_after_fees_and_stress_cents': 2, 'stress_cents': 1,
                   'required_feature_coverage': 1.0, 'decision': 'first_quality_eligible_snapshot_per_ticker',
                   'model': 'frozen historical train partition; prior selected research family'},
    'capture_universe': 'scanner top_candidates; refreshed sources; 300s worker and ticker warmup for model; 60s for ETH',
    'eth_entry': {'signal': 'existing eth_flow rule', 'contracts': 3,
                  'wait_seconds': 60, 'improvement_cents': 1,
                  'fill': 'first observed independently confirmed executable quote; no maker fills',
                  'misses': 'zero return per original opportunity; no fallback entry'},
    'regime': {'horizon_seconds': 3600, 'snapshot_max_age_seconds': 90,
               'exit_grace_seconds': 60, 'nonoverlapping_per_asset': True,
               'minimum_quality': .9, 'spot_max_age_seconds': 2,
               'mode': 'forecast_only; no fees, funding or executable trade PnL claimed',
               'comparators': ['original direction', 'always up', 'probability 0.5']},
    'virtual_portfolio': {'daily_entries': 25, 'max_open': 4, 'max_per_expiry': 2,
                          'max_exposure': 45, 'gross_daily_loss_limit': 45,
                          'scope': 'challenger only; ETH paired study is diagnostic'},
    'review': {'days': 30, 'family_size': 3, 'alpha': .05, 'minimum_days': 20,
               'minimum_challenger_markets': 200, 'minimum_eth_pairs': 200,
               'minimum_regime_expiries': 100,
               'method': 'day and expiry bounded cluster lower bounds; fixed alpha spending'},
}
POLICY_HASH = digest(PROTOCOL)


def utcnow():
    return datetime.now(timezone.utc)


def mean(values):
    return sum(values) / len(values) if values else None


def research_candidate(candidate):
    """Match scanner apply_candidate_pricing's interval-to-feature binding.

    The read-only refresh adapter returns its recomputed interval separately;
    training snapshots read it from probability. Never substitute zeros or reuse
    the old scan's bounds when refreshed bounds are available.
    """
    result = deepcopy(candidate)
    interval = result.get('probability_interval') or {}
    if interval.get('p_yes') is not None and interval.get('p_low') is not None:
        result.setdefault('probability', {}).update(interval)
    return result


def lower(rows, value, cluster, width, alpha=.05 / 12):
    """Bounded weighted cluster means; unrestricted dependence within a cluster."""
    groups = defaultdict(list)
    for row in rows:
        groups[cluster(row)].append(value(row))
    if len(groups) < 2:
        return None
    n = len(rows)
    weights = [len(v) / n for v in groups.values()]
    radius = width * math.sqrt(math.log(1 / alpha) * sum(w*w for w in weights) / 2)
    return mean([value(r) for r in rows]) - radius


def consistency(rows, value):
    rows = sorted(rows, key=lambda r: r['captured_at'])
    middle = len(rows) // 2
    days = defaultdict(float)
    for r in rows:
        days[local_day(r['captured_at'])] += value(r)
    return {'both_halves_positive': bool(middle and sum(map(value, rows[:middle])) > 0
                                        and sum(map(value, rows[middle:])) > 0),
            'drop_best_day_positive': bool(len(days) > 1 and sum(days.values()) - max(days.values()) > 0)}


def quote_evidence(candidate, side, contracts, quote, checked, ceiling):
    reasons = quality_reasons(candidate, checked) + confirmation_reasons(quote, 1, checked)
    price = number(quote.get('full_size_entry_price_cents'))
    if number(quote.get('available_contracts')) < contracts or not 0 < price < 100:
        reasons.append('insufficient_executable_depth')
    if price > ceiling + 1e-9:
        reasons.append('price_chased')
    fee = kalshi_order_fee(price, contracts, schedule=candidate.get('fee_schedule') or {})
    if not fee.get('authoritative') or not fee.get('exact'):
        reasons.append('authoritative_supported_fee_unavailable')
    fee_dollars = number(fee.get('conservative_fee_dollars'))
    return reasons, {'side': side, 'contracts': contracts, 'entry_price_cents': price,
                     'cost_dollars': price * contracts / 100 + fee_dollars,
                     'fee_dollars': fee_dollars, 'fee_assumption': 'conservative_cent_cash_alignment; net_fee_unverified',
                     'confirmation': deepcopy(quote), 'entered_at': checked.isoformat()}


class Expansion:
    def __init__(self, path, now=None):
        self.path = Path(path)
        now = now or utcnow()
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
            'version': PROTOCOL['version'], 'policy_hash': POLICY_HASH,
            'protocol': deepcopy(PROTOCOL), 'registered_at': now.isoformat(),
            'records': [], 'forecasts': [], 'regime': [], 'eth_pairs': [], 'reviews': [],
            'rejections': {}, 'coverage': {}, 'scan_count': 0,
            'model': None, 'automatic_promotion': False, 'mode': 'shadow_only',
        }
        if self.state.get('policy_hash') != POLICY_HASH:
            raise ValueError('Research protocol changed: preserve this ledger and register a separate cohort')
        self.events = []

    def reject(self, lane, reasons, now, **evidence):
        reasons = sorted(set(reasons))
        if not reasons:
            return
        for reason in reasons:
            key = lane + ':' + reason
            self.state['rejections'][key] = self.state['rejections'].get(key, 0) + 1
        self.events.append({'lane': lane, 'reasons': reasons, 'captured_at': now.isoformat(), **evidence})

    def install_model(self, artifact, now=None):
        now = now or utcnow()
        if digest(artifact['model']) != artifact.get('model_hash'):
            raise ValueError('Challenger model hash mismatch')
        if not parsed(artifact.get('frozen_at')) or parsed(artifact['frozen_at']) > now:
            raise ValueError('Challenger freeze timestamp invalid')
        existing = self.state.get('model')
        if existing:
            if existing['model_hash'] != artifact['model_hash']:
                raise ValueError('Frozen challenger cannot be replaced inside a forward cohort')
            return
        # An artifact already frozen at cohort creation shares its start date.
        start = parsed(self.state['registered_at']) if parsed(artifact['frozen_at']) <= parsed(self.state['registered_at']) else now
        self.state['model'] = {**deepcopy(artifact), 'registered_at': start.isoformat()}

    def prediction(self, candidate, now):
        artifact = self.state.get('model')
        if not artifact:
            return None, None, ['model_training_pending']
        row = candidate_snapshot(research_candidate(candidate), now)
        # Same market-implied anchor and feature builder as historical training.
        anchor = (candidate.get('probability') or {}).get('market_implied_yes')
        if anchor is None or not 0 < number(anchor) < 100:
            return None, row, ['market_anchor_unavailable']
        available = row['research_features'].get('available') or {}
        missing = [f for f in artifact['model']['features'] if not available.get(f)]
        if missing:
            return None, row, ['model_feature_missing:' + f for f in missing]
        return _predict_research_residual(artifact['model'], row), row, []

    def capacity(self, close, now):
        rows = self.state['records']
        opened = [r for r in rows if r['status'] == 'open']
        today = [r for r in rows if local_day(r['captured_at']) == local_day(now.isoformat())]
        loss = sum(max(0, -number(r.get('profit_dollars'))) for r in rows
                   if local_day(r.get('settled_at')) == local_day(now.isoformat()))
        # Reserve the full worst-case $3.06 cost, including conservative fees.
        return (len(today) < 25 and len(opened) < 4
                and sum(r['close_time'] == close for r in opened) < 2
                and sum(r['cost_dollars'] for r in opened) + 3.06 <= 45 and loss < 45)

    def challenger(self, initial, refresh, confirm, now_fn=utcnow):
        now = now_fn()
        ticker = initial.get('ticker')
        if (not self.state.get('model') or now < parsed(self.state['model']['registered_at'])
                or any(r['ticker'] == ticker for r in self.state['forecasts'])):
            return
        close = parsed(initial.get('close_time'))
        if not close or not 2 <= (close - now).total_seconds() / 60 <= 13:
            return
        reasons = quality_reasons(initial, now)
        p, snapshot, unavailable = self.prediction(initial, now)
        reasons += unavailable
        if reasons:
            self.reject('challenger', reasons, now, ticker=ticker)
            return
        forecast = {'ticker': ticker, 'asset': initial['asset'], 'close_time': initial['close_time'],
                    'captured_at': now.isoformat(), 'probability_yes': p,
                    'market_probability_yes': snapshot['market_probability'], 'status': 'open',
                    'model_hash': self.state['model']['model_hash'],
                    'research_features': snapshot['research_features']}
        self.state['forecasts'].append(forecast)
        if not self.capacity(initial['close_time'], now):
            self.reject('challenger', ['virtual_portfolio_limit'], now, ticker=ticker)
            return
        side = max(('yes', 'no'), key=lambda s: (p if s == 'yes' else 1-p) * 100 - number(initial.get(s+'_ask'), 100))
        signal = number(initial.get(side + '_ask'))
        if not 35 <= signal <= 80:
            self.reject('challenger', ['signal_price_outside_band'], now, ticker=ticker)
            return
        quote = confirm(ticker, side, 3)
        latest = refresh(initial)
        checked = now_fn()
        reasons, entry = quote_evidence(latest, side, 3, quote, checked, signal)
        p2, snapshot2, unavailable = self.prediction(latest, checked)
        reasons += unavailable
        if not 35 <= entry['entry_price_cents'] <= 80:
            reasons.append('confirmed_price_outside_band')
        if not 2 <= (close - checked).total_seconds() / 60 <= 13:
            reasons.append('confirmation_outside_decision_window')
        edge = ((p2 if side == 'yes' else 1-p2) * 100 - entry['cost_dollars'] / 3 * 100 - 1) if p2 is not None else -100
        if edge < 2:
            reasons.append('edge_below_2c_after_fees_and_stress')
        self.reject('challenger', reasons, checked, ticker=ticker, confirmation=quote, forecast_probability=p2)
        if reasons:
            return
        self.state['records'].append({**entry, 'ticker': ticker, 'asset': initial['asset'],
            'close_time': initial['close_time'], 'captured_at': checked.isoformat(), 'status': 'open',
            'model_hash': self.state['model']['model_hash'], 'probability_yes': p2,
            'edge_after_stress_cents': edge, 'fresh_input_snapshot': snapshot2})

    def eth_entry(self, initial, refresh, confirm, now_fn=utcnow):
        now = now_fn()
        if now < parsed(self.state['registered_at']):
            return
        ticker = initial.get('ticker')
        if any(r['ticker'] == ticker for r in self.state['eth_pairs']):
            return
        rule = hypothesis(initial, 'eth_flow', now)
        if not rule or quality_reasons(initial, now):
            return
        side = rule['side']
        signal = number(initial.get(side+'_ask'))
        if not 35 <= signal <= 65:
            return
        quote = confirm(ticker, side, 3)
        latest = refresh(initial)
        checked = now_fn()
        reasons, entry = quote_evidence(latest, side, 3, quote, checked, signal)
        rule2 = hypothesis(latest, 'eth_flow', checked)
        if not rule2 or rule2['side'] != side:
            reasons.append('flow_changed')
        if not 35 <= entry['entry_price_cents'] <= 65:
            reasons.append('confirmed_price_outside_band')
        self.reject('eth_entry', reasons, checked, ticker=ticker, confirmation=quote)
        if reasons:
            return
        self.state['eth_pairs'].append({'ticker': ticker, 'asset': 'ETH', 'side': side,
            'captured_at': checked.isoformat(), 'close_time': initial['close_time'],
            'deadline': (checked + timedelta(seconds=60)).isoformat(), 'status': 'open',
            'immediate': {**entry, 'status': 'open'}, 'wait': {'status': 'waiting'},
            'target_price_cents': entry['entry_price_cents'] - 1,
            'polls': 0, 'maximum_poll_gap_seconds': 0, 'last_poll_at': checked.isoformat()})

    def advance_eth(self, candidates, refresh, confirm, now_fn=utcnow):
        by_ticker = {r.get('ticker'): r for r in candidates}
        for pair in self.state['eth_pairs']:
            if pair['wait']['status'] != 'waiting':
                continue
            now = now_fn()
            gap = (now - parsed(pair['last_poll_at'])).total_seconds()
            pair['maximum_poll_gap_seconds'] = max(pair['maximum_poll_gap_seconds'], gap)
            pair['last_poll_at'] = now.isoformat()
            if now > parsed(pair['deadline']):
                pair['wait'] = {'status': 'no_fill', 'reason': 'deadline_elapsed', 'profit_dollars': 0, 'stress_profit_dollars': 0}
                continue
            initial = by_ticker.get(pair['ticker'])
            if not initial:
                continue
            latest = refresh(initial)
            pair['polls'] += 1
            rule = hypothesis(latest, 'eth_flow', now_fn())
            if not rule or rule['side'] != pair['side'] or quality_reasons(latest, now_fn()):
                continue
            if number(latest.get(pair['side']+'_ask'), 100) > pair['target_price_cents']:
                continue
            quote = confirm(pair['ticker'], pair['side'], 3)
            latest = refresh(initial)
            checked = now_fn()
            reasons, entry = quote_evidence(latest, pair['side'], 3, quote, checked, pair['target_price_cents'])
            rule = hypothesis(latest, 'eth_flow', checked)
            if not rule or rule['side'] != pair['side']:
                reasons.append('flow_changed')
            if checked > parsed(pair['deadline']):
                reasons.append('confirmation_after_wait_deadline')
            if not 35 <= entry['entry_price_cents'] <= 65:
                reasons.append('confirmed_price_outside_band')
            self.reject('eth_wait', reasons, checked, ticker=pair['ticker'], confirmation=quote)
            if not reasons:
                pair['wait'] = {**entry, 'status': 'open'}

    def regime(self, snapshot, spots, now=None):
        now = now or utcnow()
        for row in self.state['regime']:
            if row['status'] != 'open' or now < parsed(row['due_at']):
                continue
            spot = spots.get(row['asset']) or {}
            observed = parsed(spot.get('observed_at'))
            if now > parsed(row['due_at']) + timedelta(seconds=60):
                row.update(status='unscored', reason='no_timely_exit_spot', unscored_at=now.isoformat())
            elif (observed and 0 <= (now-observed).total_seconds() <= 2
                  and observed >= parsed(row['due_at']) and number(spot.get('mid')) > 0):
                ret = spot['mid'] / row['start_spot'] - 1
                label = int(ret > 0)
                direction = 'up' if ret > 0 else 'down' if ret < 0 else 'flat'
                row.update(status='settled', settled_at=now.isoformat(), exit_observed_at=observed.isoformat(),
                    return_bps=ret*10000, end_spot=spot['mid'], result=direction,
                    original_correct=row['original_direction'] == direction,
                    reversal_correct=row['reversal_direction'] == direction,
                    original_brier=(row['probability_up']-label)**2,
                    reversal_brier=(1-row['probability_up']-label)**2,
                    neutral_brier=(.5-label)**2, always_up_correct=direction == 'up')
        generated = parsed((snapshot or {}).get('generated_at'))
        if not generated or not 0 <= (now-generated).total_seconds() <= 90 or generated < parsed(self.state['registered_at']):
            self.reject('regime', ['snapshot_stale_or_before_registration'], now)
            return
        for asset, data in sorted((snapshot.get('assets') or {}).items()):
            if asset not in {'BTC', 'ETH', 'SOL', 'XRP', 'DOGE'}:
                continue
            forecast = (data.get('forecast_horizons') or {}).get('1h') or {}
            direction = forecast.get('direction')
            if direction not in {'up', 'down'} or not forecast.get('available') or number(forecast.get('data_quality')) < .9:
                continue
            prior = [r for r in self.state['regime'] if r['asset'] == asset]
            if prior and now < max(parsed(r['due_at']) for r in prior):
                continue
            if any(r['source_generated_at'] == generated.isoformat() for r in prior):
                continue
            spot = spots.get(asset) or {}
            observed = parsed(spot.get('observed_at'))
            p = forecast.get('probability_up')
            if (not observed or not 0 <= (now-observed).total_seconds() <= 2
                    or number(spot.get('mid')) <= 0 or p is None or not 0 <= number(p, -1) <= 1):
                self.reject('regime', ['fresh_spot_or_probability_unavailable'], now, asset=asset)
                continue
            self.state['regime'].append({'asset': asset, 'captured_at': now.isoformat(),
                'due_at': (now+timedelta(hours=1)).isoformat(), 'source_generated_at': generated.isoformat(),
                'source_forecast_hash': digest(forecast), 'start_spot': spot['mid'],
                'entry_observed_at': observed.isoformat(), 'probability_up': p,
                'original_direction': direction, 'reversal_direction': 'down' if direction == 'up' else 'up',
                'status': 'open', 'mode': 'forecast_only'})

    def settle(self, fetch_market, now=None):
        now = now or utcnow()
        outcomes = {}
        rows = self.state['forecasts'] + self.state['records'] + self.state['eth_pairs']
        for row in rows:
            if row['status'] != 'open' or now < parsed(row['close_time']):
                continue
            ticker = row['ticker']
            if ticker not in outcomes:
                try:
                    market = fetch_market(ticker)
                    market = market.get('market', market)
                    outcomes[ticker] = market.get('result') if market.get('status') in {'finalized','settled'} else None
                except Exception as exc:
                    outcomes[ticker] = None
                    self.reject('settlement', [type(exc).__name__], now, ticker=ticker)
            result = outcomes[ticker]
            if result not in {'yes', 'no'}:
                continue
            row.update(status='settled', result=result, settled_at=now.isoformat())
            if 'immediate' in row:
                if row['wait']['status'] == 'waiting':
                    row['wait'] = {'status':'no_fill', 'reason':'market_closed', 'profit_dollars':0, 'stress_profit_dollars':0}
                for arm in ('immediate', 'wait'):
                    if row[arm]['status'] == 'open':
                        self._pnl(row[arm], result)
            elif 'contracts' in row:
                self._pnl(row, result)
            else:
                y = int(result == 'yes')
                row.update(challenger_brier=(row['probability_yes']-y)**2,
                           market_brier=(row['market_probability_yes']-y)**2)

    @staticmethod
    def _pnl(row, result):
        profit = (row['contracts'] if row['side'] == result else 0) - row['cost_dollars']
        row.update(status='settled', won=row['side'] == result, profit_dollars=round(profit,8),
                   stress_profit_dollars=round(profit-.01*row['contracts'],8))

    def observe_coverage(self, candidate, btc_context, now):
        payload = research_feature_payload(research_candidate(candidate), btc_context=btc_context, scanned_at=now.isoformat())
        for feature, available in payload['available'].items():
            counts = self.state['coverage'].setdefault(feature, {'observations':0, 'available':0})
            counts['observations'] += 1
            counts['available'] += int(bool(available))
        self.events.append({'lane':'feature_coverage', 'ticker':candidate.get('ticker'),
                            'captured_at':now.isoformat(), 'research_features':payload})

    def summary(self, now=None, cutoff=None, alpha=.05/12):
        now = now or utcnow()
        def settled(field):
            return [r for r in self.state[field] if r['status']=='settled'
                    and (not cutoff or parsed(r['settled_at']) <= cutoff)]
        trades, forecasts, pairs, regime = (settled(f) for f in ['records','forecasts','eth_pairs','regime'])
        def evidence(rows, value, expiry, width):
            return {'settled':len(rows), 'active_dates':len({local_day(r['captured_at']) for r in rows}),
                    'expiries':len({expiry(r) for r in rows}),
                    'day_lower':lower(rows,value,lambda r:local_day(r['captured_at']),width,alpha),
                    'expiry_lower':lower(rows,value,expiry,width,alpha), **consistency(rows,value)}
        challenger = evidence(trades,lambda r:r['stress_profit_dollars']/3,lambda r:r['close_time'],2.1)
        challenger.update(profit_dollars=sum(r['profit_dollars'] for r in trades),
            stress_profit_dollars=sum(r['stress_profit_dollars'] for r in trades),
            forecasts=len(forecasts), brier_improvement=mean([r['market_brier']-r['challenger_brier'] for r in forecasts]),
            cost_dollars=sum(r['cost_dollars'] for r in trades),
            open=sum(r['status']=='open' for r in self.state['records']))
        delta = lambda r: (r['wait']['stress_profit_dollars']-r['immediate']['stress_profit_dollars'])/3
        eth = evidence(pairs,delta,lambda r:r['close_time'],4.2)
        eth.update(immediate_profit=sum(r['immediate']['profit_dollars'] for r in pairs),
            wait_profit=sum(r['wait']['profit_dollars'] for r in pairs),
            wait_stress_profit=sum(r['wait']['stress_profit_dollars'] for r in pairs),
            paired_stress_delta=sum(delta(r)*3 for r in pairs),
            filled=sum(r['wait']['status']=='settled' for r in pairs),
            missed_winners=sum(r['wait']['status']=='no_fill' and r['immediate']['won'] for r in pairs),
            avoided_losers=sum(r['wait']['status']=='no_fill' and not r['immediate']['won'] for r in pairs),
            maximum_poll_gap_seconds=max((r['maximum_poll_gap_seconds'] for r in self.state['eth_pairs']),default=0),
            open=sum(r['status']=='open' for r in self.state['eth_pairs']))
        regimes = evidence(regime,lambda r:int(r['reversal_correct'])-int(r['original_correct']),
                           lambda r:int(parsed(r['captured_at']).timestamp())//3600,2)
        regimes.update(original_accuracy=mean([int(r['original_correct']) for r in regime]),
            reversal_accuracy=mean([int(r['reversal_correct']) for r in regime]),
            always_up_accuracy=mean([int(r['always_up_correct']) for r in regime]),
            brier_improvement=mean([r['original_brier']-r['reversal_brier'] for r in regime]),
            unscored=sum(r['status']=='unscored' and (not cutoff or parsed(r['unscored_at'])<=cutoff) for r in self.state['regime']),
            open=sum(r['status']=='open' for r in self.state['regime']), mode='forecast_only')
        registered = parsed(self.state['registered_at'])
        model = self.state.get('model') or {}
        return {'version':PROTOCOL['version'], 'generated_at':now.isoformat(),
            'registered_at':self.state['registered_at'], 'policy_hash':POLICY_HASH,
            'mode':'shadow_only', 'automatic_promotion':False, 'scan_count':self.state['scan_count'],
            'next_review_at':(registered+timedelta(days=30*(len(self.state['reviews'])+1))).isoformat(),
            'model_status':'frozen_collecting' if model else 'training_pending',
            'model_hash':model.get('model_hash'), 'model_registered_at':model.get('registered_at'),
            'challenger':challenger, 'eth_entry':eth, 'regime':regimes,
            'rejections':dict(sorted(self.state['rejections'].items(),key=lambda kv:-kv[1])[:15]),
            'feature_coverage':{k:v for k,v in self.state['coverage'].items() if 'btc_relative' in k or 'latency' in k},
            'latest_review':self.state['reviews'][-1] if self.state['reviews'] else None,
            'note':'Separate studies; do not sum profits. Regime is forecast-only. Fixed reviews never enable live orders.'}

    def reviews(self, now=None):
        now = now or utcnow()
        registered = parsed(self.state['registered_at'])
        due = int((now-registered).total_seconds()//(30*86400))
        for index in range(1,due+1):
            if any(r['index']==index for r in self.state['reviews']):
                continue
            cutoff = registered+timedelta(days=30*index)
            summary = self.summary(now,cutoff,alpha=.05/(index*(index+1)*3*2))
            checks = {}
            for lane,minimum in [('challenger',200),('eth_entry',200),('regime',100)]:
                s = summary[lane]
                gates = {'minimum_sample':s['expiries' if lane=='regime' else 'settled']>=minimum,
                    'minimum_dates':s['active_dates']>=20, 'day_lower_positive':number(s['day_lower'],-1)>0,
                    'expiry_lower_positive':number(s['expiry_lower'],-1)>0,
                    'both_halves_positive':s['both_halves_positive'],
                    'drop_best_day_positive':s['drop_best_day_positive']}
                if lane=='challenger':
                    model = self.state.get('model') or {}
                    gates['model_30_days'] = bool(parsed(model.get('registered_at')) and
                        (cutoff-parsed(model['registered_at'])).total_seconds()>=30*86400)
                    gates['forecast_improvement'] = number(s['brier_improvement'],-1)>0
                if lane=='regime':
                    gates['complete_exits'] = s['unscored']==0
                    gates['beats_always_up'] = number(s['reversal_accuracy'])>number(s['always_up_accuracy'])
                    gates['probability_improves'] = number(s['brier_improvement'],-1)>0
                if lane=='eth_entry':
                    gates['wait_profitable_after_stress'] = s['wait_stress_profit']>0
                checks[lane] = {'gates':gates, 'metrics':s,
                    'status':('ready_for_further_forecast_research' if lane=='regime' else 'ready_for_manual_review')
                             if all(gates.values()) else 'continue_shadow'}
            self.state['reviews'].append({'index':index,'cutoff':cutoff.isoformat(),
                'automatic_promotion':False,'studies':checks})

    def persist(self):
        # Archive diagnostics losslessly before committing their counters/state.
        if self.events:
            self.state['_pending_evidence_archives'] = [
                {'field':'events','record':{'id':digest(e),**e}} for e in self.events]
            flush_evidence_archives(self.path,self.state)
            self.events.clear()
        retain_records(self.state,3000)
        flush_evidence_archives(self.path,self.state)
        atomic_json(self.path,self.state)
