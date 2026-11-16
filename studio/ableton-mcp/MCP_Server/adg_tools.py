"""
File-route tools for Audio Effect Rack presets (.adg) and saved sets (.als).

Live's Python API cannot map a macro to a parameter, set chain zones, or save a rack as a preset.
These tools edit the gzipped XML instead and always write a NEW file (never the source).

What the layout below is based on (read from Live's own factory racks in Core Library/Racks, and a
user-saved preset, Live 12.4): it is inferred from those files, not from documentation. Open the
written file in Live and check it before relying on it.

  * A preset is  <GroupDevicePreset><Device><AudioEffectGroupDevice/></Device><BranchPresets>...
    Each branch holds <DevicePresets>, whose children are <AbletonDevicePreset> (one device) or
    <GroupDevicePreset> (a nested rack, same shape as the top one). The rack in the file is device 0,
    so a device path is [0, chain, device, chain, device, ...], the same path Live's API uses for a
    rack loaded on an empty track.
  * A macro is mapped to a parameter by a <KeyMidi> block on that parameter: Channel 16,
    NoteOrController = macro number - 1, IsNote false. The mapped range is the parameter's
    <MidiControllerRange> (Min/Max in the parameter's own units; Min > Max inverts).
  * A chain zone is <BranchSelectorRange> Min/Max/CrossfadeMin/CrossfadeMax (0-127): the chain fades
    in over Min..CrossfadeMin and out over CrossfadeMax..Max. CrossfadeMin = Min and CrossfadeMax = Max
    is a hard edge.
"""

from __future__ import annotations

import copy
import gzip
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

MACRO_CHANNEL = 16
MAX_MACROS = 16
RACK_TAGS = ("AudioEffectGroupDevice", "InstrumentGroupDevice", "MidiEffectGroupDevice", "DrumGroupDevice")
UNSUPPORTED_DEVICE_HINTS = ("PluginDevice", "AuPluginDevice", "Vst3PluginDevice", "MxDevice")
_SKIP_PARAM_TAGS = ("MacroControls",)


# ---------------------------------------------------------------- file io

def _read_xml(path: str) -> ET.Element:
    data = Path(path).expanduser().read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return ET.fromstring(data)


def _write_xml(root: ET.Element, out_path: str, source: str | None, overwrite: bool) -> str:
    out = Path(out_path).expanduser()
    if source and out.resolve() == Path(source).expanduser().resolve():
        raise ValueError("out_path is the source file. Refusing to overwrite it; pick a new name.")
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} already exists. Pick a new name (or pass overwrite=true).")
    out.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="\t")
    body = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(gzip.compress(body))
    os.replace(tmp, out)
    return str(out)


def _num(value: Any) -> str:
    f = float(value)
    return str(int(f)) if f == int(f) else repr(f)


def _val(elem: ET.Element, tag: str, default: str | None = None) -> str | None:
    child = elem.find(tag)
    return child.get("Value", default) if child is not None else default


# ------------------------------------------------------- preset navigation

def _top(root: ET.Element) -> ET.Element:
    gp = root.find("GroupDevicePreset")
    if gp is None:
        raise ValueError("Not a rack preset: no <GroupDevicePreset> (only .adg racks are supported)")
    return gp


def _preset_device(node: ET.Element) -> ET.Element:
    dev = node.find("Device")
    if dev is None or len(dev) == 0:
        raise ValueError("Malformed preset node <" + node.tag + ">")
    return dev[0]


def _branches(node: ET.Element) -> list[ET.Element]:
    bp = node.find("BranchPresets")
    return list(bp) if bp is not None else []


def _branch_devices(branch: ET.Element) -> list[ET.Element]:
    dp = branch.find("DevicePresets")
    return list(dp) if dp is not None else []


