from __future__ import annotations

import pytest

from app.voyages import build_voyages, conflict_dict, voyage_dict


def ev(event_id, port_id, start, end, code=None):
    return {
        "event_id": event_id,
        "port_id": port_id,
        "port_code": code,
        "start_time": start,
        "end_time": end,
    }


PORTS = {1: "CNSHA", 2: "CNNING"}


def test_cross_port_adjacent_stays_form_one_voyage():
    events = [ev(10, 1, 0, 1800), ev(11, 2, 7200, 9000)]
    voyages, conflicts = build_voyages(events, PORTS)

    assert conflicts == []
    assert len(voyages) == 1
    voyage = voyages[0]
    assert voyage.order == 1
    assert voyage.departure_event_id == 10
    assert voyage.arrival_event_id == 11
    assert voyage.departure_port_code == "CNSHA"
    assert voyage.arrival_port_code == "CNNING"
    # Departure is the end of the first stay, arrival the start of the next.
    assert voyage.departure_start_time == 0
    assert voyage.departure_end_time == 1800
    assert voyage.arrival_start_time == 7200
    assert voyage.arrival_end_time == 9000
    assert voyage.sailing_seconds == 7200 - 1800

    payload = voyage_dict(voyage)
    assert payload["departure"]["event_id"] == 10
    assert payload["departure"]["port_code"] == "CNSHA"
    assert payload["departure"]["start_time"] == 0
    assert payload["departure"]["end_time"] == 1800
    assert payload["arrival"]["event_id"] == 11
    assert payload["arrival"]["start_time"] == 7200
    assert payload["sailing_seconds"] == 5400


def test_same_port_adjacent_emits_nothing_and_does_not_bridge():
    # A1, A2 (same port), B: only A2 -> B is a voyage; A1 is never bridged
    # over A2 straight to B.
    events = [
        ev(1, 1, 0, 1800),
        ev(2, 1, 3600, 5400),
        ev(3, 2, 7200, 9000),
    ]
    voyages, conflicts = build_voyages(events, PORTS)

    assert conflicts == []
    assert len(voyages) == 1
    assert voyages[0].departure_event_id == 2
    assert voyages[0].arrival_event_id == 3
    assert voyages[0].order == 2


def test_overlapping_stays_are_conflicts_and_produce_no_voyage():
    # First stay ends at 3600, second starts at 1800: overlap of 1800 s.
    events = [ev(1, 1, 0, 3600), ev(2, 2, 1800, 3600)]
    voyages, conflicts = build_voyages(events, PORTS)

    assert voyages == []
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.reason == "overlapping_stays"
    assert conflict.overlap_seconds == 1800
    assert conflict.first_event_id == 1
    assert conflict.second_event_id == 2

    payload = conflict_dict(conflict)
    assert payload["first"]["event_id"] == 1
    assert payload["second"]["port_code"] == "CNNING"
    assert payload["overlap_seconds"] == 1800


def test_same_port_overlap_is_also_a_conflict():
    events = [ev(1, 1, 0, 3600), ev(2, 1, 1800, 3600)]
    voyages, conflicts = build_voyages(events, PORTS)

    assert voyages == []
    assert len(conflicts) == 1
    assert conflicts[0].reason == "same_port_overlap"
    assert conflicts[0].overlap_seconds == 1800


def test_touching_stays_do_not_overlap_and_zero_length_voyage_is_valid():
    # end of first == start of second: no overlap, sailing time exactly zero.
    events = [ev(1, 1, 0, 1800), ev(2, 2, 1800, 3600)]
    voyages, conflicts = build_voyages(events, PORTS)

    assert conflicts == []
    assert len(voyages) == 1
    assert voyages[0].sailing_seconds == 0


def test_conflict_does_not_block_later_pairing():
    # A and B overlap; A2 later still pairs with B.
    events = [
        ev(1, 1, 0, 3600),
        ev(2, 2, 1800, 3600),
        ev(3, 1, 7200, 9000),
    ]
    voyages, conflicts = build_voyages(events, PORTS)

    assert len(conflicts) == 1
    assert len(voyages) == 1
    assert voyages[0].departure_event_id == 2
    assert voyages[0].arrival_event_id == 3


def test_events_are_sorted_by_start_time_then_event_id():
    # Deliberately shuffled input; each start time is unique (the events table
    # enforces one event per vessel+start time, so real snapshots never tie).
    events = [
        ev(30, 2, 7200, 9000),
        ev(20, 1, 3600, 5400),
        ev(10, 2, 0, 1800),
    ]
    voyages, _ = build_voyages(events, PORTS)

    assert [v.departure_event_id for v in voyages] == [10, 20]
    assert [v.arrival_event_id for v in voyages] == [20, 30]


def test_event_id_breaks_ties_on_identical_start_time():
    # Defensive: an identical start time (only reachable via hand-built data)
    # must still order deterministically by event id.  Such pairs overlap, so
    # they surface as conflicts rather than voyages.
    events = [
        ev(20, 2, 0, 1800),
        ev(10, 1, 0, 1800),
    ]
    _, conflicts = build_voyages(events, PORTS)
    assert len(conflicts) == 1
    assert conflicts[0].first_event_id == 10
    assert conflicts[0].second_event_id == 20


def test_fewer_than_two_events_yields_nothing():
    assert build_voyages([], PORTS) == ([], [])
    assert build_voyages([ev(1, 1, 0, 1800)], PORTS) == ([], [])


def test_port_code_resolved_from_mapping_when_absent():
    events = [
        {"event_id": 1, "port_id": 1, "start_time": 0, "end_time": 1800},
        {"event_id": 2, "port_id": 2, "start_time": 3600, "end_time": 5400},
    ]
    voyages, _ = build_voyages(events, PORTS)
    assert voyages[0].departure_port_code == "CNSHA"
    assert voyages[0].arrival_port_code == "CNNING"


def test_unknown_port_id_in_mapping_is_a_programming_error():
    events = [ev(1, 99, 0, 1800), ev(2, 2, 3600, 5400)]
    with pytest.raises(KeyError):
        build_voyages(events, PORTS)
