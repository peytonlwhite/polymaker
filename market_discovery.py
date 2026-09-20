import os
from collections import Counter

from kalshi_common import fetch_kalshi_markets, log_line, market_liquidity, market_prices, market_volume, write_json


LOG_FILE = "market_discovery_log.txt"
OUTPUT_JSON = "market_discovery.json"
OUTPUT_TXT = "market_discovery.txt"

KEYWORD_BUCKETS = {
    "sports": [
        "nba", "nfl", "mlb", "nhl", "ncaa", "college football", "college basketball",
        "basketball", "baseball", "football", "hockey", "soccer", "tennis", "ufc",
        "game", "match", "team", "championship", "playoffs", "world cup",
    ],
    "econ": ["fed", "cpi", "inflation", "jobs", "unemployment", "gdp", "recession", "rates", "treasury"],
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto"],
    "politics": ["election", "president", "senate", "congress", "governor", "nominee"],
    "companies": ["earnings", "stock", "market cap", "tesla", "nvidia", "apple", "amazon", "meta"],
}


def classify_market(market):
    text = " ".join(
        str(market.get(field, ""))
        for field in ("ticker", "event_ticker", "title", "subtitle", "category", "rules_primary")
    ).lower()
    buckets = []
    for bucket, keywords in KEYWORD_BUCKETS.items():
        if any(keyword in text for keyword in keywords):
            buckets.append(bucket)
    return buckets or ["other"]


def get_spread_cents(market):
    prices = market_prices(market)
    if prices["yes_bid"] and prices["yes_ask"]:
        return round(prices["yes_ask"] - prices["yes_bid"], 2)
    return None


def summarize_market(market):
    prices = market_prices(market)
    buckets = classify_market(market)
    return {
        "ticker": market.get("ticker"),
        "event_ticker": market.get("event_ticker"),
        "series_ticker": market.get("series_ticker"),
        "title": market.get("title"),
        "subtitle": market.get("subtitle"),
        "category": market.get("category"),
        "buckets": buckets,
        "open_time": market.get("open_time"),
        "close_time": market.get("close_time"),
        "expiration_time": market.get("expiration_time"),
        "volume": market_volume(market),
        "liquidity_dollars": market_liquidity(market),
        "yes_bid": prices["yes_bid"],
        "yes_ask": prices["yes_ask"],
        "no_bid": prices["no_bid"],
        "no_ask": prices["no_ask"],
        "spread_cents": get_spread_cents(market),
    }


def discover():
    status = os.getenv("DISCOVERY_STATUS", "open")
    max_pages = int(os.getenv("DISCOVERY_MAX_PAGES", "20"))
    min_volume = float(os.getenv("DISCOVERY_MIN_VOLUME", "0"))
    min_liquidity = float(os.getenv("DISCOVERY_MIN_LIQUIDITY", "0"))

    log_line(LOG_FILE, f"Discovery started status={status} max_pages={max_pages}")
    markets = fetch_kalshi_markets(status=status, max_pages=max_pages)
    summaries = [summarize_market(market) for market in markets]

    if min_volume:
        summaries = [market for market in summaries if market["volume"] >= min_volume]
    if min_liquidity:
        summaries = [market for market in summaries if market["liquidity_dollars"] >= min_liquidity]

    bucket_counts = Counter(bucket for market in summaries for bucket in market["buckets"])
    series_counts = Counter(market.get("series_ticker") or "unknown" for market in summaries)
    category_counts = Counter(market.get("category") or "unknown" for market in summaries)

    by_volume = sorted(summaries, key=lambda row: row["volume"], reverse=True)[:50]
    by_liquidity = sorted(summaries, key=lambda row: row["liquidity_dollars"], reverse=True)[:50]
    tight_spreads = sorted(
        [row for row in summaries if row["spread_cents"] is not None],
        key=lambda row: (row["spread_cents"], -row["volume"]),
    )[:50]
    sports = [row for row in summaries if "sports" in row["buckets"]]

    output = {
        "status": status,
        "total_markets": len(summaries),
        "bucket_counts": dict(bucket_counts.most_common()),
        "top_series": dict(series_counts.most_common(40)),
        "top_categories": dict(category_counts.most_common(40)),
        "top_by_volume": by_volume,
        "top_by_liquidity": by_liquidity,
        "tightest_spreads": tight_spreads,
        "sports_candidates": sorted(sports, key=lambda row: row["volume"], reverse=True)[:100],
    }
    write_json(OUTPUT_JSON, output)
    write_text_report(output)
    log_line(LOG_FILE, f"Discovery complete markets={len(summaries)} sports={len(sports)}")
    return output


def write_text_report(output):
    lines = []
    lines.append(f"Kalshi Market Discovery ({output['status']})")
    lines.append(f"Total markets: {output['total_markets']}")
    lines.append("")
    lines.append("Bucket counts:")
    for bucket, count in output["bucket_counts"].items():
        lines.append(f"  {bucket}: {count}")

    def section(title, rows):
        lines.append("")
        lines.append(title)
        for row in rows[:20]:
            lines.append(
                f"  {row['ticker']} | vol={row['volume']} liq=${row['liquidity_dollars']:.2f} "
                f"spread={row['spread_cents']}c | {row['title']}"
            )

    section("Top volume", output["top_by_volume"])
    section("Top liquidity", output["top_by_liquidity"])
    section("Tightest spreads", output["tightest_spreads"])
    section("Sports candidates", output["sports_candidates"])

    with open(OUTPUT_TXT, "w", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    discover()
