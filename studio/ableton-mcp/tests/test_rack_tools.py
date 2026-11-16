"""
Tests for the rack / device-path tools (Remote Script 1.8.0).

Part 1 runs the Remote Script's rack code against a fake Live object model (no Live needed), so
path walking, bounds errors and the edit calls are checked. It does NOT prove Live accepts the
calls: that needs the Live session check in docs of the PR (introspect, then a real rack).
Part 2 checks the MCP tools forward the right command and params.

    uv run pytest tests/test_rack_tools.py -v
"""

import importlib.util
import json
import queue
import sys
import types
from pathlib import Path

import pytest

import MCP_Server.server as server

SCRIPT = Path(__file__).resolve().parent.parent / "AbletonMCP_Remote_Script" / "__init__.py"


def load_script():
    fw = types.ModuleType("_Framework")
    cs = types.ModuleType("_Framework.ControlSurface")
    cs.ControlSurface = type("ControlSurface", (), {})
    sys.modules.setdefault("_Framework", fw)
    sys.modules["_Framework.ControlSurface"] = cs
    spec = importlib.util.spec_from_file_location("abl_script_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["abl_script_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class Param:
    def __init__(self, name, value=0.0, lo=0.0, hi=1.0):
        self.name, self.value, self.min, self.max = name, value, lo, hi
        self.value_string = str(value)


class Mixer:
    def __init__(self):
        self.volume, self.panning = Param("Volume", 0.85), Param("Pan", 0.0, -1.0, 1.0)
        self.sends = [Param("A", 0.0), Param("B", 0.0)]


class Container:
    """Track or chain: holds devices; delete/insert like Live's Track/Chain."""

    def __init__(self, name="c"):
        self.name = name
        self.devices = []
        self.mixer_device = Mixer()
        self.mute = self.solo = False
        self.color_index = 0

    def insert_device(self, device_name, target_index=None):
        d = Device(device_name)
        if target_index is None:
            self.devices.append(d)
        else:
            self.devices.insert(target_index, d)

    def delete_device(self, index):
        del self.devices[index]


class Device:
    can_have_drum_pads = False
    class_display_name = "Audio Effect"

    def __init__(self, name, rack=False):
        self.name = name
        self.class_name = "AudioEffectGroupDevice" if rack else "Eq8"
        self.can_have_chains = rack
        self.parameters = [Param("Device On", 1.0)] + (
            [Param("Macro %d" % i, 0.0, 0.0, 127.0) for i in range(1, 9)] if rack else [Param("Gain", 0.5)])
        self.chains = []
        self.visible_macro_count = 8
        self.variation_count = 0
        self.is_active = True

    def insert_chain(self, index=None):
        c = Container("Chain")
        self.chains.insert(len(self.chains) if index is None else index, c)

    def add_macro(self):
        self.visible_macro_count += 1


class Song:
    def __init__(self):
        self.tracks = [Container("Sax"), Container("Other")]
        self.return_tracks = [Container("A-Reverb")]
        self.master_track = Container("Master")
        self.view = types.SimpleNamespace(selected_track=None, select_device=lambda d: None)
        self.file_path = "/tmp/x.als"

    def create_return_track(self):
        self.return_tracks.append(Container("B-Return"))

    def move_device(self, device, target, position):
        for t in self.tracks + self.return_tracks + [self.master_track]:
            self._remove(t, device)
        target.devices.insert(position, device)

    def _remove(self, node, device):
        if device in node.devices:
            node.devices.remove(device)
        for d in node.devices:
            for c in d.chains:
                self._remove(c, device)


@pytest.fixture
def script():
    mod = load_script()
    inst = object.__new__(mod.AbletonMCP)
    inst._song = Song()
    inst.log_message = lambda *a: None
    inst._get_device_type = lambda d: "rack" if d.can_have_chains else "audio_effect"
    inst.schedule_message = lambda delay, fn: fn()
    return inst


def test_tree_and_paths(script):
    sax = script._song.tracks[0]
    script._rt_insert_device(0, "track", "Audio Effect Rack")
    sax.devices[0] = Device("Rack", rack=True)
    script._rt_rack_insert_chain(0, "track", [0])
    script._rt_rack_insert_chain(0, "track", [0])
    script._rt_insert_device(0, "track", "EQ Eight", 0, [0, 1])
    script._rt_insert_device(0, "track", "Compressor", -1, [0, 1])

    tree = script._rt_get_device_tree(0)
    rack = tree["devices"][0]
    assert rack["type"] == "rack" and len(rack["chains"]) == 2
    assert [d["name"] for d in rack["chains"][1]["devices"]] == ["EQ Eight", "Compressor"]
    assert rack["macros"][0]["name"] == "Macro 1" and rack["visible_macro_count"] == 8

    r = script._rt_set_device_parameter_at(0, "track", [0, 1, 0], 1, 5.0)
    assert r["value"] == 1.0  # clamped to the parameter's max
    assert script._rt_get_device_parameters_at(0, "track", [0, 1, 1])["device"]["name"] == "Compressor"


def test_move_delete_and_errors(script):
    sax = script._song.tracks[0]
    sax.devices = [Device("Rack", rack=True), Device("Utility")]
    script._rt_rack_insert_chain(0, "track", [0])
    out = script._rt_move_device(0, "track", [1], 0, "track", [0, 0], -1)
    assert out["destination_devices"] == ["Utility"] and [d.name for d in sax.devices] == ["Rack"]
    script._rt_delete_device(0, "track", [0, 0, 0])
    assert sax.devices[0].chains[0].devices == []
    with pytest.raises(ValueError):
        script._rt_container(sax, [0])  # odd length chain_path
    with pytest.raises(IndexError):
        script._rt_device(sax, [0, 3, 0])  # no such chain
    with pytest.raises(IndexError):
        script._rt_device(script._song.tracks[1], [0])  # empty track


def test_macros_chain_mixer_and_scopes(script):
    script._song.tracks[0].devices = [Device("Rack", rack=True)]
    script._rt_rack_insert_chain(0, "track", [0])
    res = script._rt_rack_macros(0, "track", [0], [
        {"op": "set", "macro": 2, "value": 64}, {"op": "add_macro"}, {"op": "store_variation"},
        {"op": "set", "macro": 99, "value": 1}])
    assert [r["ok"] for r in res["results"]] == [True, True, False, False]
    assert res["macros"][1]["value"] == 64 and res["visible_macro_count"] == 9
    cm = script._rt_set_chain_mixer(0, "track", [0], 0, name="Dry", volume=2.0, pan=-0.5, mute=True)
    assert cm["chain"] == "Dry" and cm["volume"] == 1.0 and cm["panning"] == -0.5 and cm["mute"]
    assert script._rt_set_track_mixer(0, "return", volume=0.5)["track"] == "A-Reverb"
    assert script._rt_set_track_mixer(0, "master", pan=0.2)["panning"] == 0.2
    assert script._rt_set_send(0, "track", 1, 0.3)["send"] == "B"
    assert script._rt_create_return_track("Verb")["return_index"] == 1
    assert script._rt_get_set_path() == {"path": "/tmp/x.als"}
    with pytest.raises(ValueError):
        script._rt_track(0, "bogus")


def test_dispatch_table_names_exist(script):
    for command, method in type(script).RACK_COMMANDS.items():
        assert hasattr(script, method), command
    mod = sys.modules["abl_script_under_test"]
    for command in type(script).RACK_COMMANDS:
        assert command in mod.SCRIPT_CAPABILITIES, command


# ---- MCP side ----

class FakeConnection:
    def __init__(self):
        self.sent = []

    def send_command(self, command_type, params=None):
        self.sent.append((command_type, params))
        return {"ok": command_type}


@pytest.fixture
def conn(monkeypatch):
    fake = FakeConnection()
    monkeypatch.setattr(server, "get_ableton_connection", lambda: fake)
    monkeypatch.setattr(server, "_capability_guard", lambda name: None)
    return fake


def test_tools_forward_commands(conn):
    server.insert_device(None, 0, "EQ Eight", 1, [0, 2])
    server.move_device(None, 0, [1], 0, [0, 0])
    server.get_device_tree(None, 3, scope="return")
    server.rack_macros(None, 0, [0], [{"op": "set", "macro": 1, "value": 3}])
    sent = dict(conn.sent)
    assert sent["insert_device"] == {"track_index": 0, "scope": "track", "name": "EQ Eight",
                                     "index": 1, "chain_path": [0, 2]}
    assert sent["move_device"]["dst_chain_path"] == [0, 0] and sent["move_device"]["src_device_path"] == [1]
    assert sent["get_device_tree"]["scope"] == "return"
    assert sent["rack_macros"]["ops"][0]["macro"] == 1


def test_missing_capability_is_reported(monkeypatch):
    monkeypatch.setattr(server, "_capability_guard", lambda name: "missing capability " + name)
    assert "missing capability insert_device" in server.insert_device(None, 0, "EQ Eight")
