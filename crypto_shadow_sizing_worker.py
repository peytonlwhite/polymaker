"""Independent read-only quote worker for the prospective sizing lab."""
import json
import os
from pathlib import Path
import time

from crypto_shadow_sizing import SizingLab
from crypto_shadow_expansion import utcnow, parsed
from crypto_shadow_challenger import atomic_json, digest
from crypto_shadow_worker import single_instance

ROOT = Path(__file__).resolve().parent


def implementation_hash():
    files = ('crypto_shadow_sizing.py','crypto_shadow_sizing_worker.py',
             'crypto_shadow_expansion.py','crypto_shadow_challenger.py','crypto_shadow_worker.py',
             'crypto_prospective_shadow.py','crypto_execution_safety.py','crypto_pricing.py',
             'crypto_paper_bettor.py','crypto_microstructure.py','crypto_market_stream.py','crypto_evidence.py')
    return digest({name:digest((ROOT/name).read_text(encoding='utf-8')) for name in files})


def run():
    os.chdir(ROOT)
    lock = single_instance(ROOT/'crypto_shadow_sizing.lock')
    if lock is None: return 0
    import crypto_paper_bettor as bot
    bot.LOG_FILE = ROOT/'crypto_shadow_sizing_adapter.log'
    settings = bot.load_settings()
    settings.update({'CRYPTO_LIVE_ORDER_ENABLED':'false', 'CRYPTO_15M_LEARNING_ACTIVE_ENABLED':'false',
        'CRYPTO_LIVE_UNDERLYING_REFRESH_ENABLED':'true','CRYPTO_LIVE_UNDERLYING_REQUIRE_FRESH':'true',
        'CRYPTO_LIVE_UNDERLYING_MAX_AGE_SECONDS':'2','CRYPTO_KALSHI_WS_MAX_AGE_SECONDS':'1',
        'CRYPTO_EXECUTION_LAB_MAX_QUOTE_AGE_SECONDS':'1'})
    bot.get_coinbase_microstructure_stream(settings,['ETH'])
    bot.get_kraken_microstructure_stream(settings,['ETH'])
    engine = SizingLab(ROOT/'crypto_shadow_sizing.json')
    fingerprint = implementation_hash()
    if engine.state.get('implementation_hash', fingerprint) != fingerprint:
        raise ValueError('Sizing implementation changed: retain ledger and register a separate cohort')
    engine.state['implementation_hash'] = fingerprint
    engine.persist()
    started = time.monotonic()
    first_seen = {}
    last_settle = 0
    while True:
        tick = time.monotonic()
        try:
            now = utcnow()
            report = json.loads((ROOT/'crypto_live_report.json').read_text(encoding='utf-8-sig'))
            generated = parsed(report.get('generated_at'))
            fresh_report = bool(generated and 0 <= (now-generated).total_seconds() <= 90)
            candidates = [r for r in report.get('top_candidates',[]) if r.get('asset')=='ETH'
                          and r.get('market_lane')=='crypto_15m'] if fresh_report else []
            bot.get_kalshi_crypto_stream(settings,[r['ticker'] for r in candidates])
            runtime = bot.build_execution_shadow_runtime(settings,independent=True)
            def refresh(candidate):
                fresh = runtime['fresh_snapshot'](candidate)
                observed = bot.KALSHI_CRYPTO_STREAM.snapshot(candidate['ticker'],max_age_seconds=1) if bot.KALSHI_CRYPTO_STREAM else {}
                if observed.get('sequence_valid') is not True:
                    fresh['data_quality'] = {'score':0}
                return fresh
            def quote_all(ticker, side):
                # Every size consumes the same independent REST book, with its own
                # executable depth and rounded fee. Arms never share hypothetical fills.
                payload, fetched_at = bot.fetch_fresh_kalshi_orderbook(settings,ticker,force_rest=True)
                return {q:{**bot.kalshi_orderbook_depth_summary(payload,side,required_contracts=q),
                           'fetched_at':fetched_at,'sequence_valid':payload.get('sequence_valid',True),
                           'source':'kalshi_rest_orderbook','book_revision':payload.get('book_revision')}
                        for q in range(1,6)}
            # Settle before deciding sizes: only already observed outcomes may
            # influence drawdown and recovery. No retrospective sizing replay.
            if time.monotonic()-last_settle >= 30:
                engine.settle(bot.fetch_low_edge_shadow_market)
                last_settle = time.monotonic()
            if not fresh_report: engine.reject('scanner_report_stale')
            for candidate in candidates:
                first_seen.setdefault(candidate['ticker'],time.monotonic())
                if min(time.monotonic()-started,time.monotonic()-first_seen[candidate['ticker']]) < 300:
                    engine.reject('source_history_warmup')
                    continue
                engine.observe(refresh(candidate),refresh,quote_all)
            engine.state['scan_count'] += 1
            engine.reviews()
            engine.persist()
            summary = engine.summary()
            summary['worker'] = {'pid':os.getpid(),'status':'running','report_fresh':fresh_report,
                'source_report_at':report.get('generated_at'), 'iteration_seconds':round(time.monotonic()-tick,3)}
            atomic_json(ROOT/'crypto_shadow_sizing_report.json',summary)
            atomic_json(ROOT/'crypto_shadow_sizing_health.json',{'generated_at':utcnow().isoformat(),'status':'running','pid':os.getpid()})
        except Exception as exc:
            error = {'generated_at':utcnow().isoformat(),'status':'error','error_type':type(exc).__name__,'pid':os.getpid()}
            print(json.dumps(error),flush=True)
            atomic_json(ROOT/'crypto_shadow_sizing_health.json',error)
        time.sleep(max(1,5-(time.monotonic()-tick)))


if __name__ == '__main__':
    raise SystemExit(run())
