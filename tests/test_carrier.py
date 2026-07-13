"""
Tests for the Carrier — pack, carry, receive cycle.
"""

import os
import time
import json
import tempfile
import pytest
from carry import Carrier, FenceConfig, Route, Waypoint, Store
from carry.carrier import DeliveryReceipt, ReceivedMessage
from carry.parcel import Priority


@pytest.fixture
def tmp_store():
    """Create a temporary store for each test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # SQLite will create it
    store = Store(path)
    yield store
    store.close()
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def carrier(tmp_store):
    """Create a carrier with a temp store."""
    c = Carrier(node_id="node-a", store=tmp_store, fence_config=FenceConfig(power_budget_mw=1000))
    return c


class TestPack:
    def test_pack_json_message(self, carrier):
        parcel = carrier.pack(
            message={"temp": 42, "humidity": 60},
            destination="gateway-1",
        )
        assert parcel.envelope.origin == "node-a"
        assert parcel.envelope.destination == "gateway-1"
        assert parcel.fence.checksum
        assert parcel.fence.size_bytes > 0
        assert len(parcel.fence.hop_log) == 1
        assert parcel.fence.hop_log[0].action == "packed"

    def test_pack_text_message(self, carrier):
        parcel = carrier.pack(
            message="hello mountains",
            destination="gateway-1",
        )
        assert parcel.payload.type == "text"

    def test_pack_compressed(self, carrier):
        big_msg = {"data": "x" * 500}
        parcel = carrier.pack(message=big_msg, destination="gw")
        assert parcel.envelope.compression in ("gzip", "zstd")
        # Compressed body should be smaller than raw
        assert parcel.fence.size_bytes < len(json.dumps(big_msg))

    def test_pack_urgent_priority(self, carrier):
        parcel = carrier.pack(
            message="emergency",
            destination="gateway-1",
            priority=Priority.URGENT.value,
        )
        assert parcel.envelope.priority == "urgent"

    def test_pack_stores_locally(self, carrier):
        parcel = carrier.pack(message="test", destination="gw")
        stored = carrier.store.get(parcel.parcel_id)
        assert stored is not None
        assert stored.parcel_id == parcel.parcel_id


class TestCarry:
    def test_carry_to_destination(self, carrier):
        """When the carrier IS the destination, deliver locally."""
        parcel = carrier.pack(message="hello", destination="node-a")
        route = Route.from_waypoints(["node-a"])
        receipt = carrier.carry(parcel, route)
        assert receipt.success is True
        assert receipt.status == "delivered"

    def test_carry_with_transport_success(self, carrier):
        parcel = carrier.pack(message="hello", destination="node-b")
        route = Route.from_waypoints([
            Waypoint("node-a", hop_cost_mw=10),
            Waypoint("node-b", hop_cost_mw=10),
        ])
        results = []
        def transport(p, next_node):
            results.append((p.parcel_id, next_node))
            return True

        receipt = carrier.carry(parcel, route, transport=transport)
        assert receipt.success is True
        assert receipt.status == "forwarded"
        assert receipt.next_node == "node-b"
        assert len(results) == 1

    def test_carry_transport_failure(self, carrier):
        parcel = carrier.pack(message="hello", destination="node-b")
        route = Route.from_waypoints([
            Waypoint("node-a", hop_cost_mw=10),
            Waypoint("node-b", hop_cost_mw=10),
        ])
        def transport(p, next_node):
            return False

        receipt = carrier.carry(parcel, route, transport=transport)
        assert receipt.success is False
        assert receipt.status == "failed"

    def test_carry_no_transport(self, carrier):
        """Without transport, parcel is stored and held."""
        parcel = carrier.pack(message="hello", destination="node-b")
        route = Route.from_waypoints(["node-a", "node-b"])
        receipt = carrier.carry(parcel, route)
        assert receipt.success is False
        assert receipt.status == "held"

    def test_carry_expired(self, carrier):
        parcel = carrier.pack(message="hello", destination="node-b", ttl_hours=0)
        # Force expiry
        parcel.envelope.expires_at = int(time.time()) - 1
        carrier.store.update_parcel(parcel)
        route = Route.from_waypoints(["node-a", "node-b"])
        receipt = carrier.carry(parcel, route)
        assert receipt.success is False
        assert receipt.status == "expired"

    def test_carry_increments_hop_count(self, carrier):
        parcel = carrier.pack(message="hello", destination="node-c")
        route = Route.from_waypoints([
            Waypoint("node-a", hop_cost_mw=10),
            Waypoint("node-b", hop_cost_mw=10),
            Waypoint("node-c", hop_cost_mw=10),
        ])
        def transport(p, next_node):
            return True

        receipt = carrier.carry(parcel, route, transport=transport)
        assert receipt.hop_count == 1


class TestReceive:
    def test_receive_valid_parcel(self, carrier):
        # Create a parcel from another node
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        other_store = Store(path)
        other = Carrier(node_id="node-x", store=other_store)

        msg = {"alert": "storm incoming"}
        parcel = other.pack(message=msg, destination="node-a")

        # Receive it
        result = carrier.receive(parcel)
        assert result.success is True
        assert result.data == msg
        assert result.is_final_delivery is True

        other_store.close()
        if os.path.exists(path):
            os.unlink(path)

    def test_receive_corrupted_parcel(self, carrier):
        parcel = carrier.pack(message="hello", destination="node-a")
        parcel.payload.body = "corrupted"
        result = carrier.receive(parcel)
        assert result.success is False
        assert result.error == "checksum_mismatch"

    def test_receive_loop_detected(self, carrier):
        parcel = carrier.pack(message="hello", destination="node-b")
        # Simulate that this node already forwarded the parcel
        from carry.parcel import HopEntry
        parcel.fence.add_hop(HopEntry(node=carrier.node_id, timestamp=0, action="forwarded"))
        result = carrier.receive(parcel)
        assert result.success is False
        assert result.error == "route_loop"


class TestStore:
    def test_store_and_retrieve(self, tmp_store):
        import hashlib
        from carry import Parcel, Envelope, Payload, Fence

        env = Envelope(origin="a", destination="b", expires_at=int(time.time()) + 3600)
        body = "test data"
        payload = Payload(body=body)
        checksum = hashlib.blake2b(body.encode(), digest_size=32).hexdigest()
        fence = Fence(checksum=checksum, size_bytes=len(body))

        parcel = Parcel(envelope=env, payload=payload, fence=fence)
        tmp_store.put(parcel)
        retrieved = tmp_store.get(parcel.parcel_id)
        assert retrieved is not None
        assert retrieved.envelope.origin == "a"

    def test_get_pending_ordered_by_priority(self, tmp_store):
        import hashlib
        from carry import Parcel, Envelope, Payload, Fence

        for i, prio in enumerate(["deferred", "urgent", "normal"]):
            env = Envelope(
                origin="a",
                destination="b",
                expires_at=int(time.time()) + 3600,
                priority=prio,
            )
            body = f"msg-{i}"
            payload = Payload(body=body)
            checksum = hashlib.blake2b(body.encode(), digest_size=32).hexdigest()
            fence = Fence(checksum=checksum, size_bytes=len(body))
            parcel = Parcel(envelope=env, payload=payload, fence=fence)
            tmp_store.put(parcel)

        pending = tmp_store.get_pending(limit=10)
        assert pending[0].envelope.priority == "urgent"
        assert pending[1].envelope.priority == "normal"
        assert pending[2].envelope.priority == "deferred"

    def test_record_attempt_backoff(self, tmp_store):
        import hashlib
        from carry import Parcel, Envelope, Payload, Fence

        env = Envelope(origin="a", destination="b", expires_at=int(time.time()) + 3600)
        body = "msg"
        payload = Payload(body=body)
        checksum = hashlib.blake2b(body.encode(), digest_size=32).hexdigest()
        fence = Fence(checksum=checksum, size_bytes=len(body))
        parcel = Parcel(envelope=env, payload=payload, fence=fence)
        tmp_store.put(parcel)

        # Record a few attempts
        tmp_store.record_attempt(parcel.parcel_id, 30)
        tmp_store.record_attempt(parcel.parcel_id, 30)

        retrieved = tmp_store.get(parcel.parcel_id)
        # Parcel should still be retrievable
        assert retrieved is not None

    def test_purge_expired(self, tmp_store):
        import hashlib
        from carry import Parcel, Envelope, Payload, Fence

        # Expired parcel
        env = Envelope(origin="a", destination="b", expires_at=int(time.time()) - 1)
        body = "expired"
        payload = Payload(body=body)
        checksum = hashlib.blake2b(body.encode(), digest_size=32).hexdigest()
        fence = Fence(checksum=checksum, size_bytes=len(body))
        parcel = Parcel(envelope=env, payload=payload, fence=fence)
        tmp_store.put(parcel)

        count = tmp_store.purge_expired()
        assert count == 1
        assert tmp_store.get(parcel.parcel_id) is None


class TestRoute:
    def test_from_waypoints_strings(self):
        route = Route.from_waypoints(["a", "b", "c"])
        assert len(route.waypoints) == 3
        assert route.hop_count == 2

    def test_next_waypoint(self):
        route = Route.from_waypoints(["a", "b", "c"])
        next_wp = route.next_waypoint("a")
        assert next_wp.node == "b"

        next_wp = route.next_waypoint("b")
        assert next_wp.node == "c"

        next_wp = route.next_waypoint("c")
        assert next_wp is None

    def test_is_destination(self):
        route = Route.from_waypoints(["a", "b", "c"])
        assert route.is_destination("c") is True
        assert route.is_destination("a") is False

    def test_subroute(self):
        route = Route.from_waypoints(["a", "b", "c", "d"])
        sub = route.subroute_from("b")
        assert sub is not None
        assert [wp.node for wp in sub.waypoints] == ["b", "c", "d"]

    def test_json_roundtrip(self):
        route = Route.from_waypoints([
            Waypoint("a", hop_cost_mw=10),
            Waypoint("b", hop_cost_mw=20),
        ])
        j = route.to_json()
        route2 = Route.from_json(j)
        assert route2.waypoints[0].node == "a"
        assert route2.waypoints[1].hop_cost_mw == 20


class TestFlush:
    def test_flush_multiple_parcels(self, carrier):
        """Flush should send multiple pending parcels."""
        p1 = carrier.pack(message="msg1", destination="node-b", priority="urgent")
        p2 = carrier.pack(message="msg2", destination="node-b", priority="normal")

        route = Route.from_waypoints([
            Waypoint("node-a", hop_cost_mw=5),
            Waypoint("node-b", hop_cost_mw=5),
        ])

        sent = []
        def transport(p, next_node):
            sent.append(p.parcel_id)
            return True

        receipts = carrier.flush(route, transport, limit=10)
        assert len(receipts) == 2
        assert all(r.success for r in receipts)
        assert len(sent) == 2
