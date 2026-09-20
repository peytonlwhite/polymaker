"""Freeze a research residual model for a separate forward-only shadow cohort."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from crypto_15m_learning import (
    load_dataset, unique_market_split, _train_research_residual,
    _predict_research_residual, market_weighted_metrics,
    RESEARCH_FEATURE_SCHEMA_VERSION,
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(data, sort_keys=True, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def fit_challenger(dataset, source_state, destination, cutoff=None):
    """Reuse the previously selected family, never select on new forward outcomes.

    Coefficients use the historical training partition only. Its validation/test
    scores are development diagnostics, never entries in the forward ledger.
    The saved artifact is immutable: retraining requires a new destination/cohort.
    """
    if Path(destination).exists():
        raise FileExistsError('Existing challenger is immutable')
    cutoff = cutoff or datetime.now(timezone.utc)
    source = json.loads(Path(source_state).read_text(encoding='utf-8-sig'))
    research = source.get('research_shadow_ablation') or {}
    features = research.get('selected_features') or []
    if not features or not research.get('qualified_for_future_promotion_review'):
        raise ValueError('No qualified research family to freeze')
    rows = [r for r in load_dataset(dataset)
            if r.get('research_feature_schema_version') == RESEARCH_FEATURE_SCHEMA_VERSION
            and datetime.fromisoformat(r['close_time'].replace('Z', '+00:00')) < cutoff
            and datetime.fromisoformat(r['snapshot_at'].replace('Z', '+00:00')) < cutoff]
    train, validation, test = unique_market_split(rows)
    if min(len({r['ticker'] for r in x}) for x in (train, validation, test)) < 20:
        raise ValueError('Insufficient independent chronological partitions')
    model = _train_research_residual(train, features)
    diagnostics = {}
    for label, partition in [('validation', validation), ('test', test)]:
        labels = [r['label_yes'] for r in partition]
        keys = [r['ticker'] for r in partition]
        diagnostics[label] = {
            'challenger': market_weighted_metrics(
                [_predict_research_residual(model, r) for r in partition], labels, keys),
            'market': market_weighted_metrics([r['market_probability'] for r in partition], labels, keys),
        }
    artifact = {
        'version': 'frozen-research-challenger-v1', 'model': model,
        'model_hash': digest(model), 'source_state_hash': digest(source),
        'selected_family': research['selected_family'],
        'training_cutoff': cutoff.isoformat(),
        'training_last_close': max(r['close_time'] for r in train),
        'frozen_at': datetime.now(timezone.utc).isoformat(),
        'development_diagnostics': diagnostics, 'automatic_promotion': False,
        'affects_live_probability': False, 'historical_backfill': False,
    }
    atomic_json(destination, artifact)
    return artifact


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', default='crypto_15m_training_dataset.jsonl')
    parser.add_argument('--source-state', default='crypto_15m_model_state.json')
    parser.add_argument('--output', default='crypto_shadow_challenger_model.json')
    args = parser.parse_args()
    result = fit_challenger(args.dataset, args.source_state, args.output)
    print(json.dumps({k: result[k] for k in ['model_hash', 'frozen_at', 'development_diagnostics']}))
