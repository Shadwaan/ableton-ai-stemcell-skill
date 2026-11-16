"""
Tests for the .adg / .als file tools (MCP_Server/adg_tools.py).

They run on a small synthetic rack, plus one check against a Live factory rack when Live is installed
(skipped otherwise). They prove the XML edits are well-formed, round-trip through adg_read and never
touch the source. They do NOT prove Live loads the written file: open it in Live to confirm.

    uv run --with pytest pytest tests/test_adg_tools.py -v
"""

import gzip
import hashlib
from pathlib import Path

import pytest

from MCP_Server import adg_tools as a

PARAM = ('<{tag}><LomId Value="0" /><Manual Value="{v}" /><MidiControllerRange><Min Value="0" />'
         '<Max Value="1" /></MidiControllerRange><AutomationTarget Id="0"><LockEnvelope Value="0" />'
         '</AutomationTarget><ModulationTarget Id="0"><LockEnvelope Value="0" /></ModulationTarget></{tag}>')


def macros():
    out = []
    for i in range(16):
        out.append(PARAM.format(tag=f"MacroControls.{i}", v="0"))
    for i in range(16):
        out.append(f'<MacroDisplayNames.{i} Value="Macro {i + 1}" />')
    return "".join(out)


def preset_device(tag, i):
    return (f'<AbletonDevicePreset Id="{i}"><OverwriteProtectionNumber Value="3076" /><Device>'
            f'<{tag} Id="0"><LomId Value="0" /><UserName Value="" />{PARAM.format(tag="DryWet", v="0.5")}'
            f'{PARAM.format(tag="Gain", v="0")}</{tag}></Device></AbletonDevicePreset>')


def branch(name, i):
    return (f'<AudioEffectBranchPreset Id="{i}"><Name Value="{name}" /><DevicePresets>'
            f'{preset_device("Reverb", 0)}{preset_device("Saturator", 1)}</DevicePresets>'
            '<BranchSelectorRange><Min Value="0" /><Max Value="127" /><CrossfadeMin Value="0" />'
            '<CrossfadeMax Value="127" /></BranchSelectorRange></AudioEffectBranchPreset>')


ADG = ('<?xml version="1.0" encoding="UTF-8"?><Ableton MajorVersion="5"><GroupDevicePreset>'
       '<OverwriteProtectionNumber Value="3076" /><Device><AudioEffectGroupDevice Id="0">'
       '<LomId Value="0" /><UserName Value="" /><NumVisibleMacroControls Value="8" />'
       + macros() + PARAM.format(tag="ChainSelector", v="0") +
       '</AudioEffectGroupDevice></Device><BranchPresets>' + branch("Dry", 0) + branch("Wet", 1) +
       '</BranchPresets></GroupDevicePreset></Ableton>')


@pytest.fixture
def adg(tmp_path):
    p = tmp_path / "rack.adg"
    p.write_bytes(gzip.compress(ADG.encode()))
    return p


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def test_read_structure(adg):
    r = a.adg_read(str(adg), include_params=True)
    assert r["rack_type"] == "AudioEffectGroupDevice" and r["visible_macro_count"] == 8
    chains = r["rack"]["chains"]
    assert [c["name"] for c in chains] == ["Dry", "Wet"]
    assert [d["device"] for d in chains[1]["devices"]] == ["Reverb", "Saturator"]
    assert chains[1]["devices"][0]["path"] == [0, 1, 0]
    assert "DryWet" in chains[0]["devices"][0]["parameters"]
    assert r["mappings"] == []


def test_map_macro_roundtrip_and_source_untouched(adg, tmp_path):
    before = sha(adg)
    out = tmp_path / "mapped.adg"
    res = a.adg_map_macro(str(adg), 3, [0, 1, 0], "DryWet", 0.1, 0.9, str(out))
    assert res["written"] == str(out) and sha(adg) == before
    maps = a.adg_read(str(out))["mappings"]
    assert maps == [{"is_note": False, "channel": 16, "controller": 2, "min": 0.1, "max": 0.9,
                     "macro": 3, "device_path": [0, 1, 0], "param": "DryWet"}]
    # map the chain selector for morphing, and re-map the same parameter (replaces, never duplicates)
    out2 = tmp_path / "mapped2.adg"
    a.adg_map_macro(str(out), 1, [0], "ChainSelector", 0, 127, str(out2))
    a.adg_map_macro(str(out2), 5, [0, 1, 0], "DryWet", 1, 0, str(tmp_path / "mapped3.adg"))
    final = a.adg_read(str(tmp_path / "mapped3.adg"))["mappings"]
    assert sorted((m["macro"], m["param"]) for m in final) == [(1, "ChainSelector"), (5, "DryWet")]
    assert [m for m in final if m["param"] == "DryWet"][0]["min"] == 1.0  # inverted range kept


