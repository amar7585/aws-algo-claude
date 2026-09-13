"""
market-classifier layer - one classification, both frames.

    from market_classifier import classify, decorate, session_vwap

daily-market-sentiment runs it on daily candles, intraday-market-sentiment on
5-minute candles. Same rules, same taxonomy, same score scale, so a daily row
and an intraday row can be read against each other.

Pure Python, stdlib only. It shares a zip with no dependency of its own.
"""

from .classify import (
    BEARISH,
    BULLISH,
    HIGH,
    LOW,
    NORMAL,
    RANGE_BOUND,
    SIDEWAYS,
    TRENDING,
    VOLATILE_EXPANSION,
    classify,
)
from .indicators import (
    SMA_PERIODS,
    decorate,
    rolling_mean,
    session_vwap,
    wilder_atr,
    wilder_rsi,
)
from .structure import TRANSITIONAL, swing_points
from .structure import read as read_structure

__all__ = [
    "BEARISH",
    "BULLISH",
    "HIGH",
    "LOW",
    "NORMAL",
    "RANGE_BOUND",
    "SIDEWAYS",
    "SMA_PERIODS",
    "TRANSITIONAL",
    "TRENDING",
    "VOLATILE_EXPANSION",
    "classify",
    "decorate",
    "read_structure",
    "rolling_mean",
    "session_vwap",
    "swing_points",
    "wilder_atr",
    "wilder_rsi",
]
