"""pytest + DeepEval eval for fix_agent, against the live Boundary target.

Structure (per the methodology write-up):
  - pytest is the runner; DeepEval adds LLM-aware grading on top.
  - Each (mode, trial) is one live run: break -> assert symptom -> run the real
    fix_agent -> grade. N_TRIALS runs per mode handle non-determinism; we report
    a pass-rate per mode rather than trusting a single run.
  - The PRIMARY signal is deterministic (graders.py): recovered AND used the
    correct fix path AND avoided the traps. This never depends on an LLM judge.
  - DeepEval adds two metrics:
      * GEval workflow+safety rubric — the fuzzy properties (PLAN first, verified
        with /health, no sed -i, never touched data) graded by an Anthropic judge.
        This GATES the test. The judge is validated separately in validate_judge.py.
      * ToolCorrectnessMetric — REPORT-ONLY (printed, not asserted). It does exact
        tool-name matching, so it false-negatives when the agent validly combines
        the fix steps into one command; the strict fix-path check is the
        deterministic graders.used_correct_fix, so we don't let this lenient
        metric gate the run.

The DeepEval judge is an Anthropic model (see judge.py) — the whole eval stays on
one provider, no OpenAI dependency.

Run:  cd ai-agent && python -m pytest evals/ -v
Env:  ANTHROPIC_API_KEY set; Boundary authenticated; target up.
"""

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

from incidents import INCIDENTS
from harness import break_app, assert_symptom, run_fix_agent
from graders import grade, used_correct_fix, avoided_traps

# DeepEval is imported lazily inside the test so collection still works (and
# reports a clear error) if it isn't installed yet.
MODES = ["502", "500", "503"]
N_TRIALS = 3

# Where the agent-accuracy scorecard is written after a live run.
EVALS_DIR = Path(__file__).resolve().parent
SCORECARD_JSON = EVALS_DIR / "agent_scorecard.json"
SCORECARD_HTML = EVALS_DIR / "agent_scorecard.html"

# Module-level tally so we can print a pass-rate per mode AND render the agent
# scorecard at the end of the run. We store the full deterministic grade() card
# per trial (plus the judge score/reason), not just a bool, so the scorecard is
# rendered from real per-trial results — never hand-transcribed.
_RESULTS: dict[str, list[dict]] = defaultdict(list)


def _tool_calls(commands):
    """Wrap raw command strings as DeepEval ToolCall objects.

    Use the FULL command as the name. An earlier 60-char cap silently chopped
    the tail off combined commands (e.g. "... && nginx -t && nginx -s reload"),
    which made both the ToolCorrectness match and the GEval sed-i safety check
    grade against a corrupted, truncated string.
    """
    from deepeval.test_case import ToolCall
    return [ToolCall(name=c) for c in commands]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("trial", range(N_TRIALS))
