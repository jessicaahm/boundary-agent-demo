#!/usr/bin/env python3
"""Generate an HTML scorecard from a validate_judge.py run.

The scorecard is rendered from the REAL per-fixture results — scores, verdicts,
and the judge's own `reason` strings — never from hand-transcribed terminal text.
That's the whole point: the HTML reflects whatever the judge actually did on the
run that produced the JSON, so it can't silently drift from the numbers.

Two ways to use it:

    # one shot: run the judge and write the HTML in one go (needs ANTHROPIC_API_KEY)
    cd ai-agent && python evals/scorecard.py -o evals/scorecard.html

    # or from a JSON dumped earlier by validate_judge.py --json:
    cd ai-agent && python evals/validate_judge.py --json /tmp/run.json
    python evals/scorecard.py --from-json /tmp/run.json -o evals/scorecard.html

Design mirrors a Confident AI testing report: an overview strip (agreement donut +
headline stats) over a per-fixture table with score / threshold / verdict / reason.
"""

import argparse
import html
import json
import sys
from pathlib import Path

# ai-agent/ (for boundary_common) and evals/ (for validate_judge, judge) on path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# One arc-length so the donut's stroke-dasharray matches the SVG circle (r=56).
DONUT_CIRCUMFERENCE = 2 * 3.141592653589793 * 56


def _rows(results):
    """Render the <tbody> rows from real per-fixture results."""
    out = []
    for r in results:
        agree = r["agree"]
        judged_pass = r["judged_pass"]
        score = r["score"]
        # Meter fill uses the score directly; clamp so tiny/над-1 values still render.
        pct = max(2.0, min(100.0, score * 100))
        thresh_pct = r["threshold"] * 100
        hi_lo = "hi" if judged_pass else "lo"
        badge = "pass" if judged_pass else "fail"
        verdict = "PASS" if judged_pass else "FAIL"
        label_txt = "PASS" if r["label"] else "FAIL"
        kind_cls = "good" if r["kind"] == "good" else "trap"
        kind_txt = "correct run" if r["kind"] == "good" else "bad &mdash; should fail"

        # Agreement line: a green check when the judge matched the label, a red
        # cross when it didn't (that row is the one to investigate).
        if agree:
            agree_line = (f'<div class="agree"><span class="ok">&check;</span> '
                          f'matches label <b>{label_txt}</b></div>')
            row_cls = "pass"
        else:
            agree_line = (f'<div class="agree disagree"><span class="xx">&times;</span> '
                          f'label said <b>{label_txt}</b></div>')
            row_cls = "fail"

        reason = html.escape(r["reason"] or "")
        out.append(f"""          <tr class="{row_cls}">
            <td>
              <div class="case-name">{html.escape(r['name'])}</div>
              <span class="case-kind {kind_cls}">{kind_txt}</span>
            </td>
            <td class="num">
              <span class="score {hi_lo}">{score:.2f}</span><br>
              <span class="thresh">&ge; {r['threshold']:.2f}</span>
              <div class="meter"><div class="fill {hi_lo}" style="width:{pct:.0f}%"></div><div class="tick" style="left:{thresh_pct:.0f}%"></div></div>
            </td>
            <td>
              <span class="badge {badge}"><span class="ico"></span>{verdict}</span>
              {agree_line}
            </td>
            <td class="reason">{reason}</td>
          </tr>""")
    return "\n".join(out)


def render(data) -> str:
    """Build the full scorecard HTML document from a {meta, results} payload."""
    meta = data["meta"]
    results = data["results"]
    agree, n = meta["agree"], meta["n"]
    pct = round(100 * agree / n) if n else 0
    all_ok = agree == n
    n_good = sum(1 for r in results if r["kind"] == "good")
    n_trap = n - n_good

    # Donut arc: green sweep proportional to agreement.
    dash = DONUT_CIRCUMFERENCE
    offset = dash * (1 - agree / n) if n else dash
    ring_color = "var(--pass)" if all_ok else "var(--fail)"

    if all_ok:
        banner_cls, banner = "ok", (
            f"<b>Judge is trustworthy.</b> All {n} fixtures agree &mdash; the "
            "grader can be relied on in the live agent eval.")
    else:
        banner_cls, banner = "bad", (
            f"<b>{n - agree} disagreement(s).</b> The judge diverged from the hand "
            "labels &mdash; tighten the rubric before trusting the metric.")

    rows = _rows(results)

    return TEMPLATE.format(
        metric=html.escape(meta["metric"]),
        judge_model=html.escape(meta["judge_model"]),
        threshold=f"{meta['threshold']:.2f}",
        date=html.escape(meta["date"]),
        pct=pct,
        agree=agree,
        n=n,
        disagree=n - agree,
        n_good=n_good,
        n_trap=n_trap,
        dash=f"{dash:.2f}",
        offset=f"{offset:.2f}",
        ring_color=ring_color,
        banner_cls=banner_cls,
        banner=banner,
        agree_v_cls="good" if all_ok else "bad",
        disagree_v_cls="bad" if not all_ok else "good",
        rows=rows,
    )


