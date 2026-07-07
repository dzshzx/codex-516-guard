"""Self-test for the fold state machine with canned upstream rounds.

Run: uv run python test_fold.py
"""
import asyncio
import json

from codexcomp.fold import (
    DONE,
    RoundOpenError,
    fold,
    requested_effort,
    should_retry_zero_reasoning,
    STEP,
)


def reasoning_round(rid: str, reasoning_toks: int, text: str | None, enc: bool = True):
    """Canned upstream events for one round."""
    item = {"id": rid, "type": "reasoning", "summary": []}
    done_item = dict(item)
    if enc:
        done_item["encrypted_content"] = "ENC_" + rid
    evs = [
        {"type": "response.created", "sequence_number": 0,
         "response": {"id": "resp_1", "created_at": 111, "status": "in_progress"}},
        {"type": "response.in_progress", "sequence_number": 1, "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_item.done", "output_index": 0, "item": done_item},
    ]
    if text is not None:
        msg = {"id": "msg_" + rid, "type": "message", "role": "assistant"}
        evs += [
            {"type": "response.output_item.added", "output_index": 1, "item": msg},
            {"type": "response.output_text.delta", "output_index": 1,
             "item_id": msg["id"], "content_index": 0, "delta": text},
            {"type": "response.output_item.done", "output_index": 1,
             "item": {**msg, "content": [{"type": "output_text", "text": text}]}},
        ]
    evs.append({"type": "response.completed", "response": {
        "id": "resp_1", "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": reasoning_toks + (20 if text else 0),
                  "total_tokens": 120 + reasoning_toks,
                  "output_tokens_details": {"reasoning_tokens": reasoning_toks}},
    }})
    # NB: real upstream sends [DONE] after the terminal event; the fold stops at
    # the terminal, so DONE never reaches it — stream close is the terminator.
    return evs


def message_round(rid: str, reasoning_toks: int, text: str):
    """Canned upstream events for a message-only round."""
    msg = {"id": "msg_" + rid, "type": "message", "role": "assistant"}
    return [
        {"type": "response.created", "sequence_number": 0,
         "response": {"id": "resp_1", "created_at": 111, "status": "in_progress"}},
        {"type": "response.in_progress", "sequence_number": 1, "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0, "item": msg},
        {"type": "response.output_text.delta", "output_index": 0,
         "item_id": msg["id"], "content_index": 0, "delta": text},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {**msg, "content": [{"type": "output_text", "text": text}]}},
        {"type": "response.completed", "response": {
            "id": "resp_1", "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": reasoning_toks + 20,
                      "total_tokens": 120 + reasoning_toks,
                      "output_tokens_details": {"reasoning_tokens": reasoning_toks}},
        }},
    ]


async def collect_fold(base_body, rounds):
    """Run fold() against canned rounds and return opened bodies plus events."""
    opened_bodies = []

    async def opener(body):
        opened_bodies.append(body)
        idx = len(opened_bodies) - 1

        async def gen():
            for ev in rounds[idx]:
                yield ev
        return gen()

    out = [ev async for ev in fold(base_body, opener)]
    return opened_bodies, out


