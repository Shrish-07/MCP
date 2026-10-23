"""Financial news sentiment analysis using ModernBERT-based models.

ML dependencies (torch / transformers) are OPTIONAL at import time: the
trading agent and every other module must keep working on an environment
without the multi-GB ML stack. The imports are wrapped so only actually
LOADING the sentiment model fails (with a clear, actionable error).
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache
from typing import List, Optional

try:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    _HAS_ML_DEPS = True
except ImportError:
    torch = None  # type: ignore[assignment]
    AutoModelForSequenceClassification = None  # type: ignore[assignment]
    AutoTokenizer = None  # type: ignore[assignment]
    _HAS_ML_DEPS = False

logger = logging.getLogger("robinhood-agent.core.data.sentiment")

PRIMARY_MODEL = "AnkitAI/FinSense-ModernBERT-Financial-News-Sentiment-Analysis"
FALLBACK_MODEL = "AnkitAI/distilbert-base-uncased-financial-news-sentiment-analysis"


class SentimentModel:
    """Singleton sentiment analyzer with lazy loading and GPU/CPU auto-detection."""

    _instance: Optional["SentimentModel"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "SentimentModel":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self.model = None
        self.tokenizer = None
        self.device = None
        self.model_name = None
        self.id2label = None
        self.label2id = None
        self._initialized = True
        self._load_model()

    def _load_model(self) -> None:
        """Load the sentiment model with fallback support."""
        if not _HAS_ML_DEPS:
            raise RuntimeError(
                "ML dependencies not installed (torch/transformers). "
                "Run: pip install torch transformers accelerate safetensors. "
                "The trading agent runs fine without them — only the weekly "
                "sentiment ticker selection needs this stack."
            )
        # Determine device
        if torch.cuda.is_available():
            self.device = torch.device("cuda")
            logger.info("sentiment_model using CUDA")
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            self.device = torch.device("mps")
            logger.info("sentiment_model using MPS")
        else:
            self.device = torch.device("cpu")
            logger.info("sentiment_model using CPU")

        # Try primary model first, then fallback
        for model_name in [PRIMARY_MODEL, FALLBACK_MODEL]:
            try:
                logger.info("sentiment_model loading %s", model_name)
                self.tokenizer = AutoTokenizer.from_pretrained(model_name)
                self.model = AutoModelForSequenceClassification.from_pretrained(
                    model_name,
                    torch_dtype=torch.float16 if self.device.type == "cuda" else torch.float32,
                )
                self.model.to(self.device)
                self.model.eval()

                # Get label mapping from model config
                self.id2label = self.model.config.id2label
                self.label2id = self.model.config.label2id
                self.model_name = model_name

                logger.info("sentiment_model loaded successfully: %s (labels: %s)", model_name, self.id2label)
                return

            except Exception as exc:
                logger.warning("sentiment_model failed to load %s: %s", model_name, exc)
                continue

        raise RuntimeError("Failed to load any sentiment model (tried primary and fallback)")
    def _resolve_label_indices(self) -> tuple[int, int, int]:
        """Resolve (negative_idx, neutral_idx, positive_idx) from the loaded
        model's actual label mapping — keyword scan, never blind defaults.

        Hard-fails (raises) if any class cannot be resolved, because silently
        defaulting to 0/1/2 would invert the sign of every sentiment score
        for a model with a different label ordering.
        """
        resolved = {}
        for label, idx in (self.label2id or {}).items():
            low = str(label).lower()
            if "pos" in low:
                resolved["positive"] = idx
            elif "neg" in low:
                resolved["negative"] = idx
            elif "neu" in low:
                resolved["neutral"] = idx
        missing = {"negative", "neutral", "positive"} - set(resolved)
        if missing:
            raise RuntimeError(
                f"sentiment label mapping ambiguous for {self.model_name}: "
                f"label2id={self.label2id}, could not resolve {sorted(missing)}. "
                f"Refusing to guess (a wrong guess silently inverts all scores)."
            )
        logger.info("sentiment_labels_resolved %s -> %s", self.model_name, resolved)
        return resolved["negative"], resolved["neutral"], resolved["positive"]

    def _get_sentiment_score(self, logits: torch.Tensor) -> float:
        """Convert model logits to continuous sentiment score in [-1, +1]."""
        probs = torch.softmax(logits, dim=-1).squeeze().cpu().numpy()

        # Map labels to sentiment values — resolved from the model's own
        # config (keyword-scanned at load time; hard-fails if ambiguous).
        # Score = P(positive) - P(negative)  -> range [-1, +1]
        # Neutral is ignored in the score but contributes to confidence

        if not hasattr(self, "_label_idx"):
            self._label_idx = self._resolve_label_indices()
        negative_idx, _neutral_idx, positive_idx = self._label_idx

        p_neg = float(probs[negative_idx]) if negative_idx < len(probs) else 0.0
        p_pos = float(probs[positive_idx]) if positive_idx < len(probs) else 0.0

        # Continuous score: positive - negative (neutral doesn't directly affect score)
        score = p_pos - p_neg

        # Clamp to [-1, 1]
        return max(-1.0, min(1.0, score))

    def score_headlines(self, headlines: List[str], batch_size: int = 16) -> List[float]:
        """Score a list of headlines, returning sentiment scores in [-1, +1].

        Args:
            headlines: List of news headlines/text to score
            batch_size: Batch size for inference (adjust for memory)

        Returns:
            List of sentiment scores, one per headline
        """
        if not headlines:
            return []

        if self.model is None or self.tokenizer is None:
            logger.error("sentiment_model not loaded, returning neutral scores")
            return [0.0] * len(headlines)

        scores = []

        # Process in batches
        for i in range(0, len(headlines), batch_size):
            batch = headlines[i : i + batch_size]

            try:
                # Tokenize
                inputs = self.tokenizer(
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=512,
                    return_tensors="pt",
                )
                inputs = {k: v.to(self.device) for k, v in inputs.items()}

                # Inference
                with torch.no_grad():
                    outputs = self.model(**inputs)
                    logits = outputs.logits

                # Score each item in batch
                for j in range(logits.shape[0]):
                    score = self._get_sentiment_score(logits[j : j + 1])
                    scores.append(score)

            except Exception as exc:
                logger.warning("sentiment_model batch scoring failed: %s", exc)
                # Fill with neutral for failed batch
                scores.extend([0.0] * len(batch))

        return scores

    def score_single(self, text: str) -> float:
        """Score a single headline/text."""
        scores = self.score_headlines([text])
        return scores[0] if scores else 0.0

    def get_model_info(self) -> dict:
        """Return info about the loaded model."""
        return {
            "model_name": self.model_name,
            "device": str(self.device),
            "labels": self.id2label,
        }

@lru_cache(maxsize=1)
def get_sentiment_model() -> SentimentModel:
    """Get the singleton sentiment model instance (cached)."""
    return SentimentModel()


def score_news_items(headlines: List[str]) -> List[float]:
    """Convenience function to score headlines using the singleton model."""
    model = get_sentiment_model()
    return model.score_headlines(headlines)


def score_single_headline(headline: str) -> float:
    """Convenience function to score a single headline."""
    model = get_sentiment_model()
    return model.score_single(headline)