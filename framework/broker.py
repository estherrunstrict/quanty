"""BaseBroker ABC — abstracts order execution only (no data methods)."""
from abc import ABC, abstractmethod
from typing import List

from framework.types import Order, OrderResult, Position


class BaseBroker(ABC):
    """Abstract base for all brokers. Handles execution only."""

    @abstractmethod
    def get_positions(self) -> List[Position]:
        """Return current holdings."""

    @abstractmethod
    def get_cash_balance(self) -> float:
        """Return available cash for trading."""

    @abstractmethod
    def place_order(self, order: Order) -> OrderResult:
        """Execute an order. Returns result with fill info."""
