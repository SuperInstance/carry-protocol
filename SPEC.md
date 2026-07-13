# Carry Protocol Specification

**Version:** 1.0  
**Status:** Draft

---

## 1. Overview

The Carry Protocol defines how edge devices exchange data across unreliable, intermittent, or non-existent connectivity. It is designed for scenarios where:

- End-to-end connectivity is not guaranteed (or not expected)
- Devices have limited power (battery, solar, scavenged)
- Data must traverse multiple relay hops
- Latency is measured in hours or days, not milliseconds
- Reliability matters more than speed

### 1.1 Design Goals

| Goal | Approach |
|------|----------|
| Offline operation | Store-and-forward at every node |
| Power conservation | Compression, priority deferral, power budgets |
| Integrity | Fence checksums validated at every hop |
| Simplicity | JSON envelope, minimal required fields |
| Extensibility | Optional fields, pluggable compression/checksum |

### 1.2 Terminology

| Term | Definition |
|------|-----------|
| **Parcel** | The atomic unit of transfer: envelope + payload + fence |
| **Carrier** | An edge node that creates, forwards, or receives parcels |
| **Waypoint** | A node on a planned route where a parcel may rest |
| **Route** | An ordered sequence of waypoints |
| **Fence** | The validation layer applied at each hop |
| **Hop** | A single parcel transfer between two adjacent waypoints |
| **Delivery Receipt** | A record of a carry attempt (success, failure, or pending) |

---

## 2. Parcel Format

A parcel is a JSON document with three sections: **envelope**, **payload**, and **fence**.

### 2.1 Structure

```json
{
  "envelope": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "version": 1,
    "origin": "sensor-alpha",
    "destination": "gateway-beta",
    "created_at": 1721830320,
    "expires_at": 1722435120,
    "priority": "normal",
    "compression": "gzip",
    "hop_count": 0,
    "max_hops": 16,
    "power_budget_mw": 500
  },
  "payload": {
    "type": "json",
    "encoding": "utf-8",
    "body": "<compressed-and-base64-encoded-data>"
  },
  "fence": {
    "checksum_algo": "blake2b",
    "checksum": "a1b2c3d4e5f6...",
    "size_bytes": 234,
    "hop_log": [
      {
        "node": "sensor-alpha",
        "timestamp": 1721830320,
        "action": "packed",
        "power_used_mw": 5
      }
    ]
  }
}
```

### 2.2 Envelope Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string (UUIDv4) | Yes | Unique parcel identifier |
| `version` | int | Yes | Protocol version (currently 1) |
| `origin` | string | Yes | Node ID of the creator |
| `destination` | string | Yes | Node ID of the final recipient |
| `created_at` | int (epoch) | Yes | Creation timestamp (seconds) |
| `expires_at` | int (epoch) | Yes | Expiry timestamp. After this, the parcel is discarded |
| `priority` | enum | Yes | One of: `urgent`, `normal`, `deferred` |
| `compression` | enum | Yes | One of: `none`, `gzip`, `zstd` |
| `hop_count` | int | Yes | Number of hops completed. Incremented at each hop |
| `max_hops` | int | Yes | Maximum hops before the parcel is discarded |
| `power_budget_mw` | int | Yes | Total power budget in milliwatts for the entire route |

### 2.3 Payload Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | enum | Yes | One of: `text`, `json`, `binary` |
| `encoding` | string | Yes | Encoding of the body (`utf-8`, `base64`) |
| `body` | string | Yes | The (possibly compressed, possibly encoded) message data |

### 2.4 Fence Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `checksum_algo` | enum | Yes | Algorithm used: `blake2b`, `sha256`, `crc32` |
| `checksum` | string | Yes | Hex digest of the checksum over the payload body |
| `size_bytes` | int | Yes | Size of the payload body in bytes |
| `hop_log` | array | Yes | Append-only log of hop events |

### 2.5 Hop Log Entry

Each entry in `hop_log` is appended at each hop:

```json
{
  "node": "relay-1",
  "timestamp": 1721830920,
  "action": "forwarded",
  "power_used_mw": 12,
  "checksum_valid": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `node` | string | Node ID performing the hop |
| `timestamp` | int (epoch) | When the hop occurred |
| `action` | enum | `packed`, `forwarded`, `received`, `held`, `rejected` |
| `power_used_mw` | int | Power consumed by this hop |
| `checksum_valid` | bool | Whether the fence checksum validated |

---

## 3. Handshake

The Carry handshake is lightweight — a three-step exchange that establishes whether a neighbor can accept a parcel.

### 3.1 Sequence

```
Node A                        Node B
  |                              |
  |  --- OFFER (parcel metadata) --> |
  |                              |
  |  <-- ACK/NAK --------------- |
  |                              |
  |  --- TRANSFER (full parcel) --> |
  |                              |
  |  <-- FENCE_OK/FENCE_FAIL ---- |
  |                              |
