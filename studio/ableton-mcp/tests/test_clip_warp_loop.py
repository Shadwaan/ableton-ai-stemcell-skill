"""
Server-side tests for set_clip_warp_loop and the Remote Script method behind it.

The Ableton socket is mocked and the Remote Script method is run against fake Live clips, so these run anywhere.

    uv run pytest tests/test_clip_warp_loop.py -v
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

import MCP_Server.server as server

BUNDLED_SCRIPT = (Path(__file__).resolve().parents[1] / "MCP_Server" / "bundled_ableton_remote_script"
                  / "AbletonMCP_init.py")


def _load_script_module():
    """The Live-side script with Live's _Framework stubbed out (same approach as test_remote_script_bridge.py)."""
    framework = types.ModuleType("_Framework")
    cs_mod = types.ModuleType("_Framework.ControlSurface")

    class _Stub(object):
        def __init__(self, *a, **k):
            pass

        def log_message(self, *a, **k):
            pass

        def show_message(self, *a, **k):
            pass

    cs_mod.ControlSurface = _Stub
    framework.ControlSurface = cs_mod
    sys.modules.setdefault("_Framework", framework)
    sys.modules.setdefault("_Framework.ControlSurface", cs_mod)
    spec = importlib.util.spec_from_file_location("warp_loop_script_under_test", BUNDLED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AbletonMCP = _load_script_module().AbletonMCP


class FakeConnection:
    def __init__(self):
        self.sent = []

    def send_command(self, command_type, params=None):
        self.sent.append((command_type, params))
        return {"ok": True}


@pytest.fixture
def conn(monkeypatch):
    fake = FakeConnection()
    monkeypatch.setattr(server, "get_ableton_connection", lambda: fake)
    monkeypatch.setattr(server, "_capability_guard", lambda name: None)
    return fake


def test_defaults_are_warp_complex_loop(conn):
    server.set_clip_warp_loop(None, [{"track_index": 2, "clip_index": 0}])
    cmd, p = conn.sent[0]
    assert cmd == "set_clip_warp_loop"
    assert p["warping"] is True and p["warp_mode"] == 4 and p["looping"] is True and p["beats"] is None


def test_mode_names_map_to_live_values(conn):
    server.set_clip_warp_loop(None, [{"track_index": 0, "clip_index": 0}], warp_mode="Beats", beats=16)
    assert conn.sent[0][1]["warp_mode"] == 0 and conn.sent[0][1]["beats"] == 16


def test_bad_input_never_reaches_live(conn):
    assert "warp_mode must be one of" in server.set_clip_warp_loop(None, [{"track_index": 0, "clip_index": 0}], warp_mode="warpy")
    assert "slots must be" in server.set_clip_warp_loop(None, [])
    assert "slots must be" in server.set_clip_warp_loop(None, [{"track_index": 1}])
    assert conn.sent == []


class FakeClip:
    def __init__(self, audio=True, length=7.0):
        self.is_audio_clip, self.name = audio, "c"
        self.warping, self.warp_mode, self.looping = False, 0, False
        self.start_marker, self.end_marker, self.loop_start, self.loop_end, self.length = 0.0, length, 0.0, length, length


def script_with(tracks):
    s = AbletonMCP.__new__(AbletonMCP)
    s._song = types.SimpleNamespace(tracks=tracks)
    return s


def track(*clips):
    return types.SimpleNamespace(clip_slots=[types.SimpleNamespace(has_clip=c is not None, clip=c) for c in clips])


def test_remote_script_warps_loops_and_sizes_the_range():
    clip = FakeClip(length=7.0)
    out = script_with([track(clip)])._set_clip_warp_loop([{"track_index": 0, "clip_index": 0}], True, 4, True, 16)
    r = out["results"][0]
    assert out["ok"] and r["warping"] and r["warp_mode"] == 4 and r["looping"]
    assert (r["loop_start"], r["loop_end"], r["start_marker"], r["end_marker"]) == (0.0, 16.0, 0.0, 16.0)


def test_remote_script_narrows_a_range_without_inverting_it():
    clip = FakeClip(length=32.0)
    out = script_with([track(clip)])._set_clip_warp_loop([{"track_index": 0, "clip_index": 0}], True, 4, True, 4)
    assert out["ok"] and clip.loop_end == 4.0 and clip.end_marker == 4.0


def test_one_bad_slot_does_not_hide_the_others():
    good = FakeClip()
    out = script_with([track(good, None, FakeClip(audio=False))])._set_clip_warp_loop(
        [{"track_index": 0, "clip_index": 0}, {"track_index": 0, "clip_index": 1}, {"track_index": 0, "clip_index": 2},
         {"track_index": 9, "clip_index": 0}], True, 4, True, None)
    ok = [r["ok"] for r in out["results"]]
    assert ok == [True, False, False, False] and not out["ok"]
    assert "empty" in out["results"][1]["error"] and "Not an audio" in out["results"][2]["error"]
    assert good.warping and good.looping


def test_beats_without_warping_is_refused():
    out = script_with([track(FakeClip())])._set_clip_warp_loop([{"track_index": 0, "clip_index": 0}], False, None, True, 16)
    assert not out["ok"] and "beats needs warping" in out["results"][0]["error"]
