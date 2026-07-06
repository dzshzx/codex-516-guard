#!/usr/bin/env python3
"""A/B eval: does codexcomp's continuation folding fix the candy puzzle?

Runs a model x effort x proxy-on/off grid of `codex exec` calls on the candy
pigeonhole puzzle (answer: 21), records reasoning tokens + correctness per run,
and prints a per-condition summary. This is the harness behind the measurements
posted to openai/codex#30364 (issuecomment-4893087004).

Task credit: haowang02/codex-candy-eval (the community degradation eval);
prompt reproduced verbatim, graded by the same standalone-21 rule. The answer
(21) was independently verified by brute-force min-max before adoption.

Both modes wire `openai_base_url` explicitly, so results do not depend on the
ambient ~/.codex/config.toml wiring:
  on  -> the local codexcomp proxy (must already be running)
  off -> the upstream backend directly

Per-round fold data for `on` runs is read from the systemd journal when
available (journalctl --user -u codexcomp); without it the summary falls back
to final-usage fingerprints, which undercounts folded runs.

Usage:
  python evals/candy_eval.py                          # default grid, 5 reps
  python evals/candy_eval.py -m gpt-5.5 -r xhigh -n 3 --modes on,off
Results append to <out>/results.jsonl; completed run ids are skipped, so an
interrupted eval resumes by re-running the same command.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROMPT = """不使用任何外部工具回答以下问题：

在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）

        苹果味  桃子味  西瓜味
