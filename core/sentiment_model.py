"""Compatibility exports from core.data.sentiment."""

from core.data import sentiment as _implementation

__all__ = list(
    getattr(
        _implementation,
        "__all__",
        [name for name in vars(_implementation) if not name.startswith("_")],
    )
)
globals().update({name: getattr(_implementation, name) for name in __all__})
