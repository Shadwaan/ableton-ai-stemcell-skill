"""Stem-sync helpers shared by the MCP tools (and mirrored by tools/stems_place.py).

Everything goes through `send(command, params)` (AbletonConnection.send_command), so no socket code here.
Trimming and placing use the Remote Script's `arrangement_clip_ops` and the overwrite-and-delete method:
Live's API cannot set an Arrangement clip's end, but a clip placed over a clip's tail trims it.
"""
from __future__ import annotations

import re

SCR = 4000.0  # scratch time far past the arrangement, removed again at the end


def ops(send, track, o):
    log = send("arrangement_clip_ops", {"track_index": track, "ops": o})["log"]
    last = log[-1]
    if "error" in last:
        raise RuntimeError(str(last["error"]) + " op=" + str(last["op"]))
    return last["clips"]


def find(clips, start):
    m = [i for i, c in enumerate(clips) if abs(c["start"] - start) < 1e-3]
    if len(m) != 1:
        raise RuntimeError("clip at %s: %d matches" % (start, len(m)))
    return m[0]


def tracks(send):
    return send("get_track_meters")["tracks"]


def resample_track(send):
    """Index of the resample track: the audio track whose input is Resampling (its name varies)."""
    found = [t["index"] for t in tracks(send) if t.get("input") == "Resampling"]
    if len(found) != 1:
        raise RuntimeError("expected exactly one Resampling track, found %d" % len(found))
    return found[0]


def pieces(send):
    ti = resample_track(send)
    cl = ops(send, ti, [])
    return ti, [(c["start"], c["end"], c["start_marker"]) for c in cl]


def trim_end(send, track, clip_index, end_time):
    """Shorten an Arrangement clip so it ends at end_time (beats). Start and loop stay.

    MIDI: a new empty clip exactly covering the tail is placed over it, then deleted.
    Audio: a copy of the clip itself is placed at end_time (it is long), then deleted; refused if another
    clip starts inside the copy's span, because the copy would overwrite it.
    """
    cl = ops(send, track, [])
    c = cl[clip_index]
    if not (c["start"] < end_time < c["end"]):
        raise ValueError("end_time must be inside the clip (%s..%s)" % (c["start"], c["end"]))
    if c["midi"]:
        o = [{"op": "create_midi", "start": float(end_time), "length": float(c["end"] - end_time)}]
    else:
        span_end = end_time + (c["end"] - c["start"])
        if any(end_time < x["start"] < span_end for i, x in enumerate(cl) if i != clip_index):
            raise RuntimeError("another clip starts inside the span the temporary copy would cover; "
                               "trim by hand or move that clip first")
        o = [{"op": "dup", "clip_index": clip_index, "time": float(end_time)}]
    cl = ops(send, track, o)
    return ops(send, track, [{"op": "delete", "clip_index": find(cl, float(end_time))}])


def unmappable(cl):
    """Non-looped clips whose arrangement length differs from their content length are unwarped (markers in
    seconds, not beats): their position in the original cannot be mapped in beats. Names of such clips."""
    return [c for c in cl if not c["looping"]
            and abs((c["end"] - c["start"]) - (c["end_marker"] - c["start_marker"])) > 0.05]


def plan_for(cl, P):
    """Where every part of every clip on a track lands under the pieces P = [(start, end, source_offset)].

    Each clip is mapped through the pieces by where it sat in the ORIGINAL timeline, whatever its start,
    length or looping: a piece plays original [src, src + (end - start)) at [start, end); the part of the
    clip inside that window goes to the same place under the piece. Parts of a clip in a cut-out stretch
    are dropped. Returns [(dest_start, dest_end, clip_index, play_from)] sorted by dest_start.
    play_from is where in the clip's own content that part must start: inside the loop for looped clips.
    """
    plan = []
    for k, c in enumerate(cl):
        s, e, m = c["start"], c["end"], c["start_marker"]
        ls, le = c["loop_start"], c["loop_end"]
        for a, b, src in P:
            lo, hi = max(s, src), min(e, src + (b - a))
            if hi - lo < 1e-6:
                continue
            pos = m + (lo - s)
            pf = ls + ((pos - ls) % (le - ls)) if c["looping"] and le > ls else pos
            plan.append((a + lo - src, a + hi - src, k, pf))
    plan.sort()
    return plan