def _resolve(root: ET.Element, path: list[int]) -> ET.Element:
    """Preset node (<GroupDevicePreset> or <AbletonDevicePreset>) at [0, chain, device, ...]."""
    path = [int(p) for p in path]
    if not path or path[0] != 0 or len(path) % 2 == 0:
        raise ValueError("device_path must look like [0] or [0, chain, device, ...] (odd length, "
                         "starting with 0 = the rack in the file), got " + str(path))
    node = _top(root)
    for i in range(1, len(path), 2):
        chain, dev = path[i], path[i + 1]
        branches = _branches(node)
        if chain < 0 or chain >= len(branches):
            raise IndexError(f"chain {chain} out of range at path position {i} ({len(branches)} chains)")
        devices = _branch_devices(branches[chain])
        if dev < 0 or dev >= len(devices):
            raise IndexError(f"device {dev} out of range in chain {chain} ({len(devices)} devices)")
        node = devices[dev]
    return node


def _params(device: ET.Element) -> list[tuple[str, ET.Element]]:
    """(relative path, element) of every parameter below a device: elements with Manual + AutomationTarget."""
    out: list[tuple[str, ET.Element]] = []

    def walk(e: ET.Element, prefix: str) -> None:
        for c in e:
            tag = c.tag
            if tag.split(".")[0] in _SKIP_PARAM_TAGS:
                continue
            rel = prefix + tag
            if c.find("Manual") is not None and c.find("AutomationTarget") is not None:
                out.append((rel, c))
            elif len(c):
                walk(c, rel + "/")

    walk(device, "")
    return out


def _find_param(device: ET.Element, spec: str) -> ET.Element:
    params = _params(device)
    exact = [e for rel, e in params if rel == spec]
    if exact:
        return exact[0]
    tail = [(rel, e) for rel, e in params if rel.split("/")[-1] == spec]
    if len(tail) == 1:
        return tail[0][1]
    if len(tail) > 1:
        raise ValueError(f"'{spec}' is ambiguous in {device.tag}; use one of: " + ", ".join(r for r, _ in tail))
    raise ValueError(f"No parameter '{spec}' in {device.tag}. Parameters: "
                     + ", ".join(r for r, _ in params[:80]) + (" ..." if len(params) > 80 else ""))


def _mapping(param: ET.Element) -> dict[str, Any] | None:
    km = param.find("KeyMidi")
    if km is None:
        return None
    rng = param.find("MidiControllerRange")
    info: dict[str, Any] = {
        "is_note": _val(km, "IsNote") == "true",
        "channel": int(_val(km, "Channel", "-1")),
        "controller": int(_val(km, "NoteOrController", "-1")),
    }
    if rng is not None:
        info["min"], info["max"] = float(_val(rng, "Min", "0")), float(_val(rng, "Max", "0"))
    if info["channel"] == MACRO_CHANNEL and not info["is_note"]:
        info["macro"] = info["controller"] + 1
    return info


# ------------------------------------------------------------------ adg_read

def _describe_node(node: ET.Element, path: list[int], include_params: bool, maps: list) -> dict[str, Any]:
    dev = _preset_device(node)
    info: dict[str, Any] = {"path": path, "device": dev.tag, "name": _val(dev, "UserName", "") or dev.tag}
    params = _params(dev)
    for rel, el in params:
        m = _mapping(el)
        if m:
            maps.append(dict(m, device_path=path, param=rel))
    if include_params:
        info["parameters"] = [rel for rel, _ in params]
    branches = _branches(node)
    if node.tag == "GroupDevicePreset":
        chains = []
        for ci, br in enumerate(branches):
            rng = br.find("BranchSelectorRange")
            chains.append({
                "index": ci, "name": _val(br, "Name", ""),
                "zone": ({"min": int(_val(rng, "Min", "0")), "max": int(_val(rng, "Max", "127")),
                          "crossfade_min": int(_val(rng, "CrossfadeMin", "0")),
                          "crossfade_max": int(_val(rng, "CrossfadeMax", "127"))} if rng is not None else None),
                "devices": [_describe_node(n, path + [ci, di], include_params, maps)
                            for di, n in enumerate(_branch_devices(br))],
            })
        info["chains"] = chains
    return info


