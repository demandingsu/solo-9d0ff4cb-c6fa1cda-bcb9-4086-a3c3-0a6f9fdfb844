"""Inter-port voyage derivation from a vessel's port-stay events.

A voyage connects two *adjacent* stays of the same vessel in different ports.
The input is an immutable snapshot of the vessel's events as of one committed
change cursor; reconstructing it from the append-only change log makes the
result reproducible long after later uploads, retractions or restarts.

Pairing rules (operational contract):

* Stays are ordered by ``(start_time, event_id)`` and only consecutive stays
  are ever paired.
* Two consecutive stays in the **same port** describe no inter-port movement:
  no voyage is emitted and the pair is not bridged — the second stay is simply
  paired with whatever follows it.
* Two consecutive stays whose time ranges overlap
  (``a.end_time > b.start_time``) cannot be a real sequence of port calls:
  a vessel cannot depart one port before it arrives at the next.  The pair is
  reported as a conflict and no voyage is generated for it.  Same-port
  overlaps are conflicts too, because the data itself is inconsistent.
* Otherwise a voyage is emitted, carrying both event ids, both ports and the
  elapsed time from the departure (end of the first stay) to the arrival
  (start of the next stay).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# An event snapshot, as reconstructed from ``event_changes``.  ``event_id``
# disambiguates events sharing a start time so ordering stays deterministic.
SNAPSHOT_FIELDS = (
    "event_id",
    "port_id",
    "vessel_id",
    "start_time",
    "end_time",
)


@dataclass(frozen=True)
class Voyage:
    order: int
    departure_event_id: int
    arrival_event_id: int
    departure_port_id: int
    arrival_port_id: int
    departure_port_code: str
    arrival_port_code: str
    # Full stay windows; the voyage departs at the end of the first stay and
    # arrives at the start of the second one.
    departure_start_time: int
    departure_end_time: int
    arrival_start_time: int
    arrival_end_time: int

    @property
    def sailing_seconds(self) -> int:
        return self.arrival_start_time - self.departure_end_time


@dataclass(frozen=True)
class Conflict:
    order: int
    first_event_id: int
    second_event_id: int
    first_port_id: int
    second_port_id: int
    first_port_code: str
    second_port_code: str
    first_start_time: int
    first_end_time: int
    second_start_time: int
    second_end_time: int
    overlap_seconds: int
    reason: str


def _event_ref(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "port_id": event["port_id"],
        "port_code": event["port_code"],
        "start_time": event["start_time"],
        "end_time": event["end_time"],
    }


def voyage_dict(voyage: Voyage) -> dict[str, Any]:
    return {
        "order": voyage.order,
        "departure": _event_ref({
            "event_id": voyage.departure_event_id,
            "port_id": voyage.departure_port_id,
            "port_code": voyage.departure_port_code,
            "start_time": voyage.departure_start_time,
            "end_time": voyage.departure_end_time,
        }),
        "arrival": _event_ref({
            "event_id": voyage.arrival_event_id,
            "port_id": voyage.arrival_port_id,
            "port_code": voyage.arrival_port_code,
            "start_time": voyage.arrival_start_time,
            "end_time": voyage.arrival_end_time,
        }),
        "sailing_seconds": voyage.sailing_seconds,
    }


def conflict_dict(conflict: Conflict) -> dict[str, Any]:
    return {
        "order": conflict.order,
        "reason": conflict.reason,
        "overlap_seconds": conflict.overlap_seconds,
        "first": {
            **_event_ref({
                "event_id": conflict.first_event_id,
                "port_id": conflict.first_port_id,
                "port_code": conflict.first_port_code,
                "start_time": conflict.first_start_time,
                "end_time": conflict.first_end_time,
            }),
        },
        "second": {
            **_event_ref({
                "event_id": conflict.second_event_id,
                "port_id": conflict.second_port_id,
                "port_code": conflict.second_port_code,
                "start_time": conflict.second_start_time,
                "end_time": conflict.second_end_time,
            }),
        },
    }


def build_voyages(
    events: Sequence[Mapping[str, Any]],
    port_code_by_id: Mapping[int, str],
) -> tuple[list[Voyage], list[Conflict]]:
    """Pair adjacent stays into voyages and overlap conflicts.

    Every event mapping must provide ``event_id``, ``port_id``,
    ``start_time`` and ``end_time`` (see :data:`SNAPSHOT_FIELDS`);
    ``port_code`` is optional and, when absent, resolved through
    ``port_code_by_id``.  Events need not arrive sorted — they are ordered
    here by ``(start_time, event_id)``.
    """
    ordered = sorted(events, key=lambda e: (e["start_time"], e["event_id"]))

    def code_of(event: Mapping[str, Any]) -> str:
        code = event.get("port_code")
        if code is not None:
            return str(code)
        return port_code_by_id[event["port_id"]]

    voyages: list[Voyage] = []
    conflicts: list[Conflict] = []
    for index in range(len(ordered) - 1):
        first, second = ordered[index], ordered[index + 1]
        order = index + 1

        # Overlapping time ranges make the pair impossible regardless of port.
        overlap = first["end_time"] - second["start_time"]
        if overlap > 0:
            conflicts.append(
                Conflict(
                    order=order,
                    first_event_id=first["event_id"],
                    second_event_id=second["event_id"],
                    first_port_id=first["port_id"],
                    second_port_id=second["port_id"],
                    first_port_code=code_of(first),
                    second_port_code=code_of(second),
                    first_start_time=first["start_time"],
                    first_end_time=first["end_time"],
                    second_start_time=second["start_time"],
                    second_end_time=second["end_time"],
                    overlap_seconds=overlap,
                    reason=(
                        "same_port_overlap"
                        if first["port_id"] == second["port_id"]
                        else "overlapping_stays"
                    ),
                )
            )
            continue

        # Same port, no overlap: an intra-port sequence, never an inter-port
        # voyage.  Do not skip the second stay — it still pairs with the next.
        if first["port_id"] == second["port_id"]:
            continue

        voyages.append(
            Voyage(
                order=order,
                departure_event_id=first["event_id"],
                arrival_event_id=second["event_id"],
                departure_port_id=first["port_id"],
                arrival_port_id=second["port_id"],
                departure_port_code=code_of(first),
                arrival_port_code=code_of(second),
                departure_start_time=first["start_time"],
                departure_end_time=first["end_time"],
                arrival_start_time=second["start_time"],
                arrival_end_time=second["end_time"],
            )
        )
    return voyages, conflicts
