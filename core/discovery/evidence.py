"""L6 hand-off from one validated shortlist generation to decision prompts."""

GUIDANCE = """The screen removes failed data/gates and measures features; it does not
recommend trades. Build the thesis from facts. Explain why the market may have
ignored positive news and what would disprove the thesis. Negative divergence on
a held name is a warning, not an order. Do not sell because profit exists or hold
because the original thesis sounded attractive. Provide catalyst/horizon,
counter-thesis, risks, entry/hold/add/exit conditions and explicit invalidation.
"""


def candidate_tickers(snapshot, held, limit):
    """Apply current holdings and the configured quota to the published order."""
    if snapshot is None:
        return []
    return [
        row["ticker"] for row in snapshot["new_candidates"] if row["ticker"] not in held
    ][:limit]


def ranked_evidence(snapshot, ticker):
    """Link ticker evidence to the generation used to select it."""
    if snapshot is None:
        return {}
    row = snapshot.get("all_ranked", {}).get(ticker)
    if row is None:
        return {}
    evidence = dict(row)
    evidence["discovery_quote_timestamp"] = evidence.pop("quote_timestamp", None)
    return dict(
        evidence,
        discovery_run_id=snapshot["run_id"],
        discovery_data_cutoff=snapshot.get("data_cutoff"),
        discovery_universe_version=snapshot.get("universe_version"),
    )