def test_map_macro_errors(adg, tmp_path):
    out = tmp_path / "o.adg"
    with pytest.raises(ValueError, match="source"):
        a.adg_map_macro(str(adg), 1, [0, 0, 0], "DryWet", 0, 1, str(adg))
    with pytest.raises(ValueError, match="No parameter"):
        a.adg_map_macro(str(adg), 1, [0, 0, 0], "Nope", 0, 1, str(out))
    with pytest.raises(ValueError, match="1-16"):
        a.adg_map_macro(str(adg), 17, [0, 0, 0], "DryWet", 0, 1, str(out))
    with pytest.raises(IndexError):
        a.adg_map_macro(str(adg), 1, [0, 5, 0], "DryWet", 0, 1, str(out))
    with pytest.raises(ValueError, match="odd length"):
        a.adg_map_macro(str(adg), 1, [0, 0], "DryWet", 0, 1, str(out))
    a.adg_map_macro(str(adg), 1, [0, 0, 0], "DryWet", 0, 1, str(out))
    with pytest.raises(FileExistsError):
        a.adg_map_macro(str(adg), 1, [0, 0, 0], "DryWet", 0, 1, str(out))
    a.adg_map_macro(str(adg), 2, [0, 0, 0], "DryWet", 0, 1, str(out), overwrite=True)


def test_chain_zones(adg, tmp_path):
    out = tmp_path / "zones.adg"
    a.adg_set_chain_zones(str(adg), [
        {"chain": 0, "min": 0, "max": 63, "crossfade_min": 0, "crossfade_max": 25},
        {"chain": 1, "min": 23, "max": 127, "crossfade_min": 61}], str(out))
    chains = a.adg_read(str(out))["rack"]["chains"]
    assert chains[0]["zone"] == {"min": 0, "max": 63, "crossfade_min": 0, "crossfade_max": 25}
    assert chains[1]["zone"] == {"min": 23, "max": 127, "crossfade_min": 61, "crossfade_max": 127}
    with pytest.raises(ValueError, match="crossfade"):
        a.adg_set_chain_zones(str(adg), [{"chain": 0, "min": 10, "max": 20, "crossfade_min": 5}],
                              str(tmp_path / "bad.adg"))


def test_extract_rack_from_set(tmp_path):
    dev = ('<AudioEffectGroupDevice Id="5"><LomId Value="1" /><OverwriteProtectionNumber Value="3076" />'
           '<Branches><AudioEffectBranch Id="0"><LomId Value="2" /><Name Value="Wet" /><IsSoloed Value="false" />'
           '<DeviceChain><AudioToAudioDeviceChain Id="0"><Devices><Reverb Id="0"><LomId Value="3" />'
           + PARAM.format(tag="DryWet", v="0.5").replace('Id="0"', 'Id="77"') +
           '</Reverb></Devices></AudioToAudioDeviceChain></DeviceChain>'
           '<BranchSelectorRange><Min Value="0" /><Max Value="127" /><CrossfadeMin Value="0" />'
           '<CrossfadeMax Value="127" /></BranchSelectorRange><Color Value="4" />'
           '<MixerDevice><LomId Value="9" /><Volume><Manual Value="1" /></Volume></MixerDevice>'
           '</AudioEffectBranch></Branches><ReturnBranches /><NumVisibleMacroControls Value="8" />'
           '</AudioEffectGroupDevice>')
    als = ('<Ableton MajorVersion="5"><LiveSet><Tracks><AudioTrack Id="1"><Name><EffectiveName Value="Sax" />'
           '<UserName Value="Sax" /></Name><DeviceChain><DeviceChain><Devices>' + dev +
           '</Devices></DeviceChain></DeviceChain></AudioTrack></Tracks></LiveSet></Ableton>')
    src = tmp_path / "set.als"
    src.write_bytes(gzip.compress(als.encode()))
    before = sha(src)
    out = tmp_path / "sax.adg"
    a.als_extract_rack(str(src), "Sax", 0, str(out))
    assert sha(src) == before
    r = a.adg_read(str(out), include_params=True)
    chain = r["rack"]["chains"][0]
    assert chain["name"] == "Wet" and [d["device"] for d in chain["devices"]] == ["Reverb"]
    # ids are zeroed the way factory presets carry them, and the layout is the preset layout
    root = a._read_xml(str(out))
    assert all(e.get("Id") == "0" for e in root.iter("AutomationTarget"))
    assert a._top(root).find("ReturnBranchPresets") is not None
    assert a._branches(a._top(root))[0].find("MixerPreset") is not None
    with pytest.raises(ValueError, match="No track"):
        a.als_extract_rack(str(src), "Nope", 0, str(tmp_path / "x.adg"))
    with pytest.raises(IndexError):
        a.als_extract_rack(str(src), "Sax", 3, str(tmp_path / "x.adg"))


FACTORY = Path("/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library/Racks/"
               "Audio Effect Racks/Mixing & Mastering/Drum Buss Gritty.adg")


@pytest.mark.skipif(not FACTORY.exists(), reason="Live factory racks not installed")
def test_factory_rack_macro_mapping_layout():
    """Macro 1 of Drum Buss Gritty is 'Over Drive' and maps to the Pedal's Dry/Wet: the KeyMidi
    channel-16 layout the tools read and write."""
    r = a.adg_read(str(FACTORY))
    assert r["macros"][0]["name"] == "Over Drive"
    pedal = [m for m in r["mappings"] if m["macro"] == 1 and m["param"] == "DryWet" and m["device_path"] == [0, 0, 0]]
    assert pedal and pedal[0]["min"] == 0.0 and pedal[0]["max"] == 1.0