```

### 3.2 OFFER

Node A sends only the envelope (no payload) to Node B:

```json
{
  "type": "CARRY_OFFER",
  "envelope": { /* envelope fields only */ }
}
```

### 3.3 ACK / NAK

Node B evaluates whether it can accept:

- Is the destination reachable (eventually)?
- Is the parcel expired?
- Has the hop count been exceeded?
- Is there enough local storage?
- Is the priority acceptable given current power state?

```json
{
  "type": "CARRY_ACK",
  "parcel_id": "550e8400-...",
  "accepts": true,
  "reason": null
}
```

Or:

```json
{
  "type": "CARRY_NAK",
  "parcel_id": "550e8400-...",
  "accepts": false,
  "reason": "storage_full"
}
```

### 3.4 TRANSFER

On ACK, Node A sends the full parcel. Node B validates the fence checksum and responds:

```json
{
  "type": "CARRY_FENCE_OK",
  "parcel_id": "550e8400-...",
  "timestamp": 1721830920
}
```

Or:

```json
{
  "type": "CARRY_FENCE_FAIL",
  "parcel_id": "550e8400-...",
  "reason": "checksum_mismatch",
  "timestamp": 1721830920
}
```

On FENCE_FAIL, Node A retains the parcel and may retry.

---

## 4. Store-and-Forward Semantics

### 4.1 Local Persistence

Every Carrier maintains a local SQLite store. Parcels persist across reboots, power cycles, and connectivity gaps.

### 4.2 Forwarding Loop

When a Carrier has parcels to forward and a neighbor becomes available:

1. Query the local store for parcels sorted by priority (`urgent` > `normal` > `deferred`) and then by `created_at` (oldest first).
2. For each parcel, initiate the handshake with the neighbor.
3. On successful transfer, mark the parcel as `delivered_to: <neighbor>` in the local store.
4. Do not delete the parcel until FENCE_OK is received from the neighbor.
5. On failure, retain the parcel and apply backoff.

### 4.3 Backoff Strategy

Failed transfer attempts use exponential backoff:

| Attempt | Delay |
|---------|-------|
| 1 | 30 seconds |
| 2 | 2 minutes |
| 3 | 8 minutes |
| 4 | 30 minutes |
| 5+ | 2 hours |

After 12 failed attempts, the parcel is marked `stalled` but NOT discarded. A stalled parcel remains in the store until it expires or is manually cleared.

### 4.4 Priority and Power

When power is constrained, the Carrier defers `deferred` and `normal` parcels, forwarding only `urgent` traffic until power reserves recover. The threshold is configurable:

```python
# Only forward urgent parcels below 20% battery
if power_state.percent < 20:
    forward_only(priority="urgent")
