# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A demo showing how an AI agent troubleshoots a deliberately-breakable 3-tier app
through a HashiCorp Boundary SSH tunnel — without a VPN and without ever holding
long-lived credentials. Two pieces live here: the **demo stack** (Docker Compose)
and the **AI agents** (`ai-agent/`) that diagnose and fix it. The agents are the
substance; the stack exists to be broken and fixed.

## The two-agent workflow (the core design)

The agent side is split into a **read-only diagnosis agent** and a
**write-capable fix agent**, deliberately as two processes:

- `ai-agent/sre_agent.py` — diagnoses using **read-only commands only** (its
  `run_remote(..., allow_writes=False)` refuses anything else). It ends by
  emitting a structured JSON diagnosis, then **spawns `fix_agent.py` as a
  subprocess**, passing the diagnosis via a temp file whose path is in the
  `BOUNDARY_HANDOFF_FILE` env var.
- `ai-agent/fix_agent.py` — does **not** re-diagnose. It reads the handoff,
  presents a PLAN in plain text, and applies the smallest safe fix, with a human
  `y/N` approval gate before every state-changing command.

The handoff goes via a **temp file, not stdin** — on purpose: `fix_agent`'s stdin
stays attached to the terminal so its approval prompt works. Changing this to
stdin will break the human gate (`input()` hits EOF).

Different models by role: `sre_agent` uses `claude-haiku-4-5`, `fix_agent` uses
`claude-sonnet-4-6`. Both are plain `anthropic.Anthropic()` tool-use loops
(`MAX_TURNS = 20`), not a framework.

## The shared guardrail layer

`ai-agent/boundary_common.py` is imported by **both** agents so the guardrails are
identical. Anything about how commands are approved, executed, or how the tunnel
is managed belongs here — do not duplicate it into an agent.

- `run_remote(cmd, plan, allow_writes)` is the single execution path. It:
  1. checks `DENYLIST` (rm -rf /, mkfs, curl|sh, fork bombs, …) → refuse;
  2. ensures the tunnel is up (lazy open / auto-reconnect);
  3. auto-approves if `read_only(cmd)` is true; else refuses (diagnose-only) or
     requires a `plan` + human `y` (fix agent);
  4. runs it over SSH as `sudo -n bash -lc <cmd>`, with **retry-on-transient-
     tunnel-failure** (reconnect with `force=True`, then retry).
- `read_only()` is intentionally strict: a command auto-approves **only** if it
  has no shell chaining/redirection (`;`, `&&`, `>`, `$(…)`, backticks) AND every
  pipe segment matches `READONLY_ALLOWLIST`. This is why the agents' system
  prompts tell them to run read-only checks as bare single commands — wrapping one
  in `&&`/`2>&1` forces an approval prompt.
- **Tunnel lifecycle**: each agent `register_target()`s but opens **no** tunnel at
  startup. `ensure_tunnel()` opens one lazily on the first command that needs it,
  and `close_owned_tunnel()` tears down only what that process opened. An agent
  that runs no commands opens no session. Both agents reuse the same local
  listener on `127.0.0.1:2222`.
- `BOUNDARY_AUTO_APPROVE=1` skips the human `y` gate — set by the eval fixtures
  for unattended runs. The interactive demo default is unchanged: every write
  still needs a human `y`.

## Running the agents

```sh
cd ai-agent
pip install anthropic
export ANTHROPIC_API_KEY=...
export BOUNDARY_ADDR=<controller-url>
boundary authenticate password -auth-method-id <ampw_XXXX>   # must be logged in
python sre_agent.py          # pick the target, describe the incident; it hands off to fix_agent
```

`sre_agent` only proceeds for the target named `it-ssh-cert-target` (`ALLOWED_TARGET`);
any other target is refused by design.

## The demo stack

`browser → nginx (:8080) → Flask app (:5000) → Postgres (:5432)`, run via
`docker-compose.yml` on the **target host** (not the operator machine). Container
names `demo-nginx` / `demo-app` / `demo-db` are **fixed** — the chaos scripts and
the eval's ground truth address them by name; renaming a container breaks both.

```sh
docker compose up -d --build
curl http://localhost:8080/health     # {"status": "ok"}
```

Three scripted faults (run one per demo, then restore):

```sh
./chaos/break-nginx.sh          # 502 — nginx upstream points at app:5001 (should be 5000)
./chaos/break-db-password.sh    # 500 — stale DB credentials
./chaos/break-db-down.sh        # 503 — demo-db stopped
./chaos/restore.sh              # fix everything
```