async def test_happy_fold():
    opened_bodies = []
    rounds = [
        reasoning_round("rs_1", STEP - 2, "TRUNCATED ANSWER"),   # 516 -> continue
        reasoning_round("rs_2", 2 * STEP - 2, "STILL TRUNCATED"),  # 1034 -> continue
        reasoning_round("rs_3", 404, "REAL ANSWER"),             # clean
    ]

    async def opener(body):
        opened_bodies.append(body)
        idx = len(opened_bodies) - 1

        async def gen():
            for ev in rounds[idx]:
                yield ev
        return gen()

    out = []
    async for ev in fold({"model": "gpt-5.5", "input": [{"type": "message", "role": "user"}],
                          "stream": True}, opener):
        out.append(ev)

    # --- assertions -----------------------------------------------------------
    assert len(opened_bodies) == 3, f"expected 3 rounds, got {len(opened_bodies)}"

    # continuation bodies replay reasoning + commentary nudge, drop prev id
    b2 = opened_bodies[1]
    types2 = [i.get("type") for i in b2["input"]]
    assert types2 == ["message", "reasoning", "message"], types2
    assert b2["input"][-1]["phase"] == "commentary"
    assert "reasoning.encrypted_content" in b2["include"]
    b3 = opened_bodies[2]
    assert [i.get("type") for i in b3["input"]] == [
        "message", "reasoning", "message", "reasoning", "message"]

    dict_events = [e for e in out if isinstance(e, dict)]
    # exactly one terminal, and it is the LAST dict event
    terminals = [e for e in dict_events if e["type"].startswith("response.")
                 and e["type"] in ("response.completed", "response.failed", "response.incomplete")]
    assert len(terminals) == 1 and dict_events[-1] is terminals[0]
    term = terminals[0]["response"]

    # truncated messages are discarded; only the clean round's text is flushed
    deltas = [e["delta"] for e in dict_events if e["type"] == "response.output_text.delta"]
    assert deltas == ["REAL ANSWER"], deltas

    # sequence numbers proxy-owned and monotonic; output_index renumbered 0..3
    seqs = [e["sequence_number"] for e in dict_events]
    assert seqs == list(range(len(seqs))), "sequence not monotonic"
    ois = sorted({e.get("output_index") for e in dict_events if "output_index" in e})
    assert ois == [0, 1, 2, 3], ois  # 3 reasoning items + 1 flushed message

    # usage: reasoning summed, input from round 1, billed usage in metadata
    u = term["usage"]
    expect_reason = (STEP - 2) + (2 * STEP - 2) + 404
    assert u["output_tokens_details"]["reasoning_tokens"] == expect_reason
    assert u["input_tokens"] == 100
    assert term["metadata"]["proxy_billed_usage"]["input_tokens"] == 300
    assert [r["n"] for r in term["metadata"]["proxy_rounds"]] == [1, 2, None]

    # output preserved in order: rs_1, rs_2, rs_3 reasoning + final message
    otypes = [(i["type"], i.get("id")) for i in term["output"]]
    assert otypes == [("reasoning", "rs_1"), ("reasoning", "rs_2"),
                      ("reasoning", "rs_3"), ("message", "msg_rs_3")], otypes

    print("terminal usage:", json.dumps(u))


async def test_round1_rejected():
    """Upstream rejects round 1: fold itself yields the response.failed terminal."""
    async def opener(body):
        raise RoundOpenError(429, "quota exceeded")

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    assert len(out) == 1, out
    ev = out[0]
    assert ev["type"] == "response.failed", ev
    assert ev["response"]["status"] == "failed"
    assert ev["response"]["error"]["code"] == 429
    assert "quota exceeded" in ev["response"]["error"]["message"]
    assert ev["sequence_number"] == 0


async def test_continuation_open_fails():
    """Round 2 fails to open: incomplete terminal, round 1 reasoning kept,
    truncated message never flushed."""
    calls = []

    async def opener(body):
        calls.append(body)
        if len(calls) > 1:
            raise RoundOpenError(500, "boom")

        async def gen():
            for ev in reasoning_round("rs_1", STEP - 2, "TRUNCATED"):
                yield ev
        return gen()

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    assert len(calls) == 2, len(calls)
    term = out[-1]
    assert term["type"] == "response.incomplete", term
    resp = term["response"]
    assert resp["incomplete_details"]["reason"] == "upstream_error"
    assert resp["metadata"]["proxy_stopped_reason"] == "upstream_error"
    assert [i["type"] for i in resp["output"]] == ["reasoning"]
    deltas = [e["delta"] for e in out
              if isinstance(e, dict) and e["type"] == "response.output_text.delta"]
    assert deltas == [], deltas


async def test_upstream_eof():
    """Stream ends without a terminal event: incomplete terminal, tentative
    output is not an answer."""
    evs = reasoning_round("rs_1", 404, "TENTATIVE")[:-1]  # strip the terminal

    async def opener(body):
        async def gen():
            for ev in evs:
                yield ev
        return gen()

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    term = out[-1]
    assert term["type"] == "response.incomplete", term
    assert term["response"]["incomplete_details"]["reason"] == "upstream_eof"
    assert [i["type"] for i in term["response"]["output"]] == ["reasoning"]
    deltas = [e["delta"] for e in out
              if isinstance(e, dict) and e["type"] == "response.output_text.delta"]
    assert deltas == [], deltas


