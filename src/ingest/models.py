"""
Shared data models for the ingest layer.
These are pure data containers — no pricing logic here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Literal, Optional


OptionType = Literal["C", "P"]


@dataclass(frozen=True)
class OptionContract:
    """
    A single option contract as returned by the market data source.
    All fields are raw market data — no derived quantities.
    """
    ticker: str
    expiry: date
    strike: float
    option_type: OptionType
    bid: Optional[float]
    ask: Optional[float]
    volume: int
    open_interest: int
    # Underlying snapshot at time of fetch
    spot: float
    # Optional: continuous dividend yield (annualised decimal)
    dividend_yield: float = 0.0
    # Timestamp this quote was captured (UTC)
    quote_time: Optional[datetime] = None

    @property
    def contract_id(self) -> str:
        """Human-readable contract identifier."""
        return f"{self.ticker} {self.expiry} {self.option_type} {self.strike:.2f}"

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def bid_ask_spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def bid_ask_spread_pct(self) -> Optional[float]:
        """Bid-ask spread as a fraction of mid price."""
        m = self.mid
        if m is None or m == 0:
            return None
        spread = self.bid_ask_spread
        if spread is None:
            return None
        return spread / m


@dataclass(frozen=True)
class SpotData:
    """Current spot price and metadata for an underlying."""
    ticker: str
    price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    dividend_yield: float = 0.0
    timestamp: Optional[datetime] = None


@dataclass
class HistoricalPrices:
    """Daily closing price series for an underlying, used for realized vol."""
    ticker: str
    # Daily closes in chronological order (oldest first)
    closes: List[float] = field(default_factory=list)
    # Corresponding dates, parallel to closes
    dates: List[date] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.closes)
