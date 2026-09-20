"""Prospective ETH entry/sizing comparisons. No account or order API exists here."""
from copy import deepcopy
from datetime import timedelta
import json
import math
from pathlib import Path

from crypto_prospective_shadow import hypothesis, quality_reasons, parsed, number, local_day
from crypto_shadow_expansion import utcnow, quote_evidence, lower, consistency
from crypto_shadow_challenger import atomic_json, digest
from crypto_evidence import flush_evidence_archives, retain_records

POLICIES = ('fixed_3', 'signal', 'defensive', 'bounded_recovery', 'confidence_bound')
TIMINGS = ('immediate', 'patient')
PROTOCOL = {
    'version': 'eth-sizing-20260914-v1', 'shadow_only': True, 'automatic_promotion': False,
    'entry': 'first fresh ETH persistence signal per ticker after worker 300s warmup',
    'timings': {'immediate': 'first confirmed quote', 'patient': '60s for 1c improvement; no fallback'},
    'policies': list(POLICIES), 'starting_cash_per_arm': 500,
    'signal_tiers': {'weak': 1, 'ordinary': 2, 'aligned': 3, 'strong': 4, 'very_strong': 5},
    'strong_thresholds': {'flow60': .35, 'flow300': .2, 'trades': 40, 'quality': .98, 'spread': 1},
    'very_strong_thresholds': {'flow60': .5, 'flow300': .3, 'trades': 60},
    'drawdown': {'halve_dollars': 10, 'one_contract_dollars': 20, 'stop_dollars': 30},
    'recovery': {'extra_contracts': 1, 'maximum_extra_cost_per_day': 5,
                 'requires_strong_signal': True, 'maximum_drawdown_dollars': 10,
                 'requires_last_settled_loss': True, 'no_loss_multiplier': True},
    'confidence_bound': {'fractional_kelly': .25, 'requires_empirical_coverage_qualified': True,
                         'minimum_edge_after_cost_and_stress_cents': 2},
    'limits': {'contracts': 5, 'trade_risk_fraction': .01, 'trade_risk_dollars': 5,
               'open': 4, 'per_expiry': 2, 'exposure': 45, 'daily_gross_loss_plus_open_risk': 45,
               'daily_entries': 25},
    'stress_cents_per_contract': 1, 'reviews': {'days': 30, 'minimum_markets': 200,
        'minimum_dates': 20, 'family_size': 10, 'automatic_promotion': False},
    'comparison': 'separate equal-bankroll arms; missed fills count zero per opportunity; never sum arms',
}
POLICY_HASH = digest(PROTOCOL)


def signal_features(candidate, side):
    orientation = -1 if candidate.get('market_kind') == 'below' else 1
    direction = orientation * (1 if side == 'yes' else -1)
    venues = [(candidate.get('microstructure') or {}).get(v) or {} for v in ('coinbase', 'kraken')]
    return {'flow60': min(number(v.get('trade_flow_60s')) * direction for v in venues),
            'flow300': min(number(v.get('trade_flow_300s')) * direction for v in venues),
            'trades': min(number(v.get('trade_count_60s')) for v in venues),
            'quality': number((candidate.get('data_quality') or {}).get('score')),
            'spread': number((candidate.get('kalshi_microstructure') or {}).get('spread_yes_cents'), 999)}


def signal_size(features):
    f = features
    if f['quality'] < .95 or f['spread'] > 1.5:
        return 1
    if f['flow60'] >= .35 and f['flow300'] >= .2 and f['trades'] >= 40 and f['quality'] >= .98 and f['spread'] <= 1:
        return 5 if f['flow60'] >= .5 and f['flow300'] >= .3 and f['trades'] >= 60 else 4
    return 3 if f['flow60'] >= .25 and f['flow300'] >= .15 else 2


def probability_lower(candidate, side):
    interval = candidate.get('probability_interval') or {}
    if interval.get('interval_status') != 'empirical_coverage_qualified':
        return None
    lo, hi = number(interval.get('p_low'), -1), number(interval.get('p_high'), -1)
    if not 0 <= lo <= hi <= 100:
        return None
    return (lo if side == 'yes' else 100-hi) / 100


