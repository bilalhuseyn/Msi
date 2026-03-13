from enum import Enum, IntEnum


class SignalDirection(IntEnum):
    BEARISH = -1
    NEUTRAL = 0
    BULLISH = 1


class SpreadStatus(str, Enum):
    MM_ACTIVE = "MM_ACTIVE"
    MM_CAUTIOUS = "MM_CAUTIOUS"
    MM_THINNING = "MM_THINNING"
    MM_WITHDRAWN = "MM_WITHDRAWN"


class VetoReason(str, Enum):
    NONE = "NONE"
    VPIN_CRITICAL = "VPIN_CRITICAL"
    SPREAD_CRISIS = "SPREAD_CRISIS"
    CLEARANCE_ACTIVE = "CLEARANCE_ACTIVE"


class RegimeType(str, Enum):
    LONG_GAMMA = "LONG_GAMMA"
    SHORT_GAMMA = "SHORT_GAMMA"


class ClearanceStatus(str, Enum):
    NORMAL = "NORMAL"
    CLEARANCE_POSSIBLE = "CLEARANCE_POSSIBLE"
    CLEARANCE_ACTIVE = "CLEARANCE_ACTIVE"


class DepthErosionStatus(str, Enum):
    NEUTRAL = "NEUTRAL"
    HIDDEN_BUY = "HIDDEN_BUY"
    HIDDEN_SELL = "HIDDEN_SELL"
    BASELINE_SET = "BASELINE_SET"
    SKIP = "SKIP"


class DecisionAction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"
    VETO = "VETO"


class FeedStatus(str, Enum):
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"
