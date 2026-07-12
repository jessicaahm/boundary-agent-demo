#!/usr/bin/env python3
"""Render an agent-accuracy scorecard from a live fix_agent eval run.

This is the AGENT counterpart to scorecard.py (which renders JUDGE accuracy —
how well the LLM judge agrees with hand labels). This one answers the operational
question: how accurately does the fix_agent itself recover incidents?

Like scorecard.py, it renders ONLY from real per-trial results — the deterministic
grade() cards produced by an actual live run (test_fix_agent.py writes them out).
Nothing here is hand-transcribed, so the HTML can't drift from the numbers.

A run "passes" by the deterministic lucky-fix guard (graders.grade): it
recovered AND used the correct fix path AND avoided the traps. The LLM judge score
is recorded and shown per trial for insight, but it does NOT gate the headline
pass-rate (it carries its own variance; see METHODOLOGY.md).

Two ways to use it:

    # from the JSON the eval wrote on its last live run:
    cd ai-agent && python evals/agent_scorecard.py \
        --from-json evals/agent_scorecard.json -o evals/agent_scorecard.html

    # or import render() and hand it a {meta, results} payload directly
    # (that's what test_fix_agent.py does at the end of a live run).

Payload shape (one entry per trial, grouped by mode):
    {
      "meta": {"modes": [...], "n_trials": 3, "date": "...",
               "judge_model": "...", "total_pass": 7, "total_runs": 9},
      "results": {"502": [ {trial, recovered, used_correct_fix, avoided_traps,
                            missing_fix_steps, tripped_traps, passed,
                            judge_score, judge_pass, judge_reason, transcript},
                           ... ], "500": [...], "503": [...]}
    }
A mode with an empty list renders as a muted "not yet run" placeholder, so the
scorecard is valid and viewable before any live run has happened.
"""

import argparse
import html
import json
import sys
from pathlib import Path

# ai-agent/ (for boundary_common) and evals/ (for incidents) on path, matching
# scorecard.py — so this runs standalone from the ai-agent/ dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# One arc-length so the donut's stroke-dasharray matches the SVG circle (r=56).
DONUT_CIRCUMFERENCE = 2 * 3.141592653589793 * 56

# The three deterministic dimensions, in display order. Each maps a card key to a
# column header — this is the definition of "correct fix", shown per trial.
DIMENSIONS = [
    ("recovered", "Recovered"),
    ("used_correct_fix", "Correct fix"),
    ("avoided_traps", "No traps"),
]


def _tick(ok: bool) -> str:
    """A green check or red cross cell for a boolean dimension."""
    if ok:
        return '<span class="tick-ok" title="pass">&check;</span>'
    return '<span class="tick-no" title="fail">&times;</span>'


def _detail(card: dict) -> str:
    """The 'why it failed' line: which fix steps were missing / traps tripped.

    These come straight from grade() (missing_fix_steps, tripped_traps) and are the
    single most useful diagnostic on the card — they name exactly what went wrong.
    """
    bits = []
    missing = card.get("missing_fix_steps") or []
    tripped = card.get("tripped_traps") or []
    if missing:
        pats = ", ".join(f"<code>{html.escape(p)}</code>" for p in missing)
        bits.append(f'<span class="miss">missing: {pats}</span>')
    if tripped:
        pats = ", ".join(f"<code>{html.escape(p)}</code>" for p in tripped)
        bits.append(f'<span class="trip">tripped: {pats}</span>')
    return '<div class="detail">' + " &middot; ".join(bits) + "</div>" if bits else ""


def _judge_cell(card: dict) -> str:
    """The judge score column: a score + pass/fail chip, or a dash if not recorded."""
    if card.get("judge_score") is None:
        return '<span class="thresh">&mdash;</span>'
    score = float(card["judge_score"])
    ok = bool(card.get("judge_pass"))
    hi_lo = "hi" if ok else "lo"
    reason = html.escape(card.get("judge_reason") or "")
    return (f'<span class="score {hi_lo}" title="{reason}">{score:.2f}</span>')


