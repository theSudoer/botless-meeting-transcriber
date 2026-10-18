"""Transcript cleanup rules: echo, hallucinations, Teams-output filter, paragraphs, markdown."""

from meetingrecorder.transcriber import (
    build_lines,
    drop_echo,
    in_teams_spans,
    is_hallucination,
    paragraphs,
    render_markdown,
)


def seg(start, text, lp=-0.2, end=None):
    return {"start": start, "end": end if end is not None else start + 1.5, "text": text, "lp": lp, "nsp": 0.0}


def test_speaker_echo_on_the_mic_is_dropped():
    # Call on a Bluetooth speaker: the far side is played at 101.3 s and the speaker's own mic hears it 0.3 s later
    others = [seg(101.3, "Hello? Hello?")]
    me = [seg(96.0, "Hello? Hello?"), seg(101.6, "Hello? Hello?")]
    kept = drop_echo(me, others)
    assert [m["start"] for m in kept] == [96.0]


def test_real_reply_overlapping_others_is_kept():
    others = [seg(10.0, "Can you send the report by Friday?")]
    me = [seg(11.0, "Yes, I'll have it done Thursday.")]
    assert drop_echo(me, others) == me


def test_hallucinations():
    assert is_hallucination(seg(0, "Thank you.", lp=-0.94))
    assert is_hallucination(seg(0, "You", lp=-0.1))
    assert not is_hallucination(seg(0, "Thank you.", lp=-0.2))  # confident: a person said it
    assert not is_hallucination(seg(0, "Thank you, that's really helpful.", lp=-0.9))


def test_teams_spans():
    spans = [[100.0, 200.0]]
    assert in_teams_spans(seg(150, "x"), spans)
    assert in_teams_spans(seg(98, "x"), spans)  # padding
    assert not in_teams_spans(seg(20, "a video playing before the call"), spans)


def test_paragraphs_merge_same_speaker():
    lines = paragraphs([{**seg(0, "Hi all."), "who": "Others"}, {**seg(2, "Let's start."), "who": "Others"},
                        {**seg(5, "Sounds good."), "who": "Me"}])
    assert [(p["who"], p["text"]) for p in lines] == [("Others", "Hi all. Let's start."), ("Me", "Sounds good.")]


def test_build_and_render():
    me = [seg(19.4, "This is a quick test call."), seg(23.3, "Action item: send the report by Friday."),
          seg(41.4, "You", lp=-0.8)]
    them = [seg(30.0, "Hi, this is the test call.")]
    lines = build_lines(me, them)
    assert [ln["who"] for ln in lines] == ["Me", "Others"]
    meta = {"started": "2026-01-15T10:00:00", "duration_s": 300,
            "tracks": [{"role": "me", "device": "Headset (Example Headphones)", "endpoint_id": "m"},
                       {"role": "them", "device": "Headphones (Example Headphones)", "endpoint_id": "x"},
                       {"role": "them", "device": "Speakers (Laptop Audio)", "endpoint_id": "r"}],
            "teams_outputs": {"x": [[0, 300]]}}
    md = render_markdown(meta, lines)
    assert md.startswith("# Meeting 15 Jan 2026, 10:00 (5 min)")
    assert "Others = Headphones (Example Headphones)" in md and "Laptop" not in md
    assert "**[00:00:19] Me:** This is a quick test call. Action item: send the report by Friday." in md
