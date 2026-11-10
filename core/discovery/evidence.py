import json

GUIDANCE = """The screen removes failed data/gates and measures features; it does not
recommend trades. Build the thesis from facts. Explain why the market may have
ignored positive news and what would disprove the thesis. Negative divergence on
a held name is a warning, not an order. Do not sell because profit exists or hold
because the original thesis sounded attractive. Provide catalyst/horizon,
counter-thesis, risks, entry/hold/add/exit conditions and explicit invalidation.
"""


def factual_block(row):
    return "DIVERGENCE EVIDENCE — orientation, not recommendation\n" + json.dumps(
        row, sort_keys=True
    )
