"""
Tests for the conservation fence validator.
"""

import time
import pytest
from carry import Parcel, Envelope, Payload, Fence, FenceConfig, FenceValidator
from carry.fence import FenceStatus, FenceResult
from carry.parcel import Priority, HopEntry, HopAction


def make_valid_parcel(body="test payload", origin="node-a", destination="node-b", **kwargs):
    """Helper to create a valid parcel."""
    import hashlib

    env = Envelope(
        origin=origin,
        destination=destination,
        expires_at=int(time.time()) + 3600,
        **kwargs,
    )
    payload = Payload(body=body)
    body_bytes = body.encode("utf-8")
    checksum = hashlib.blake2b(body_bytes, digest_size=32).hexdigest()
    fence = Fence(checksum=checksum, size_bytes=len(body_bytes))
    return Parcel(envelope=env, payload=payload, fence=fence)


class TestFenceValidator:
    def test_valid_parcel_passes(self):
        validator = FenceValidator()
        parcel = make_valid_parcel()
        result = validator.validate(parcel, node_id="relay-1")
        assert result.passed is True
        assert result.status == FenceStatus.PASS

    def test_expired_parcel(self):
        validator = FenceValidator()
        parcel = make_valid_parcel()
        parcel.envelope.expires_at = int(time.time()) - 1
        result = validator.validate(parcel, node_id="relay-1")
        assert result.status == FenceStatus.EXPIRED
        assert result.should_discard is True

    def test_max_hops_exceeded(self):
        validator = FenceValidator()
        parcel = make_valid_parcel()
        parcel.envelope.hop_count = 16
        parcel.envelope.max_hops = 16
        result = validator.validate(parcel, node_id="relay-1")
        assert result.status == FenceStatus.MAX_HOPS_EXCEEDED
        assert result.should_reject is True

    def test_checksum_mismatch(self):
        validator = FenceValidator()
        parcel = make_valid_parcel()
        parcel.fence.checksum = "0" * 64
        result = validator.validate(parcel, node_id="relay-1")
        assert result.status == FenceStatus.CHECKSUM_MISMATCH
        assert result.should_reject is True

    def test_size_mismatch(self):
        validator = FenceValidator()
        parcel = make_valid_parcel()
        parcel.fence.size_bytes = 99999
        result = validator.validate(parcel, node_id="relay-1")
        assert result.status == FenceStatus.SIZE_MISMATCH

    def test_loop_detection(self):
        validator = FenceValidator()
        parcel = make_valid_parcel()
        parcel.fence.add_hop(HopEntry(node="relay-1", timestamp=0, action="forwarded"))
        result = validator.validate(parcel, node_id="relay-1")
        assert result.status == FenceStatus.ROUTE_LOOP

    def test_power_insufficient(self):
        validator = FenceValidator()
        parcel = make_valid_parcel()
        parcel.envelope.power_budget_mw = 100
        # Add hops that use most of the budget
        parcel.fence.add_hop(HopEntry(node="node-a", timestamp=0, action="packed", power_used_mw=90))
        result = validator.validate(parcel, node_id="relay-1", next_hop_cost_mw=50)
        assert result.status == FenceStatus.POWER_INSUFFICIENT
        assert result.should_hold is True

    def test_power_sufficient(self):
        validator = FenceValidator()
        parcel = make_valid_parcel()
        parcel.envelope.power_budget_mw = 500
        parcel.fence.add_hop(HopEntry(node="node-a", timestamp=0, action="packed", power_used_mw=10))
        result = validator.validate(parcel, node_id="relay-1", next_hop_cost_mw=50)
        assert result.passed is True

    def test_unsupported_version(self):
        config = FenceConfig(supported_versions=(1,))
        validator = FenceValidator(config)
        parcel = make_valid_parcel()
        parcel.envelope.version = 99
        result = validator.validate(parcel, node_id="relay-1")
        assert result.status == FenceStatus.VERSION_UNSUPPORTED

    def test_create_hop_entry(self):
        validator = FenceValidator()
        entry = validator.create_hop_entry(
            node_id="relay-1",
            action=HopAction.FORWARDED,
            power_used_mw=15,
        )
        assert entry.node == "relay-1"
        assert entry.action == "forwarded"
        assert entry.power_used_mw == 15