def _mode_section(mode: str, trials: list, n_trials: int) -> str:
    """Render one failure mode: a pass-rate header + a per-trial table.

    An empty `trials` list means this mode has no live data yet — render a muted
    placeholder rather than fabricate numbers.
    """
    if not trials:
        return f"""      <div class="mode">
        <div class="mode-head">
          <span class="mode-name">{html.escape(mode)}</span>
          <span class="mode-rate placeholder">&mdash; not yet run &mdash;</span>
        </div>
        <div class="placeholder-body">No live run recorded for this mode yet.
          Run <code>python -m pytest evals/ -v</code> to fill it in.</div>
      </div>"""

    n_pass = sum(1 for t in trials if t.get("passed"))
    n = len(trials)
    rate = n_pass / n if n else 0
    rate_cls = "hi" if n_pass == n else ("lo" if n_pass == 0 else "mid")

    rows = []
    for t in trials:
        pass_cls = "pass" if t.get("passed") else "fail"
        dim_cells = "".join(f"<td class='dim'>{_tick(bool(t.get(k)))}</td>"
                            for k, _ in DIMENSIONS)
        # Transcript link (relative path as stored); shown as a short basename.
        tp = t.get("transcript") or ""
        tp_name = html.escape(Path(tp).name) if tp else ""
        tp_cell = (f'<a class="tscript" href="{html.escape(tp)}">{tp_name}</a>'
                   if tp else '<span class="thresh">&mdash;</span>')
        rows.append(f"""          <tr class="{pass_cls}">
            <td class="tnum">#{t.get('trial', '?')}</td>
            {dim_cells}
            <td class="num">{_judge_cell(t)}</td>
            <td>
              <span class="badge {pass_cls}"><span class="ico"></span>{'PASS' if t.get('passed') else 'FAIL'}</span>
              {_detail(t)}
            </td>
            <td class="tscript-cell">{tp_cell}</td>
          </tr>""")

    dim_heads = "".join(f'<th class="dim">{h}</th>' for _, h in DIMENSIONS)
    return f"""      <div class="mode">
        <div class="mode-head">
          <span class="mode-name">{html.escape(mode)}</span>
          <span class="mode-rate {rate_cls}">{n_pass}/{n} passed &nbsp;({rate:.0%})</span>
        </div>
        <div class="scroll">
          <table class="trials">
            <thead>
              <tr>
                <th class="tnum">Trial</th>
                {dim_heads}
                <th class="num">Judge</th>
                <th>Verdict</th>
                <th>Transcript</th>
              </tr>
            </thead>
            <tbody>
{chr(10).join(rows)}
            </tbody>
          </table>
        </div>
      </div>"""


def render(data) -> str:
    """Build the full agent-accuracy scorecard HTML from a {meta, results} payload."""
    meta = data["meta"]
    results = data.get("results", {})
    modes = meta.get("modes") or list(results.keys())

    total_pass = int(meta.get("total_pass", 0))
    total_runs = int(meta.get("total_runs", 0))
    pct = round(100 * total_pass / total_runs) if total_runs else 0
    all_pass = total_runs > 0 and total_pass == total_runs
    any_data = total_runs > 0

    # Donut arc: green sweep proportional to overall pass-rate.
    dash = DONUT_CIRCUMFERENCE
    offset = dash * (1 - total_pass / total_runs) if total_runs else dash
    if not any_data:
        ring_color = "var(--ink-faint)"
    elif all_pass:
        ring_color = "var(--pass)"
    else:
        ring_color = "var(--fail)" if total_pass == 0 else "var(--accent)"

    if not any_data:
        banner_cls, banner = "muted", (
            "<b>No live run yet.</b> This is the template &mdash; run "
            "<code>python -m pytest evals/ -v</code> against the target to fill in "
            "real per-mode pass-rates.")
    elif all_pass:
        banner_cls, banner = "ok", (
            f"<b>All {total_runs} runs passed.</b> Every trial recovered the app "
            "using the correct fix path and avoided the traps.")
    else:
        banner_cls, banner = "bad", (
            f"<b>{total_runs - total_pass}/{total_runs} runs failed.</b> A fail "
            "means the run did not recover, used the wrong fix path, or tripped a "
            "trap &mdash; see the per-trial detail below.")

    sections = "\n".join(
        _mode_section(m, results.get(m, []), meta.get("n_trials", 0)) for m in modes)

    return TEMPLATE.format(
        judge_model=html.escape(str(meta.get("judge_model", "n/a"))),
        n_trials=html.escape(str(meta.get("n_trials", "?"))),
        date=html.escape(str(meta.get("date", ""))),
        pct=pct,
        total_pass=total_pass,
        total_runs=total_runs,
        n_modes=len(modes),
        dash=f"{dash:.2f}",
        offset=f"{offset:.2f}",
        ring_color=ring_color,
        banner_cls=banner_cls,
        banner=banner,
        pct_v_cls="good" if all_pass else ("bad" if any_data and total_pass == 0 else ""),
        sections=sections,
    )


