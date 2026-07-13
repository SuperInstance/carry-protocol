"""
Conservation Fence — validation at each hop.

The fence ensures parcel integrity, enforces power budgets, and maintains
an append-only hop log. A parcel that fails the fence is held or rejected,
never silently dropped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .parcel import Parcel, HopEntry, HopAction


class FenceStatus(str, Enum):
    PASS = "pass"
    EXPIRED = "expired"
    MAX_HOPS_EXCEEDED = "max_hops_exceeded"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    SIZE_MISMATCH = "size_mismatch"
    POWER_INSUFFICIENT = "power_insufficient"
    ROUTE_LOOP = "route_loop"
    PARSE_ERROR = "parse_error"
    VERSION_UNSUPPORTED = "version_unsupported"


@dataclass
class FenceResult:
    """Result of a fence validation."""
    status: FenceStatus
    message: str = ""
    action: str = ""  # What the carrier should do: forward, hold, discard, reject
    timestamp: int = field(default_factory=lambda: int(time.time()))

    @property
    def passed(self) -> bool:
        return self.status == FenceStatus.PASS

    @property
    def should_discard(self) -> bool:
        return self.status in (FenceStatus.EXPIRED,)

    @property
    def should_hold(self) -> bool:
        return self.status == FenceStatus.POWER_INSUFFICIENT

    @property
    def should_reject(self) -> bool:
        return self.status in (
            FenceStatus.MAX_HOPS_EXCEEDED,
            FenceStatus.CHECKSUM_MISMATCH,
            FenceStatus.SIZE_MISMATCH,
            FenceStatus.ROUTE_LOOP,
            FenceStatus.PARSE_ERROR,
            FenceStatus.VERSION_UNSUPPORTED,
        )


@dataclass
class FenceConfig:
    """Configuration for fence validation."""
    checksum_algo: str = "blake2b"
    max_hops: int = 16
    default_ttl_hours: int = 168  # 1 week
    power_budget_mw: int = 500
    compression: str = "gzip"
    supported_versions: tuple[int, ...] = (1,)
    # Power thresholds (% of budget remaining before deferring non-urgent)
    power_critical_threshold: float = 0.10
    power_low_threshold: float = 0.20
    power_moderate_threshold: float = 0.50


class FenceValidator:
    """
    Validates parcels against the conservation fence.

    Each check runs in order. The first failure determines the result.
    On success, a hop entry is created for the log.
    """

    def __init__(self, config: FenceConfig | None = None):
        self.config = config or FenceConfig()

    def validate(
        self,
        parcel: Parcel,
        node_id: str,
        next_hop_cost_mw: int = 0,
    ) -> FenceResult:
        """
        Run all fence checks on an incoming parcel.

        Args:
            parcel: The parcel to validate.
            node_id: The node performing the validation.
            next_hop_cost_mw: Estimated power cost for the next hop.

        Returns:
            FenceResult with status and recommended action.
        """
        ts = int(time.time())

        # 1. Version check
        if parcel.envelope.version not in self.config.supported_versions:
            return FenceResult(
                status=FenceStatus.VERSION_UNSUPPORTED,
                message=f"Version {parcel.envelope.version} not in supported versions",
                action="reject",
            )

        # 2. Expiry check
        if parcel.is_expired:
            return FenceResult(
                status=FenceStatus.EXPIRED,
                message=f"Parcel expired at {parcel.envelope.expires_at}",
                action="discard",
            )

        # 3. Hop limit check
        if parcel.is_hops_exceeded:
            return FenceResult(
                status=FenceStatus.MAX_HOPS_EXCEEDED,
                message=f"Hop count {parcel.envelope.hop_count} >= max {parcel.envelope.max_hops}",
                action="reject",
            )

        # 4. Checksum validation
        if not parcel.validate_checksum():
            return FenceResult(
                status=FenceStatus.CHECKSUM_MISMATCH,
                message="Stored checksum does not match recomputed checksum",
                action="reject",
            )

        # 5. Size validation
        body_bytes = parcel.payload.body.encode("utf-8") if isinstance(parcel.payload.body, str) else parcel.payload.body
        if len(body_bytes) != parcel.fence.size_bytes:
            return FenceResult(
                status=FenceStatus.SIZE_MISMATCH,
                message=f"Size {len(body_bytes)} != declared {parcel.fence.size_bytes}",
                action="reject",
            )

        # 6. Loop detection
        if parcel.has_loop(node_id):
            return FenceResult(
                status=FenceStatus.ROUTE_LOOP,
                message=f"Node {node_id} already in hop log",
                action="reject",
            )

        # 7. Power budget
        if next_hop_cost_mw > 0:
            remaining = parcel.remaining_power_budget_mw
            if remaining < next_hop_cost_mw:
                return FenceResult(
                    status=FenceStatus.POWER_INSUFFICIENT,
                    message=f"Remaining budget {remaining}mW < next hop cost {next_hop_cost_mw}mW",
                    action="hold",
                )

        # All checks passed
        return FenceResult(
            status=FenceStatus.PASS,
            message="All fence checks passed",
            action="forward",
        )

    def create_hop_entry(
        self,
        node_id: str,
        action: HopAction,
        power_used_mw: int = 0,
        checksum_valid: bool = True,
    ) -> HopEntry:
        """Create a hop entry for the fence log."""
        return HopEntry(
            node=node_id,
            timestamp=int(time.time()),
            action=action.value if isinstance(action, HopAction) else action,
            power_used_mw=power_used_mw,
            checksum_valid=checksum_valid,
        )
