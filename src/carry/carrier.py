"""
Carrier — an edge node that carries data across a divide.

The Carrier is the core class of the Carry Protocol. It packs messages
into parcels, stores them locally, forwards them when a route is available,
and receives parcels from other carriers. Every operation is offline-first.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .fence import FenceConfig, FenceResult, FenceStatus, FenceValidator
from .parcel import (
    Compression,
    Envelope,
    Fence,
    HopAction,
    HopEntry,
    Parcel,
    Payload,
    PayloadType,
    Priority,
)
from .route import Route, Waypoint
from .store import Store, STATUS_DELIVERED, STATUS_PENDING

logger = logging.getLogger("carry")


@dataclass
class DeliveryReceipt:
    """
    Receipt from a carry attempt.

    Like a stamp in a carrier's logbook — proof that the parcel moved,
    or proof that it couldn't, and why.
    """
    parcel_id: str
    success: bool
    status: str  # delivered, forwarded, held, failed, expired
    message: str = ""
    next_node: str = ""
    timestamp: int = field(default_factory=lambda: int(time.time()))
    hop_count: int = 0


class Carrier:
    """
    An edge node that carries data across a divide.

    A carrier can:
        - pack() messages into parcels with conservation fence validation
        - carry() parcels along routes with store-and-forward semantics
        - receive() parcels from other carriers, validating against the fence
        - flush() pending parcels when neighbors appear

    All operations are local-first. No network calls are made inside the
    carrier — the caller provides transport via callbacks.
    """

    def __init__(
        self,
        node_id: str,
        store: Store | None = None,
        fence_config: FenceConfig | None = None,
    ):
        self.node_id = node_id
        self.store = store or Store(f"carry_{node_id}.db")
        self.fence_config = fence_config or FenceConfig()
        self.fence_validator = FenceValidator(self.fence_config)

    def pack(
        self,
        message: Any,
        fence_config: FenceConfig | None = None,
        destination: str = "",
        priority: str = Priority.NORMAL.value,
        ttl_hours: int | None = None,
    ) -> Parcel:
        """
        Pack a message with conservation fence validation.

        The message is compressed, encoded, sealed in a parcel envelope,
        and the fence checksum is computed. The parcel is stored locally
        and is ready for carrying.

        Args:
            message: The data to carry (dict, string, or bytes).
            fence_config: Override fence configuration for this parcel.
            destination: The final destination node ID.
            priority: urgent, normal, or deferred.
            ttl_hours: Time-to-live in hours (default from fence config).

        Returns:
            The packed Parcel, ready for carrying.
        """
        fc = fence_config or self.fence_config

        # Determine message type and serialize
        if isinstance(message, (dict, list)):
            payload_type = PayloadType.JSON.value
            raw_body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        elif isinstance(message, bytes):
            payload_type = PayloadType.BINARY.value
            raw_body = message
        else:
            payload_type = PayloadType.TEXT.value
            raw_body = str(message).encode("utf-8")

        # Compress
        compression = fc.compression
        if compression == Compression.GZIP.value and len(raw_body) > 100:
            compressed = gzip.compress(raw_body)
            body = base64.b64encode(compressed).decode("ascii")
            encoding = "base64"
        elif compression == Compression.ZSTD.value and len(raw_body) > 100:
            # Use zlib as fallback (zstd may not be available)
            compressed = zlib.compress(raw_body)
            body = base64.b64encode(compressed).decode("ascii")
            encoding = "base64"
        else:
            body = raw_body.decode("utf-8")
            encoding = "utf-8"
            compression = Compression.NONE.value

        # Build envelope
        now = int(time.time())
        ttl = ttl_hours if ttl_hours is not None else fc.default_ttl_hours

        envelope = Envelope(
            origin=self.node_id,
            destination=destination,
            created_at=now,
            expires_at=now + (ttl * 3600),
            priority=priority,
            compression=compression,
            hop_count=0,
            max_hops=fc.max_hops,
            power_budget_mw=fc.power_budget_mw,
        )

        # Build payload
        payload = Payload(
            type=payload_type,
            encoding=encoding,
            body=body,
        )

        # Build fence
        import hashlib

        body_bytes = body.encode("utf-8")
        if fc.checksum_algo == "blake2b":
            checksum = hashlib.blake2b(body_bytes, digest_size=32).hexdigest()
        elif fc.checksum_algo == "sha256":
            checksum = hashlib.sha256(body_bytes).hexdigest()
        else:
            checksum = format(hash(body_bytes) & 0xFFFFFFFF, "08x")

        fence = Fence(
            checksum_algo=fc.checksum_algo,
            checksum=checksum,
            size_bytes=len(body_bytes),
            hop_log=[],
        )

        parcel = Parcel(envelope=envelope, payload=payload, fence=fence)

        # Add initial hop entry
        parcel.fence.add_hop(self.fence_validator.create_hop_entry(
            node_id=self.node_id,
            action=HopAction.PACKED,
            power_used_mw=5,
            checksum_valid=True,
        ))

        # Store locally
        self.store.put(parcel, status=STATUS_PENDING)

        logger.info("Packed parcel %s -> %s (priority=%s, %d bytes)",
                     parcel.parcel_id, destination, priority, len(body_bytes))

        return parcel

    def carry(
        self,
        parcel: Parcel,
        route: Route,
        transport: Callable[[Parcel, str], bool] | None = None,
    ) -> DeliveryReceipt:
        """
        Move a parcel across an unreliable connection.

        Store-and-forward semantics:
            1. Ensure the parcel is in the local store.
            2. If at the destination, deliver locally.
            3. Find the next waypoint on the route.
            4. If transport is available, attempt the transfer.
            5. On failure, retain and apply backoff.

        Args:
            parcel: The parcel to carry.
            route: The planned route.
            transport: A callable(parcel, next_node) -> bool that performs
                       the actual transfer. Returns True on success.
                       If None, the parcel is only stored locally.

        Returns:
            DeliveryReceipt describing the outcome.
        """
        # Ensure in store
        self.store.put(parcel, status=STATUS_PENDING)

        # Check if we're the destination
        if parcel.envelope.destination == self.node_id:
            return self._deliver_locally(parcel)

        # Check expiry
        if parcel.is_expired:
            logger.warning("Parcel %s expired", parcel.parcel_id)
            return DeliveryReceipt(
                parcel_id=parcel.parcel_id,
                success=False,
                status="expired",
                message="Parcel has expired",
                hop_count=parcel.envelope.hop_count,
            )

        # Find next waypoint
        next_wp = route.next_waypoint(self.node_id)
        if next_wp is None:
            # Check if any waypoint matches (might have deviated)
            if route.is_destination(self.node_id):
                return self._deliver_locally(parcel)
            logger.warning("No next waypoint from %s on route", self.node_id)
            return DeliveryReceipt(
                parcel_id=parcel.parcel_id,
                success=False,
                status="failed",
                message=f"No next waypoint from {self.node_id}",
                hop_count=parcel.envelope.hop_count,
            )

        # Validate fence for the next hop
        fence_result = self.fence_validator.validate(
            parcel=parcel,
            node_id=self.node_id,
            next_hop_cost_mw=next_wp.hop_cost_mw,
        )

        if fence_result.should_discard:
            logger.warning("Parcel %s discarded: %s", parcel.parcel_id, fence_result.message)
            return DeliveryReceipt(
                parcel_id=parcel.parcel_id,
                success=False,
                status="expired",
                message=fence_result.message,
                hop_count=parcel.envelope.hop_count,
            )

        if fence_result.should_reject:
            logger.warning("Parcel %s rejected: %s", parcel.parcel_id, fence_result.message)
            parcel.fence.add_hop(self.fence_validator.create_hop_entry(
                node_id=self.node_id,
                action=HopAction.REJECTED,
                power_used_mw=0,
                checksum_valid=False,
            ))
            self.store.update_parcel(parcel)
            return DeliveryReceipt(
                parcel_id=parcel.parcel_id,
                success=False,
                status="failed",
                message=fence_result.message,
                next_node=next_wp.node,
                hop_count=parcel.envelope.hop_count,
            )

        if fence_result.should_hold:
            logger.info("Parcel %s held: %s", parcel.parcel_id, fence_result.message)
            parcel.fence.add_hop(self.fence_validator.create_hop_entry(
                node_id=self.node_id,
                action=HopAction.HELD,
                power_used_mw=0,
            ))
            self.store.update_parcel(parcel)
            self.store.mark_held(parcel.parcel_id)
            return DeliveryReceipt(
                parcel_id=parcel.parcel_id,
                success=False,
                status="held",
                message=fence_result.message,
                next_node=next_wp.node,
                hop_count=parcel.envelope.hop_count,
            )

        # Fence passed — attempt transfer
        if transport is None:
            logger.info("No transport available, parcel %s stored", parcel.parcel_id)
            return DeliveryReceipt(
                parcel_id=parcel.parcel_id,
                success=False,
                status="held",
                message="No transport provided",
                next_node=next_wp.node,
                hop_count=parcel.envelope.hop_count,
            )

        # Attempt the transfer
        try:
            success = transport(parcel, next_wp.node)
        except Exception as e:
            logger.error("Transport error for parcel %s: %s", parcel.parcel_id, e)
            success = False

        if success:
            # Mark in transit
            self.store.mark_in_transit(parcel.parcel_id, next_wp.node)

            # Update parcel hop info
            parcel.envelope.hop_count += 1
            parcel.fence.add_hop(self.fence_validator.create_hop_entry(
                node_id=self.node_id,
                action=HopAction.FORWARDED,
                power_used_mw=next_wp.hop_cost_mw,
            ))
            self.store.update_parcel(parcel)

            logger.info("Parcel %s forwarded to %s (hop %d)",
                        parcel.parcel_id, next_wp.node, parcel.envelope.hop_count)

            return DeliveryReceipt(
                parcel_id=parcel.parcel_id,
                success=True,
                status="forwarded",
                message=f"Forwarded to {next_wp.node}",
                next_node=next_wp.node,
                hop_count=parcel.envelope.hop_count,
            )
        else:
            # Transfer failed — record attempt with backoff
            self.store.record_attempt(parcel.parcel_id, 30)
            logger.warning("Transfer failed for parcel %s to %s",
                          parcel.parcel_id, next_wp.node)
            return DeliveryReceipt(
                parcel_id=parcel.parcel_id,
                success=False,
                status="failed",
                message=f"Transfer to {next_wp.node} failed",
                next_node=next_wp.node,
                hop_count=parcel.envelope.hop_count,
            )

    def receive(self, parcel: Parcel) -> "ReceivedMessage":
        """
        Receive and validate a parcel against the fence.

        Performs all fence checks: version, expiry, hop limit, checksum,
        size, loop detection, and power budget. On success, the parcel
        is stored locally.

        Args:
            parcel: The incoming parcel.

        Returns:
            ReceivedMessage with the decoded data or an error.
        """
        fence_result = self.fence_validator.validate(
            parcel=parcel,
            node_id=self.node_id,
        )

        if not fence_result.passed:
            logger.warning("Parcel %s failed fence: %s (%s)",
                          parcel.parcel_id, fence_result.status.value, fence_result.message)
            return ReceivedMessage(
                parcel_id=parcel.parcel_id,
                success=False,
                error=fence_result.status.value,
                message=fence_result.message,
            )

        # Add received hop entry
        parcel.fence.add_hop(self.fence_validator.create_hop_entry(
            node_id=self.node_id,
            action=HopAction.RECEIVED,
            power_used_mw=2,
        ))

        # Store
        self.store.put(parcel, status=STATUS_PENDING)

        # Decode payload
        try:
            data = self._decode_payload(parcel)
        except Exception as e:
            logger.error("Failed to decode payload for parcel %s: %s",
                        parcel.parcel_id, e)
            return ReceivedMessage(
                parcel_id=parcel.parcel_id,
                success=False,
                error="decode_error",
                message=str(e),
            )

        logger.info("Parcel %s received and validated", parcel.parcel_id)

        # Check if we're the destination
        is_final = parcel.envelope.destination == self.node_id
        if is_final:
            self.store.mark_delivered(parcel.parcel_id)

        return ReceivedMessage(
            parcel_id=parcel.parcel_id,
            success=True,
            data=data,
            origin=parcel.envelope.origin,
            destination=parcel.envelope.destination,
            is_final_delivery=is_final,
            hop_count=parcel.envelope.hop_count,
        )

    def flush(
        self,
        route: Route,
        transport: Callable[[Parcel, str], bool],
        priority: str | None = None,
        limit: int = 10,
    ) -> list[DeliveryReceipt]:
        """
        Flush pending parcels to the next waypoint.

        Called when a neighbor appears — like a carrier arriving at
        a relay station and the keeper bringing out the queue.

        Args:
            route: The route to follow.
            transport: Transfer function(parcel, next_node) -> bool.
            priority: Only flush this priority level (None = all).
            limit: Maximum parcels to flush.

        Returns:
            List of DeliveryReceipts.
        """
        parcels = self.store.get_pending(limit=limit, priority=priority)
        receipts = []

        for parcel in parcels:
            receipt = self.carry(parcel, route, transport=transport)
            receipts.append(receipt)
            if receipt.status == "failed" and receipt.message.startswith("Transfer"):
                # Stop flushing on transport failure (neighbor may be gone)
                break

        return receipts

    def _deliver_locally(self, parcel: Parcel) -> DeliveryReceipt:
        """Deliver a parcel to this node (it's the destination)."""
        self.store.mark_delivered(parcel.parcel_id)

        logger.info("Parcel %s delivered to %s",
                    parcel.parcel_id, self.node_id)

        return DeliveryReceipt(
            parcel_id=parcel.parcel_id,
            success=True,
            status="delivered",
            message=f"Delivered to {self.node_id}",
            hop_count=parcel.envelope.hop_count,
        )

    def _decode_payload(self, parcel: Parcel) -> Any:
        """Decode a parcel's payload back to the original message."""
        body = parcel.payload.body
        compression = parcel.envelope.compression
        encoding = parcel.payload.encoding

        # Decode encoding
        if encoding == "base64":
            raw = base64.b64decode(body)
        else:
            raw = body.encode("utf-8")

        # Decompress
        if compression == Compression.GZIP.value:
            raw = gzip.decompress(raw)
        elif compression == Compression.ZSTD.value:
            raw = zlib.decompress(raw)

        # Deserialize by type
        if parcel.payload.type == PayloadType.JSON.value:
            return json.loads(raw)
        elif parcel.payload.type == PayloadType.BINARY.value:
            return raw
        else:
            return raw.decode("utf-8")

    def close(self) -> None:
        """Close the carrier's store."""
        self.store.close()


@dataclass
class ReceivedMessage:
    """
    Result of receiving a parcel.

    If success is True, data contains the decoded message.
    If success is False, error contains the failure reason.
    """
    parcel_id: str
    success: bool
    data: Any = None
    origin: str = ""
    destination: str = ""
    is_final_delivery: bool = False
    hop_count: int = 0
    error: str = ""
    message: str = ""