TEMPLATE = """<title>Agent Accuracy Scorecard</title>
<style>
  :root {{
    --ground: #f7f8fa; --surface: #ffffff; --surface-2: #f0f2f5; --border: #e2e5ea;
    --ink: #1c2128; --ink-dim: #59626e; --ink-faint: #8b94a1; --accent: #6366f1;
    --accent-soft: #eef0fe; --pass: #16a34a; --pass-soft: #e7f6ec; --fail: #dc2626;
    --fail-soft: #fdeaea;
    --shadow: 0 1px 2px rgba(20,25,40,.04), 0 4px 16px rgba(20,25,40,.05);
    --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
    --ui: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --ground: #0e1116; --surface: #161b22; --surface-2: #1c222b; --border: #2a313c;
      --ink: #e6edf3; --ink-dim: #9aa4b2; --ink-faint: #6a7382; --accent: #818cf8;
      --accent-soft: #23264a; --pass: #3fb950; --pass-soft: #12261a; --fail: #f85149;
      --fail-soft: #2a1517;
      --shadow: 0 1px 2px rgba(0,0,0,.3), 0 4px 20px rgba(0,0,0,.35);
    }}
  }}
  :root[data-theme="light"] {{
    --ground: #f7f8fa; --surface: #ffffff; --surface-2: #f0f2f5; --border: #e2e5ea;
    --ink: #1c2128; --ink-dim: #59626e; --ink-faint: #8b94a1; --accent: #6366f1;
    --accent-soft: #eef0fe; --pass: #16a34a; --pass-soft: #e7f6ec; --fail: #dc2626;
    --fail-soft: #fdeaea; --shadow: 0 1px 2px rgba(20,25,40,.04), 0 4px 16px rgba(20,25,40,.05);
  }}
  :root[data-theme="dark"] {{
    --ground: #0e1116; --surface: #161b22; --surface-2: #1c222b; --border: #2a313c;
    --ink: #e6edf3; --ink-dim: #9aa4b2; --ink-faint: #6a7382; --accent: #818cf8;
    --accent-soft: #23264a; --pass: #3fb950; --pass-soft: #12261a; --fail: #f85149;
    --fail-soft: #2a1517; --shadow: 0 1px 2px rgba(0,0,0,.3), 0 4px 20px rgba(0,0,0,.35);
  }}

  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--ground); color: var(--ink); font-family: var(--ui); line-height: 1.5; -webkit-font-smoothing: antialiased; }}
  .wrap {{ max-width: 1040px; margin: 0 auto; padding: 40px 24px 64px; }}

  header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; flex-wrap: wrap; margin-bottom: 28px; }}
  .title-block h1 {{ margin: 0; font-size: 1.55rem; letter-spacing: -.02em; font-weight: 650; }}
  .title-block .sub {{ margin: 4px 0 0; color: var(--ink-dim); font-size: .92rem; }}
  .runmeta {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
  .chip {{ font-family: var(--mono); font-size: .72rem; color: var(--ink-dim); background: var(--surface); border: 1px solid var(--border); border-radius: 999px; padding: 5px 11px; white-space: nowrap; }}
  .chip b {{ color: var(--ink); font-weight: 600; }}
  code {{ font-family: var(--mono); font-size: .82rem; }}
  code.inline {{ font-family: var(--mono); font-size: .82rem; }}

  .overview {{ display: grid; grid-template-columns: auto 1fr; gap: 24px; align-items: center; background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 24px 28px; box-shadow: var(--shadow); margin-bottom: 28px; }}
  .donut {{ position: relative; width: 132px; height: 132px; }}
  .donut svg {{ transform: rotate(-90deg); }}
  .donut .center {{ position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1px; }}
  .donut .pct {{ font-family: var(--mono); font-size: 1.9rem; font-weight: 600; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }}
  .donut .pct-label {{ font-size: .68rem; text-transform: uppercase; letter-spacing: .09em; color: var(--ink-faint); }}

  .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; }}
  .stat {{ display: flex; flex-direction: column; gap: 3px; }}
  .stat .k {{ font-size: .72rem; text-transform: uppercase; letter-spacing: .07em; color: var(--ink-faint); font-weight: 600; }}
  .stat .v {{ font-family: var(--mono); font-size: 1.5rem; font-weight: 600; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }}
  .stat .v.good {{ color: var(--pass); }}
  .stat .v.bad  {{ color: var(--fail); }}
  .stat .note {{ font-size: .74rem; color: var(--ink-dim); }}

  .verdict-banner {{ display: flex; align-items: center; gap: 10px; margin: 0 0 28px; border-radius: 12px; padding: 12px 16px; font-size: .9rem; font-weight: 500; }}
  .verdict-banner.ok {{ background: var(--pass-soft); border: 1px solid color-mix(in srgb, var(--pass) 35%, transparent); color: var(--pass); }}
  .verdict-banner.bad {{ background: var(--fail-soft); border: 1px solid color-mix(in srgb, var(--fail) 35%, transparent); color: var(--fail); }}
  .verdict-banner.muted {{ background: var(--surface-2); border: 1px solid var(--border); color: var(--ink-dim); }}
  .verdict-banner .dot {{ width: 8px; height: 8px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 4px color-mix(in srgb, currentColor 20%, transparent); flex: none; }}
  .verdict-banner b {{ font-weight: 650; }}
  .verdict-banner code {{ background: color-mix(in srgb, currentColor 12%, transparent); padding: 1px 6px; border-radius: 5px; }}

  .mode {{ background: var(--surface); border: 1px solid var(--border); border-radius: 16px; box-shadow: var(--shadow); overflow: hidden; margin-bottom: 18px; }}
  .mode-head {{ display: flex; align-items: baseline; gap: 14px; padding: 15px 20px; border-bottom: 1px solid var(--border); background: var(--surface-2); }}
  .mode-name {{ font-family: var(--mono); font-size: 1.15rem; font-weight: 700; letter-spacing: -.01em; }}
  .mode-rate {{ font-family: var(--mono); font-size: .9rem; font-weight: 650; }}
  .mode-rate.hi {{ color: var(--pass); }}
  .mode-rate.lo {{ color: var(--fail); }}
  .mode-rate.mid {{ color: var(--accent); }}
  .mode-rate.placeholder {{ color: var(--ink-faint); font-weight: 500; }}
  .placeholder-body {{ padding: 18px 20px; color: var(--ink-dim); font-size: .86rem; }}

  .scroll {{ overflow-x: auto; }}
  table.trials {{ width: 100%; border-collapse: collapse; min-width: 720px; }}
  table.trials thead th {{ text-align: left; font-size: .7rem; text-transform: uppercase; letter-spacing: .07em; color: var(--ink-faint); font-weight: 650; padding: 11px 14px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  table.trials th.dim, table.trials td.dim {{ text-align: center; }}
  table.trials th.num, table.trials td.num {{ text-align: right; }}
  table.trials th.tnum, table.trials td.tnum {{ text-align: left; }}
  table.trials tbody td {{ padding: 13px 14px; border-bottom: 1px solid var(--border); vertical-align: top; font-size: .88rem; }}
  table.trials tbody tr:last-child td {{ border-bottom: none; }}
  table.trials tbody tr {{ position: relative; }}
  table.trials tbody tr::before {{ content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; }}
  table.trials tbody tr.pass::before {{ background: var(--pass); }}
  table.trials tbody tr.fail::before {{ background: var(--fail); }}
  table.trials tbody tr:hover {{ background: color-mix(in srgb, var(--accent) 4%, transparent); }}
  td.tnum {{ font-family: var(--mono); color: var(--ink-dim); font-variant-numeric: tabular-nums; }}

  .tick-ok {{ color: var(--pass); font-weight: 800; font-size: 1.05rem; }}
  .tick-no {{ color: var(--fail); font-weight: 800; font-size: 1.05rem; }}

  td.num {{ font-family: var(--mono); font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .score {{ font-weight: 650; font-size: .95rem; cursor: help; }}
  .score.hi {{ color: var(--pass); }}
  .score.lo {{ color: var(--fail); }}
  .thresh {{ color: var(--ink-faint); font-size: .82rem; }}

  .badge {{ display: inline-flex; align-items: center; gap: 6px; font-family: var(--mono); font-size: .7rem; font-weight: 650; padding: 4px 10px; border-radius: 999px; white-space: nowrap; }}
  .badge.pass {{ color: var(--pass); background: var(--pass-soft); }}
  .badge.fail {{ color: var(--fail); background: var(--fail-soft); }}
  .badge .ico {{ width: 6px; height: 6px; border-radius: 50%; background: currentColor; }}

  .detail {{ margin-top: 6px; font-size: .76rem; line-height: 1.5; }}
  .detail .miss {{ color: var(--fail); }}
  .detail .trip {{ color: var(--fail); }}
  .detail code {{ font-size: .72rem; background: var(--surface-2); padding: 1px 5px; border-radius: 4px; }}

  .tscript-cell {{ white-space: nowrap; }}
  a.tscript {{ font-family: var(--mono); font-size: .74rem; color: var(--accent); text-decoration: none; }}
  a.tscript:hover {{ text-decoration: underline; }}

  .section-label {{ display: flex; align-items: baseline; gap: 10px; margin: 4px 4px 12px; }}
  .section-label h2 {{ margin: 0; font-size: .82rem; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-dim); font-weight: 650; }}
  .section-label .count {{ font-family: var(--mono); font-size: .78rem; color: var(--ink-faint); }}

  footer {{ margin-top: 22px; padding: 0 4px; color: var(--ink-faint); font-size: .76rem; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
  footer code {{ font-family: var(--mono); color: var(--ink-dim); background: var(--surface-2); padding: 1px 6px; border-radius: 5px; }}

  @media (max-width: 640px) {{
    .overview {{ grid-template-columns: 1fr; justify-items: center; text-align: center; }}
    .stats {{ width: 100%; }}
  }}
</style>

<div class="wrap">
  <header>
    <div class="title-block">
      <h1>Agent Accuracy Scorecard</h1>
      <p class="sub">Does <code class="inline">fix_agent</code> recover incidents with the <b>correct</b> fix &mdash; not a lucky one? &mdash; live eval, <code class="inline">test_fix_agent.py</code></p>
    </div>
    <div class="runmeta">
      <span class="chip">agent+judge&nbsp;<b>{judge_model}</b></span>
      <span class="chip">trials/mode&nbsp;<b>{n_trials}</b></span>
      <span class="chip">run&nbsp;<b>{date}</b></span>
    </div>
  </header>

  <section class="overview">
    <div class="donut" role="img" aria-label="{pct} percent of runs passed">
      <svg width="132" height="132" viewBox="0 0 132 132">
        <circle cx="66" cy="66" r="56" fill="none" stroke="var(--surface-2)" stroke-width="14"/>
        <circle cx="66" cy="66" r="56" fill="none" stroke="{ring_color}" stroke-width="14"
                stroke-linecap="round" stroke-dasharray="{dash}" stroke-dashoffset="{offset}"/>
      </svg>
      <div class="center">
        <span class="pct">{pct}%</span>
        <span class="pct-label">Pass rate</span>
      </div>
    </div>
    <div class="stats">
      <div class="stat">
        <span class="k">Runs passed</span>
        <span class="v {pct_v_cls}">{total_pass}/{total_runs}</span>
        <span class="note">recovered + correct fix + no traps</span>
      </div>
      <div class="stat">
        <span class="k">Failure modes</span>
        <span class="v">{n_modes}</span>
        <span class="note">502 / 500 / 503 incidents</span>
      </div>
      <div class="stat">
        <span class="k">Trials / mode</span>
        <span class="v">{n_trials}</span>
        <span class="note">pass-rate, not single run</span>
      </div>
    </div>
  </section>

  <div class="verdict-banner {banner_cls}">
    <span class="dot" aria-hidden="true"></span>
    <span>{banner}</span>
  </div>

  <div class="section-label">
    <h2>Per failure mode</h2>
    <span class="count">a run passes only if it recovered AND used the correct fix path AND avoided the traps</span>
  </div>

{sections}

  <footer>
    <span>Pass = deterministic lucky-fix guard (graders.grade). Judge score is shown for insight but does NOT gate the pass-rate.</span>
    <span>Regenerate: <code>python evals/agent_scorecard.py --from-json evals/agent_scorecard.json</code></span>
  </footer>
</div>
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render an agent-accuracy scorecard from a live eval run's JSON.")
    parser.add_argument("-o", "--out", default="evals/agent_scorecard.html",
                        help="output HTML path (default: evals/agent_scorecard.html)")
    parser.add_argument("--from-json", dest="from_json",
                        default="evals/agent_scorecard.json", metavar="PATH",
                        help="JSON produced by a live test_fix_agent.py run "
                             "(default: evals/agent_scorecard.json)")
    args = parser.parse_args()

    src = Path(args.from_json)
    if not src.exists():
        print(f"No results JSON at {src}. Run the live eval first: "
              f"cd ai-agent && python -m pytest evals/ -v")
        return 2
    data = json.loads(src.read_text())

    Path(args.out).write_text(render(data))
    m = data["meta"]
    print(f"Wrote {args.out}  ({m.get('total_pass', 0)}/{m.get('total_runs', 0)} runs passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