```

---

## 5. Conservation Fence

The fence is the heart of the protocol. Named after the conservation principle — a parcel must conserve its integrity across every hop, and the network must conserve power across every transfer.

### 5.1 Validation at Each Hop

When a Carrier receives a parcel, it performs these checks **in order**:

| Check | Action on Failure |
|-------|-------------------|
| 1. Parse: valid JSON, required fields present | Reject. Log `parse_error`. |
| 2. Expiry: `expires_at` > now | Discard. Log `expired`. |
| 3. Hop limit: `hop_count` < `max_hops` | Reject. Log `max_hops_exceeded`. |
| 4. Checksum: recompute and compare to `fence.checksum` | Reject. Log `checksum_mismatch`. |
| 5. Size: `size_bytes` matches actual payload size | Reject. Log `size_mismatch`. |
| 6. Power: remaining budget ≥ estimated next-hop cost | Hold. Log `power_insufficient`. |

### 5.2 Checksum Computation

The checksum is computed over the **raw payload body** (after compression, before encoding):

```
checksum = blake2b(payload_body, digest_size=32)
```

For devices without BLAKE2b support, `sha256` or `crc32` are acceptable alternatives. The algorithm is declared in `fence.checksum_algo`.

### 5.3 Hop Log Integrity

The hop log is append-only. A Carrier that receives a parcel with a hop log entry from itself (loop detection) rejects the parcel with `route_loop_detected`.

### 5.4 Fence Actions

| Outcome | Action | Parcel Retained? |
|---------|--------|-------------------|
| All checks pass | Forward or deliver | Yes (until FENCE_OK from next hop) |
| Expiry | Discard | No |
| Max hops exceeded | Reject, return to sender if possible | Return attempt |
| Checksum mismatch | Reject, do not forward | Held for investigation |
| Size mismatch | Reject | Held for investigation |
| Power insufficient | Hold | Yes (pending power recovery) |
| Route loop | Reject | No |

---

## 6. Offline-First Semantics

### 6.1 No Connectivity Assumption

The protocol never assumes a connection is available. Every operation is local-first:

- **Pack**: Write to local store immediately.
- **Carry**: Attempt forward. If no neighbor, store locally. No error.
- **Receive**: Validate locally. No external calls.

### 6.2 Neighbor Discovery

Carriers discover neighbors opportunistically:

- Broadcast a lightweight beacon (BLE, LoRa, WiFi Direct, or any available transport)
- Listen for beacons from other Carriers
- When a neighbor appears, initiate forwarding for queued parcels

Beacons are transport-agnostic. The protocol doesn't care how nodes find each other — only that they do, eventually.

### 6.3 Connection Events

```
NEIGHBOR_APPEARED → flush urgent queue → flush normal queue → flush deferred queue
NEIGHBOR_DISAPPEARED → resume local storage mode
```

---

## 7. Wattage-Aware Operation

### 7.1 Compression

All payloads are compressed by default. The `compression` field in the envelope declares the algorithm:

| Algorithm | When to Use |
|-----------|-------------|
| `gzip` | Default. Good ratio, moderate CPU cost. |
| `zstd` | When available. Better ratio, lower CPU cost. |
| `none` | For payloads smaller than ~100 bytes where overhead exceeds savings. |

### 7.2 Priority-Based Power Triage

| Power State | Urgent | Normal | Deferred |
|-------------|--------|--------|----------|
| > 50% | Forward | Forward | Forward |
| 20-50% | Forward | Forward | Hold |
| 10-20% | Forward | Hold | Hold |
| < 10% | Forward (critical only) | Hold | Hold |

### 7.3 Power Budget Tracking

The envelope carries a `power_budget_mw` that is decremented at each hop by the actual power used (recorded in `hop_log[].power_used_mw`). When the remaining budget is insufficient for another hop, the parcel is held until the node's power state allows forwarding.

```python
remaining_budget = envelope.power_budget_mw
for hop in fence.hop_log:
    remaining_budget -= hop.power_used_mw

if remaining_budget < estimated_next_hop_cost_mw:
    hold_parcel(reason="power_insufficient")
```

---

## 8. Route Planning

### 8.1 Route Structure

A route is an ordered list of waypoints. Each waypoint represents a node (or node type) where a parcel may rest:

```json
{
  "id": "route-east-west-001",
  "waypoints": [
    {"node": "sensor-alpha", "hop_cost_mw": 0},
    {"node": "relay-1", "hop_cost_mw": 15},
    {"node": "relay-2", "hop_cost_mw": 20},
    {"node": "gateway-beta", "hop_cost_mw": 10}
  ],
  "total_cost_mw": 45,
  "estimated_transit_hours": 96
}
```

### 8.2 Dynamic Routing

Routes are advisory, not mandatory. A Carrier may deviate from the planned route if:

- A planned waypoint is unreachable for longer than the parcel's TTL
- A better path is discovered via neighbor beacons
- The power budget won't cover the planned route

When deviating, the original route is preserved in the hop log for auditability.

### 8.3 Last-Mile Delivery

The final waypoint is the destination node. When a Carrier receives a parcel where `envelope.destination == self.node_id`, the parcel is delivered locally and removed from the forwarding queue.

---

## 9. Transport Agnosticism

The Carry Protocol does not specify the physical or link-layer transport. It works over:

- Bluetooth Low Energy (BLE)
- LoRa / LoRaWAN
- WiFi Direct / WiFi Aware
- NFC
- Serial / USB
- Acoustic coupling
- Physical media (SD card, USB drive carried between locations)

The only requirement: the transport can move a JSON document (or equivalent byte sequence) from one node to another, eventually.

---

## 10. Security Considerations

The base protocol provides **integrity** (fence checksums) but **not confidentiality** or **authentication**. These are layered on top when the transport and device capabilities allow:

- **Encryption**: Payload may be encrypted before packing. The envelope is always readable for routing.
- **Authentication**: Hop log entries may include signatures when devices have key material.
- **Tamper detection**: Checksum validation catches accidental corruption. Intentional tampering requires cryptographic signatures (out of scope for v1).

Future versions may define optional security extensions.

---

## 11. Protocol Versioning

The `version` field in the envelope indicates the protocol version. This specification defines version 1.

Receivers MUST reject parcels with unsupported versions and log `unsupported_version`.

---

*"We carry."*
