"""Compatibility exports for the shared, stdlib-only watchdog wire contract."""

from elspeth.contracts.process_watchdog_wire import (
    FRAME_COUNT_LIMIT as FRAME_COUNT_LIMIT,
)
from elspeth.contracts.process_watchdog_wire import (
    FRAME_LIMIT as FRAME_LIMIT,
)
from elspeth.contracts.process_watchdog_wire import (
    GRACE_NS as GRACE_NS,
)
from elspeth.contracts.process_watchdog_wire import (
    FrameKind as FrameKind,
)
from elspeth.contracts.process_watchdog_wire import (
    FrameReader as FrameReader,
)
from elspeth.contracts.process_watchdog_wire import (
    RecoveryReason as RecoveryReason,
)
from elspeth.contracts.process_watchdog_wire import (
    WatchdogFrame as WatchdogFrame,
)
from elspeth.contracts.process_watchdog_wire import (
    WatchdogProtocolError as WatchdogProtocolError,
)
from elspeth.contracts.process_watchdog_wire import (
    decode_frame as decode_frame,
)
from elspeth.contracts.process_watchdog_wire import (
    encode_frame as encode_frame,
)