def place(send, track, P):
    """Rebuild every clip of one track under the pieces P. Overwrites the originals.

    Every original is first copied to its own scratch time (far past the arrangement) because placing a
    piece overwrites whatever it covers, then the originals are deleted, the parts are placed in time order
    (each trimmed to its end by overwriting the tail with a copy and deleting the copy), and the scratch
    copies are removed. Looped clips get their play-from point through start_marker; non-looped clips through
    loop_start (Live ignores start_marker on a non-looped clip; loop_start sets it and moves the clip's end).
    """
    cl = ops(send, track, [])
    if unmappable(cl):
        raise ValueError("unwarped clip (content length differs from its arrangement length): warp it first; "
                         "nothing was changed")
    plan = plan_for(cl, P)
    longest = max([c["end"] - c["start"] for c in cl] + [1.0])
    horizon = max([c["end"] for c in cl] + [p[1] for p in P]) + longest
    base_t = max(SCR, horizon + 500.0)
    step = longest + 500.0
    scr = [base_t + step * k for k in range(len(cl))]
    orig = [(c["start"], c["end"]) for c in cl]
    for k, (s, _) in enumerate(orig):
        cl = ops(send, track, [{"op": "dup", "clip_index": find(cl, s), "time": scr[k]}])
    for s, _ in orig:
        cl = ops(send, track, [{"op": "delete", "clip_index": find(cl, s)}])
    for ts, te, k, pf in plan:
        cl = ops(send, track, [{"op": "dup", "clip_index": find(cl, scr[k]), "time": ts}])
        ni = find(cl, ts)
        prop = "start_marker" if cl[ni]["looping"] else "loop_start"
        cl = ops(send, track, [{"op": "set", "clip_index": ni, "prop": prop, "value": float(pf)}])
        if cl[find(cl, ts)]["end"] - te > 1e-3:
            cl = ops(send, track, [{"op": "dup", "clip_index": find(cl, scr[k]), "time": te}])
            cl = ops(send, track, [{"op": "delete", "clip_index": find(cl, te)}])
    for k in range(len(orig)):
        cl = ops(send, track, [{"op": "delete", "clip_index": find(cl, scr[k])}])
    return cl, plan


def place_all(send, only_track=None):
    ti, P = pieces(send)
    report = []
    for t in tracks(send):
        if t["index"] == ti or t["is_group"] or (only_track and t["name"] != only_track):
            continue
        cl = ops(send, t["index"], [])
        if not cl:
            continue
        if unmappable(cl):
            report.append("skip %s (has an unwarped clip: warp it first)" % t["name"])
            continue
        plan = plan_for(cl, P)
        if not plan:
            report.append("skip %s (nothing of it falls under the pieces)" % t["name"])
            continue
        have = sorted((round(c["start"], 3), round(c["end"], 3)) for c in cl)
        want = sorted((round(p[0], 3), round(p[1], 3)) for p in plan)
        if have == want:
            report.append("skip %s (already placed under the pieces)" % t["name"])
            continue
        try:
            out, plan = place(send, t["index"], P)
            got = sorted((round(c["start"], 3), round(c["end"], 3)) for c in out)
            ok = len(got) == len(want) and all(abs(g[0] - w[0]) < 1e-3 and (g[1] <= w[1] + 1e-3) for g, w in zip(got, want))
            report.append(("OK    " if ok else "CHECK ") + "%s (%d clips for %d parts)" % (t["name"], len(out), len(plan)))
        except Exception as e:  # stop at the first failure so nothing is left half-done unnoticed
            report.append("FAIL  %s: %s" % (t["name"], e))
            break
    return {"resample_track": ti, "pieces": [list(p) for p in P], "report": report}


def base(name):
    return re.sub(r"^\d+-", "", name)


def delete_one_at_a_time(send, wanted):
    """Delete tracks one call at a time (a bulk call once failed in Live and left a half-deleted set).

    `wanted` = [{"index","name"}] checked against the live list by the caller. Groups go first (a group
    takes its children with it), then the rest, highest index first. Live renumbers the "NN-" name prefix after
    every delete, so each target is re-found by (name without prefix, parent group, is_group) in a fresh list.
    """
    cur = tracks(send)
    by_idx = {t["index"]: t for t in cur}
    keys = []
    for w in wanted:
        t = by_idx[w["index"]]
        keys.append((t["is_group"], t["index"], (base(t["name"]), t.get("group") and base(t["group"]), bool(t["is_group"]))))
    keys.sort(key=lambda k: (0 if k[0] else 1, -k[1]))
    done, gone = [], []
    for _, _, key in keys:
        now = [t for t in tracks(send) if (base(t["name"]), t.get("group") and base(t["group"]), bool(t["is_group"])) == key]
        if not now:
            gone.append(key[0])  # already removed together with its group
            continue
        if len(now) > 1:
            raise RuntimeError("ambiguous track %r: %d matches; nothing more deleted" % (key[0], len(now)))
        t = now[0]
        before = len(send("get_track_meters")["returns"])
        send("delete_tracks", {"tracks": [{"index": t["index"], "name": t["name"]}]})
        if len(send("get_track_meters")["returns"]) != before:
            raise RuntimeError("the number of return tracks changed after deleting %r; stopped" % t["name"])
        done.append(t["name"])
    return {"deleted": done, "removed_with_their_group": gone, "remaining": len(tracks(send))}
