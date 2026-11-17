"""Compatibility exports from core.data.news."""

from core.data import news as _implementation

__all__ = list(
    getattr(
        _implementation,
        "__all__",
        [name for name in vars(_implementation) if not name.startswith("_")],
    )
)
globals().update({name: getattr(_implementation, name) for name in __all__})