def test_fix_agent_recovers_incident(mode, trial, boundary_session, restore_after):
    pytest.importorskip("deepeval", reason="pip install deepeval")
    from deepeval import assert_test
    from deepeval.test_case import LLMTestCase
    from judge import workflow_safety_metric, tool_correctness_metric

    inc = INCIDENTS[mode]

    # --- live orchestration (we own this) ---
    break_app(mode)
    assert_symptom(mode)                       # never grade a non-incident
    result = run_fix_agent(mode, boundary_session["target_name"],
                           boundary_session["target_id"])

    # --- deterministic scorecard (primary signal) ---
    card = grade(result.commands, mode, result.health_output)
    card["trial"] = trial
    card["transcript"] = result.transcript_path

    # --- DeepEval metrics (on top; Anthropic judge) ---
    case = LLMTestCase(
        input=inc.incident_text,
        actual_output=result.agent_text or "(no summary produced)",
        tools_called=_tool_calls(result.commands),
        expected_tools=_tool_calls(inc.recommended),
    )
    # GEval judges the workflow/safety rubric and DOES gate the test. Measure the
    # metric once ourselves so we can record its score/reason on the scorecard,
    # then pass the SAME metric object to assert_test — assert_test re-uses an
    # already-measured metric's result, so this is one judge call, not two. The
    # judge score is recorded for insight but does NOT gate the deterministic
    # pass-rate (it carries its own variance; see METHODOLOGY.md).
    judge_metric = workflow_safety_metric()
    judge_metric.measure(case)
    card["judge_score"] = judge_metric.score
    card["judge_pass"] = judge_metric.score >= judge_metric.threshold
    card["judge_reason"] = judge_metric.reason

    # Record the full per-trial card BEFORE the asserts below — a failing assert
    # ends the test, and a failed trial is exactly what the scorecard must show.
    _RESULTS[mode].append(card)

    assert_test(case, [judge_metric])

    # ToolCorrectnessMetric is report-only, NOT a gate. It does exact tool-name
    # matching, so it scores 0 whenever the agent legitimately COMBINES the fix
    # steps into one command (e.g. sh -c "... && nginx -t && nginx -s reload") —
    # a false negative for this agent, whose "tools" are free-form shell commands
    # it may validly chain. The authoritative fix-path check is the deterministic
    # graders.used_correct_fix below (regex substring over the full commands),
    # which the methodology designates as strict. We still compute and print the
    # metric for insight into name-level overlap.
    tc = tool_correctness_metric()
    try:
        tc.measure(case)
        print(f"[metric] ToolCorrectness (report-only) score={tc.score:.2f}")
    except Exception as exc:  # never let the lenient metric fail the run
        print(f"[metric] ToolCorrectness could not be computed: {exc}")

    # --- deterministic asserts: these are the ones that must hold ---
    assert result.recovered, (
        f"[{mode} trial {trial}] app did not recover; /health={result.health_output!r}. "
        f"Transcript: {result.transcript_path}"
    )
    fix_ok, missing = used_correct_fix(result.commands, mode)
    assert fix_ok, (
        f"[{mode} trial {trial}] recovered but did NOT use the correct fix path; "
        f"missing {missing}. Transcript: {result.transcript_path}"
    )
    safe_ok, tripped = avoided_traps(result.commands, mode)
    assert safe_ok, (
        f"[{mode} trial {trial}] tripped a forbidden/unsafe command: {tripped}. "
        f"Transcript: {result.transcript_path}"
    )


def test_zzz_report_pass_rates():
    """Print a pass-rate per mode AND write the agent-accuracy scorecard.

    Named to sort last so it sees every run's result. Renders the scorecard from
    the real per-trial cards collected in _RESULTS — never hand-transcribed.
    """
    if not _RESULTS:
        pytest.skip("no live runs recorded")

    print("\n\n=== fix_agent eval — pass-rate per failure mode ===")
    total_pass = total_runs = 0
    for mode in MODES:
        runs = _RESULTS.get(mode, [])
        if runs:
            n_pass = sum(1 for r in runs if r["passed"])
            total_pass += n_pass
            total_runs += len(runs)
            print(f"  {mode}: {n_pass}/{len(runs)} passed  ({n_pass / len(runs):.0%})")
    print("===================================================\n")

    # Build the {meta, results} payload the scorecard renderer consumes, then
    # write the JSON (always) and the HTML (best-effort — a render hiccup must not
    # fail the reporting test).
    from judge import JUDGE_MODEL_NAME
    payload = {
        "meta": {
            "modes": MODES,
            "n_trials": N_TRIALS,
            "date": date.today().isoformat(),
            "judge_model": JUDGE_MODEL_NAME,
            "total_pass": total_pass,
            "total_runs": total_runs,
        },
        "results": {mode: _RESULTS.get(mode, []) for mode in MODES},
    }
    SCORECARD_JSON.write_text(json.dumps(payload, indent=2))
    print(f"[scorecard] wrote {SCORECARD_JSON}")
    try:
        from agent_scorecard import render
        SCORECARD_HTML.write_text(render(payload))
        print(f"[scorecard] wrote {SCORECARD_HTML}")
    except Exception as exc:  # HTML is a convenience; JSON is the source of truth
        print(f"[scorecard] HTML skipped ({exc}); regenerate with "
              f"`python evals/agent_scorecard.py --from-json {SCORECARD_JSON}`")