def adg_read(path: str, include_params: bool = False) -> dict[str, Any]:
    """Structure of a rack preset: macros (name, default, value), chains with zones, nested devices,
    and every parameter mapped to a macro. Paths in the answer are the device_path to pass back in."""
    root = _read_xml(path)
    top = _top(root)
    rack = _preset_device(top)
    macros = []
    visible = int(_val(rack, "NumVisibleMacroControls", "8"))
    for i in range(MAX_MACROS):
        ctl = rack.find(f"MacroControls.{i}")
        macros.append({
            "macro": i + 1, "visible": i < visible,
            "name": _val(rack, f"MacroDisplayNames.{i}", f"Macro {i + 1}"),
            "default": _val(rack, f"MacroDefaults.{i}"),
            "value": _val(ctl, "Manual") if ctl is not None else None,
        })
    maps: list = []
    tree = _describe_node(top, [0], include_params, maps)
    return {"file": str(Path(path).expanduser()), "rack_type": rack.tag,
            "visible_macro_count": visible, "macros": macros, "mappings": maps, "rack": tree,
            "note": "Mappings are KeyMidi blocks on channel 16 (macro = controller + 1); layout inferred "
                    "from Live's factory racks."}


# ------------------------------------------------------------ adg_map_macro

def adg_map_macro(path: str, macro: int, device_path: list[int], param: str, min_value: float,
                  max_value: float, out_path: str, overwrite: bool = False) -> dict[str, Any]:
    """Map macro (1-16) to a parameter and save as a new file. min_value/max_value are the parameter
    values at macro 0 and macro 127, in the parameter's own units (min > max inverts)."""
    macro = int(macro)
    if not 1 <= macro <= MAX_MACROS:
        raise ValueError("macro must be 1-16")
    root = _read_xml(path)
    dev = _preset_device(_resolve(root, device_path))
    el = _find_param(dev, param)
    km = el.find("KeyMidi")
    if km is None:
        km = ET.Element("KeyMidi")
        el.insert(1 if el.find("LomId") is not None else 0, km)
    km.clear()
    for tag, value in (("PersistentKeyString", ""), ("IsNote", "false"), ("Channel", str(MACRO_CHANNEL)),
                       ("NoteOrController", str(macro - 1)), ("LowerRangeNote", "-1"),
                       ("UpperRangeNote", "-1"), ("ControllerMapMode", "0")):
        ET.SubElement(km, tag).set("Value", value)
    rng = el.find("MidiControllerRange")
    if rng is None:
        rng = ET.SubElement(el, "MidiControllerRange")
    for child in list(rng):
        rng.remove(child)
    ET.SubElement(rng, "Min").set("Value", _num(min_value))
    ET.SubElement(rng, "Max").set("Value", _num(max_value))
    rack = _preset_device(_top(root))
    warn = None
    if macro > int(_val(rack, "NumVisibleMacroControls", "8")):
        warn = f"macro {macro} is beyond the {_val(rack, 'NumVisibleMacroControls', '8')} visible macros"
    written = _write_xml(root, out_path, path, overwrite)
    return {"written": written, "macro": macro, "device": dev.tag, "param": param,
            "min": float(min_value), "max": float(max_value), "warning": warn}


# --------------------------------------------------------- adg_set_chain_zones

