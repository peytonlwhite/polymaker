"""Companion worker for shadow analytics; never invokes the trading scan loop."""
from datetime import timedelta
import json
import os
from pathlib import Path
import time

from crypto_shadow_expansion import Expansion, utcnow, parsed, number
from crypto_shadow_challenger import atomic_json, digest

ROOT = Path(__file__).resolve().parent
ASSETS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE']


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def single_instance(path):
    """OS releases this exclusive lock on exit, including a crash."""
    handle = Path(path).open('a+b')
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if Path(path).stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
    except OSError:
        handle.close()
        return None
    return handle


def spots_from_stream(stream, now):
    spots, snapshots = {}, {}
    for asset in ASSETS:
        snapshot = stream.snapshot(asset) if stream else {}
        snapshots[asset] = snapshot
        age = number(snapshot.get('book_age_seconds'), 999)
        if snapshot.get('connected') and 0 <= age <= 2 and number(snapshot.get('mid')) > 0:
            spots[asset] = {'mid':snapshot['mid'], 'observed_at':(now-timedelta(seconds=age)).isoformat()}
    return spots, snapshots


def run():
    os.chdir(ROOT)
    lock = single_instance(ROOT / 'crypto_shadow_expansion.lock')
    if lock is None:
        return 0
    # Import existing read-only quote adapters without executing main()/scan().
    import crypto_paper_bettor as bot
    bot.LOG_FILE = ROOT / 'crypto_shadow_expansion_adapter.log'
    settings = bot.load_settings()
    settings.update({'CRYPTO_LIVE_ORDER_ENABLED':'false',
        'CRYPTO_15M_LEARNING_ACTIVE_ENABLED':'false',
        'CRYPTO_LIVE_UNDERLYING_REFRESH_ENABLED':'true',
        'CRYPTO_LIVE_UNDERLYING_REQUIRE_FRESH':'true',
        'CRYPTO_LIVE_UNDERLYING_MAX_AGE_SECONDS':'2',
        'CRYPTO_KALSHI_WS_MAX_AGE_SECONDS':'1',
        'CRYPTO_EXECUTION_LAB_MAX_QUOTE_AGE_SECONDS':'1'})
    cb = bot.get_coinbase_microstructure_stream(settings, ASSETS)
    bot.get_kraken_microstructure_stream(settings, ASSETS)
    engine = Expansion(ROOT / 'crypto_shadow_expansion.json')
    files = ['crypto_shadow_worker.py','crypto_shadow_expansion.py','crypto_shadow_challenger.py',
             'crypto_15m_learning.py','crypto_scan_intelligence.py','crypto_execution_safety.py',
             'crypto_pricing.py','crypto_paper_bettor.py','crypto_microstructure.py']
    implementation = digest({name:digest((ROOT/name).read_text(encoding='utf-8')) for name in files})
    old = engine.state.get('implementation_hash')
    if old and old != implementation:
        raise ValueError('Research implementation changed; preserve the cohort and register a new ledger')
    engine.state['implementation_hash'] = implementation
    engine.persist()
    last_settle = 0
    stream_started = time.monotonic()
    ticker_started = {}
    while True:
        started = time.monotonic()
        try:
            now = utcnow()
            artifact = ROOT / 'crypto_shadow_challenger_model.json'
            if artifact.exists():
                engine.install_model(read_json(artifact), now)
            report = read_json(ROOT / 'crypto_live_report.json')
            generated = parsed(report.get('generated_at'))
            fresh_report = bool(generated and 0 <= (now-generated).total_seconds() <= 90)
            candidates = [r for r in report.get('top_candidates',[]) if r.get('market_lane')=='crypto_15m'] if fresh_report else []
            bot.get_kalshi_crypto_stream(settings,[r['ticker'] for r in candidates])
            for row in candidates:
                ticker_started.setdefault(row['ticker'],time.monotonic())
            # The frozen model uses this explicitly recorded scanner universe.
            runtime = bot.build_execution_shadow_runtime(settings, independent=True)
            confirm = runtime['confirm_fill']
            def refresh(candidate):
                fresh = runtime['fresh_snapshot'](candidate)
                stream = bot.KALSHI_CRYPTO_STREAM
                observed = stream.snapshot(candidate['ticker'],max_age_seconds=1) if stream else {}
                if observed.get('sequence_valid') is not True:
                    fresh['data_quality'] = {'score':0,'reason':'companion_stream_not_synchronized'}
                return fresh
            spots, snapshots = spots_from_stream(cb, now)
            engine.regime((report.get('market_regime') or {}).get('latest') or {}, spots, now)
            engine.advance_eth(candidates, refresh, confirm)
            if not fresh_report:
                engine.reject('worker',['scanner_report_stale'],now)
            for candidate in candidates:
                try:
                    initial = refresh(candidate)
                    if not initial.get('ticker'):
                        engine.reject('worker',[initial.get('error') or 'snapshot_unavailable'],utcnow(),ticker=candidate.get('ticker'))
                        continue
                    # Fresh synchronous BTC context is collected for later lead/lag work;
                    # it does not change the frozen challenger feature definition.
                    captured = utcnow()
                    fresh_spots, fresh_snapshots = spots_from_stream(cb,captured)
                    if candidate.get('asset') in fresh_spots:
                        btc = fresh_snapshots.get('BTC',{}) if 'BTC' in fresh_spots else {}
                        engine.observe_coverage(initial,btc,captured)
                    history_seconds = min(time.monotonic()-stream_started,
                        time.monotonic()-ticker_started[candidate['ticker']])
                    if history_seconds >= 300:
                        engine.challenger(initial,refresh,confirm)
                    else:
                        engine.reject('challenger',['source_history_warmup'],captured,ticker=candidate['ticker'])
                    if history_seconds >= 60:
                        engine.eth_entry(initial,refresh,confirm)
                except Exception as exc:
                    engine.reject('worker',[type(exc).__name__],utcnow(),ticker=candidate.get('ticker'))
            if time.monotonic()-last_settle >= 30:
                engine.settle(bot.fetch_low_edge_shadow_market)
                last_settle = time.monotonic()
            engine.state['scan_count'] += 1
            engine.reviews()
            engine.persist()
            summary = engine.summary()
            summary['worker'] = {'pid':os.getpid(), 'status':'running',
                'source_report_at':report.get('generated_at'), 'report_fresh':fresh_report,
                'iteration_seconds':round(time.monotonic()-started,3),
                'candidate_universe':'scanner top_candidates; all returned crypto_15m candidates',
                'coinbase_connected':bool(snapshots.get('BTC',{}).get('connected'))}
            atomic_json(ROOT / 'crypto_shadow_expansion_report.json',summary)
        except Exception as exc:
            # Never replace unreadable input/state with a new empty experiment.
            error = {'at':utcnow().isoformat(),'error_type':type(exc).__name__}
            print(json.dumps(error),flush=True)
            atomic_json(ROOT / 'crypto_shadow_expansion_health.json',error)
        time.sleep(max(1,5-(time.monotonic()-started)))


if __name__ == '__main__':
    raise SystemExit(run())
