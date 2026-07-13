"""
Tests for Parcel dataclass and fence checksum validation.
"""

import json
import time
import pytest
from carry import Parcel, Envelope, Payload, Fence, HopEntry
from carry.parcel import Priority, Compression, PayloadType, ChecksumAlgo, HopAction


class TestEnvelope:
    def test_default_envelope(self):
        env = Envelope(origin="node-a", destination="node-b", expires_at=int(time.time()) + 3600)
        assert env.origin == "node-a"
        assert env.destination == "node-b"
        assert env.version == 1
        assert env.priority == Priority.NORMAL.value
        assert env.max_hops == 16

    def test_roundtrip(self):
        env = Envelope(
            origin="sensor-1",
            destination="gateway-1",
            expires_at=int(time.time()) + 7200,
            priority=Priority.URGENT.value,
            max_hops=8,
        )
        d = env.to_dict()
        env2 = Envelope.from_dict(d)
        assert env2.origin == env.origin
        assert env2.destination == env.destination
        assert env2.priority == env.priority
        assert env2.max_hops == env.max_hops


class TestFence:
    def test_add_hop(self):
        fence = Fence(checksum="abc123", size_bytes=100)
        entry = HopEntry(node="relay-1", timestamp=int(time.time()), action="forwarded")
        fence.add_hop(entry)
        assert len(fence.hop_log) == 1
        assert fence.hop_log[0].node == "relay-1"

    def test_roundtrip(self):
        fence = Fence(
            checksum="deadbeef",
            checksum_algo="sha256",
            size_bytes=42,
            hop_log=[
                HopEntry(node="a", timestamp=1000, action="packed"),
                HopEntry(node="b", timestamp=2000, action="forwarded", power_used_mw=10),
            ],
        )
        d = fence.to_dict()
        fence2 = Fence.from_dict(d)
        assert fence2.checksum == fence.checksum
        assert fence2.size_bytes == fence.size_bytes
        assert len(fence2.hop_log) == 2
        assert fence2.hop_log[1].power_used_mw == 10


class TestParcel:
    def _make_parcel(self, body="hello world", destination="node-b"):
        env = Envelope(
            origin="node-a",
            destination=destination,
            expires_at=int(time.time()) + 3600,
        )
        payload = Payload(type=PayloadType.TEXT.value, body=body)
        import hashlib
        body_bytes = body.encode("utf-8")
        checksum = hashlib.blake2b(body_bytes, digest_size=32).hexdigest()
        fence = Fence(checksum=checksum, size_bytes=len(body_bytes))
        return Parcel(envelope=env, payload=payload, fence=fence)

    def test_json_roundtrip(self):
        p = self._make_parcel()
        j = p.to_json()
        p2 = Parcel.from_json(j)
        assert p2.envelope.origin == p.envelope.origin
        assert p2.payload.body == p.payload.body
        assert p2.fence.checksum == p.fence.checksum

    def test_checksum_validation(self):
        p = self._make_parcel()
        assert p.validate_checksum() is True

    def test_checksum_tamper_detection(self):
        p = self._make_parcel()
        p.payload.body = "tampered"
        assert p.validate_checksum() is False

    def test_expiry(self):
        p = self._make_parcel()
        p.envelope.expires_at = int(time.time()) - 1
        assert p.is_expired is True

    def test_hops_exceeded(self):
        p = self._make_parcel()
        p.envelope.hop_count = 16
        p.envelope.max_hops = 16
        assert p.is_hops_exceeded is True

    def test_loop_detection(self):
        p = self._make_parcel()
        # 'packed' action should NOT trigger loop detection (origin is expected)
        p.fence.add_hop(HopEntry(node="node-a", timestamp=0, action="packed"))
        assert p.has_loop("node-a") is False
        # 'forwarded' action SHOULD trigger loop detection
        p.fence.add_hop(HopEntry(node="node-a", timestamp=1, action="forwarded"))
        assert p.has_loop("node-a") is True
        assert p.has_loop("node-b") is False

    def test_remaining_power_budget(self):
        p = self._make_parcel()
        p.envelope.power_budget_mw = 500
        p.fence.add_hop(HopEntry(node="node-a", timestamp=0, action="packed", power_used_mw=100))
        p.fence.add_hop(HopEntry(node="relay-1", timestamp=0, action="forwarded", power_used_mw=50))
        assert p.remaining_power_budget_mw == 350

    def test_different_checksum_algos(self):
        p = self._make_parcel()
        sha = p.compute_checksum(algo="sha256")
        assert len(sha) == 64  # SHA-256 hex digest
