"""Stem placement math (MCP_Server/stemsync.py): where each part of a clip lands under the resample pieces.

    uv run --with pytest python -m pytest tests/test_stemsync.py -v
"""
from MCP_Server.stemsync import plan_for, unmappable


def clip(start, end, looping=True, loop=(0.0, 32.0), marker=0.0, end_marker=32.0):
    return {"start": start, "end": end, "looping": looping, "loop_start": loop[0], "loop_end": loop[1],
            "start_marker": marker, "end_marker": end_marker, "midi": False}


def test_full_length_clip_follows_each_piece():
    cl = [clip(0, 1560, loop=(0, 1560), end_marker=1560)]
    pieces = [(0, 58, 272), (58, 66, 618)]
    assert plan_for(cl, pieces) == [(0, 58, 0, 272), (58, 66, 0, 618)]


def test_short_loop_play_from_is_inside_the_loop():
    cl = [clip(0, 1560, loop=(0, 16), end_marker=16)]
    (ts, te, k, pf), = plan_for(cl, [(0, 8, 276)])
    assert (ts, te, k) == (0, 8, 0) and pf == 4.0  # 276 % 16


def test_clip_that_starts_mid_timeline_lands_at_the_same_relative_spot():
    cl = [clip(660, 692)]
    (ts, te, k, pf), = plan_for(cl, [(224, 444, 656)])
    assert (ts, te, pf) == (228, 260, 0.0)  # 4 beats into the piece, playing from the clip's start


def test_clip_starting_before_a_piece_plays_from_inside_itself():
    cl = [clip(640, 672, looping=False)]
    (ts, te, k, pf), = plan_for(cl, [(224, 444, 656)])
    assert (ts, te, pf) == (224, 240, 16.0)


def test_parts_in_cut_out_stretches_are_dropped_and_clips_can_split():
    cl = [clip(8, 40)]
    assert plan_for(cl, [(0, 16, 0), (16, 48.5, 32)]) == [(8, 16, 0, 0.0), (16, 24, 0, 24.0)]
    assert plan_for([clip(100, 120)], [(0, 16, 0)]) == []


def test_unwarped_non_looped_clip_is_flagged():
    unwarped = clip(8, 40, looping=False, end_marker=14.33)
    warped = clip(8, 40, looping=False)
    assert unmappable([unwarped]) == [unwarped] and unmappable([warped]) == []
