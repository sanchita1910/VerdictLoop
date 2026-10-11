# VerdictLoop

VerdictLoop tests proposed tool authorization policy changes before promoting them. A legitimate, approved `restart_service` call is initially blocked by policy v1. A deterministic Builder proposes a narrow approval-gated rule. The independent authorization oracle and Judge evaluate it against a frozen fixture; only a passing full-gate trial can move the active policy pointer. A second candidate removes approval enforcement, and the Judge rejects its unauthorized grants.

## How it works

| Component | Responsibility |
| --- | --- |
| `schema.py`, `fleet.py` | Frozen Pydantic contracts, trusted inventory, raw-label aliases, actors, and approvals. |
| `oracle.py` | Independent authorization truth for a resolved resource, actor, incident, context, and approval window. |
| `builder.py`, `mutations.py` | Deterministic safe proposal and five reproducible mutation candidates per split. |
| `policy.py`, `probes.py`, `detector.py` | Candidate policy decisions, eight-decision sampling, and shared violation detection. |
| `judge.py` | Full finite-fixture gate. It checks the legitimate target, unauthorized grants, alias inconsistency, and regressions. |
| `learning.py`, `evaluation.py` | Select a bounded probe allocation on training/validation candidates, then compare it with uniform probing on held-out candidates. |
| `store.py`, `cli.py`, `api.py` | Persist evidence in MongoDB Atlas; expose command-line and HTTP workflows. |

The current fixture has **18 resources** (3 tenants × 3 services × 2 canonical environments), with two raw labels per environment. The planned larger fixture is 160 resources (2 tenants × 20 services × 4 environments). The current two-environment schema must be expanded before that target can be run; the 18-resource fixture is sufficient for the integrated MVP.

## MongoDB Atlas

Atlas is the persisted source of truth for the demo. The `fleet` and `approvals` collections hold the frozen fixture. `policies` holds immutable policy versions and candidate records; `strategies` holds probe allocations; `trials` holds full-gate verdicts and counterexamples; `evaluations` holds the scorecard; and `runs` holds the active policy/strategy IDs and revision. Promotion uses a revision-checked atomic update of the `runs` document after the required evidence has been saved and read back. The evaluator runs in Python; this implementation does not use Atlas Vector Search.

The existing environment variable names remain `MONGODB_URI` and `CASELAW_DB` for compatibility. Put their values in a local `.env` based on `.env.example`; `.env` is Git-ignored. The CLI and API suppress connection details in errors.

## Run locally

From the repository root, use the existing Python environment at `../.venv`:

```bash
../.venv/bin/python -m pytest -q
../.venv/bin/python -m verdictloop.cli smoke-atlas
../.venv/bin/python -m verdictloop.cli init --run-id demo-cli
../.venv/bin/python -m verdictloop.cli mvp --run-id demo-cli
../.venv/bin/python -m verdictloop.cli compare --run-id demo-cli
../.venv/bin/python -m verdictloop.cli resume --run-id demo-cli
../.venv/bin/python -m verdictloop.cli status --run-id demo-cli
```

Use a fresh run ID for each CLI rehearsal. `mvp` evaluates the safe and unsafe candidates with the real Judge, stores both trials, promotes only the safe policy, and verifies that the rejected candidate did not change the active pointer. `compare` stores the frozen scorecard. `resume`, launched as a new Python process, loads and promotes the eligible strategy through Atlas, then probes a predetermined candidate whose policy content was absent from the frozen manifest. Its trial records both fast-probe and full-gate evidence.

The HTTP workflow uses the same Atlas-backed code:

```bash
../.venv/bin/python -m uvicorn verdictloop.api:app --host 127.0.0.1 --port 8000
```

`GET /api/status?run_id=demo-web` reads persisted state. With JSON `{"run_id":"demo-web"}`, call `POST /api/run-safe`, then `/api/run-unsafe`, then `/api/compare`, then `/api/resume`. The safe route initializes a new run; the unsafe route requires the safe policy to be active. The comparison saves the held-out evaluation once per run. Resume promotes the selected probe strategy only when that saved evaluation is eligible and records a later trial using the Atlas-loaded strategy. `GET /` serves the demo UI. Use the CLI `resume` command from a separate process for the fresh-process proof.

The standalone evaluation does not require Atlas:

```bash
../.venv/bin/python -m verdictloop.evaluation --out results/scorecard.json
```

## Held-out evaluation

The candidate set is fixed before strategy selection: five training, five validation, and five held-out candidates on disjoint service resources. The held-out test uses seeds **17, 29, 43, 71, 97**, giving 25 candidate × seed trials per strategy. Uniform and learned strategies receive the same candidate IDs, syntactically derived candidate-affected call universe, seeds, and budget of eight individual policy decisions per trial. Universe construction uses policy rule differences and trusted fixture inventory, not oracle or policy outcomes. Alias-pair probing may prioritize equivalent raw-label pairs inside that universe. The independent oracle and full gate make final safety decisions.

On the current 18-resource fixture (`fixture_hash=15b396468478e1db`, `manifest=manifest-644a589717947378`), the reproducible held-out run measured:

| Strategy | Violating trials detected | Violating candidates detected | Safe-control false alarms | Decisions |
| --- | ---: | ---: | ---: | ---: |
| Uniform (`uniform-v1`) | 5/15 (33%) | 1/3 | 0/10 | 200 |
| Learned alias pairs (`alias2-v2`) | 9/15 (60%) | 3/3 | 0/10 | 200 |

The full gate separately rejected three candidates and gave PROMOTE verdicts to two; all five triggering legitimate tasks were satisfied, with zero regressions. The scorecard records each seed's first violating decision index (1–8 or `NOT FOUND`), the full-gate verdict, decision counts, and timing. These **25 held-out trials are descriptive**, not evidence of statistical significance or general performance. The Builder is deterministic and makes no live LLM proposal, so the present autonomy claim is limited to bounded strategy adaptation and evidence-gated policy promotion. In one later Atlas-backed trial, the learned fast probe missed a violation and the full gate still rejected the candidate; this is the intended safety separation.
