"""Standalone public-GET-only worker for the 30-day sports research cohort."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import gzip
import hashlib
import html
import json
import os
from pathlib import Path
import re
import time

from runtime_guard import acquire_single_instance, AlreadyRunningError
from sports_itf_shadow import (CENTRAL, SERIES, atomic_json, market_series, now_central, number,
                               outcome_routes, pair_markets, parsed, read_json, route_quote, stamp)
from sports_itf_shadow_worker import PublicKalshi, checked_phase, milestone_map
from sports_research_shadow import (HEALTH, REPORT, RULES, STATE, Research, candidate_signal,
                                    fee_rates, fresh, market_ok)

ROOT = Path(__file__).resolve().parent
PARTIAL = ('KXMLBF5TOTAL', 'KXNFL1HTOTAL')
INTERVAL = 30
TICKER = r'KX[A-Z0-9]+-[A-Z0-9.-]{1,160}'
IMPLEMENTATION_FILES = ('sports_research_shadow.py', 'sports_research_worker.py',
                        'sports_itf_shadow.py', 'sports_itf_shadow_worker.py', 'runtime_guard.py')


class Reader(PublicKalshi):
    """The HTTP boundary cannot authenticate, POST, cancel, or access a portfolio."""
    def __init__(self):
        super().__init__()
        self.session.headers['User-Agent'] = 'Polymaker-public-shadow-research/1'
        self.scan_calls = 0
        self.fee_cache = {}

    def get(self, path, params=None):
        p = params or {}
        allowed = False
        if path == '/markets':
            allowed = p.get('series_ticker') in set(SERIES) | set(PARTIAL) and p.get('status') == 'open'
        elif path == '/markets/orderbooks':
            ts = p.get('tickers')
            allowed = isinstance(ts, list) and 0 < len(ts) <= 100 and all(re.fullmatch(TICKER, x) for x in ts)
        elif path == '/markets/trades':
            allowed = bool(re.fullmatch(TICKER, str(p.get('ticker', '')))) and p.get('is_block_trade') == 'false'
        elif re.fullmatch('/markets/' + TICKER, path):
            allowed = not p
        elif re.fullmatch(r'/series/KX[A-Z0-9]+', path):
            allowed = not p
        elif path == '/incentive_programs':
            allowed = p.get('status') == 'active' and p.get('type') == 'liquidity'
        elif path == '/milestones':
            allowed = p.get('competition') == 'ITF'
        elif path == '/live_data/batch':
            ids = p.get('milestone_ids')
            allowed = isinstance(ids, list) and 0 < len(ids) <= 100 and all(re.fullmatch(r'[a-fA-F0-9-]{36}', x) for x in ids)
        if not allowed:
            raise ValueError('Public research endpoint rejected')
        if self.scan_calls >= 120:
            raise RuntimeError('Public research per-scan request budget exceeded')
        time.sleep(max(0, .15 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.calls += 1
        self.scan_calls += 1
        r = self.session.get('https://external-api.kalshi.com/trade-api/v2' + path,
                             params=p, timeout=(5, 12), allow_redirects=False)
        if r.status_code != 200:
            raise RuntimeError('Public Kalshi HTTP ' + str(r.status_code) + ' at ' + path.split('/')[1])
        return r.json()

    def market(self, ticker):
        m = self.get('/markets/' + ticker).get('market') or {}
        if m.get('ticker') != ticker:
            raise ValueError('Mismatched public market')
        return m

    def rates(self, ticker):
        series, now = market_series(ticker), now_central()
        cached = self.fee_cache.get(series)
        if not cached or not fresh(cached['at'], now, 900):
            s = self.get('/series/' + series).get('series') or {}
            if s.get('ticker') != series:
                raise ValueError('Mismatched public series')
            self.fee_cache[series] = {'at': stamp(now), 'rates': fee_rates(s)}
        return self.fee_cache[series]['rates']

    def trades_since(self, ticker, since):
        result, cursor, seen = [], None, set()
        for _ in range(5):
            p = {'ticker': ticker, 'min_ts': int(parsed(since).timestamp())-1,
                 'limit': 1000, 'is_block_trade': 'false'}
            if cursor:
                p['cursor'] = cursor
            d = self.get('/markets/trades', p)
            if not isinstance(d.get('trades'), list):
                return [], False
            result.extend(d['trades'])
            cursor = d.get('cursor')
            if not cursor:
                return result, True
            if cursor in seen:
                break
            seen.add(cursor)
        return [], False

    def incentives(self):
        d = self.get('/incentive_programs', {'status': 'active', 'type': 'liquidity', 'limit': 10000})
        if d.get('next_cursor') or not isinstance(d.get('incentive_programs'), list):
            raise ValueError('Incomplete incentive universe')
        now = now_central()
        rows = []
        for p in d['incentive_programs']:
            start, end = parsed(p.get('start_date')), parsed(p.get('end_date'))
            target, discount, pool = (number(p.get(k)) for k in ('target_size_fp', 'discount_factor_bps', 'period_reward'))
            # Begin with daily gas-price programs: short settlement horizons, independent of sports.
            if (str(p.get('market_ticker', '')).startswith('KXAAAGASD') and start and end
                    and start <= now < end <= now + timedelta(days=7)
                    and target and target > 0 and discount and 0 < discount <= 10000 and pool and pool > 0):
                rows.append(p)
        rows.sort(key=lambda p: (-p['period_reward'] / (parsed(p['end_date'])-parsed(p['start_date'])).total_seconds(), p['market_ticker']))
        return rows


def manual_event(row):
    return row.get('event_ticker') or str(row.get('kalshi_ticker', '')).rsplit('-', 1)[0]


def manual_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=CENTRAL)
    except (TypeError, ValueError):
        return None


def manual_signals(engine, portfolio, now):
    rows = [r for r in [*portfolio.get('bets', []), *portfolio.get('history', [])]
            if r.get('source') in {'manual', 'user_manual'} or r.get('strategy_owner') == 'user_manual' or r.get('user_manual')]
    if engine.state['manual_baseline'] is None:
        engine.state['manual_baseline'] = sorted({manual_event(r) for r in rows})
        return []
    out = []
    for r in rows:
        event, ticker = manual_event(r), str(r.get('kalshi_ticker', ''))
        side = r.get('order_side') or r.get('kalshi_order_side')
        at = manual_time(r.get('placed_at'))
        if (event in engine.state['manual_baseline'] or not re.fullmatch(TICKER, ticker)
                or market_series(ticker) not in {*SERIES, 'KXWTAMATCH'} or side not in {'yes','no'}
                or not at or at < parsed(engine.state['started_at']) or not fresh(stamp(at), now, 180)
                or r.get('status') not in {'open', 'pending'}):
            continue
        engine.state['manual_journal'].setdefault(event, {
            'observed_at': stamp(now), 'source_placed_at': stamp(at), 'ticker': ticker, 'side': side,
            'source_entry_price': number(r.get('entry_price')),
            'reason': r.get('reason_summary') or r.get('entry_reason') or None,
            'reason_available': bool(r.get('reason_summary') or r.get('entry_reason'))})
        out.append({'ticker': ticker, 'event': event, 'side': side,
                    'evidence': engine.state['manual_journal'][event]})
    return out


def itf_signals(engine, pairs, books, phases, now):
    out = []
    for event, pair in pairs.items():
        phase = phases.get(event, {}).get('phase')
        if (phase not in {'pregame', 'live'} or not all(m['ticker'] in books for m in pair)
                or not fresh(phases.get(event, {}).get('at'), now, 30)):
            continue
        if sum(number(m.get('volume_fp'), 0) for m in pair) < 100:
            continue
        for outcome in outcome_routes(pair, books):
            if not outcome['routes']:
                continue
            route = outcome['routes'][0]
            if route['spread'] < 0 or route['spread'] > .030001:
                continue
            selected = outcome['selected']['ticker']
            key = event + '|' + selected
            history = engine.state['itf_history'].get(key, [])
            old = [r for r in history if 300 <= (now-parsed(r['at'])).total_seconds() <= 900]
            recent = history[-1] if history and fresh(history[-1]['at'], now, 90) else None
            if phase == 'pregame' and .55 <= route['ask'] <= .80:
                engine.state['itf_anchors'].setdefault(key, {'at': stamp(now), 'ask': route['ask']})
                out.append(('itf_favorite', event, route, {'phase': phase, 'selected': selected}))
            if phase == 'live' and old and recent:
                anchor = engine.state['itf_anchors'].get(key)
                if (anchor and .40 <= route['ask'] <= .60 and anchor['ask']-route['ask'] >= .10
                        and min(route['bid'], recent['bid']) >= min(r['bid'] for r in old) + .02 - 1e-8):
                    out.append(('itf_favorite_rebound', event, route, {'phase': phase, 'selected': selected,
                                'pregame_anchor': anchor, 'prior_low_bid': min(r['bid'] for r in old)}))
                if ('DOUBLES' not in market_series(event) and .25 <= route['ask'] <= .45
                        and min(route['bid'], recent['bid']) >= old[0]['bid'] + .03 - 1e-8):
                    out.append(('itf_dog_momentum', event, route, {'phase': phase, 'selected': selected,
                                'prior': old[0], 'second_confirmation': recent}))
            engine.state['itf_history'][key] = [r for r in history if fresh(r['at'], now, 900)] + [
                {'at': stamp(now), 'bid': route['bid'], 'ask': route['ask']}]
    engine.state['itf_history'] = {k:v for k,v in engine.state['itf_history'].items()
                                    if v and fresh(v[-1]['at'], now, 900)}
    return out


def partial_signals(markets, books):
    groups, out = defaultdict(list), []
    for m in markets:
        strike = number(m.get('floor_strike'))
        if (market_series(m.get('ticker')) not in PARTIAL or not market_ok(m) or m.get('strike_type') != 'greater'
                or strike is None or abs(strike % 1 - .5) > 1e-8 or m['ticker'] not in books):
            continue
        q = route_quote(books[m['ticker']], 'yes')
        if 0 < q['bid'] <= q['ask'] < 1 and q['spread'] <= .040001:
            # All non-strike settlement language must match, including rescheduling terms.
            rule = re.sub(r'(?<=than )\d+(?:\.\d+)?', '<strike>', str(m.get('rules_primary', '')))
            groups[(m['event_ticker'], rule, m.get('rules_secondary', ''))].append((strike, m, q))
    for rows in groups.values():
        rows.sort(key=lambda x: x[0])
        for lo, current, hi in zip(rows, rows[1:], rows[2:]):
            width = hi[0] - lo[0]
            if not 0 < width <= (4 if market_series(current[1]['ticker']) == 'KXMLBF5TOTAL' else 14):
                continue
            p_lo, p_hi = (lo[2]['bid']+lo[2]['ask'])/2, (hi[2]['bid']+hi[2]['ask'])/2
            if p_lo < p_hi:
                continue
            fair = p_lo + (p_hi-p_lo)*(current[0]-lo[0])/width
            for side, probability in [('yes', fair), ('no', 1-fair)]:
                q = route_quote(books[current[1]['ticker']], side)
                if .20 <= q['ask'] <= .80 and probability - .03 - q['ask'] >= .04:
                    out.append({'ticker': current[1]['ticker'], 'event': current[1]['event_ticker'],
                                'side': side, 'max_price': probability-.07,
                                'evidence': {'source': 'neighboring_Kalshi_total_midpoints', 'fair_proxy': probability,
                                             'neighbors': [lo[1]['ticker'], hi[1]['ticker']],
                                             'neighbor_probabilities': [p_lo, p_hi], 'uncertainty_haircut': .03}})
    return sorted(out, key=lambda r: r['ticker'])


def write_view(root, report):
    rows = ''.join('<tr>'+''.join('<td>'+html.escape(str(x))+'</td>' for x in (
        s['name'], s['entries'], s['settled'], s['unfilled'], s['open']+s['working'],
        s['profit_units'], s['roi_pct'], s['max_drawdown'], s['unique_events'], s['evidence']))+'</tr>'
        for s in report['strategies'])
    doc = '''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30">
<title>Sports shadow research</title><style>body{background:#101820;color:#e6edf3;font:16px system-ui;margin:32px}
h1{color:#91e8d0}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:12px;border-bottom:1px solid #34424f}
p{max-width:1050px;line-height:1.5}.muted{color:#a8bac9}</style><h1>Sports shadow research</h1>'''
    doc += '<p>Shadow only · $10 hypothetical unit · no live execution · no automatic promotion</p>'
    doc += '<p class="muted">Updated '+html.escape(report['generated_at'])+' · '+html.escape(report['status'])+' · Entry window ends '+html.escape(report['ends_at'])+'</p>'
    doc += '<table><tr>'+''.join('<th>'+x+'</th>' for x in ('Experiment','Entries','Settled','No fill','Open / quoting','Net U','ROI %','Drawdown $','Games','Evidence'))+'</tr>'+rows+'</table>'
    doc += '<p>Reward estimates are separate from trading profit. Estimated reward so far: $'+str(round(sum(s['estimated_reward'] for s in report['strategies']),4))+'. No reward cash has been received by this simulation.</p>'
    doc += '<p>'+html.escape(' '.join(report['limitations']))+'</p>'
    doc += '<p>Coverage: '+html.escape(json.dumps(report['coverage'], sort_keys=True))+'</p>'
    path = root / 'archives' / 'sports_research' / 'index.html'
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(doc, encoding='utf-8')
    os.replace(tmp, path)


def scan(api, engine, root, cache):
    api.scan_calls = 0
    now, started = now_central(), time.monotonic()
    errors, coverage = [], {}
    try:
        report = read_json(root / 'sports_paper_report.json')
        if not isinstance(report, dict):
            raise ValueError('Bad source report')
    except (OSError, ValueError):
        report = {}
        errors.append('core_source_unavailable')
    signals = [s for c in report.get('top_candidates', [])
               if (s := candidate_signal(c, report.get('generated_at'), now))]
    coverage['core_source_candidates'] = len(report.get('top_candidates', []))
    coverage['core_fresh_exact_signals'] = len(signals)
    coverage['core_source_fresh'] = fresh(report.get('generated_at'), now, 120)
    try:
        manual = manual_signals(engine, read_json(root / 'sports_paper_portfolio.json'), now)
    except (OSError, ValueError, TypeError):
        manual = []
        errors.append('manual_source_unavailable')
    collecting = now < parsed(engine.state['ends_at'])
    if collecting and (not cache or not fresh(cache.get('at'), now, 300)):
        itf = api.markets()
        partial = [m for s in PARTIAL for m in api.pages('/markets', 'markets', {'series_ticker':s,'status':'open','limit':1000})]
        pairs = pair_markets(itf)
        milestones = milestone_map(api.milestones(now-timedelta(days=15)), pairs) if pairs else {}
        incentives = api.incentives()
        cache.update(at=stamp(now_central()), itf=itf, partial=partial, milestones=milestones, incentives=incentives)
    markets = {m['ticker']:m for m in [*cache.get('itf', []), *cache.get('partial', [])]}
    pairs = pair_markets(cache.get('itf', [])) if collecting else {}
    milestones = cache.get('milestones', {})
    ids = sorted({m['id'] for m in milestones.values()}) if collecting else []
    live = api.live(ids) if ids else {}
    phase_map = {event:{'phase':checked_phase(m, live.get(m['id']), now_central()), 'at':stamp(now_central())}
                 for event,m in milestones.items()}
    selected_programs, reward_checks, selected_events = [], 0, set()
    programs = cache.get('incentives', [])
    cursor = cache.get('reward_cursor', 0) % max(1, len(programs))
    for p in programs[cursor:] + programs[:cursor]:
        if len(selected_programs) >= 4 or not collecting:
            break
        cache['reward_cursor'] = (cache.get('reward_cursor', 0)+1) % max(1,len(programs))
        ticker = p['market_ticker']
        event = ticker.rsplit('-', 1)[0]
        if event in selected_events or not engine.can_enter('liquidity_rewards', event, now):
            continue
        if reward_checks >= 12:
            break
        reward_checks += 1
        m = api.market(ticker)
        expiry = parsed(m.get('expected_expiration_time'))
        if not market_ok(m) or not expiry or not now < expiry <= now+timedelta(days=7):
            continue
        bid, ask = number(m.get('yes_bid_dollars')), number(m.get('yes_ask_dollars'))
        if bid is None or ask is None or not .10 <= bid <= .90 or not 0 <= ask-bid <= .040001:
            continue
        markets[ticker] = m
        selected_programs.append(p)
        selected_events.add(event)
    for s in [*signals, *manual]:
        if s['ticker'] not in markets:
            markets[s['ticker']] = api.market(s['ticker'])
    held = [r for r in engine.state['positions'].values() if r['status'] in {'working','open'}]
    # Closed markets vanish from discovery; explicit settlement reads rotate across all holdings.
    due = sorted(held, key=lambda r:r.get('settlement_checked_at',''))[:20]
    for r in due:
        try:
            markets[r['ticker']] = api.market(r['ticker'])
            r['settlement_checked_at'] = stamp(now_central())
        except Exception as exc:
            errors.append('settlement:'+type(exc).__name__)
    for r in held:
        if r['status'] != 'working':
            continue
        try:
            trades, complete = api.trades_since(r['ticker'], r['last_trade_check'])
            engine.process_trades(r, trades, now_central(), complete=complete)
        except Exception as exc:
            errors.append('trades:'+type(exc).__name__)
            engine.process_trades(r, [], now_central(), complete=False)
    engine.settle(markets, now_central())
    tickers = {m['ticker'] for pair in pairs.values() for m in pair
               if phase_map.get(m['event_ticker'],{}).get('phase') in {'live','pregame'}}
    tickers.update(m['ticker'] for m in cache.get('partial', []) if market_ok(m))
    tickers.update(s['ticker'] for s in [*signals,*manual])
    tickers.update(p['market_ticker'] for p in selected_programs)
    tickers.update(r['ticker'] for r in held if r['status']=='working')
    if not collecting:
        tickers = {r['ticker'] for r in held if r['status']=='working'}
    if len(tickers) > 1000:
        raise ValueError('Research universe exceeded 1000-book bound; do not silently truncate')
    first, times1 = api.books(sorted(tickers)) if tickers else ({},{})
    if tickers:
        time.sleep(1.1)
    second, times2 = api.books(sorted(tickers)) if tickers else ({},{})
    now = now_central()
    good = {t for t in tickers if fresh(times1.get(t),now,30) and fresh(times2.get(t),now,15)
            and (parsed(times2[t])-parsed(times1[t])).total_seconds() >= 1}
    first, second = {t:first[t] for t in good}, {t:second[t] for t in good}
    if ids:
        live = api.live(ids)
        phase_map = {event:{'phase':checked_phase(m, live.get(m['id']), now_central()), 'at':stamp(now_central())}
                     for event,m in milestones.items()}
        now = now_central()
    for r in held:
        if r['status']=='working' and r['ticker'] in second:
            engine.accrue_reward(r, second[r['ticker']], now)
    def enter(strategy, s, *, maker=False, expiry=None, reward=None):
        t, side = s['ticker'], s['side']
        if t not in good or not engine.can_enter(strategy, s['event'], now):
            return
        m = markets[t]
        rates = api.rates(t)
        # Rates may need a fetch; never let that extend quote freshness silently.
        if not fresh(times2[t], now_central(), 15):
            engine.reject('fee_fetch_quote_expired')
            return
        return engine.enter(strategy,s['event'],m,side,first[t],second[t],rates,now_central(),
                            evidence=s.get('evidence'),max_price=s.get('max_price',.99),
                            maker=maker,expiry=expiry,reward=reward)
    if collecting:
        # Recheck underlying timestamps after network discovery, not just at scan start.
        signals = [s for c in report.get('top_candidates', [])
                   if (s:=candidate_signal(c,report.get('generated_at'),now))]
        for s in signals:
            t = s['ticker']
            if t not in good:
                continue
            q = route_quote(second[t], s['side'])
            if q['spread'] > .030001 or not .25 <= q['ask'] <= .70:
                continue
            rates = api.rates(t)
            s['max_price'] = s['probability'] - .02 - rates['taker']*.25 - .001
            if q['ask'] <= s['max_price']:
                enter('core_'+s['kind'], s)
            if s['phase'] == 'pregame' and q['bid'] <= s['probability']-.02-rates['maker']*.25-.001:
                expiry = min(now+timedelta(seconds=180),parsed(s['start'])-timedelta(seconds=120))
                maker_signal = {**s, 'max_price':s['probability']-.02-rates['maker']*.25-.001}
                row = enter('pregame_maker', maker_signal, maker=True, expiry=expiry)
                if row:
                    enter('pregame_taker_control', {**s,'max_price':.70})
        itf_candidates = itf_signals(engine,pairs,second,phase_map,now)
        for strategy,event,route,evidence in itf_candidates:
            limit = {'itf_favorite':.80,'itf_favorite_rebound':.60,'itf_dog_momentum':.45}[strategy]
            enter(strategy,{'ticker':route['ticker'],'side':route['side'],'event':event,'evidence':evidence,'max_price':limit})
        for s in partial_signals(cache.get('partial',[]),second):
            rates = api.rates(s['ticker'])
            s['max_price'] -= rates['taker']*.25+.001
            enter('partial_total_ladder',s)
        for s in manual:
            if fresh(s['evidence']['source_placed_at'],now,180) and s['ticker'] in good:
                q = route_quote(second[s['ticker']],s['side'])
                if q['spread'] <= .030001 and .20 <= q['ask'] <= .80:
                    enter('manual_tennis',{**s,'max_price':.80})
        for p in selected_programs:
            t = p['market_ticker']
            if t not in good or not parsed(p['start_date']) <= now < parsed(p['end_date']):
                continue
            m = markets[t]
            # Pick one side deterministically, nearer 50c; never two simultaneous hypothetical sides.
            qs = [(side,route_quote(second[t],side)) for side in ('yes','no')]
            qs = [(side,q) for side,q in qs if 0 <= q['spread'] <= .04 and .10 <= q['bid'] <= .85]
            if not qs:
                continue
            side,q = min(qs,key=lambda sq:(abs(sq[1]['bid']-.5),sq[0]))
            s = {'ticker':t,'side':side,'event':m['event_ticker'],
                 'evidence':{'program_id':p['id'],'selection':'daily gas reward pool; no predictive edge assumed'}}
            enter('liquidity_rewards',s,maker=True,expiry=min(now+timedelta(seconds=180),parsed(p['end_date'])),reward=p)
        coverage.update(itf_pairs=len(pairs),itf_phases=dict(Counter(x['phase'] for x in phase_map.values())),
                        itf_signals=len(itf_candidates),partial_markets=len(cache.get('partial',[])),
                        reward_programs_discovered=len(cache.get('incentives',[])),reward_programs_checked=reward_checks,
                        reward_programs_sampled=len(selected_programs),
                        manual_signals=len(manual))
    coverage.update(public_get_calls=api.scan_calls,confirmed_books=len(good),errors=errors,
                    elapsed_seconds=round(time.monotonic()-started,2))
    engine.state.update(last_scan_at=stamp(now_central()),scan_count=engine.state['scan_count']+1,coverage=coverage)
    engine.persist()
    path = root/'archives'/'sports_research'/('observations-'+now.date().isoformat()+'.jsonl.gz')
    path.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(path,'at',encoding='utf-8') as f:
        f.write(json.dumps({'at':stamp(now),'coverage':coverage,'entry_count':len(engine.state['positions'])})+'\n')
    return coverage


def launch(root=None, *, register=False, once=False):
    root = Path(root or ROOT)
    try:
        acquire_single_instance(root/'.sports_research_shadow_bot.pid','Public sports shadow research')
    except AlreadyRunningError:
        return 0
    digest = hashlib.sha256(b''.join((root/n).read_bytes().replace(b'\r\n',b'\n') for n in IMPLEMENTATION_FILES)).hexdigest()
    engine = Research(root,register=register,implementation_hash=digest)
    if register:
        manual_signals(engine,read_json(root/'sports_paper_portfolio.json'),now_central())
        engine.persist()
    api, cache, failures = Reader(), {}, 0
    while True:
        started, error = time.monotonic(), None
        try:
            coverage = scan(api,engine,root,cache)
            failures = 0
            if coverage.get('errors'):
                error = 'Some public reads unavailable; retained positions remain open'
        except Exception as exc:
            failures += 1
            error = type(exc).__name__+': '+str(exc)[:240]
            engine = Research(root,implementation_hash=digest)
        report = engine.summary()
        report['worker'] = {'pid':os.getpid(),'error':error,'public_get_only':True,'poll_seconds':INTERVAL,
                            'implementation_hash':digest,'consecutive_failures':failures}
        atomic_json(root/REPORT,report)
        atomic_json(root/HEALTH,{'at':stamp(now_central()),'last_success':engine.state['last_scan_at'],
                               'status':report['status'],**report['worker']})
        write_view(root,report)
        print(json.dumps({'at':report['generated_at'],'status':report['status'],'scan':report['scan_count'],
                          'positions':len(engine.state['positions']),'error':error}),flush=True)
        if once or (report['status']=='complete' and not error):
            return 1 if error else 0
        time.sleep(max(2,min(300,INTERVAL*max(1,failures))-(time.monotonic()-started)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--register',action='store_true')
    parser.add_argument('--once',action='store_true')
    args = parser.parse_args()
    raise SystemExit(launch(register=args.register,once=args.once))
