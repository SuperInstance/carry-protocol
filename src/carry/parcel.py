"""
Parcel dataclass — the atomic unit of transfer in the Carry Protocol.

A parcel has three sections:
  - Envelope: routing metadata
  - Payload: the actual data being carried
  - Fence: integrity validation and hop log
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Priority(str, Enum):
    URGENT = "urgent"
    NORMAL = "normal"
    DEFERRED = "deferred"


class Compression(str, Enum):
    NONE = "none"
    GZIP = "gzip"
    ZSTD = "zstd"


class PayloadType(str, Enum):
    TEXT = "text"
    JSON = "json"
    BINARY = "binary"


class ChecksumAlgo(str, Enum):
    BLAKE2B = "blake2b"
    SHA256 = "sha256"
    CRC32 = "crc32"


class HopAction(str, Enum):
    PACKED = "packed"
    FORWARDED = "forwarded"
    RECEIVED = "received"
    HELD = "held"
    REJECTED = "rejected"


@dataclass
class HopEntry:
    """A single entry in the fence hop log — appended at each hop."""
    node: str
    timestamp: int
    action: str
    power_used_mw: int = 0
    checksum_valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "timestamp": self.timestamp,
            "action": self.action,
            "power_used_mw": self.power_used_mw,
            "checksum_valid": self.checksum_valid,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HopEntry:
        return cls(
            node=d["node"],
            timestamp=d["timestamp"],
            action=d["action"],
            power_used_mw=d.get("power_used_mw", 0),
            checksum_valid=d.get("checksum_valid", True),
        )


@dataclass
class Envelope:
    """Routing metadata for a parcel."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    version: int = 1
    origin: str = ""
    destination: str = ""
    created_at: int = field(default_factory=lambda: int(time.time()))
    expires_at: int = 0  # Must be set explicitly
    priority: str = Priority.NORMAL.value
    compression: str = Compression.GZIP.value
    hop_count: int = 0
    max_hops: int = 16
    power_budget_mw: int = 500

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "origin": self.origin,
            "destination": self.destination,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "priority": self.priority,
            "compression": self.compression,
            "hop_count": self.hop_count,
            "max_hops": self.max_hops,
            "power_budget_mw": self.power_budget_mw,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Envelope:
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            version=d.get("version", 1),
            origin=d["origin"],
            destination=d["destination"],
            created_at=d["created_at"],
            expires_at=d["expires_at"],
            priority=d.get("priority", Priority.NORMAL.value),
            compression=d.get("compression", Compression.GZIP.value),
            hop_count=d.get("hop_count", 0),
            max_hops=d.get("max_hops", 16),
            power_budget_mw=d.get("power_budget_mw", 500),
        )


@dataclass
class Payload:
    """The data being carried."""
    type: str = PayloadType.JSON.value
    encoding: str = "utf-8"
    body: str = ""  # May be compressed + encoded

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "encoding": self.encoding,
            "body": self.body,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Payload:
        return cls(
            type=d.get("type", PayloadType.JSON.value),
            encoding=d.get("encoding", "utf-8"),
            body=d["body"],
        )


@dataclass
class Fence:
    """Conservation fence — integrity validation and hop log."""
    checksum_algo: str = ChecksumAlgo.BLAKE2B.value
    checksum: str = ""
    size_bytes: int = 0
    hop_log: list[HopEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checksum_algo": self.checksum_algo,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "hop_log": [h.to_dict() for h in self.hop_log],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Fence:
        return cls(
            checksum_algo=d.get("checksum_algo", ChecksumAlgo.BLAKE2B.value),
            checksum=d.get("checksum", ""),
            size_bytes=d.get("size_bytes", 0),
            hop_log=[HopEntry.from_dict(h) for h in d.get("hop_log", [])],
        )

    def add_hop(self, entry: HopEntry) -> None:
        """Append a hop entry to the log."""
        self.hop_log.append(entry)


@dataclass
class Parcel:
    """
    The atomic unit of transfer in the Carry Protocol.

    A parcel contains an envelope (routing), a payload (data), and a fence
    (integrity + hop log). Parcels are self-contained — everything needed
    to route, validate, and deliver is inside.
    """
    envelope: Envelope = field(default_factory=Envelope)
    payload: Payload = field(default_factory=Payload)
    fence: Fence = field(default_factory=Fence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope": self.envelope.to_dict(),
            "payload": self.payload.to_dict(),
            "fence": self.fence.to_dict(),
        }

    def to_json(self) -> str:
        """Serialize the parcel to a JSON string."""
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Parcel:
        return cls(
            envelope=Envelope.from_dict(d["envelope"]),
            payload=Payload.from_dict(d["payload"]),
            fence=Fence.from_dict(d["fence"]),
        )

    @classmethod
    def from_json(cls, j: str | bytes) -> Parcel:
        """Deserialize a parcel from a JSON string."""
        if isinstance(j, bytes):
            j = j.decode("utf-8")
        return cls.from_dict(json.loads(j))

    @property
    def parcel_id(self) -> str:
        return self.envelope.id

    @property
    def is_expired(self) -> bool:
        return int(time.time()) > self.envelope.expires_at

    @property
    def is_hops_exceeded(self) -> bool:
        return self.envelope.hop_count >= self.envelope.max_hops

    @property
    def remaining_power_budget_mw(self) -> int:
        used = sum(h.power_used_mw for h in self.fence.hop_log)
        return max(0, self.envelope.power_budget_mw - used)

    def compute_checksum(self, algo: str | None = None) -> str:
        """
        Compute the checksum over the raw payload body.

        This is recomputed at each hop and compared to fence.checksum.
        """
        algo = algo or self.fence.checksum_algo
        body_bytes = self.payload.body.encode("utf-8") if isinstance(self.payload.body, str) else self.payload.body

        if algo == ChecksumAlgo.BLAKE2B.value:
            return hashlib.blake2b(body_bytes, digest_size=32).hexdigest()
        elif algo == ChecksumAlgo.SHA256.value:
            return hashlib.sha256(body_bytes).hexdigest()
        elif algo == ChecksumAlgo.CRC32.value:
            return format(hash(body_bytes) & 0xFFFFFFFF, "08x")
        else:
            raise ValueError(f"Unknown checksum algorithm: {algo}")

    def validate_checksum(self) -> bool:
        """Validate that the stored checksum matches the recomputed checksum."""
        if not self.fence.checksum:
            return False
        return self.compute_checksum() == self.fence.checksum

    def has_loop(self, node_id: str) -> bool:
        """
        Check if this node has already handled this parcel (loop detection).

        Only checks 'forwarded' and 'received' entries — the originator's
        'packed' entry is expected and is not a loop.
        """
        loop_actions = {"forwarded", "received"}
        return any(
            h.node == node_id and h.action in loop_actions
            for h in self.fence.hop_log
        )
