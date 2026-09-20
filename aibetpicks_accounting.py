"""AIBetPicks fill reconciliation and read-only dashboard accounting.

The worker supplies the authenticated GET-only fill reader. This module cannot
place orders or write files; the worker persists changes through its normal lock.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def cents(value):
    return float(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def stamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (result if result.tzinfo else result.replace(tzinfo=CHICAGO)).astimezone(CHICAGO)
    except (TypeError, ValueError):
        return None


def is_aibetpicks(row):
    return row.get('source') == 'aibetpicks' or row.get('strategy_owner') == 'aibetpicks'


def order_ids(row):
    orders = [row.get('live_order') or {}, *(row.get('topups') or [])]
    return [str((order.get('response') or {}).get('order_id') or order.get('order_id') or '') for order in orders]


def fill_signature(row):
    data = [order_ids(row), row.get('kalshi_ticker'), row.get('kalshi_order_side') or row.get('order_side'), row.get('stake'), row.get('contracts')]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def verified_fee(row):
    audit = row.get('aibetpicks_fill_accounting') or {}
    fee = number(audit.get('fee'))
    if audit.get('status') == 'verified' and audit.get('signature') == fill_signature(row) and fee is not None and fee >= 0:
        return float(fee)
    return None


def exact_fill_totals(row, fills):
    identifiers = order_ids(row)
    if not identifiers or not all(identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError('order_identity_missing_or_duplicate')
    selected, seen = [], {}
    side = str(row.get('kalshi_order_side') or row.get('order_side') or '').lower()
    if side not in {'yes', 'no'}:
        raise ValueError('held_side_missing')
    for fill in fills:
        if str(fill.get('order_id') or '') not in identifiers:
            continue
        identity = fill.get('fill_id') or fill.get('trade_id')
        if not identity:
            raise ValueError('fill_identity_missing')
        if identity in seen:
            if seen[identity] != fill:
                raise ValueError('conflicting_duplicate_fill')
            continue
        seen[identity] = fill
        # V2 event orders express NO exposure as an ask on YES. Canonical
        # outcome_side/book_side supersede the deprecated action/side pair.
        direction = fill.get('outcome_side')
        book_side = fill.get('book_side')
        if not direction and book_side in {'bid', 'ask'}:
            direction = 'yes' if book_side == 'bid' else 'no'
        if not direction and fill.get('action') in {'buy', 'sell'} and fill.get('side') in {'yes', 'no'}:
            direction = fill['side'] if fill['action'] == 'buy' else ('no' if fill['side'] == 'yes' else 'yes')
        if direction != side or (book_side in {'bid', 'ask'} and book_side != ('bid' if side == 'yes' else 'ask')) or (fill.get('ticker') or fill.get('market_ticker')) != row.get('kalshi_ticker'):
            raise ValueError('fill_identity_mismatch')
        count = number(fill.get('count_fp') if fill.get('count_fp') is not None else fill.get('count'))
        price = number(fill.get(f'{side}_price_dollars'))
        fee = next((number(fill[k]) for k in ('fee_cost', 'fee_cost_dollars', 'fee_dollars') if fill.get(k) is not None), None)
        if count is None or count <= 0 or price is None or not 0 < price < 1 or fee is None or fee < 0:
            raise ValueError('fill_financials_missing')
        selected.append((str(fill['order_id']), count, price, fee))
    count = sum((f[1] for f in selected), Decimal(0))
    premium = sum((f[1] * f[2] for f in selected), Decimal(0))
    fee = sum((f[3] for f in selected), Decimal(0))
    if set(f[0] for f in selected) != set(identifiers) or count != number(row.get('contracts')):
        raise ValueError('incomplete_order_fills')
    if number(row.get('stake')) != Decimal(str(cents(premium))):
        raise ValueError('fill_premium_mismatch')
    return {'contracts': float(count), 'premium': float(premium), 'fee': float(fee), 'fill_count': len(selected)}


def reconcile_fill_fees(portfolio, fetch_fills, *, now=None, limit=8):
    """Reconcile existing fills only; never touch cash, quantity, or unit sizing."""
    now = now or datetime.now(CHICAGO)
    changes, attempts = [], 0
    rows = [*(portfolio.get('bets') or []), *(portfolio.get('history') or [])]
    for row in rows:
        if not is_aibetpicks(row) or row.get('mode') != 'live' or row.get('status') not in {'open', 'settled'}:
            continue
        if verified_fee(row) is not None:
            continue
        previous = row.get('aibetpicks_fill_accounting') or {}
        retry = stamp(previous.get('retry_after'))
        if retry and now < retry or attempts >= limit:
            continue
        attempts += 1
        audit = {'status': 'pending', 'checked_at': now.isoformat(), 'signature': fill_signature(row)}
        before = {k: row.get(k) for k in ('fee', 'profit')}
        try:
            placed = stamp(row.get('placed_at'))
            if not placed or not all(order_ids(row)):
                raise ValueError('order_identity_missing')
            response = fetch_fills(row['kalshi_ticker'], placed.timestamp() - 60)
            if not response.get('ok'):
                raise ValueError('exchange_fills_unavailable')
            totals = exact_fill_totals(row, response.get('fills') or [])
            audit.update(totals, status='verified')
            row.update(fee=totals['fee'], fee_source='exchange_order_fills', aibetpicks_fill_accounting=audit)
            if row.get('status') == 'settled' and row.get('result') in {'WIN', 'LOSS'}:
                payout, stake = number(row.get('payout')), number(row.get('stake'))
                if payout is not None and stake is not None:
                    row.update(gross_profit=cents(payout - stake), profit=cents(payout - stake - number(totals['fee'])), fee_accounted=True)
        except (ValueError, KeyError, TypeError, OSError):
            # Keep existing accounting on incomplete evidence. Retry independently
            # of the source-pick polling schedule; never invent a zero fee.
            audit.update(reason='exact_fill_verification_pending', retry_after=(now + timedelta(minutes=5)).isoformat())
            row['aibetpicks_fill_accounting'] = audit
        changes.append({'bet_id': row.get('id'), 'status': audit['status'], 'before': before,
                        'after': {k: row.get(k) for k in ('fee', 'profit')}, 'at': now.isoformat()})
    return changes


def local_analytics(open_rows, history, *, now=None):
    """Full live ledger, net after fees, normalized by EACH entry's dollar unit."""
    now = now or datetime.now(CHICAGO)
    rows, errors, seen = [], [], set()
    for source in [*(history or []), *(open_rows or [])]:
        if not is_aibetpicks(source) or source.get('mode') != 'live':
            continue
        identity = source.get('id')
        if not identity:
            errors.append({'id': None, 'reason': 'ledger_identity_missing'})
        if identity and identity in seen:
            errors.append({'id': identity, 'reason': 'duplicate_ledger_id'})
            continue
        seen.add(identity)
        status, result = source.get('status'), source.get('result')
        settled = status == 'settled' and result in {'WIN', 'LOSS', 'VOID', 'PUSH', 'NON_BINARY'}
        if status not in {'open', 'pending', 'resting'} and not settled:
            continue
        unit = number(source.get('unit_size')) or number((source.get('sports_units') or {}).get('unit_size'))
        unit = unit if unit is not None and unit > 0 else None
        stake, fee = number(source.get('stake')), number(source.get('fee'))
        profit = number(source.get('profit')) if settled else None
        payout = number(source.get('payout')) if settled else None
        exact_fee = verified_fee(source)
        if exact_fee is not None and fee != number(exact_fee):
            errors.append({'id': identity, 'reason': 'verified_fee_mismatch'})
        def ratio(value):
            return float(value / unit) if value is not None and unit else None
        if unit is None:
            errors.append({'id': identity, 'reason': 'entry_unit_missing'})
        if stake is None or fee is None or (settled and (profit is None or payout is None)):
            errors.append({'id': identity, 'reason': 'financial_fields_missing'})
        if settled and all(v is not None for v in (stake, fee, profit, payout)) and cents(payout - stake - fee) != float(profit):
            errors.append({'id': identity, 'reason': 'net_profit_mismatch'})
        value = number(source.get('settlement_side_value'))
        if value is None and source.get('kalshi_result') == 'scalar':
            yes_value = number(source.get('settlement_value'))
            if yes_value is not None:
                value = 100 * (yes_value if (source.get('kalshi_order_side') or source.get('order_side')) == 'yes' else 1 - yes_value)
        outcome = 'NON_BINARY' if settled and value is not None and 0 < value < 100 else result
        count = number(source.get('contracts'))
        if settled and value is not None and count is not None and payout is not None:
            if cents(count * value / 100) != float(payout):
                errors.append({'id': identity, 'reason': 'settlement_payout_mismatch'})
            if value in {0, 100} and outcome != ('WIN' if value == 100 else 'LOSS'):
                errors.append({'id': identity, 'reason': 'settlement_result_mismatch'})
        row = {k: source.get(k) for k in ('id', 'pick_id', 'placed_at', 'settled_at', 'settlement_ts', 'kalshi_ticker', 'kalshi_order_side', 'contracts', 'stake', 'fee', 'payout', 'profit')}
        row.update(bot_id=source.get('aibetpicks_bot_id') or 'unknown', name=source.get('aibetpicks_bot_name') or 'Unknown bot',
                   pick=(source.get('aibetpicks') or {}).get('pick', {}).get('pick') or source.get('selection') or source.get('selected_team'),
                   status='settled' if settled else 'open', result=outcome, unit_size=float(unit) if unit else None,
                   stake_units=ratio(stake), risk_units=ratio(Decimal(str(cents(stake + fee)))) if stake is not None and fee is not None else None,
                   fee_units=ratio(fee), profit_units=ratio(profit), payout_units=ratio(payout),
                   published_units=(source.get('sports_units') or {}).get('published_units') or (source.get('aibetpicks') or {}).get('pick', {}).get('stake_units'),
                   fee_verified=verified_fee(source) is not None)
        rows.append(row)

    def summary(group):
        closed = [r for r in group if r['status'] == 'settled']
        opened = [r for r in group if r['status'] == 'open']
        def total(items, key):
            values = [number(r.get(key)) for r in items]
            return float(sum(values, Decimal(0))) if all(v is not None for v in values) else None
        wins, losses = (sum(r['result'] == value for r in closed) for value in ('WIN', 'LOSS'))
        stake, profit = total(closed, 'stake'), total(closed, 'profit')
        return {'settled': len(closed), 'wins': wins, 'losses': losses,
                'voids': sum(r['result'] in {'VOID', 'PUSH'} for r in closed),
                'other': sum(r['result'] == 'NON_BINARY' for r in closed), 'open_count': len(opened),
                'win_rate': 100 * wins / (wins + losses) if wins + losses else None,
                'profit': cents(Decimal(str(profit))) if profit is not None else None,
                'profit_units': total(closed, 'profit_units'), 'stake': stake, 'stake_units': total(closed, 'stake_units'),
                'fees': total(closed, 'fee'), 'payout': total(closed, 'payout'),
                'risk_units': total(closed, 'risk_units'), 'open_risk_units': total(opened, 'risk_units'),
                'roi_pct': 100 * profit / stake if profit is not None and stake else None,
                'missing_unit_records': sum(r['unit_size'] is None for r in group),
                'pending_fee_records': sum(not r['fee_verified'] for r in group)}
    bots, daily = defaultdict(list), defaultdict(list)
    for row in rows:
        bots[row['bot_id']].append(row)
        at = stamp(row.get('settled_at') or row.get('settlement_ts'))
        if row['status'] == 'settled' and at:
            daily[at.date().isoformat()].append(row)
    return {**summary(rows), 'by_bot': [{'bot_id': key, 'name': group[0]['name'], **summary(group)} for key, group in sorted(bots.items())],
            'daily': [{'date': day, **summary(group)} for day, group in sorted(daily.items(), reverse=True)],
            'rows': sorted(rows, key=lambda r: r.get('placed_at') or '', reverse=True),
            'accounting': {'ok': not errors, 'errors': errors, 'checked_records': len(rows), 'as_of': now.isoformat()},
            'unit_basis': 'sum of each settled net profit / that bet’s recorded entry unit', 'roi_basis': 'after-fee profit / stake before fees'}