TEMPLATE = """<title>Judge Validation Scorecard</title>
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
  .wrap {{ max-width: 1000px; margin: 0 auto; padding: 40px 24px 64px; }}

  header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; flex-wrap: wrap; margin-bottom: 28px; }}
  .title-block h1 {{ margin: 0; font-size: 1.55rem; letter-spacing: -.02em; font-weight: 650; }}
  .title-block .sub {{ margin: 4px 0 0; color: var(--ink-dim); font-size: .92rem; }}
  .runmeta {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
  .chip {{ font-family: var(--mono); font-size: .72rem; color: var(--ink-dim); background: var(--surface); border: 1px solid var(--border); border-radius: 999px; padding: 5px 11px; white-space: nowrap; }}
  .chip b {{ color: var(--ink); font-weight: 600; }}
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
  .verdict-banner .dot {{ width: 8px; height: 8px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 4px color-mix(in srgb, currentColor 20%, transparent); flex: none; }}
  .verdict-banner b {{ font-weight: 650; }}

  .section-label {{ display: flex; align-items: baseline; gap: 10px; margin: 0 0 12px; padding: 0 4px; }}
  .section-label h2 {{ margin: 0; font-size: .82rem; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-dim); font-weight: 650; }}
  .section-label .count {{ font-family: var(--mono); font-size: .78rem; color: var(--ink-faint); }}

  .table-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 16px; box-shadow: var(--shadow); overflow: hidden; }}
  .scroll {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; min-width: 720px; }}
  thead th {{ text-align: left; font-size: .7rem; text-transform: uppercase; letter-spacing: .07em; color: var(--ink-faint); font-weight: 650; padding: 13px 18px; background: var(--surface-2); border-bottom: 1px solid var(--border); white-space: nowrap; }}
  thead th.num {{ text-align: right; }}
  tbody td {{ padding: 15px 18px; border-bottom: 1px solid var(--border); vertical-align: top; font-size: .9rem; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr {{ position: relative; }}
  tbody tr::before {{ content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; }}
  tbody tr.pass::before {{ background: var(--pass); }}
  tbody tr.fail::before {{ background: var(--fail); }}
  tbody tr:hover {{ background: color-mix(in srgb, var(--accent) 4%, transparent); }}

  .case-name {{ font-weight: 550; letter-spacing: -.01em; }}
  .case-kind {{ display: inline-block; margin-top: 3px; font-family: var(--mono); font-size: .68rem; padding: 2px 7px; border-radius: 5px; }}
  .case-kind.good {{ color: var(--pass); background: var(--pass-soft); }}
  .case-kind.trap {{ color: var(--fail); background: var(--fail-soft); }}

  td.num {{ text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .score {{ font-weight: 650; font-size: 1rem; }}
  .score.hi {{ color: var(--pass); }}
  .score.lo {{ color: var(--fail); }}
  .thresh {{ color: var(--ink-faint); font-size: .82rem; }}

  .meter {{ margin-top: 7px; height: 5px; border-radius: 3px; background: var(--surface-2); position: relative; overflow: hidden; min-width: 90px; }}
  .meter .fill {{ position: absolute; left: 0; top: 0; bottom: 0; border-radius: 3px; }}
  .meter .fill.hi {{ background: var(--pass); }}
  .meter .fill.lo {{ background: var(--fail); }}
  .meter .tick {{ position: absolute; top: -2px; bottom: -2px; width: 2px; background: var(--ink-faint); opacity: .55; }}

  .badge {{ display: inline-flex; align-items: center; gap: 6px; font-family: var(--mono); font-size: .72rem; font-weight: 650; padding: 4px 10px; border-radius: 999px; white-space: nowrap; }}
  .badge.pass {{ color: var(--pass); background: var(--pass-soft); }}
  .badge.fail {{ color: var(--fail); background: var(--fail-soft); }}
  .badge .ico {{ width: 6px; height: 6px; border-radius: 50%; background: currentColor; }}
  .agree {{ display: inline-flex; align-items: center; gap: 5px; font-size: .78rem; color: var(--ink-dim); margin-top: 6px; }}
  .agree b {{ color: var(--ink); font-weight: 600; }}
  .agree .ok {{ color: var(--pass); font-weight: 700; }}
  .agree .xx {{ color: var(--fail); font-weight: 700; }}

  .reason {{ color: var(--ink-dim); font-size: .82rem; max-width: 40ch; line-height: 1.45; }}

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
      <h1>Judge Validation Scorecard</h1>
      <p class="sub">Does the LLM judge agree with the human answer key? &mdash; <code class="inline">validate_judge.py</code></p>
    </div>
    <div class="runmeta">
      <span class="chip">metric&nbsp;<b>{metric}</b></span>
      <span class="chip">judge&nbsp;<b>{judge_model}</b></span>
      <span class="chip">threshold&nbsp;<b>{threshold}</b></span>
      <span class="chip">run&nbsp;<b>{date}</b></span>
    </div>
  </header>

  <section class="overview">
    <div class="donut" role="img" aria-label="{pct} percent agreement">
      <svg width="132" height="132" viewBox="0 0 132 132">
        <circle cx="66" cy="66" r="56" fill="none" stroke="var(--surface-2)" stroke-width="14"/>
        <circle cx="66" cy="66" r="56" fill="none" stroke="{ring_color}" stroke-width="14"
                stroke-linecap="round" stroke-dasharray="{dash}" stroke-dashoffset="{offset}"/>
      </svg>
      <div class="center">
        <span class="pct">{pct}%</span>
        <span class="pct-label">Agreement</span>
      </div>
    </div>
    <div class="stats">
      <div class="stat">
        <span class="k">Fixtures</span>
        <span class="v">{n}</span>
        <span class="note">hand-labelled cases</span>
      </div>
      <div class="stat">
        <span class="k">Judge agreed</span>
        <span class="v {agree_v_cls}">{agree}</span>
        <span class="note">verdict matched label</span>
      </div>
      <div class="stat">
        <span class="k">Disagreements</span>
        <span class="v {disagree_v_cls}">{disagree}</span>
        <span class="note">rows to investigate</span>
      </div>
    </div>
  </section>

  <div class="verdict-banner {banner_cls}">
    <span class="dot" aria-hidden="true"></span>
    <span>{banner}</span>
  </div>

  <div class="section-label">
    <h2>Fixtures</h2>
    <span class="count">{n} cases &middot; {n_good} good runs &middot; {n_trap} traps</span>
  </div>

  <div class="table-card">
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>Fixture</th>
            <th class="num">Score</th>
            <th>Verdict</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
{rows}
        </tbody>
      </table>
    </div>
  </div>

  <footer>
    <span>Score = GEval judge, 0&ndash;1 &middot; pass when score &ge; threshold &middot; agreement = judge verdict equals hand label.</span>
    <span>Regenerate: <code>python evals/scorecard.py -o evals/scorecard.html</code></span>
  </footer>
</div>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Render an HTML scorecard from a judge-validation run.")
    parser.add_argument("-o", "--out", default="evals/scorecard.html",
                        help="output HTML path (default: evals/scorecard.html)")
    parser.add_argument("--from-json", dest="from_json", metavar="PATH",
                        help="render from a JSON file produced by "
                             "`validate_judge.py --json`, instead of running the judge live")
    args = parser.parse_args()

    if args.from_json:
        data = json.loads(Path(args.from_json).read_text())
    else:
        # Live: run the judge now and render from its real output.
        try:
            import deepeval.test_case  # noqa: F401
        except ImportError:
            print("deepeval is not installed. Run: pip install deepeval")
            return 2
        from validate_judge import run_validation
        results, meta = run_validation()
        data = {"meta": meta, "results": results}

    Path(args.out).write_text(render(data))
    m = data["meta"]
    print(f"Wrote {args.out}  ({m['agree']}/{m['n']} agreement)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
