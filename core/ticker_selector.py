"""Compatibility exports from core.weekly.ticker_selector."""

from core.weekly import ticker_selector as _implementation

__all__ = list(
    getattr(
        _implementation,
        "__all__",
        [name for name in vars(_implementation) if not name.startswith("_")],
    )
)
globals().update({name: getattr(_implementation, name) for name in __all__})
