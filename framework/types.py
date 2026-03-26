"""Core types used across the framework."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
from datetime import datetime


class SignalDirection(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class MarketRegime(Enum):
    BULL = "bull"
    BEAR = "bear"
    SIDEWAYS = "sideways"
    CRISIS = "crisis"


@dataclass
class Signal:
    ticker: str
    direction: SignalDirection
    strength: float = 1.0  # 0.0 to 1.0
    reason: str = ""


@dataclass
class Order:
    ticker: str
    side: str  # "buy" or "sell"
    quantity: int
    order_type: str = "market"  # "market" or "limit"
    limit_price: Optional[float] = None
    exchange: str = ""


@dataclass
class OrderResult:
    success: bool
    ticker: str
    side: str
    quantity: int
    filled_price: float = 0.0
    message: str = ""


@dataclass
class Position:
    ticker: str
    quantity: int
    avg_price: float
    market_value: float = 0.0
    profit: float = 0.0
    profit_pct: float = 0.0


@dataclass
class Portfolio:
    """Cross-strategy portfolio state passed to get_target_allocation."""
    positions: List[Position] = field(default_factory=list)
    cash_balance: float = 0.0
    total_value: float = 0.0
    strategy_allocations: Dict[str, float] = field(default_factory=dict)
    budget_ceiling: float = 0.0  # Fixed budget cap from config

    @property
    def invested_value(self) -> float:
        return sum(p.market_value for p in self.positions)

    def get_position(self, ticker: str) -> Optional[Position]:
        for p in self.positions:
            if p.ticker == ticker:
                return p
        return None
