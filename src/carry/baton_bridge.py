"""
Baton bridge — serialize/deserialize baton lessons for carry-protocol transfer.

When a model upgrade happens on one side of the Divide (offline edge device),
the baton's lessons need to travel across to the other side. This module
provides the serializer that packs baton lessons into carry-protocol parcels
and unpacks them on arrival.

Usage:
    from carry.baton_bridge import pack_lessons, unpack_lessons

    # Edge device: pack lessons for transfer
    parcel = pack_lessons(baton_lessons, route="satellite-uplink")

    # Cloud side: unpack
    lessons = unpack_lessons(parcel)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional
from datetime import datetime, timezone


@dataclass
class BatonLessonPayload:
    """Serializable representation of a baton lesson for transfer."""
    lesson_id: str
    content: str
    confidence: float
    created_at: str
    expires_at: Optional[str] = None
    model_origin: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "lesson_id": self.lesson_id,
            "content": self.content,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "model_origin": self.model_origin,
            "tags": self.tags,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> BatonLessonPayload:
        return cls(
            lesson_id=d["lesson_id"],
            content=d["content"],
            confidence=d.get("confidence", 0.5),
            created_at=d.get("created_at", datetime.now(timezone.utc).isoformat()),
            expires_at=d.get("expires_at"),
            model_origin=d.get("model_origin"),
            tags=d.get("tags", []),
            metadata=d.get("metadata", {}),
        )


def pack_lessons(lessons: list[dict], route: str = "default") -> dict:
    """Pack baton lessons into a carry-protocol-compatible payload.

    Args:
        lessons: List of lesson dicts from baton's export.
        route: Route identifier for the carry protocol.

    Returns:
        A parcel payload dict suitable for carry-protocol transfer.
    """
    payloads = [BatonLessonPayload.from_dict(l) for l in lessons]

    return {
        "type": "baton_lessons",
        "version": "1.0",
        "route": route,
        "count": len(payloads),
        "lessons": [p.to_dict() for p in payloads],
        "checksum": _checksum([p.to_dict() for p in payloads]),
    }


def unpack_lessons(parcel_payload: dict) -> list[BatonLessonPayload]:
    """Unpack a carry-protocol payload back into baton lessons.

    Args:
        parcel_payload: The payload dict from carry-protocol delivery.

    Returns:
        List of BatonLessonPayload objects.

    Raises:
        ValueError: If payload type is wrong or checksum fails.
    """
    if parcel_payload.get("type") != "baton_lessons":
        raise ValueError(
            f"Expected payload type 'baton_lessons', got '{parcel_payload.get('type')}'"
        )

    lessons_data = parcel_payload.get("lessons", [])
    expected_checksum = parcel_payload.get("checksum")
    actual_checksum = _checksum(lessons_data)

    if expected_checksum != actual_checksum:
        raise ValueError(
            f"Checksum mismatch: lessons may be corrupted in transit. "
            f"Expected {expected_checksum}, got {actual_checksum}"
        )

    return [BatonLessonPayload.from_dict(l) for l in lessons_data]


def _checksum(data: list[dict]) -> str:
    """Simple checksum for integrity verification."""
    import hashlib
    raw = json.dumps(data, sort_keys=True).encode()
    return hashlib.blake2b(raw, digest_size=16).hexdigest()
