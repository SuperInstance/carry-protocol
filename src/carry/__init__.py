"""
Carry Protocol — Moving data across divides where infrastructure doesn't reach.

Named after the carriers who walk messages across mountains where no signal crosses.
"""

from .carrier import Carrier
from .parcel import Parcel, Envelope, Payload, Fence, HopEntry
from .route import Route, Waypoint
from .store import Store
from .fence import FenceConfig, FenceResult, FenceValidator

__version__ = "1.0.0"
__all__ = [
    "Carrier",
    "Parcel",
    "Envelope",
    "Payload",
    "Fence",
    "HopEntry",
    "Route",
    "Waypoint",
    "Store",
    "FenceConfig",
    "FenceResult",
    "FenceValidator",
]