def adg_set_chain_zones(path: str, zones: list[dict[str, Any]], out_path: str,
                        rack_path: list[int] | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Set chain selector zones. zones: [{"chain": i, "min": 0-127, "max": 0-127,
    "crossfade_min": .., "crossfade_max": ..}]; crossfades default to a hard edge (min / max)."""
    root = _read_xml(path)
    node = _resolve(root, rack_path or [0])
    if node.tag != "GroupDevicePreset":
        raise ValueError("rack_path does not point at a rack")
    branches = _branches(node)
    done = []
    for z in zones:
        ci = int(z["chain"])
        if ci < 0 or ci >= len(branches):
            raise IndexError(f"chain {ci} out of range ({len(branches)} chains)")
        lo, hi = int(z["min"]), int(z["max"])
        cmin, cmax = int(z.get("crossfade_min", lo)), int(z.get("crossfade_max", hi))
        if not (0 <= lo <= cmin <= cmax <= hi <= 127):
            raise ValueError(f"chain {ci}: need 0 <= min <= crossfade_min <= crossfade_max <= max <= 127, "
                             f"got {lo}, {cmin}, {cmax}, {hi}")
        rng = branches[ci].find("BranchSelectorRange")
        if rng is None:
            rng = ET.SubElement(branches[ci], "BranchSelectorRange")
        for child in list(rng):
            rng.remove(child)
        for tag, v in (("Min", lo), ("Max", hi), ("CrossfadeMin", cmin), ("CrossfadeMax", cmax)):
            ET.SubElement(rng, tag).set("Value", str(v))
        done.append({"chain": ci, "name": _val(branches[ci], "Name", ""), "min": lo, "max": hi,
                     "crossfade_min": cmin, "crossfade_max": cmax})
    written = _write_xml(root, out_path, path, overwrite)
    return {"written": written, "zones": done}


# ---------------------------------------------------------- als_extract_rack

def _zero_ids(elem: ET.Element) -> None:
    """Presets carry Id 0 on automation/modulation targets and pointees; LomIds are reset."""
    for e in elem.iter():
        if e.tag in ("AutomationTarget", "ModulationTarget", "Pointee") and "Id" in e.attrib:
            e.set("Id", "0")
        if e.tag == "LomId" and "Value" in e.attrib:
            e.set("Value", "0")
        if e.tag.endswith("ListWrapper") and "LomId" in e.attrib:
            e.set("LomId", "0")


def _preset_ref(tag: str) -> ET.Element:
    ref = ET.Element("PresetRef")
    d = ET.SubElement(ref, "AbletonDefaultPresetRef", Id="0")
    fr = ET.SubElement(d, "FileRef")
    for t, v in (("RelativePathType", "0"), ("RelativePath", ""), ("Path", ""), ("Type", "2"),
                 ("LivePackName", ""), ("LivePackId", ""), ("OriginalFileSize", "0"),
                 ("OriginalCrc", "0"), ("SourceHint", "")):
        ET.SubElement(fr, t).set("Value", v)
    ET.SubElement(d, "DeviceId").set("Name", tag)
    return ref


def _wrap_device(dev: ET.Element, index: int | None, protection: str) -> ET.Element:
    if any(h in dev.tag for h in UNSUPPORTED_DEVICE_HINTS):
        raise ValueError(f"'{dev.tag}' (plugin / Max for Live device) cannot be converted to a preset "
                         "this way. Remove it from the rack, or save the rack in Live (Cmd+G is not "
                         "needed: drag the rack to the User Library).")
    if dev.tag in RACK_TAGS:
        return _rack_to_preset(dev, index, protection)
    node = ET.Element("AbletonDevicePreset", Id=str(index if index is not None else 0))
    ET.SubElement(node, "OverwriteProtectionNumber").set("Value", protection)
    holder = ET.SubElement(node, "Device")
    clone = copy.deepcopy(dev)
    clone.set("Id", "0")
    _zero_ids(clone)
    holder.append(clone)
    node.append(_preset_ref(dev.tag))
    return node


def _rack_to_preset(rack: ET.Element, index: int | None, protection: str) -> ET.Element:
    if rack.tag != "AudioEffectGroupDevice":
        raise ValueError(f"Only Audio Effect Racks are converted, not {rack.tag}")
    gp = ET.Element("GroupDevicePreset")
    if index is not None:
        gp.set("Id", str(index))
    ET.SubElement(gp, "OverwriteProtectionNumber").set("Value", protection)
    holder = ET.SubElement(gp, "Device")
    clone = copy.deepcopy(rack)
    clone.set("Id", "0")
    for tag in ("Branches", "ReturnBranches"):
        old = clone.find(tag)
        if old is not None:
            old.clear()  # the chains move to <BranchPresets>; the empty element stays where Live puts it
        else:
            ET.SubElement(clone, tag)
    _zero_ids(clone)
    holder.append(clone)
    gp.append(_preset_ref(rack.tag))
    bp = ET.SubElement(gp, "BranchPresets")
    for ci, branch in enumerate(list(rack.find("Branches")) if rack.find("Branches") is not None else []):
        bp.append(_branch_to_preset(branch, ci, protection))
    ET.SubElement(gp, "ReturnBranchPresets")
    return gp


def _branch_to_preset(branch: ET.Element, index: int, protection: str) -> ET.Element:
    out = ET.Element("AudioEffectBranchPreset", Id=str(index))
    for tag in ("Name", "IsSoloed"):
        e = branch.find(tag)
        if e is not None:
            out.append(copy.deepcopy(e))
    dp = ET.SubElement(out, "DevicePresets")
    chain = branch.find("DeviceChain")
    devices = None
    if chain is not None and len(chain):
        devices = chain[0].find("Devices")
    for di, dev in enumerate(devices if devices is not None else []):
        dp.append(_wrap_device(dev, di, protection))
    mixer = branch.find("MixerDevice")
    if mixer is not None:
        mp = ET.SubElement(out, "MixerPreset")
        node = ET.SubElement(mp, "AbletonDevicePreset", Id="0")
        ET.SubElement(node, "OverwriteProtectionNumber").set("Value", protection)
        holder = ET.SubElement(node, "Device")
        m = copy.deepcopy(mixer)
        m.tag = "AudioBranchMixerDevice"
        m.set("Id", "0")
        _zero_ids(m)
        holder.append(m)
        node.append(_preset_ref("AudioBranchMixerDevice"))
    for tag in ("BranchSelectorRange", "SessionViewBranchWidth"):
        e = branch.find(tag)
        if e is not None:
            out.append(copy.deepcopy(e))
    color = branch.find("Color")
    if color is not None:
        ET.SubElement(out, "DocumentColorIndex").set("Value", color.get("Value", "0"))
    for tag in ("AutoColored", "AutoColorScheme", "SourceContext"):
        e = branch.find(tag)
        if e is not None:
            out.append(copy.deepcopy(e))
    return out


def _track_devices(track: ET.Element) -> ET.Element:
    outer = track.find("DeviceChain")
    inner = outer.find("DeviceChain") if outer is not None else None
    devices = inner.find("Devices") if inner is not None else None
    if devices is None:
        raise ValueError("Track has no device chain")
    return devices


def als_extract_rack(als_path: str, track: str | int, device: int, out_adg: str,
                     overwrite: bool = False) -> dict[str, Any]:
    """Pull an Audio Effect Rack out of a saved set into a new .adg. track: name (as in Live, e.g.
    "Sax") or index; device: index in the track's device chain. The set is only read."""
    root = _read_xml(als_path)
    tracks = list(root.iter("AudioTrack")) + list(root.iter("MidiTrack")) + list(root.iter("GroupTrack"))
    chosen = None
    if isinstance(track, int) or (isinstance(track, str) and track.isdigit()):
        audio = [t for t in root.find("LiveSet/Tracks") if t.tag in ("AudioTrack", "MidiTrack", "GroupTrack")]
        chosen = audio[int(track)] if 0 <= int(track) < len(audio) else None
    else:
        want = str(track).lower()
        for t in tracks:
            names = [_val(t.find("Name"), "EffectiveName", ""), _val(t.find("Name"), "UserName", "")]
            if any(n and (n.lower() == want or n.lower().split("-", 1)[-1] == want) for n in names):
                chosen = t
                break
    if chosen is None:
        raise ValueError(f"No track '{track}' in {als_path}. Tracks: "
                         + ", ".join(_val(t.find('Name'), 'EffectiveName', '?') for t in tracks))
    devices = list(_track_devices(chosen))
    if device < 0 or device >= len(devices):
        raise IndexError(f"device {device} out of range ({len(devices)} devices on the track: "
                         + ", ".join(d.tag for d in devices) + ")")
    protection = _val(devices[device], "OverwriteProtectionNumber", "3076") or "3076"
    if devices[device].tag not in RACK_TAGS:
        raise ValueError(f"'{devices[device].tag}' is not a rack")
    out_root = ET.Element("Ableton", dict(root.attrib))
    # the top-level GroupDevicePreset carries no Id attribute in a saved preset (index None)
    out_root.append(_rack_to_preset(devices[device], None, protection))
    written = _write_xml(out_root, out_adg, None, overwrite)
    return {"written": written, "rack": devices[device].tag,
            "track": _val(chosen.find("Name"), "EffectiveName", ""),
            "note": "Converted from the set's layout to the preset layout; open it in Live to confirm "
                    "(drag it onto a track), and remember to save the set first so the rack is current."}
