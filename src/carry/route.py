"""
Route planning with offline waypoints.

A route is an ordered sequence of waypoints — nodes where a parcel may rest
until the next leg becomes available. Routes are advisory, not mandatory.
A carrier may deviate when waypoints are unreachable or better paths appear.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Waypoint:
    """
    A node on a planned route where a parcel may rest.

    Like a relay station on the high route — the parcel waits there
    until the next carrier arrives and conditions allow the carry.
    """
    node: str
    hop_cost_mw: int = 0  # Estimated power cost to reach this waypoint
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "hop_cost_mw": self.hop_cost_mw,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Waypoint:
        return cls(
            node=d["node"],
            hop_cost_mw=d.get("hop_cost_mw", 0),
            metadata=d.get("metadata", {}),
        )


@dataclass
class Route:
    """
    An ordered sequence of waypoints for parcel delivery.

    Named after the cairn-marked paths across the Divide — advisory
    markers that guide the way but yield to conditions on the ground.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    waypoints: list[Waypoint] = field(default_factory=list)
    estimated_transit_hours: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_waypoints(
        cls,
        waypoints: list[Waypoint | str],
        estimated_transit_hours: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> Route:
        """Create a route from a list of waypoints (or node ID strings)."""
        wps = []
        for wp in waypoints:
            if isinstance(wp, str):
                wps.append(Waypoint(node=wp))
            else:
                wps.append(wp)
        return cls(
            waypoints=wps,
            estimated_transit_hours=estimated_transit_hours,
            metadata=metadata or {},
        )

    @property
    def total_cost_mw(self) -> int:
        """Total estimated power cost across all hops."""
        return sum(wp.hop_cost_mw for wp in self.waypoints)

    @property
    def hop_count(self) -> int:
        """Number of hops in the route (waypoints - 1)."""
        return max(0, len(self.waypoints) - 1)

    def next_waypoint(self, current_node: str) -> Waypoint | None:
        """
        Find the next waypoint after the current node.

        Returns None if the current node is the last waypoint (destination).
        """
        for i, wp in enumerate(self.waypoints):
            if wp.node == current_node and i < len(self.waypoints) - 1:
                return self.waypoints[i + 1]
        return None

    def is_destination(self, node_id: str) -> bool:
        """Check if a node is the final waypoint."""
        return bool(self.waypoints) and self.waypoints[-1].node == node_id

    def subroute_from(self, node_id: str) -> Route | None:
        """
        Return the sub-route starting from the given node.

        Returns None if the node is not in the route.
        """
        for i, wp in enumerate(self.waypoints):
            if wp.node == node_id:
                return Route(
                    waypoints=self.waypoints[i:],
                    estimated_transit_hours=self.estimated_transit_hours,
                    metadata={**self.metadata, "parent_route": self.id},
                )
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "waypoints": [wp.to_dict() for wp in self.waypoints],
            "total_cost_mw": self.total_cost_mw,
            "estimated_transit_hours": self.estimated_transit_hours,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Route:
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            waypoints=[Waypoint.from_dict(w) for w in d.get("waypoints", [])],
            estimated_transit_hours=d.get("estimated_transit_hours", 0.0),
            metadata=d.get("metadata", {}),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_json(cls, j: str | bytes) -> Route:
        if isinstance(j, bytes):
            j = j.decode("utf-8")
        return cls.from_dict(json.loads(j))
