"""Implemented L0–L6 discovery funnel, wired through `discovery.enabled`.

The pipeline validates the universe, applies eligibility gates, computes
technical and news features with local sentiment inference, scores
divergence, and publishes a shortlist. The application passes evidence
from that shortlist to the decision engine. Paired evaluation runs offline.
"""
