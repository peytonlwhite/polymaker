"""Lossless Crypto evidence retention; pure summaries stay in the hot ledger."""

import gzip
import hashlib
import json
import os
from pathlib import Path


def jsonl_sources(path):
    path = Path(path)
    archives = path.parent / "archives"
    found = [p for p in archives.rglob(path.name + ".*") if p.is_file() and not p.name.endswith(".tmp")] if archives.exists() else []
    return sorted(found, key=lambda p: (p.stat().st_mtime_ns, str(p))) + ([path] if path.exists() else [])


def iter_jsonl(paths):
    for path in paths:
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8-sig") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row


def retain_records(ledger, maximum_full_records, field="records"):
    """Never discard sample IDs, outcomes, costs or timestamps at a row limit.

    Large input payloads are archived before the caller persists the ledger.
    The original outcome objects and all scalar fields remain available for
    cumulative qualification, chronological halves and deduplication.
    """
    rows = ledger.get(field) or []
    full = [r for r in rows if not r.get("evidence_compacted")]
    excess = max(0, len(full) - maximum_full_records)
    for row in full:
        if not excess:
            break
        if row.get("status") in {"open", "working", "filled"}:
            continue
        ledger.setdefault("_pending_evidence_archives", []).append({"field": field, "record": dict(row)})
        for key in ("fresh_input_snapshot", "kalshi_input_snapshot", "microstructure",
                    "data_quality", "signal_evidence", "model_snapshot", "flow_review"):
            row.pop(key, None)
        row["evidence_compacted"] = True
        excess -= 1
    ledger["retention_method"] = "lossless_raw_gzip_plus_cumulative_evaluation_rows_v1"


def flush_evidence_archives(path, ledger):
    pending = ledger.get("_pending_evidence_archives") or []
    if not pending:
        return
    path = Path(path)
    content = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in pending).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    directory = path.parent / "archives" / "crypto_evidence"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (path.stem + "." + digest + ".jsonl.gz")
    if not destination.exists():
        temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.tmp")
        with gzip.open(temporary, "wb") as handle:
            handle.write(content)
        os.replace(temporary, destination)
    ledger.setdefault("evidence_archive_files", [])
    relative = str(destination.relative_to(path.parent))
    if relative not in ledger["evidence_archive_files"]:
        ledger["evidence_archive_files"].append(relative)
    ledger.pop("_pending_evidence_archives", None)


def register_policy(ledger, configuration, now):
    previous = (ledger.get("configuration") or {}).get("policy_hash")
    active = configuration.get("policy_hash")
    registered = ledger.get("registered_at") or now
    ledger.setdefault("policy_registered_at", registered)
    if previous and active and previous != active:
        ledger.setdefault("policy_history", []).append({"changed_at": now, "from_policy_hash": previous, "to_policy_hash": active})
        ledger["policy_registered_at"] = now
    ledger["configuration"] = configuration

