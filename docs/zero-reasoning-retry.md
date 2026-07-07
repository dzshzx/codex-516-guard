# Zero-Reasoning Retry

This note explains the narrow zero-reasoning retry added on top of
codexcomp's existing `518n - 2` fold path.

## Context

codexcomp already retries and folds rounds that end at the public truncation
fingerprint described in
[openai/codex#30364](https://github.com/openai/codex/issues/30364): `516`,
`1034`, `1552`, and other `518n - 2` reasoning-token boundaries.

A separate failure shape can appear when a request explicitly asks for a
high-effort `gpt-5.5` response but the terminal event reports
`reasoning_tokens == 0`. Zero reasoning can be valid for tiny direct answers,
so the retry is intentionally not global.

## Scope

The retry only fires when all of these are true:

- `output_tokens_details.reasoning_tokens == 0`
- the requested model is `gpt-5.5` or a `gpt-5.5-*` snapshot
- the requested reasoning effort normalizes to `high`, `xhigh`, `x_high`,
  `extra_high`, or `extrahigh`
- the upstream round reached a terminal event
- the shared continuation cap has not been exhausted

Other models, other efforts, missing usage, and normal nonzero reasoning rounds
keep their previous behavior.

## Behavior

The zero-reasoning retry uses the same folding contract as the boundary retry:

1. Buffer tentative message/tool output from the zero-reasoning round.
2. Append the usual `phase:"commentary"` `"Continue thinking..."` nudge to the
   replayed input.
3. Open the next upstream round.
4. Stream reasoning live and flush only the final accepted round's buffered
   output.
5. Record per-round metadata in `metadata.proxy_rounds`.

When zero-reasoning repeats, codexcomp stops at the same `MAX_CONTINUE` guard
used by the `518n - 2` path and records
`metadata.proxy_stopped_reason = "zero_reasoning_max_continue"`.

## Why This Shape

The implementation is deliberately conservative:

- It does not change clean-pass behavior for normal rounds.
- It preserves the existing encrypted-reasoning requirement for `518n - 2`
  boundary continuations.
- It avoids retrying every zero-reasoning answer, because some simple prompts
  legitimately need no hidden reasoning.
- It keeps cost bounded by reusing the existing continuation limit.

## Tests

The tests use synthetic Responses event streams only. They cover:

- zero reasoning on high/extra-high `gpt-5.5` retries and suppresses the
  tentative first answer
- the clean follow-up answer is flushed as the visible response
- medium-effort `gpt-5.5` zero reasoning does not retry
- non-`gpt-5.5` zero reasoning does not retry
- repeated zero-reasoning rounds respect the continuation cap
- the existing `516`/`1034` fold test still passes unchanged

Run:

```bash
uv run python test_fold.py
```
