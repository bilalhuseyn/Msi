from __future__ import annotations

import abc
from dataclasses import dataclass, field
from config.constants import SignalDirection


@dataclass
class SignalOutput:
    """Standardized output from every signal module."""
    module: str
    direction: SignalDirection
    raw_value: float = 0.0
    confidence: float = 0.0
    metadata: dict = field(default_factory=dict)

    @property
    def score(self) -> int:
        return int(self.direction)


class BaseSignalModule(abc.ABC):
    """
    Abstract base for all signal modules.
    Every module produces -1 (BEARISH), 0 (NEUTRAL), or +1 (BULLISH).
    No module can make a trade decision alone.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        ...

    @abc.abstractmethod
    def update(self, data: dict) -> SignalOutput:
        """Process new data and return signal output."""

    @abc.abstractmethod
    def reset(self) -> None:
        """Clear internal state."""
