# ableton-mcp (studio fork)

A copy of [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp) (MIT, see `LICENSE`) with the
changes the studio tools in `../tools` and `../skills` rely on. It is self-contained: install it from this
folder, no other checkout needed. Remote Script version **1.8.0** (upstream's is older).

## What this adds to upstream
- **Arrangement and stem-sync:** `arrangement_clip_ops`, `trim_arrangement_clip`, `stemsync_place`, `warp_marker_ops`
  (reads warp markers; Live's API cannot create them), `save_timeline_snapshot`, `set_clip_warp_loop`.
- **Tracks and routing:** meters, arm, monitoring, input/output routing, name-verified `delete_tracks`.
- **Racks and devices (1.8.0):** `get_device_tree`, path-addressed `get_device_parameters_at` /
  `set_device_parameter_at`, `insert_device`, `rack_insert_chain`, `move_device`, `delete_device`,
  `set_chain_mixer`, `rack_macros`, `list_user_folders`, `load_item_by_path`, `select_device`,
  `set_track_mixer`, `set_send`, `create_return_track`, `get_set_path`, `introspect`.
  Paths: a `device_path` is `[device, chain, device, ...]` (odd length); `[]` is the track for chain paths.
- **File route for presets (`MCP_Server/adg_tools.py`):** `adg_read`, `adg_map_macro`, `adg_set_chain_zones`,
  `als_extract_rack` edit the gzipped XML of `.adg` / `.als` files and always write a new file. A macro mapping
  is a `KeyMidi` block (channel 16, controller = macro - 1) on the parameter, as in Live's own racks.

## Install
```bash
cd studio/ableton-mcp
uv run ableton-mcp-install-script          # copies the Remote Script into Live's User Library
# restart Live, then Preferences > Link, Tempo & MIDI > Control Surface: AbletonMCP
claude mcp add ableton -- uv run --directory "$PWD" ableton-mcp
uv run --with pytest pytest -q            # tests (no Live needed)
```
Live loads the Remote Script only at startup: restart Live after installing or updating it
(re-selecting the control surface does not re-import).

## Telemetry
Upstream's anonymous usage telemetry is **on by default** (tool names, success, duration; no prompts, MIDI or
names). Turn it off with `export ABLETON_MCP_DISABLE_TELEMETRY=true`. Dataset recording is opt-in. See the
Telemetry section of `README.md`.

## Verification status
Tested in Live 12.4: insert/move/delete device, rack chains, chain mixer, macros, select, tree reads. Not yet run in
Live: `load_item_by_path`, `create_return_track`, `set_track_mixer`, `set_send`, and opening any `.adg` written by
the file tools. If a call says it is "not available in this Live version", run `introspect` to see what that
version exposes.