**The nginx bind-mount trap** (recurring gotcha, encoded across the codebase):
`nginx/default.conf` is bind-mounted as a single file into `demo-nginx`. Editing
it in place with `sed -i` fails with "Device or resource busy" — the rename can't
happen on a bind mount — and any `&&`-chained reload then silently never runs, so
the 502 looks fixed but isn't. The correct fix writes a temp file and `cat >`s it
back over the original inode. This is baked into `fix_agent`'s system prompt, the
compose comment, and the eval's `forbidden` patterns.

`app/app.py` (Flask) opens a fresh, short-timeout DB connection per request (no
pool) so a password rotation or a stopped DB takes effect on the very next
request — this is what makes the chaos scripts' faults appear immediately rather
than after a pool recycles. It also maps `psycopg2.OperationalError` to the two
status codes the chaos scripts and eval rely on: an auth-failure message → 500,
anything else (unreachable/stopped DB) → 503. `break-nginx.sh` produces the third
code (502) at the nginx layer, never reaching the app.

## The eval (`ai-agent/evals/`)

An eval-driven-development harness for `fix_agent`: it breaks the app for real,
runs the actual agent over the tunnel, and grades whether it fixed things **the
right way**, not just whether the app came back up. Read
`ai-agent/evals/METHODOLOGY.md` for the full rationale before changing grading.

Key files and their roles:

- `incidents.py` — the **single source of truth**. One `Incident` per mode with
  `expected_tools` (regexes a correct fix MUST include) and `forbidden` (regexes
  it must NOT). "Correct" is defined here as data, read by both the deterministic
  graders and the LLM metrics.
- `graders.py` — the **deterministic, primary signal**: `recovered` (parses
  `/health` JSON), `used_correct_fix`, `avoided_traps`. A run passes only if
  `recovered AND used_correct_fix AND avoided_traps` — recovering by the wrong
  means (a "lucky fix") fails by design. These are plain functions on purpose, so
  the pass/fail never depends on an LLM.
- `judge.py` — the one LLM-graded dimension: a DeepEval `GEval` rubric
  (`WORKFLOW_SAFETY_CRITERIA`) for fuzzy properties (presented a plan, avoided the
  `sed -i` trap, never touched data). The judge is pinned to an **Anthropic**
  model so the whole eval stays on one provider. This is the flaky part; treat its
  score as a soft signal, not ground truth.
- `validate_judge.py` — validates the judge against hand-labelled fixtures BEFORE
  trusting it. **When you change `WORKFLOW_SAFETY_CRITERIA`, run this** and expect
  full agreement; if a fixture disagrees, read the printed `reason:` and tighten
  the wording. Needs no live target.
- `harness.py` — live orchestration (`break_app` → `assert_symptom` →
  `run_fix_agent` → probe `/health`), reusing the agents' own `run_remote`.
- `conftest.py` — opens the Boundary session once per session, restores the app
  after **every** test (`restore_after`), and sets `BOUNDARY_AUTO_APPROVE=1`.

### Running the eval

```sh
cd ai-agent
pip install pytest deepeval
export ANTHROPIC_API_KEY=...            # judge + the agent under test
# Boundary authenticated and the target host up:
python -m pytest evals/ -v                                  # full live eval, pass-rate per mode
python -m pytest evals/test_fix_agent.py -k 502 -vv -rA -s  # one mode; -rA shows the reason line
python evals/validate_judge.py          # judge validation only — NO live target needed
```

Non-determinism is handled by `N_TRIALS = 3` per mode, reported as a pass-rate,
not a single pass/fail. Full agent stdout for every run is saved to
`evals/transcripts/<mode>-<timestamp>.log` — these are the primary artifact for
failure analysis.

### Reading a failure

A red test is not necessarily the agent's fault — triage by which line failed in
`test_fix_agent.py`. `assert_test(...)` (the GEval judge) runs first and is the
non-deterministic one; the deterministic `assert result.recovered` /
`fix_ok` / `safe_ok` follow. A failure in `break_app` (e.g.
`kex_exchange_identification: Connection reset by peer`) is a Boundary tunnel
hiccup, not a code bug. Match the failing line to its category before concluding
the agent regressed.

## Conventions

- The eval runs from `ai-agent/`, so `boundary_common`, `sre_agent`, `incidents`,
  `harness`, `judge` all import as **top-level** modules (`conftest.py` and the
  eval scripts put `ai-agent/` and `evals/` on `sys.path`). Keep that layout.
- No pytest config file — invoke pytest from `ai-agent/` with explicit paths.
- Secrets live in `.env` (gitignored); never commit real Boundary/Anthropic creds.