圆形       7      9      8
五角星形   7      6      4
"""
ANSWER_PATTERN = re.compile(r"(?<!\d)21(?!\d)")

STEP = 518
BOUNDARIES = {STEP * n - 2 for n in range(1, 41)}
TIMEOUTS = {"low": 600, "medium": 600, "high": 1200, "xhigh": 1800}
FOLD_ROUND_RE = re.compile(
    r"round (\d+): in=(\d+) (?:cached=(\d+) )?out=(\d+) reason=(\d+) total=(\d+) \| "
    r"n=(None|\d+) buffered=(\[.*?\]) -> (\w+)"
)


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def journal_available() -> bool:
    if not shutil.which("journalctl"):
        return False
    probe = subprocess.run(
        ["journalctl", "--user", "-u", "codexcomp", "-n", "1", "--no-pager"],
        capture_output=True, text=True)
    return probe.returncode == 0


def journal_fold_lines(since: str, until: str) -> list[str]:
    # done lines can lag the run's end by a moment; widen the tail.
    until_dt = datetime.strptime(until, "%Y-%m-%d %H:%M:%S") + timedelta(seconds=5)
    out = subprocess.run(
        ["journalctl", "--user", "-u", "codexcomp", "--since", since,
         "--until", until_dt.strftime("%Y-%m-%d %H:%M:%S"),
         "--no-pager", "--output=short-iso"],
        capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "codexcomp.fold" in l]


def fold_rounds(lines: list[str]) -> list[dict]:
    rounds = []
    for line in lines:
        m = FOLD_ROUND_RE.search(line)
        if m:
            rounds.append({"reason": int(m[5]), "verdict": m[9]})
    return rounds


def run_once(args, out_dir: Path, run_id: str, model: str, effort: str,
             mode: str, use_journal: bool, workdir: Path) -> dict:
    last_f = out_dir / f"last_{run_id}.txt"
    ev_f = out_dir / f"events_{run_id}.jsonl"
    base_url = args.proxy if mode == "on" else args.upstream
    cmd = ["timeout", str(TIMEOUTS[effort]), "codex", "exec", "--json",
           "--ephemeral", "-C", str(workdir), "--skip-git-repo-check",
           "-s", "read-only", "--disable", "memories",
           "-m", model, "-c", f"model_reasoning_effort={effort}",
           "-c", f'openai_base_url="{base_url}"',
           "-o", str(last_f), PROMPT]

    t0 = time.time()
    since = iso_now()
    with open(ev_f, "w") as evh:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=evh,
                              stderr=subprocess.PIPE, text=True)
    time.sleep(1)
    until = iso_now()

    usage = None
    for line in ev_f.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage")

    answer = last_f.read_text().strip() if last_f.exists() else ""
    lines = journal_fold_lines(since, until) if (mode == "on" and use_journal) else []
    if lines:
        (out_dir / f"fold_{run_id}.log").write_text("\n".join(lines) + "\n")

    return {
        "id": run_id, "model": model, "effort": effort, "mode": mode,
        "exit": proc.returncode, "duration_s": round(time.time() - t0, 1),
        "since": since, "until": until,
        "correct": bool(ANSWER_PATTERN.search(answer)),
        "reasoning_tokens": (usage or {}).get("reasoning_output_tokens"),
        "usage": usage,
        "fold_rounds": fold_rounds(lines),
        "stderr_tail": proc.stderr[-300:] if proc.returncode != 0 else "",
    }


def is_boundary_cut(rec: dict) -> bool:
    if rec["fold_rounds"]:
        return any(r["verdict"] != "clean" for r in rec["fold_rounds"])
    return rec["reasoning_tokens"] in BOUNDARIES


def summarize(recs: list[dict]) -> str:
    lines = [f"{'model':9} {'effort':7} {'mode':4} {'cut':>5} {'ok':>5}  reasoning tokens"]
    conds = sorted({(r["model"], r["effort"], r["mode"]) for r in recs},
                   key=lambda c: (c[0], list(TIMEOUTS).index(c[1]), c[2]))
    for model, effort, mode in conds:
        rs = [r for r in recs
              if (r["model"], r["effort"], r["mode"]) == (model, effort, mode)]
        ok = sum(r["correct"] for r in rs)
        cut = sum(1 for r in rs if is_boundary_cut(r))
        toks = sorted((r["reasoning_tokens"] if r["reasoning_tokens"] is not None else -1)
                      for r in rs)
        lines.append(f"{model:9} {effort:7} {mode:4} {cut:>2}/{len(rs)} {ok:>2}/{len(rs)}"
                     f"  {toks}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-m", "--models", default="gpt-5.4,gpt-5.5",
                        help="comma-separated model list")
    parser.add_argument("-r", "--efforts", default="low,medium,high,xhigh",
                        help="comma-separated reasoning efforts")
    parser.add_argument("--modes", default="on,off",
                        help="comma-separated: on (via proxy) / off (direct)")
    parser.add_argument("-n", "--reps", type=int, default=5)
    parser.add_argument("--proxy", default="http://127.0.0.1:8787/v1",
                        help="codexcomp base URL for `on` runs")
    parser.add_argument("--upstream", default="https://chatgpt.com/backend-api/codex",
                        help="direct base URL for `off` runs")
    parser.add_argument("--out", default="evals/results",
                        help="output directory (results.jsonl + per-run artifacts)")
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    efforts = [e.strip() for e in args.efforts.split(",") if e.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    for effort in efforts:
        if effort not in TIMEOUTS:
            parser.error(f"unknown effort {effort!r} (choose from {list(TIMEOUTS)})")
    for mode in modes:
        if mode not in ("on", "off"):
            parser.error(f"unknown mode {mode!r} (choose from on, off)")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = out_dir / "workdir"
    workdir.mkdir(exist_ok=True)
    results_path = out_dir / "results.jsonl"

    recs = []
    if results_path.exists():
        recs = [json.loads(l) for l in results_path.read_text().splitlines()]
    done_ids = {r["id"] for r in recs}

    use_journal = "on" in modes and journal_available()
    if "on" in modes and not use_journal:
        print("note: journalctl for codexcomp unavailable — per-round fold data "
              "will be missing; folded runs are undercounted from usage alone.")

    total = len(models) * len(efforts) * len(modes) * args.reps
    for rep in range(1, args.reps + 1):  # interleave conditions across time
        for model in models:
            for effort in efforts:
                for mode in modes:
                    run_id = f"{model}_{effort}_{mode}_r{rep}"
                    if run_id in done_ids:
                        continue
                    rec = run_once(args, out_dir, run_id, model, effort, mode,
                                   use_journal, workdir)
                    recs.append(rec)
                    with open(results_path, "a") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    print(f"[{iso_now()}] {len(recs)}/{total} {run_id} "
                          f"exit={rec['exit']} reason={rec['reasoning_tokens']} "
                          f"correct={rec['correct']}", flush=True)
                    time.sleep(2)

    print()
    print(summarize(recs))
    failures = [r["id"] for r in recs if r["exit"] != 0]
    if failures:
        print(f"\nfailed runs (excluded from nothing, judge for yourself): {failures}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
