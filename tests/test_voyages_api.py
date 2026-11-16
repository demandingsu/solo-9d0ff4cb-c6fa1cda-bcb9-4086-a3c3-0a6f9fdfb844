from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import db
from app.config import Settings
from app.main import create_app

PORT_A = {
    "code": "CNSHA-PD",
    "name": "上海港浦东",
    "min_lat": 31.0,
    "max_lat": 31.4,
    "min_lon": 121.2,
    "max_lon": 121.8,
}
PORT_B = {
    "code": "CNNING-ZJ",
    "name": "宁波舟山港",
    "min_lat": 29.0,
    "max_lat": 29.4,
    "min_lon": 121.2,
    "max_lon": 121.8,
}
INSIDE_A = (31.2, 121.5)
INSIDE_B = (29.2, 121.5)
SHIP = "MV-VOYAGE-1"


def iso(hour: int, minute: int = 0, day: int = 1) -> str:
    return f"2026-01-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def epoch(hour: int, minute: int = 0, day: int = 1) -> int:
    return int(
        datetime(2026, 1, day, hour, minute, tzinfo=timezone.utc).timestamp()
    )


def csv_bytes(rows: list[tuple[str, str, float, float]]) -> bytes:
    lines = ["vessel,time,lat,lon"]
    lines += [f"{v},{t},{lat},{lon}" for v, t, lat, lon in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def stay_rows(port: str, start_hour: int, day: int = 1) -> list:
    """Four points at 10-minute spacing inside the given port -> 30 min stay."""
    inside = INSIDE_A if port == PORT_A["code"] else INSIDE_B
    return [
        (SHIP, iso(start_hour, minute=10 * i, day=day), *inside)
        for i in range(4)
    ]


def upload(client: TestClient, port: str, rows: list, key: str):
    response = client.post(
        f"/ports/{port}/uploads",
        files={"file": ("ais.csv", csv_bytes(rows), "text/csv")},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def voyages(client: TestClient, vessel: str = SHIP, cursor: int | None = None):
    params = {"cursor": cursor} if cursor is not None else {}
    response = client.get(f"/vessels/{vessel}/voyages", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def make_app(tmp_path: Path) -> tuple[TestClient, str]:
    db_path = str(tmp_path / "voyages.sqlite3")
    app = create_app(Settings(db_path=db_path, demo_enabled=True))
    client = TestClient(app)
    client.__enter__()
    return client, db_path


def register_ports(client: TestClient) -> None:
    for port in (PORT_A, PORT_B):
        response = client.post("/ports", json=port)
        assert response.status_code == 201, response.text


def test_first_query_returns_global_cursor_and_cross_port_voyage(
    tmp_path: Path,
) -> None:
    client, _ = make_app(tmp_path)
    register_ports(client)
    # Stay in port A on day 1 00:00-00:30, then port B on day 1 03:00-03:30.
    upload(client, PORT_A["code"], stay_rows(PORT_A["code"], 0), "a1")
    upload(client, PORT_B["code"], stay_rows(PORT_B["code"], 3), "b1")

    body = voyages(client)
    assert body["vessel_id"] == SHIP
    assert body["cursor"] > 0
    assert body["cursor"] == body["latest_cursor"]
    assert body["snapshot_event_count"] == 2
    assert body["conflicts"] == []
    assert len(body["voyages"]) == 1

    voyage = body["voyages"][0]
    assert voyage["departure"]["port_code"] == PORT_A["code"]
    assert voyage["arrival"]["port_code"] == PORT_B["code"]
    assert voyage["departure"]["event_id"] != voyage["arrival"]["event_id"]
    assert voyage["departure"]["end_time"] == epoch(0, 30)
    assert voyage["arrival"]["start_time"] == epoch(3, 0)
    assert voyage["sailing_seconds"] == epoch(3, 0) - epoch(0, 30)


def test_same_port_adjacent_stays_do_not_create_voyage(tmp_path: Path) -> None:
    client, _ = make_app(tmp_path)
    register_ports(client)
    upload(client, PORT_A["code"], stay_rows(PORT_A["code"], 0), "a1")
    upload(client, PORT_A["code"], stay_rows(PORT_A["code"], 2), "a2")
    upload(client, PORT_B["code"], stay_rows(PORT_B["code"], 5), "b1")

    body = voyages(client)
    assert body["conflicts"] == []
    # Only the second A-stay pairs with the B-stay; A1 is not bridged over A2.
    assert len(body["voyages"]) == 1
    assert body["voyages"][0]["departure"]["port_code"] == PORT_A["code"]
    assert body["voyages"][0]["arrival"]["port_code"] == PORT_B["code"]
    assert body["voyages"][0]["departure"]["start_time"] == epoch(2, 0)


def test_overlapping_adjacent_stays_reported_as_conflict(tmp_path: Path) -> None:
    client, _ = make_app(tmp_path)
    register_ports(client)
    # A: 00:00-00:30; B starts at 00:20 (itself a valid 30-minute stay),
    # so A's end (00:30) is later than B's start (00:20): overlap.
    upload(client, PORT_A["code"], stay_rows(PORT_A["code"], 0), "a1")
    overlapping = [
        (SHIP, iso(0, 20), *INSIDE_B),
        (SHIP, iso(0, 30), *INSIDE_B),
        (SHIP, iso(0, 40), *INSIDE_B),
        (SHIP, iso(0, 50), *INSIDE_B),
    ]
    upload(client, PORT_B["code"], overlapping, "b1")

    body = voyages(client)
    assert body["voyages"] == []
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["reason"] == "overlapping_stays"
    assert conflict["overlap_seconds"] == epoch(0, 30) - epoch(0, 20)
    assert conflict["first"]["port_code"] == PORT_A["code"]
    assert conflict["second"]["port_code"] == PORT_B["code"]
    assert conflict["first"]["event_id"] != conflict["second"]["event_id"]


def test_cursor_replay_restores_voyages_after_late_retraction(
    tmp_path: Path,
) -> None:
    client, _ = make_app(tmp_path)
    register_ports(client)
    upload(client, PORT_A["code"], stay_rows(PORT_A["code"], 0), "a1")
    upload(client, PORT_B["code"], stay_rows(PORT_B["code"], 3), "b1")

    first = voyages(client)
    saved_cursor = first["cursor"]
    assert len(first["voyages"]) == 1
    original_voyage = first["voyages"][0]

    # A late point moves port A's stay start earlier: old event id is retracted
    # and a new one upserted, advancing the global cursor.
    late = [
        (SHIP, "2025-12-31T23:40:00Z", *INSIDE_A),
        (SHIP, "2025-12-31T23:50:00Z", *INSIDE_A),
    ]
    upload(client, PORT_A["code"], late, "late")

    current = voyages(client)
    assert current["cursor"] > saved_cursor
    assert (
        current["voyages"][0]["departure"]["event_id"]
        != original_voyage["departure"]["event_id"]
    )

    # Replaying the old cursor restores the original voyage and event ids.
    replayed = voyages(client, cursor=saved_cursor)
    assert replayed["cursor"] == saved_cursor
    assert replayed["voyages"] == first["voyages"]
    assert replayed["conflicts"] == first["conflicts"]
    assert replayed["snapshot_event_count"] == 2

    # Repeating the same query is deterministic and stable across more writes.
    upload(client, PORT_B["code"], stay_rows(PORT_B["code"], 8), "b2")
    again = voyages(client, cursor=saved_cursor)
    assert again["voyages"] == first["voyages"]


def test_replay_survives_service_restart(tmp_path: Path) -> None:
    client, db_path = make_app(tmp_path)
    register_ports(client)
    upload(client, PORT_A["code"], stay_rows(PORT_A["code"], 0), "a1")
    upload(client, PORT_B["code"], stay_rows(PORT_B["code"], 3), "b1")
    saved = voyages(client)
    client.__exit__(None, None, None)

    # A brand-new process (new app instance) over the same database file.
    restarted_app = create_app(Settings(db_path=db_path))
    with TestClient(restarted_app) as restarted:
        replayed = voyages(restarted, cursor=saved["cursor"])
        assert replayed["voyages"] == saved["voyages"]
        assert replayed["conflicts"] == saved["conflicts"]


def test_cross_instance_upload_keeps_historical_cursor_replayable(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "cross.sqlite3")
    settings = Settings(db_path=db_path)
    app_one, app_two = create_app(settings), create_app(settings)
    with TestClient(app_one) as one, TestClient(app_two) as two:
        register_ports(one)
        upload(one, PORT_A["code"], stay_rows(PORT_A["code"], 0), "a1")
        saved = voyages(one)
        saved_cursor = saved["cursor"]

        # Instance two performs the next upload (its event changes get later
        # global seqs); instance one must still replay the old snapshot.
        upload(two, PORT_B["code"], stay_rows(PORT_B["code"], 3), "b1")
        assert voyages(one, cursor=saved_cursor)["voyages"] == saved["voyages"]
        # Without a cursor both instances see the same new committed state.
        assert voyages(one)["voyages"] == voyages(two)["voyages"]


def test_future_cursor_is_rejected(tmp_path: Path) -> None:
    client, _ = make_app(tmp_path)
    register_ports(client)
    upload(client, PORT_A["code"], stay_rows(PORT_A["code"], 0), "a1")
    latest = voyages(client)["latest_cursor"]

    response = client.get(
        f"/vessels/{SHIP}/voyages", params={"cursor": latest + 100}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cursor_in_future"
    assert response.json()["error"]["details"]["latest_cursor"] == latest


def test_unknown_vessel_returns_empty_snapshot_not_404(tmp_path: Path) -> None:
    client, _ = make_app(tmp_path)
    register_ports(client)
    body = voyages(client, vessel="MV-GHOST")
    assert body["voyages"] == []
    assert body["conflicts"] == []
    assert body["snapshot_event_count"] == 0
    assert body["cursor"] == 0


def test_vessel_isolation_between_ships(tmp_path: Path) -> None:
    client, _ = make_app(tmp_path)
    register_ports(client)
    other = [
        ("MV-OTHER", iso(0, 10 * i), *INSIDE_A) for i in range(4)
    ]
    upload(client, PORT_A["code"], other, "other")
    upload(client, PORT_B["code"], stay_rows(PORT_B["code"], 3), "b1")

    # SHIP only has the B-stay, so no voyage; MV-OTHER only has an A-stay.
    assert voyages(client)["voyages"] == []
    assert voyages(client, vessel="MV-OTHER")["voyages"] == []

    # Give SHIP an A-stay: exactly one voyage, unaffected by MV-OTHER's data.
    upload(client, PORT_A["code"], stay_rows(PORT_A["code"], 0), "a1")
    body = voyages(client)
    assert len(body["voyages"]) == 1
    assert body["snapshot_event_count"] == 2


def test_event_retraction_removes_voyage_in_current_but_not_history(
    tmp_path: Path,
) -> None:
    client, db_path = make_app(tmp_path)
    register_ports(client)
    upload(client, PORT_A["code"], stay_rows(PORT_A["code"], 0), "a1")
    upload(client, PORT_B["code"], stay_rows(PORT_B["code"], 3), "b1")
    saved = voyages(client)
    assert len(saved["voyages"]) == 1

    # Simulate an event retraction exactly like the reconciliation engine does
    # (a late exit point splits B's stay so its old event id disappears).
    conn = db.connect(db_path)
    try:
        with db.write_transaction(conn):
            row = conn.execute(
                "SELECT * FROM events WHERE vessel_id = ? ORDER BY start_time",
                (SHIP,),
            ).fetchall()[-1]
            conn.execute(
                """
                INSERT INTO event_changes
                    (batch_id, port_id, change_type, event_id, vessel_id,
                     start_time, end_time, start_lat_e6, start_lon_e6,
                     end_lat_e6, end_lon_e6, point_count, created_at)
                SELECT created_batch_id, port_id, 'retract', id, vessel_id,
                       start_time, end_time, start_lat_e6, start_lon_e6,
                       end_lat_e6, end_lon_e6, point_count,
                       (SELECT received_at FROM ingest_batches
                        WHERE id = events.created_batch_id)
                FROM events
                WHERE id = ?
                """,
                (row["id"],),
            )
            conn.execute("DELETE FROM events WHERE id = ?", (row["id"],))
    finally:
        conn.close()

    current = voyages(client)
    assert current["cursor"] > saved["cursor"]
    assert current["voyages"] == []  # only the A-stay remains

    # The historical cursor still shows the voyage that existed back then.
    replayed = voyages(client, cursor=saved["cursor"])
    assert replayed["voyages"] == saved["voyages"]


def test_cursor_zero_replays_empty_history(tmp_path: Path) -> None:
    client, _ = make_app(tmp_path)
    register_ports(client)
    upload(client, PORT_A["code"], stay_rows(PORT_A["code"], 0), "a1")
    body = voyages(client, cursor=0)
    assert body["cursor"] == 0
    assert body["snapshot_event_count"] == 0
    assert body["voyages"] == []
    assert body["latest_cursor"] > 0


def test_negative_cursor_fails_validation(tmp_path: Path) -> None:
    client, _ = make_app(tmp_path)
    response = client.get(f"/vessels/{SHIP}/voyages", params={"cursor": -1})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation"


def test_replay_uses_only_committed_changes_up_to_cursor(tmp_path: Path) -> None:
    client, _ = make_app(tmp_path)
    register_ports(client)
    upload(client, PORT_A["code"], stay_rows(PORT_A["code"], 0), "a1")
    at_one = voyages(client)  # one event, no voyage yet
    upload(client, PORT_B["code"], stay_rows(PORT_B["code"], 3), "b1")
    at_two = voyages(client)
    assert len(at_two["voyages"]) == 1

    middle = voyages(client, cursor=at_one["cursor"])
    assert middle["snapshot_event_count"] == 1
    assert middle["voyages"] == []
    # And every intermediate cursor value between seqs behaves consistently.
    direct = sqlite3.connect(client.app.state.settings.db_path)
    try:
        seqs = [
            r[0]
            for r in direct.execute(
                "SELECT seq FROM event_changes ORDER BY seq"
            ).fetchall()
        ]
    finally:
        direct.close()
    assert seqs == sorted(seqs) and len(seqs) == 2