async def test_zero_reasoning_retry_for_high_effort_gpt55():
    """A zero-reasoning high-effort gpt-5.5 round is retried once, and the
    tentative zero-reasoning answer is not flushed."""
    base = {
        "model": "gpt-5.5",
        "reasoning": {"effort": "extra high"},
        "input": [{"type": "message", "role": "user"}],
        "stream": True,
    }
    opened_bodies, out = await collect_fold(base, [
        message_round("zero", 0, "ZERO ANSWER"),
        message_round("clean", 123, "REAL ANSWER"),
    ])

    assert len(opened_bodies) == 2, len(opened_bodies)
    b2 = opened_bodies[1]
    assert [i.get("type") for i in b2["input"]] == ["message", "message"]
    assert b2["input"][-1]["phase"] == "commentary"
    assert "reasoning.encrypted_content" in b2["include"]

    dict_events = [e for e in out if isinstance(e, dict)]
    deltas = [e["delta"] for e in dict_events if e["type"] == "response.output_text.delta"]
    assert deltas == ["REAL ANSWER"], deltas

    term = dict_events[-1]["response"]
    assert [r["zero_reasoning_retry"] for r in term["metadata"]["proxy_rounds"]] == [
        True, False]
    assert term["usage"]["output_tokens_details"]["reasoning_tokens"] == 123


async def test_zero_reasoning_retry_is_narrowly_gated():
    """Zero-reasoning retry should not apply to every model or effort."""
    assert requested_effort({"reasoning": {"effort": "extra high"}}) == "extra_high"
    assert requested_effort({"reasoning": {"effort": " extra high "}}) == "extra_high"
    assert requested_effort({"reasoning": {"effort": "extra-high"}}) == "extra_high"
    assert requested_effort({"reasoning": {"effort": "extraHigh"}}) == "extrahigh"
    assert requested_effort({"reasoning": {"effort": "x-high"}}) == "x_high"
    assert should_retry_zero_reasoning(
        {"model": "gpt-5.5-2026-07-06", "reasoning": {"effort": "x-high"}}, 0)
    assert not should_retry_zero_reasoning(
        {"model": "gpt-5.4", "reasoning": {"effort": "high"}}, 0)

    base = {
        "model": "gpt-5.5",
        "reasoning": {"effort": "medium"},
        "input": [{"type": "message", "role": "user"}],
        "stream": True,
    }
    opened_bodies, out = await collect_fold(base, [
        message_round("medium", 0, "ZERO MEDIUM"),
    ])

    assert len(opened_bodies) == 1, len(opened_bodies)
    deltas = [e["delta"] for e in out
              if isinstance(e, dict) and e["type"] == "response.output_text.delta"]
    assert deltas == ["ZERO MEDIUM"], deltas


async def test_repeated_zero_reasoning_respects_continue_cap():
    """Repeated zero-reasoning rounds stop at MAX_CONTINUE, same as 518n-2
    continuations do."""
    base = {
        "model": "gpt-5.5",
        "reasoning": {"effort": "high"},
        "input": [{"type": "message", "role": "user"}],
        "stream": True,
    }
    opened_bodies, out = await collect_fold(base, [
        message_round("zero1", 0, "ZERO 1"),
        message_round("zero2", 0, "ZERO 2"),
        message_round("zero3", 0, "ZERO 3"),
        message_round("zero4", 0, "ZERO 4"),
    ])

    assert len(opened_bodies) == 4, len(opened_bodies)
    dict_events = [e for e in out if isinstance(e, dict)]
    deltas = [e["delta"] for e in dict_events if e["type"] == "response.output_text.delta"]
    assert deltas == ["ZERO 4"], deltas
    term = dict_events[-1]["response"]
    assert term["metadata"]["proxy_stopped_reason"] == "zero_reasoning_max_continue"
    assert [r["zero_reasoning_retry"] for r in term["metadata"]["proxy_rounds"]] == [
        True, True, True, True]


async def main():
    await test_happy_fold()
    await test_round1_rejected()
    await test_continuation_open_fails()
    await test_upstream_eof()
    await test_zero_reasoning_retry_for_high_effort_gpt55()
    await test_zero_reasoning_retry_is_narrowly_gated()
    await test_repeated_zero_reasoning_respects_continue_cap()
    print("fold self-test: ALL PASS")


asyncio.run(main())