class SizingLab:
    def __init__(self, path, now=None):
        self.path = Path(path)
        now = now or utcnow()
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
            'registered_at': now.isoformat(), 'policy_hash': POLICY_HASH, 'protocol': deepcopy(PROTOCOL),
            'records': [], 'reviews': [], 'scan_count': 0, 'rejections': {},
        }
        if self.state.get('policy_hash') != POLICY_HASH:
            raise ValueError('Sizing policy changed; register a separate ledger, preserving this cohort')

    def reject(self, reason):
        counts = self.state['rejections']
        counts[reason] = counts.get(reason, 0) + 1

    def rows(self, key, cutoff=None):
        return [dict(arm, ticker=r['ticker'], close_time=r['close_time'])
                for r in self.state['records'] for name, arm in r['arms'].items()
                if name == key and (not cutoff or parsed(arm['captured_at']) <= cutoff)]

    def risk(self, key, now):
        rows = self.rows(key)
        settled = sorted([r for r in rows if r['status'] == 'settled' and parsed(r['settled_at']) <= now],
                         key=lambda r: (r['settled_at'], r['ticker']))
        cash, peak, max_dd = 500., 500., 0.
        for row in settled:
            cash += row['profit_dollars']
            peak = max(peak, cash)
            max_dd = max(max_dd, peak-cash)
        opened = [r for r in rows if r['status'] == 'open']
        exposure = sum(r['cost_dollars'] for r in opened)
        day = local_day(now.isoformat())
        today = [r for r in rows if local_day(r['captured_at']) == day and r['status'] in ('open', 'settled')]
        loss = sum(max(0, -r['profit_dollars']) for r in settled if local_day(r['settled_at']) == day)
        return {'cash': cash, 'drawdown': peak-cash, 'max_drawdown': max_dd,
                'opened': opened, 'exposure': exposure, 'daily_loss': loss, 'daily_entries': len(today),
                'last_loss': bool(settled and settled[-1]['profit_dollars'] < 0),
                'recovery_spent': sum(r.get('recovery_extra_cost', 0) for r in today)}

    def requested_size(self, policy, features, risk, p_low):
        base = signal_size(features)
        if risk['drawdown'] >= 30 or risk['cash'] <= 0:
            return 0, 0, 'drawdown_stop'
        if policy == 'fixed_3':
            return 3, 0, 'fixed_baseline'
        if policy == 'confidence_bound':
            return (5, 0, 'qualified_probability_bound') if p_low is not None else (0, 0, 'probability_bound_unqualified')
        if policy in ('defensive', 'bounded_recovery'):
            if risk['drawdown'] >= 20:
                return 1, 0, 'drawdown_one_contract'
            if risk['drawdown'] >= 10:
                return max(1, base//2), 0, 'drawdown_halved'
        if policy == 'bounded_recovery' and base >= 4 and risk['last_loss'] and risk['recovery_spent'] < 5:
            return min(5, base+1), int(base < 5), 'strong_signal_capped_recovery'
        return base, 0, 'signal_strength_proxy'

    def fill(self, record, timing, candidate, quotes, now):
        side = record['side']
        features = signal_features(candidate, side)
        p_low = probability_lower(candidate, side)
        ceiling = (record.get('patient_anchor_cents', record['signal_price_cents'])-1
                   if timing == 'patient' else record['signal_price_cents'])
        for policy in POLICIES:
            key = timing + ':' + policy
            arm = record['arms'][key]
            if arm['status'] != 'waiting':
                continue
            risk = self.risk(key, now)
            requested, extra, reason = self.requested_size(policy, features, risk, p_low)
            arm.update(last_reason=reason)
            if requested == 0:
                if timing == 'immediate': arm.update(status='no_fill', reason=reason)
                self.reject(key+':'+reason)
                continue
            quantities = [requested] if policy == 'fixed_3' else range(requested, 0, -1)
            for quantity in quantities:
                reasons, entry = quote_evidence(candidate, side, quantity, quotes.get(quantity, {}), now, ceiling)
                if not 35 <= entry['entry_price_cents'] <= 65:
                    reasons.append('price_outside_band')
                if reasons:
                    arm['last_reason'] = ','.join(sorted(set(reasons)))
                    continue
                cost = entry['cost_dollars']
                if cost > min(5, risk['cash']*.01, risk['cash']-risk['exposure']):
                    arm['last_reason'] = 'per_trade_cash_limit'
                    continue
                if (risk['daily_entries'] >= 25 or len(risk['opened']) >= 4
                    or sum(r['close_time'] == record['close_time'] for r in risk['opened']) >= 2
                    or risk['exposure']+cost > 45 or risk['daily_loss']+risk['exposure']+cost > 45):
                    arm['last_reason'] = 'portfolio_limit'
                    continue
                if policy == 'confidence_bound':
                    unit_cost = cost/quantity + .01
                    fraction = .25*max(0, (p_low-unit_cost)/max(1e-9, 1-unit_cost))
                    if p_low-unit_cost < .02 or quantity*unit_cost > risk['cash']*fraction:
                        arm['last_reason'] = 'insufficient_lower_bound_edge'
                        continue
                # Charge the entire marginal contract cost, including rounded fees.
                extra_cost = 0.
                if extra and quantity > signal_size(features):
                    _, smaller = quote_evidence(candidate, side, quantity-1, quotes.get(quantity-1, {}), now, ceiling)
                    extra_cost = max(cost-smaller['cost_dollars'], cost/quantity)
                    if risk['recovery_spent']+extra_cost > 5:
                        arm['last_reason'] = 'daily_recovery_budget'
                        continue
                arm.update(entry, status='open', captured_at=now.isoformat(), requested_contracts=requested,
                           sizing_reason=reason, signal_evidence=features, probability_lower=p_low,
                           risk_at_entry={k:v for k,v in risk.items() if k != 'opened'},
                           recovery_extra_cost=extra_cost, recovery_extra_contracts=int(extra_cost > 0))
                break
            if arm['status'] == 'waiting':
                self.reject(key+':'+arm['last_reason'])
                if timing == 'immediate': arm.update(status='no_fill', reason=arm['last_reason'])

    def observe(self, initial, refresh, quote_all, now_fn=utcnow):
        now = now_fn()
        if now < parsed(self.state['registered_at']): return
        ticker = initial.get('ticker')
        record = next((r for r in self.state['records'] if r['ticker'] == ticker), None)
        if record and not any(a['status'] == 'waiting' for a in record['arms'].values()): return
        rule = hypothesis(initial, 'eth_persistence', now)
        if not rule:
            self.reject('persistence_signal_not_matched')
            return
        quality = quality_reasons(initial, now)
        if quality:
            for reason in quality: self.reject(reason)
            return
        if record and (now > parsed(record['deadline']) or rule['side'] != record['side']): return
        side = rule['side']
        signal = number(initial.get(side+'_ask'))
        if not 35 <= signal <= 65: return
        if record and signal > record.get('patient_anchor_cents', record['signal_price_cents'])-1: return
        quotes = quote_all(ticker, side)
        latest = refresh(initial)
        now = now_fn()
        updated_rule = hypothesis(latest, 'eth_persistence', now)
        if quality_reasons(latest, now) or not updated_rule or updated_rule['side'] != side:
            self.reject('signal_changed_or_quality_failed_on_confirmation')
            return
        if record:
            if now <= parsed(record['deadline']): self.fill(record, 'patient', latest, quotes, now)
            return
        record = {'ticker': ticker, 'side': side, 'close_time': initial['close_time'],
            'captured_at': now.isoformat(), 'deadline': (now+timedelta(seconds=60)).isoformat(),
            'signal_price_cents': signal, 'status': 'open', 'signal_evidence': deepcopy(latest),
            'arms': {timing+':'+policy: {'status': 'waiting', 'captured_at': now.isoformat()}
                     for timing in TIMINGS for policy in POLICIES}}
        self.state['records'].append(record)
        self.fill(record, 'immediate', latest, quotes, now)
        baseline = record['arms']['immediate:fixed_3']
        record['patient_anchor_cents'] = baseline.get('entry_price_cents', signal)
        record['patient_anchor_source'] = 'confirmed_fixed_entry' if baseline['status']=='open' else 'signal_no_baseline_fill'

    def settle(self, fetch_market, now=None):
        now = now or utcnow()
        for record in self.state['records']:
            if record['status'] == 'settled': continue
            if now > parsed(record['deadline']):
                for arm in record['arms'].values():
                    if arm['status'] == 'waiting':
                        arm.update(status='no_fill', reason=arm.get('last_reason', 'wait_deadline_elapsed'))
            if now < parsed(record['close_time']): continue
            try:
                market = fetch_market(record['ticker'])
                market = market.get('market', market)
            except Exception:
                self.reject('settlement_unavailable')
                continue
            result = market.get('result')
            if market.get('status') not in ('finalized', 'settled') or result not in ('yes', 'no'): continue
            record.update(status='settled', result=result, settled_at=now.isoformat())
            for arm in record['arms'].values():
                if arm['status'] == 'open':
                    profit = (arm['contracts'] if record['side'] == result else 0)-arm['cost_dollars']
                    arm.update(status='settled', settled_at=now.isoformat(), profit_dollars=round(profit,8),
                        stress_profit_dollars=round(profit-.01*arm['contracts'],8), won=record['side']==result)
                else:
                    arm.update(status='no_fill', settled_at=now.isoformat(), profit_dollars=0., stress_profit_dollars=0.)

    def summary(self, now=None, cutoff=None, alpha=.05/40):
        now = now or utcnow()
        records = [r for r in self.state['records'] if r['status']=='settled'
                   and (cutoff is None or parsed(r['settled_at']) <= cutoff)]
        summaries = []
        for timing in TIMINGS:
            for policy in POLICIES:
                key = timing+':'+policy
                rows = [dict(r['arms'][key], ticker=r['ticker'], close_time=r['close_time'],
                             opportunity_at=r['captured_at'],
                             paired_delta=r['arms'][key]['stress_profit_dollars']-r['arms'][timing+':fixed_3']['stress_profit_dollars'],
                             immediate_delta=r['arms'][key]['stress_profit_dollars']-r['arms']['immediate:fixed_3']['stress_profit_dollars']) for r in records]
                trades = [r for r in rows if r['status']=='settled']
                cost = sum(r['cost_dollars'] for r in trades)
                profit = sum(r['profit_dollars'] for r in rows)
                contracts = sum(r['contracts'] for r in trades)
                buckets = {}
                for trade in trades:
                    bucket = buckets.setdefault(str(trade['contracts']), {'trades':0,'profit':0.,'stress_profit':0.})
                    bucket['trades'] += 1
                    bucket['profit'] += trade['profit_dollars']
                    bucket['stress_profit'] += trade['stress_profit_dollars']
                risk = self.risk(key, cutoff or now)
                value = lambda r:r['stress_profit_dollars']
                paired = lambda r:r['paired_delta']
                summaries.append({'arm':key, 'opportunities':len(rows), 'filled':len(trades),
                    'no_fill':len(rows)-len(trades), 'open':len(risk['opened']),
                    'days':len({local_day(r['captured_at']) for r in trades}),
                    'opportunity_days':len({local_day(r['opportunity_at']) for r in rows}),
                    'markets':len({r['ticker'] for r in rows}),
                    'profit':round(profit,6), 'stress_profit':round(sum(map(value,rows)),6),
                    'cost':round(cost,6), 'roi':profit/cost if cost else None,
                    'average_contracts':sum(r['contracts'] for r in trades)/len(trades) if trades else None,
                    'max_drawdown':round(risk['max_drawdown'],6), 'exposure':round(risk['exposure'],6),
                    'drawdown_basis':'realized_cash; open risk reported separately',
                    'win_rate':sum(r['won'] for r in trades)/len(trades) if trades else None,
                    'profit_per_contract':profit/contracts if contracts else None,
                    'size_buckets':buckets,
                    'recovery_trade_profit':sum(r['profit_dollars'] for r in trades if r.get('recovery_extra_contracts')),
                    'recovery_extra_contracts':sum(r.get('recovery_extra_contracts',0) for r in trades),
                    'paired_stress_delta':round(sum(map(paired,rows)),6),
                    'vs_immediate_fixed_delta':round(sum(r['immediate_delta'] for r in rows),6),
                    'day_lower':lower(rows,value,lambda r:local_day(r['opportunity_at']),10,alpha),
                    'expiry_lower':lower(rows,value,lambda r:r['close_time'],10,alpha),
                    'paired_day_lower':lower(rows,paired,lambda r:local_day(r['opportunity_at']),20,alpha),
                    **consistency([dict(r,captured_at=r['opportunity_at']) for r in rows],value)})
        return {'generated_at':now.isoformat(), 'registered_at':self.state['registered_at'],
            'policy_hash':POLICY_HASH, 'mode':'shadow_only', 'automatic_promotion':False,
            'next_review_at':(parsed(self.state['registered_at'])+timedelta(days=30*(len(self.state['reviews'])+1))).isoformat(),
            'arms':summaries, 'scan_count':self.state['scan_count'],
            'rejections':self.state['rejections'],
            'latest_review':{k:v for k,v in self.state['reviews'][-1].items() if k!='summary'} if self.state['reviews'] else None,
            'note':'Separate $500 paper arms; do not sum profits. Signal tiers are uncalibrated proxies. No live promotion.'}

    def reviews(self, now=None):
        now = now or utcnow()
        registered = parsed(self.state['registered_at'])
        due = (now-registered).days//30
        for index in range(1,due+1):
            if any(r['index']==index for r in self.state['reviews']): continue
            cutoff = registered+timedelta(days=30*index)
            report = self.summary(now,cutoff,alpha=.05/(index*(index+1)*10*4))
            checks = {}
            for arm in report['arms']:
                checks[arm['arm']] = {'minimum_markets':arm['filled']>=200, 'minimum_dates':arm['days']>=20,
                    'positive_stress':arm['stress_profit']>0, 'positive_day_lower':number(arm['day_lower'],-1)>0,
                    'positive_expiry_lower':number(arm['expiry_lower'],-1)>0,
                    'both_halves':arm['both_halves_positive'], 'drop_best_day':arm['drop_best_day_positive'],
                    'sizing_beats_baseline':arm['arm'].endswith(':fixed_3') or number(arm['paired_day_lower'],-1)>0}
            self.state['reviews'].append({'index':index,'cutoff':cutoff.isoformat(), 'checks':checks,
                'eligible_for_manual_review':[k for k,v in checks.items() if all(v.values())],
                'automatic_promotion':False,'summary':report})

    def persist(self):
        retain_records(self.state, 500)
        flush_evidence_archives(self.path, self.state)
        atomic_json(self.path, self.state)
