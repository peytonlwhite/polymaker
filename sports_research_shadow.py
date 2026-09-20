"""Frozen prospective sports experiments. No credentials, settings or execution imports.

All portfolios are independent counterfactuals, never additive deployable P&L.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING
import copy
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

from sports_itf_shadow import (atomic_json, levels, now_central, number, parsed,
                               read_json, route_quote, settlement_value, stamp)

STATE = "sports_research_shadow_state.json"
REPORT = "sports_research_shadow_report.json"
HEALTH = "sports_research_shadow_health.json"
STRATEGIES = {
    "core_moneyline": "Fresh exact-line moneyline value, fixed stake",
    "core_total": "Fresh exact-line full-game totals, fixed stake",
    "pregame_maker": "Pregame value at a resting bid",
    "pregame_taker_control": "Immediate entry for the same maker opportunities",
    "liquidity_rewards": "Resting reward quotes; sampled incentives separate",
    "partial_total_ladder": "First-five / first-half total ladder dislocations",
    "itf_favorite": "ITF pregame favorites, 55–80 cents, tight books",
    "itf_favorite_rebound": "Observed ITF pregame favorite, live dip and recovery",
    "itf_dog_momentum": "ITF singles dogs, live 25–45 cents with confirmed momentum",
    "manual_tennis": "Prospective manual ITF/WTA selections at observable prices",
}
RULES = {
    "version": "sports-research-shadow-v1", "strategies": STRATEGIES,
    "days": 30, "unit_dollars": 10.0, "max_open_per_strategy": 20,
    "max_entries_per_strategy_day": 30, "one_entry_per_event_strategy": True,
    "core": {"price": [.25, .70], "max_spread": .03, "net_edge": .02,
             "min_exact_families": 2, "max_book_age_live_seconds": 90,
             "max_book_age_pregame_seconds": 180},
    "maker": {"ttl_seconds": 180, "cancel_before_start_seconds": 120,
              "fill": "non-block opposite aggressor trades strictly through bid; volume limited",
              "queue": "strict trade-through proxy, not real queue simulation"},
    "rewards": {"ttl_seconds": 180, "max_program_days": 7,
                "max_programs_per_scan": 4, "sample_gap_limit_seconds": 90,
                "estimated_rewards_excluded_from_profit": True},
    "partial": {"net_edge": .04, "max_spread": .04, "uncertainty": .03,
                "pricing": "interpolation between neighboring same-event total midpoints, exploratory only"},
    "itf": {"max_spread": .03, "min_volume_contracts": 100,
            "favorite_range": [.55, .80], "rebound_range": [.40, .60],
            "dog_range": [.25, .45], "momentum_seconds": [300, 900],
            "dog_bid_increase": .03, "favorite_bid_recovery": .02},
    "taker_fill": "integer contracts at common depth in two public snapshots at least one second apart",
    "fees": "series-specific public multiplier; conservative whole-cent rounding per fill",
    "minimum_review_events": 100, "minimum_review_days": 30,
    "automatic_promotion": False, "historical_backfill": False,
}
RULES_HASH = hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest()


def fee(count, price, rate):
    raw = Decimal(str(rate)) * Decimal(str(count)) * Decimal(str(price)) * (1 - Decimal(str(price)))
    return float(raw.quantize(Decimal('.01'), rounding=ROUND_CEILING))


def fee_rates(series):
    multiplier = number(series.get("fee_multiplier"))
    kind = series.get("fee_type")
    if multiplier is None or multiplier < 0 or kind not in {"quadratic", "quadratic_with_maker_fees"}:
        raise ValueError("Unknown series fee schedule")
    return {"taker": .07 * multiplier,
            "maker": .0175 * multiplier if kind == "quadratic_with_maker_fees" else 0.0,
            "fee_type": kind, "multiplier": multiplier}


def fresh(value, now, seconds):
    dt = parsed(value)
    return dt is not None and 0 <= (now - dt).total_seconds() <= seconds


def market_ok(market):
    return (market.get("status") in {"active", "open"} and not market.get("result")
            and market.get("market_type") == "binary"
            and number(market.get("notional_value_dollars")) == 1
            and market.get("price_level_structure") == "linear_cent")


def confirmed_fill(first, second, side, budget, rate, max_price=.99):
    a, b = route_quote(first, side), route_quote(second, side)
    if not (0 < a['bid'] <= a['ask'] < 1 and 0 < b['bid'] <= b['ask'] < 1):
        return None
    fills, cost, count, fees = [], 0.0, 0, 0.0
    for price in sorted(a['asks']):
        if price > max_price + 1e-9:
            continue
        qty = int(min(a['asks'][price], b['asks'].get(price, 0)))
        n = min(qty, int(max(0, budget - cost) / price))
        while n and cost + n * price + fee(n, price, rate) > budget + 1e-8:
            n -= 1
        if n:
            charge = fee(n, price, rate)
            cost += n * price + charge
            count += n
            fees += charge
            fills.append({"price": price, "contracts": n, "fee": charge})
    # Require at least 90% of the fixed simulated unit, rather than tiny depth fills.
    if not count or cost < .90 * budget:
        return None
    return {"contracts": count, "cost": round(cost, 4), "fee": round(fees, 4),
            "entry_price": round((cost - fees) / count, 6), "fills": fills}


def candidate_signal(candidate, generated_at, now):
    """Use fresh exact-line book lower bounds; no score boosts or loss recovery."""
    if not fresh(generated_at, now, 120) or candidate.get("game_completed"):
        return None
    kind = candidate.get("market_type")
    side = candidate.get("order_side")
    if kind not in {"moneyline", "total"} or side not in {"yes", "no"}:
        return None
    start = parsed(candidate.get("commence_time"))
    if not start:
        return None
    pregame = (start - now).total_seconds() >= 300 and candidate.get("game_started") is False
    if not pregame and candidate.get("game_started") is not True:
        return None
    if not pregame and (candidate.get("game_state_features") or {}).get("live_state_fresh") is not True:
        return None
    consensus = (candidate.get("pricing_v2") or {}).get("consensus") or {}
    max_age = 180 if pregame else 90
    exact = [r for r in consensus.get("observations", [])
             if r.get("all_exact_lines") is True and not r.get("interpolated")
             and r.get("family") and r.get("contributing_updates")
             and all(fresh(t, now, max_age) for t in r['contributing_updates'])]
    if not consensus.get("ok") or len({r['family'] for r in exact}) < 2:
        return None
    # Recompute from only fresh exact families. Never reuse a stale aggregate's lower bound.
    families = defaultdict(list)
    for row in exact:
        probability = number(row.get('probability'))
        if probability is not None and 0 < probability < 100:
            families[row['family']].append(probability)
    values = [statistics.mean(v) for v in families.values()]
    if len(values) < 2:
        return None
    uncertainty = max(2.0, 1.645 * statistics.stdev(values) / math.sqrt(len(values)),
                      (max(values)-min(values))/4)
    lower = min(statistics.mean(values), statistics.median(values)) - uncertainty
    if not 0 < lower < 100:
        return None
    if kind == "total":
        line = number(candidate.get("market_line"))
        if line is None or abs(line % 1 - .5) > 1e-8:
            return None
    if (candidate.get("equivalent_contract_review") or {}).get("ok") is False:
        return None
    blockers = (candidate.get('qualification_v2') or {}).get('hard_blockers', [])
    price_blockers = {'qualification_v2_non_positive_all_in_edge', 'qualification_v2_non_positive_conservative_edge'}
    if any(b not in price_blockers for b in blockers):
        return None
    return {"ticker": candidate.get("kalshi_ticker"), "side": side,
            "event": candidate.get("game_key") or candidate.get("event_id"),
            "kind": kind, "probability": lower / 100, "phase": "pregame" if pregame else "live",
            "start": stamp(start), "evidence": {"source": "fresh_exact_family_lower_bound",
            "families": sorted(families), "family_probabilities": values,
            "uncertainty_haircut_pp": uncertainty, "probability": lower / 100,
            "source_build": candidate.get("strategy_build_id"),
            "source_config": candidate.get("strategy_config_hash"), "sport": candidate.get("sport_key"),
            "source_generated_at": generated_at, "market_line": candidate.get("market_line")}}


def reward_score(book, side, price, count, target, discount):
    """Counterfactual one-sided quote share; both real sides must meet target depth."""
    if not 0 < discount <= 1 or target <= 0 or count <= 0:
        return 0.0
    if any(sum(levels(book, s).values()) < target for s in ('yes', 'no')):
        return 0.0
    bids = levels(book, side)
    bids[price] = bids.get(price, 0) + count
    cumulative, reference = 0, None
    for p, qty in sorted(bids.items(), reverse=True):
        cumulative += qty
        if cumulative >= target / 5:
            reference = p
            break
    total, ours, cumulative = 0.0, 0.0, 0.0
    for p, qty in sorted(bids.items(), reverse=True):
        if cumulative >= target:
            break
        # Boundary level counts in full, matching order-level inclusion; our share is conservative.
        ticks = max(0, int(round((reference - p) * 100)))
        weight = discount ** ticks
        total += qty * weight
        if abs(p - price) < 1e-8:
            ours = count * weight
        cumulative += qty
    return ours / total / 2 if total else 0.0  # both sides together normalize to two


def metrics(rows, now):
    closed = sorted([r for r in rows if r['status'] == 'settled'], key=lambda r: (r['placed_at'], r['id']))
    groups = defaultdict(lambda: [0.0, 0.0])
    for r in closed:
        groups[r['event']][0] += r['cost']
        groups[r['event']][1] += r['profit']
    profit, cost = sum(r['profit'] for r in closed), sum(r['cost'] for r in closed)
    risk = sum(r['cost'] for r in rows if r['status'] in {'open', 'working'})
    reserved = sum(max(0, 10-r['cost']) for r in rows if r['status'] == 'working')
    days = len({parsed(r['placed_at']).date().isoformat() for r in closed})
    outcomes = sorted(closed, key=lambda r: (r['settled_at'], r['id']))
    at = defaultdict(float)
    for r in outcomes:
        at[r['settled_at']] += r['profit']
    running = peak = dd = 0.0
    for value in at.values():
        running += value
        peak = max(peak, running)
        dd = max(dd, peak - running)
    half = len(closed) // 2
    halves = [sum(r['profit'] for r in closed[:half]), sum(r['profit'] for r in closed[half:])]
    interval = None
    if len(groups) >= 10:
        rng, draws, pairs = random.Random(91726), [], list(groups.values())
        for _ in range(1000):
            sample = rng.choices(pairs, k=len(pairs))
            denom = sum(x[0] for x in sample)
            if denom:
                draws.append(100 * sum(x[1] for x in sample) / denom)
        draws.sort()
        if draws:
            interval = [round(draws[int((len(draws)-1)*q)], 3) for q in (.025, .975)]
    return {"entries": len(rows), "settled": len(closed), "unique_events": len(groups),
            "entry_days": days, "open": sum(r['status'] == 'open' for r in rows),
            "working": sum(r['status'] == 'working' for r in rows),
            "unfilled": sum(r['status'] == 'unfilled' for r in rows),
            "partial_fills": sum(0 < r['contracts'] < r.get('target_contracts', r['contracts']) for r in rows),
            "profit": round(profit, 4), "profit_units": round(profit / 10, 4),
            "cost": round(cost, 4), "roi_pct": round(100 * profit / cost, 3) if cost else None,
            "open_risk": round(risk, 4), "working_reserve": round(reserved, 4),
            "worst_case_profit": round(profit - risk - reserved, 4), "max_drawdown": round(dd, 4),
            "without_best_event": round(profit - max([0] + [x[1] for x in groups.values()]), 4),
            "chronological_half_profit": [round(x, 4) for x in halves],
            "exploratory_event_bootstrap_roi_95": interval,
            "estimated_reward": round(sum(r.get('reward_estimate', 0) for r in rows), 4),
            "automatic_promotion": False}


class Research:
    def __init__(self, root, *, register=False, now=None, implementation_hash=None):
        self.root = Path(root)
        now = now or now_central()
        path = self.root / STATE
        try:
            self.state = read_json(path)
        except FileNotFoundError:
            if not register or (self.root / REPORT).exists() or (self.root / HEALTH).exists():
                raise ValueError("Explicit first registration required; never reconstruct a missing ledger")
            self.state = {"mode": "shadow", "rules_hash": RULES_HASH, "rules": copy.deepcopy(RULES),
                          "implementation_hash": implementation_hash, "started_at": stamp(now),
                          "ends_at": stamp(now + timedelta(days=30)), "positions": {}, "itf_history": {},
                          "itf_anchors": {}, "manual_baseline": None, "manual_journal": {},
                          "scan_count": 0, "rejections": {}, "coverage": {}, "last_scan_at": None}
        s = self.state
        if s.get('mode') != 'shadow' or s.get('rules_hash') != RULES_HASH:
            raise ValueError("Frozen cohort rules mismatch; do not reset")
        if implementation_hash is not None and s.get('implementation_hash') != implementation_hash:
            raise ValueError("Frozen implementation changed; review required")
        if not parsed(s.get('started_at')) or not parsed(s.get('ends_at')) or now < parsed(s['started_at']):
            raise ValueError("Invalid research clock/window")
        if parsed(s['ends_at']) - parsed(s['started_at']) != timedelta(days=30):
            raise ValueError("Invalid frozen window")
        if not isinstance(s.get('positions'), dict):
            raise ValueError("Invalid ledger")
        for key, row in s['positions'].items():
            if (key != row.get('id') or row.get('strategy') not in STRATEGIES
                    or row.get('side') not in {'yes', 'no'} or row.get('status') not in {'working', 'open', 'settled', 'unfilled'}
                    or number(row.get('cost')) is None or row['cost'] < 0
                    or number(row.get('contracts')) is None or row['contracts'] < 0
                    or not parsed(row.get('placed_at'))):
                raise ValueError("Invalid position; preserve ledger")

    def persist(self):
        atomic_json(self.root / STATE, self.state)

    def reject(self, reason):
        self.state['rejections'][reason] = self.state['rejections'].get(reason, 0) + 1

    def can_enter(self, strategy, event, now):
        rows = [r for r in self.state['positions'].values() if r['strategy'] == strategy]
        return (parsed(self.state['started_at']) <= now < parsed(self.state['ends_at'])
                and event and strategy + '|' + event not in self.state['positions']
                and sum(r['status'] in {'working', 'open'} for r in rows) < 20
                and sum(parsed(r['placed_at']).date() == now.date() for r in rows) < 30)

    def enter(self, strategy, event, market, side, first, second, rates, now, *,
              evidence=None, max_price=.99, maker=False, expiry=None, reward=None):
        if not self.can_enter(strategy, event, now) or not market_ok(market):
            return None
        q = route_quote(second, side)
        if not 0 < q['bid'] <= q['ask'] < 1:
            return None
        if maker:
            limit = round(min(q['bid'], max_price), 2)
            if not .10 <= limit <= .85 or limit >= q['ask'] or not expiry or expiry <= now:
                return None
            n = int(10 / (limit + rates['maker'] * limit * (1-limit)))
            while n and n * limit + fee(n, limit, rates['maker']) > 10:
                n -= 1
            if not n:
                return None
            fill = {"contracts": 0, "cost": 0.0, "fee": 0.0, "entry_price": limit, "fills": [],
                    "target_contracts": n, "remaining": n, "limit": limit, "expires_at": stamp(expiry),
                    "last_trade_check": stamp(now), "reward_last_at": None, "reward_estimate": 0.0}
        else:
            fill = confirmed_fill(first, second, side, 10, rates['taker'], max_price)
            if fill is None:
                self.reject(strategy + ':insufficient_confirmed_depth')
                return None
        key = strategy + '|' + event
        row = {**fill, "id": key, "strategy": strategy, "event": event,
               "event_ticker": market['event_ticker'], "ticker": market['ticker'], "side": side,
               "series": market['ticker'].split('-')[0], "title": market.get('title'),
               "placed_at": stamp(now), "status": 'working' if maker else 'open',
               "entry_bid": q['bid'], "entry_ask": q['ask'], "rates": rates,
               "rules_primary": market.get('rules_primary'), "rules_secondary": market.get('rules_secondary'),
               "evidence": evidence or {}, "reward_program": reward, "mode": "shadow",
               "affects_execution": False, "automatic_promotion": False}
        self.state['positions'][key] = row
        return row

    def process_trades(self, row, trades, now, *, complete=True):
        if row['status'] != 'working':
            return
        lower, expiry = parsed(row['last_trade_check']), parsed(row['expires_at'])
        if not complete or (now - lower).total_seconds() > 120:
            # Never award optimistic fills after a data outage or truncated trade page.
            row['fill_data_gap'] = True
            row['status'] = 'open' if row['contracts'] else 'unfilled'
            row['remaining'] = 0
            return
        seen = set()
        for trade in sorted(trades, key=lambda t: str(t.get('created_time', ''))):
            dt = parsed(trade.get('created_time'))
            tid = trade.get('trade_id')
            if not tid or tid in seen or not dt or not lower < dt <= min(now, expiry):
                continue
            seen.add(tid)
            if trade.get('ticker') != row['ticker'] or trade.get('is_block_trade') is not False:
                continue
            aggressor = trade.get('taker_outcome_side') or trade.get('taker_side')
            if aggressor != ('no' if row['side'] == 'yes' else 'yes'):
                continue
            price = number(trade.get(row['side'] + '_price_dollars'))
            volume = number(trade.get('count_fp'))
            if price is None or not 0 < price < row['limit'] or volume is None or volume <= 0:
                continue
            n = min(row['remaining'], int(volume))
            # Round each fill conservatively, while respecting the fixed $10 budget.
            while n and row['cost'] + n * row['limit'] + fee(n, row['limit'], row['rates']['maker']) > 10:
                n -= 1
            if not n:
                continue
            charge = fee(n, row['limit'], row['rates']['maker'])
            row['contracts'] += n
            row['remaining'] -= n
            row['fee'] = round(row['fee'] + charge, 4)
            row['cost'] = round(row['cost'] + n * row['limit'] + charge, 4)
            row['fills'].append({'trade_id': tid, 'at': stamp(dt), 'price': row['limit'], 'contracts': n, 'fee': charge})
        row['last_trade_check'] = stamp(now)
        if not row['remaining'] or now >= expiry:
            row['status'] = 'open' if row['contracts'] else 'unfilled'
            row['remaining'] = 0

    def accrue_reward(self, row, book, now):
        program = row.get('reward_program')
        if not program or row['status'] != 'working':
            return
        previous = parsed(row.get('reward_last_at'))
        row['reward_last_at'] = stamp(now)
        share = reward_score(book, row['side'], row['limit'], row['remaining'],
                             float(program['target_size_fp']), float(program['discount_factor_bps']) / 10000)
        old_share = row.get('reward_last_share', 0)
        row['reward_last_share'] = share
        if not previous or not 0 < (now-previous).total_seconds() <= 90:
            return
        start, end = parsed(program['start_date']), parsed(program['end_date'])
        a, b = max(previous, start), min(now, end, parsed(row['expires_at']))
        seconds = max(0, (b-a).total_seconds())
        pool = float(program['period_reward']) / 10000  # API unit is centi-cents.
        row['reward_estimate'] += min(share, old_share) * seconds / (end-start).total_seconds() * pool
        row['reward_sampled_seconds'] = row.get('reward_sampled_seconds', 0) + seconds

    def settle(self, markets, now):
        for row in self.state['positions'].values():
            if row['status'] not in {'open', 'working'}:
                continue
            market = markets.get(row['ticker'], {})
            if market.get('ticker') != row['ticker']:
                continue
            value = settlement_value(market)
            if value is None:
                continue
            row['remaining'] = 0
            if row['contracts'] == 0:
                row['status'] = 'unfilled'
                continue
            payout = row['contracts'] * (value if row['side'] == 'yes' else 1-value)
            row.update(status='settled', settled_at=stamp(now), payout=round(payout, 4),
                       profit=round(payout-row['cost'], 4), settlement_value=value,
                       exchange_settlement_at=market.get('settlement_ts'))

    def summary(self, now=None):
        now = now or now_central()
        rows = list(self.state['positions'].values())
        result = []
        for key, label in STRATEGIES.items():
            selected = [r for r in rows if r['strategy'] == key]
            m = metrics(selected, now)
            elapsed = (now-parsed(self.state['started_at'])).total_seconds()/86400
            interval = m['exploratory_event_bootstrap_roi_95']
            gates = {'30_days_elapsed': elapsed >= 30, '100_distinct_events': m['unique_events'] >= 100,
                     'at_least_10_entry_days': m['entry_days'] >= 10,
                     'positive_after_fees': m['profit'] > 0,
                     'both_halves_positive': all(x > 0 for x in m['chronological_half_profit']),
                     'positive_without_best_event': m['without_best_event'] > 0,
                     'exploratory_interval_positive': bool(interval and interval[0] > 0),
                     'all_positions_resolved': m['open'] == 0 and m['working'] == 0}
            result.append({'id': key, 'name': label, **m, 'review_gates': gates,
                           'evidence': 'candidate_for_independent_validation' if all(gates.values()) else 'collecting_or_unproven'})
        status = 'collecting' if now < parsed(self.state['ends_at']) else 'maturing' if any(r['status'] in {'open','working'} for r in rows) else 'complete'
        return {'version': RULES['version'], 'mode': 'shadow', 'status': status,
                'generated_at': stamp(now), 'started_at': self.state['started_at'], 'ends_at': self.state['ends_at'],
                'unit_dollars': 10, 'rules_hash': RULES_HASH, 'automatic_promotion': False,
                'scan_count': self.state['scan_count'], 'last_scan_at': self.state['last_scan_at'],
                'strategies': result, 'coverage': self.state['coverage'], 'rejections': self.state['rejections'],
                'manual_journal_count': len(self.state['manual_journal']),
                'limitations': ['Independent overlapping portfolios: do not sum profits.',
                               'Shadow fills are counterfactual; strict trade-through is not an exchange queue.',
                               'Reward estimates use sparse public snapshots and are excluded from trading P&L.',
                               'Intervals are exploratory and not adjusted for multiple strategies; a fresh holdout is required.',
                               'Core candidate universe is the live report top_candidates, not every discarded candidate.',
                               'Partial totals use market-ladder interpolation, not verified sportsbook fair odds.']}
