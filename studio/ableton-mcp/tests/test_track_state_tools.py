"""
Server-side tests for the track-state tools (get_track_meters, set_track_arm,
set_track_monitoring, get/set_track_input_routing, delete_tracks).

The Ableton socket is mocked, so these run anywhere. The focus is the safety
behaviour of delete_tracks, since it is the one tool that destroys user work.

    uv run pytest tests/test_track_state_tools.py -v
"""

import json

import pytest

import MCP_Server.server as server

SET = [
    {"index": 0, "name": "Drums", "group": None, "is_group": True},
    {"index": 1, "name": "Kick", "group": "Drums", "is_group": False},
    {"index": 2, "name": "Snare", "group": "Drums", "is_group": False},
    {"index": 3, "name": "Lead", "group": None, "is_group": False},
]


class FakeConnection:
    def __init__(self):
        self.sent = []

    def send_command(self, command_type, params=None):
        self.sent.append((command_type, params))
        if command_type == "get_track_meters":
            return {"tracks": SET, "returns": [], "master": {}}
        return {"ok": command_type}

    def commands(self):
        return [c for c, _ in self.sent]


@pytest.fixture
def conn(monkeypatch):
    fake = FakeConnection()
    monkeypatch.setattr(server, "get_ableton_connection", lambda: fake)
    monkeypatch.setattr(server, "_capability_guard", lambda name: None)
    return fake


def test_delete_defaults_to_preview_and_never_deletes(conn):
    out = server.delete_tracks(None, [{"index": 3, "name": "Lead"}])
    assert "PREVIEW" in out and "Lead" in out
    assert "delete_tracks" not in conn.commands()


def test_delete_refuses_when_a_name_does_not_match(conn):
    out = server.delete_tracks(None, [{"index": 3, "name": "Pad"}], confirm=True)
    assert "Nothing deleted" in out and "expected 'Pad', found 'Lead'" in out
    assert "delete_tracks" not in conn.commands()


def test_delete_refuses_unknown_index(conn):
    out = server.delete_tracks(None, [{"index": 9, "name": "Ghost"}], confirm=True)
    assert "no such track" in out
    assert "delete_tracks" not in conn.commands()


def test_deleting_a_group_warns_about_its_members(conn):
    preview = server.delete_tracks(None, [{"index": 0, "name": "Drums"}])
    assert "WARNING" in preview and "Kick" in preview and "Snare" in preview
    blocked = server.delete_tracks(None, [{"index": 0, "name": "Drums"}], confirm=True)
    assert "Nothing deleted" in blocked
    assert "delete_tracks" not in conn.commands()


def test_group_with_all_members_listed_is_allowed(conn):
    tracks = [{"index": 0, "name": "Drums"}, {"index": 1, "name": "Kick"}, {"index": 2, "name": "Snare"}]
    server.delete_tracks(None, tracks, confirm=True)
    deletes = [p for c, p in conn.sent if c == "delete_tracks"]
    # one track per call (a bulk call once failed half way in Live), and the group goes first
    assert all(len(p["tracks"]) == 1 for p in deletes)
    assert deletes[0] == {"tracks": [{"index": 0, "name": "Drums"}]}


def test_confirmed_delete_sends_the_command(conn):
    tracks = [{"index": 3, "name": "Lead"}]
    server.delete_tracks(None, tracks, confirm=True)
    assert ("delete_tracks", {"tracks": tracks}) in conn.sent


def test_entries_need_both_index_and_name(conn):
    assert "both" in server.delete_tracks(None, [{"index": 3}], confirm=True)
    assert conn.sent == []


@pytest.mark.parametrize("state,expected", [("in", 0), ("Auto", 1), (" OFF ", 2)])
def test_monitoring_state_names(conn, state, expected):
    server.set_track_monitoring(None, 1, state)
    assert conn.sent[-1] == ("set_track_monitoring", {"track_index": 1, "state": expected})


def test_monitoring_rejects_unknown_state(conn):
    assert "must be one of" in server.set_track_monitoring(None, 1, "loud")
    assert conn.sent == []


def test_input_routing_needs_something_to_set(conn):
    assert "type, channel" in server.set_track_input_routing(None, 1)
    assert conn.sent == []


def test_input_routing_sends_only_given_fields(conn):
    server.set_track_input_routing(None, 2, channel="3-hat")
    assert conn.sent[-1] == ("set_track_input_routing", {"track_index": 2, "channel": "3-hat"})
    server.set_track_input_routing(None, 2, input_type="Live")
    assert conn.sent[-1] == ("set_track_input_routing", {"track_index": 2, "type": "Live"})


def test_arm_and_meters_pass_through(conn):
    server.set_track_arm(None, 4, arm=False)
    assert conn.sent[-1] == ("set_track_arm", {"track_index": 4, "arm": False})
    assert json.loads(server.get_track_meters(None))["tracks"] == SET


def test_old_script_without_capability_gets_a_clear_message(monkeypatch):
    monkeypatch.setattr(server, "_capability_guard", lambda name: f"script lacks {name}")
    monkeypatch.setattr(server, "get_ableton_connection", lambda: pytest.fail("must not connect"))
    assert server.get_track_meters(None) == "script lacks get_track_meters"
    assert server.delete_tracks(None, [{"index": 1, "name": "Kick"}], confirm=True) == "script lacks delete_tracks"
