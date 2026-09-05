# ApplyMerge — Project Context

Written 2026-09-05. Read this before changing anything: it records **why** the code
is shaped the way it is, and which apparent oddities are deliberate.

Built phase by phase from `applymerge_execution_plan.md` against hackathon problem
statement 04. Status: **complete, 48/48 tests passing**, verified from a clean
virtualenv. Only Phase 8's `DEMO_SCRIPT.md` was deferred by the user.

---

## 1. What the project is

A simulated infrastructure state where two concurrent, declarative changes are
reconciled into exactly one of four explainable outcomes. It never guesses at
operator intent: anything that is not a clean merge is rejected with a reason
built from the declarative model itself.

The central claim: **merging is only meaningful when each change carries explicit
semantics** — preconditions (what must be true before), postconditions (what it
sets), and invariants (what must remain true of the whole system).

## 2. File map

```
apply_merge/
  models.py        Resource, Precondition, Postcondition, Change, Invariant, InvariantResult
  invariants.py    replica_cap + ssh_not_public definitions, check_all(), failures()
  engine.py        InfraState, apply_single(), commute_check(), conflict_check(),
                   order_check(), diff_states(), apply_sequence(), reconcile()
  scenarios.py     base_state() + four scenario builders + SCENARIOS registry
  api.py           FastAPI: /state, /scenarios, /scenarios/{name}/run, /reset, /
  test_engine.py   the full suite (48 tests, phases 1-5 + coverage)
  frontend/
    index.html     single page, vanilla JS + Tailwind CDN, no build step
README.md          user-facing: how to run, the model, how to read a result
CONTEXT.md         this file
requirements.txt   pydantic, fastapi, uvicorn, pytest, httpx
.vscode/           interpreter pin for the editor only; no effect on the code
```

831 lines of source (engine 431, scenarios 148, api 100, models 89, invariants 62),
288 of frontend, 651 of tests.

## 3. Running it

```
pip install -r requirements.txt
uvicorn apply_merge.api:app --port 8000     # UI at /, Swagger at /docs
pytest -q -p no:cacheprovider               # the -p flag silences a cache
                                            # permission warning on this drive
```

Python 3.12.10 on Windows. No database, no Docker, no async, no websockets.

---

## 4. The four outcomes

`reconcile(state, change_a, change_b)` returns exactly one. **Three of them produce
no state at all** — `final_state` is `None`, and that absence is the answer.

| outcome | condition |
| --- | --- |
| `MERGED` | disjoint writes, both orders agree, all invariants pass |
| `CONFLICT` | same field different values, **or** no order applies both for non-invariant reasons |
| `ORDER_DEPENDENT` | at least one order works, but the orders disagree |
| `INVARIANT_REJECTED` | no order applies both, and every blocker is an invariant violation |

### Branch order in `reconcile` — this ordering is load-bearing

```
1. conflict_check          -> CONFLICT          (decidable from the changes alone)
2. no order applies both   -> INVARIANT_REJECTED if all blockers are invariant
                              violations, else CONFLICT
3. orders diverge          -> ORDER_DEPENDENT
4. otherwise               -> MERGED
```

**Step 2 must stay before step 3.** If it moves after, the invariant-rejected pair
gets labelled `ORDER_DEPENDENT`, which tells a judge "pick an order and you're
fine" — false, since no order applies both. This was found by a failing test in
Phase 3 and fixed deliberately.

---

## 5. Design decisions and why (do not silently "fix" these)

### 5.1 A fourth outcome was added beyond the plan

The plan specified exactly three. `INVARIANT_REJECTED` was added after discussion
because two non-overlapping changes that break a shared invariant are neither a
field conflict nor genuinely order-dependent. Labelling that `CONFLICT` makes a
judge hunt for an overlapping field that does not exist.

It is also the only scenario where **invariants do the rejecting** — the other
three reject on fields and preconditions. Without it, the declared-invariant
machinery never visibly does any work.

### 5.2 `commutes: true` on rejected results is intentional

In `scenario_order_dependent`, Alice writes `tier` and Bob writes `status` —
genuinely disjoint — yet the result is a rejection. `commute_check` reports
`true` **and** the verdict is `ORDER_DEPENDENT`.

That contradiction is the sharpest teaching moment in the demo: field-overlap
analysis only sees *writes*, and Alice's dependency on `status` lives in her
**precondition**, a read. This is why `order_check` actually executes both orders
instead of reasoning about fields.

Do not "fix" `commutes` to `false` here. It is factually true and deliberately
displayed.

### 5.3 `holds()` and `check()` live on the models, not the engine

`Precondition.holds()` and `Invariant.check()` are methods on `models.py` types
rather than functions in `engine.py`. The plan's layout suggested otherwise; this
was flagged and approved.

Reason: `>=` semantics belong to the precondition, and `order_check` needs to
evaluate preconditions against *hypothetical* states — a second call site that
must agree with the first exactly. Keeping it on the model means `apply_single`
reads as orchestration.

### 5.4 `Invariant.predicate` is `Field(exclude=True)`

A `Callable` cannot be serialized. Rather than dropping invariants from API
responses, the predicate is excluded and the rule travels as **name +
description**. `/state` therefore shows a judge the rules in plain English while
the code behind them stays server-side. Chosen over restructuring invariants into
declarative data, which would have undone an approved Phase 1 design.

### 5.5 `NO_SUCH_RESOURCE` exists though no scenario produces it

A change naming a missing resource has to go somewhere. Inventing the resource
would be exactly the guess the problem statement forbids; raising would make it
the one failure mode that escapes the `ApplyResult` trail and 500s the API. So it
is a fourth `ApplyResult.outcome`. Defensive, tested, never hit by the demo.

### 5.6 Postconditions are assignments only

No expressions, no "increment by 2". This is what makes "same field, different
values" decidable. With arithmetic postconditions, two changes could both be
satisfiable in a merged state and the engine would be back to guessing.

### 5.7 Invariants take the whole resource dict

One predicate signature, `dict[str, Resource] -> str | None`, covers both
per-resource rules (`ssh_not_public` iterates and looks at one) and cross-resource
rules (`replica_cap` sums across all). Returning `str | None` rather than
`(bool, str)` makes "failed but forgot the reason" unrepresentable.

### 5.8 `base_state()` is duplicated in `test_engine.py` and `scenarios.py`

Deliberate, not debt. The test fixture is a minimal two-database world for engine
unit tests; the scenario one is the richer demo world with a security group.
Merging them would couple engine tests to demo data, so tweaking a scenario would
break unrelated tests.

### 5.9 `state` is unused in `commute_check` and `conflict_check`

Both decide purely from the two changes. The parameter is kept so all four checks
share the `(state, change_a, change_b)` shape, as the plan specifies. Removing it
saves nothing and makes `engine.py` read inconsistently.

### 5.10 Scenario runs are pure

`POST /scenarios/{name}/run` reconciles against the scenario's **own** initial
state and never touches the module-level `_state`. Demos can therefore run in any
order, repeatedly, and always agree. `/state` and `/reset` exist to make the model
visible, not to accumulate results.

### 5.11 The order toggle needs no second API call

`POST /run` already returns `order.state_ab`, `order.state_ba`, `results_ab`,
`results_ba` and `diverging_fields`. The UI's "Apply A→B / B→A / Show both" is a
client-side switch over one response, so both orders are provably the same
computation.

### 5.12 `Change` has `origin` but no `timestamp`

Dropped in Phase 1. See known gap 8.1 — concurrency is expressed by refusing to
order the pair, not by clock values. A timestamp would invite "just apply the
earlier one", the exact guess the statement forbids.

---

## 6. Explanation format (Phase 7)

Every explanation is a compiler-style block: headline, then indented detail.
The UI renders it with `whitespace-pre-line` in a monospace block.

```
INVARIANT_REJECTED: alice-scale-primary (Alice) and bob-scale-replica (Bob) each apply cleanly alone, but no order applies both.
  Invariant replica_cap: total replicas = 11 (db-primary=5, db-replica=6), cap is 10
  A-then-B   alice-scale-primary: APPLIED, bob-scale-replica: INVARIANT_VIOLATED
  B-then-A   bob-scale-replica: APPLIED, alice-scale-primary: INVARIANT_VIOLATED
  Whichever change is applied first fits; the second never does.
  Neither change is at fault, so neither is discarded.
```

Every value comes from the declarative model: resource ids, field names, declared
values, invariant names, and the invariant's own failure reason.

**~18 tests assert on explanation substrings.** Any rewording breaks its test —
that is the tests working, not a regression. Change strings and assertions
together.

Phase 7 also added a computed note to `CONFLICT`: if picking a side would itself
violate an invariant, `reconcile` runs that change alone and says so. Only appears
when true.

---

## 7. The four demo scenarios

| scenario | changes | outcome | the point |
| --- | --- | --- | --- |
| `safe_merge` | Alice tags `sg-web.owner`, Bob moves `sg-web.port` 443&rarr;8443 | MERGED | same resource, disjoint fields, both intents survive |
| `conflict` | Alice sets `db-primary.replicas`=5, Bob=8 | CONFLICT | direct field contest; Bob's 8 would *also* breach the cap, so picking a side is no escape |
| `order_dependent` | Alice `tier=gold` **while active**, Bob `status=maintenance` | ORDER_DEPENDENT | disjoint writes, `commutes: true`, still order-sensitive via a precondition read |
| `invariant_rejected` | Alice db-primary 3&rarr;5, Bob db-replica 4&rarr;6 | INVARIANT_REJECTED | different resources, zero overlap, rejected purely by a cross-resource rule |

Shared world (`base_state()`): `db-primary` (3 replicas, active, silver),
`db-replica` (4 replicas, active, bronze), `sg-web` (port 443, ssh_cidr
10.0.0.0/8, owner unassigned). Invariants: `replica_cap` (total &le; 10),
`ssh_not_public`.

7 of 10 replicas are used at rest — that headroom is what makes
`invariant_rejected` work (5+6=11) while each change alone fits.

---

## 8. Known gaps

### 8.1 Time is not modelled — the one partial requirement

The statement says operations "may overlap in time". There is no clock,
timestamp, or interleaving. Concurrency is *assumed*: two changes arrive as an
unordered pair, and the engine's refusal to privilege an order is what expresses
it.

Defensible and arguably stronger than a timestamp would be, but if asked "where
is time in your model?", the answer is "nowhere, deliberately" — not "here".

### 8.2 Preconditions are single-field comparisons

Four operators (`==`, `!=`, `<=`, `>=`), one field, one literal. No compound
conditions, no cross-resource preconditions. Sufficient for the statement; state
it as a scope boundary rather than being caught out.

### 8.3 `POST /apply` was never built

The plan deferred freeform ad-hoc changes until base scope was confirmed. Never
requested. The engine supports it — it would be a thin endpoint over
`apply_single`.

### 8.4 `DEMO_SCRIPT.md` not written

Phase 8's second deliverable, explicitly skipped by the user. The final
regression (its first deliverable) was completed.

---

## 9. Test coverage

48 tests in one file, sectioned by phase.

| section | covers |
| --- | --- |
| Phase 1 | model validation: four operators, missing field, rejected operator, empty postconditions, `touched_fields`, invariant both ways |
| Phase 2 | apply: success, precondition rejection, invariant rejection, unknown resource, original state untouched |
| Phase 3 | commute × 3, conflict × 3, all four `reconcile` outcomes, `order_check` negative |
| Phase 4 | four scenarios vs expected outcome (parametrized, prints explanations), per-scenario content, initial states untouched, registry covers all outcomes |
| Phase 5 | every endpoint via `TestClient`, no `predicate` leaking, both candidate states in one call, purity, 404 |
| Coverage | mutual-precondition `CONFLICT` branch; all four outcomes reachable |

All nine `outcome=` branches in `engine.py` are exercised. Assertions check
explanation **content**, not just labels.

One persistent warning from Starlette (`TestClient` prefers `httpx2`) — library
internals, not ours.

---

## 10. Working agreement used throughout

From the execution plan, and worth keeping:

1. One phase at a time; no work started without explicit confirmation.
2. No mid-phase test runs. Write the phase, stop, ask before running the suite.
3. Full suite only, never a subset.
4. Ambiguities and design decisions the plan does not cover get **flagged and
   discussed**, never silently improvised. Several such decisions
   (`NO_SUCH_RESOURCE`, the fourth outcome, `commutes` on rejections, model-level
   `holds()`) were resolved this way and are recorded in section 5.

Three incidents worth remembering.

**Appends ran twice, silently.** A shell heredoc append duplicated the Phase 3
block in `engine.py` and `test_engine.py`; that was caught and rebuilt from the
boundary. It then recurred unnoticed for Phase 4, Phase 5 and the coverage block,
leaving `test_engine.py` at 905 lines with 61 `def test_` statements but only 42
unique names — Python keeps the last definition, so the duplicates were inert and
the suite still reported 48. The signal was visible and missed: 61 definitions
against 48 collected tests. Removing the second copy of each block took the file
to 651 lines with the same 48 tests passing.

**Guard against it recurring:** after any append to a source file, check
`ast.parse` for repeated top-level names rather than trusting a green suite —
identical duplicates do not fail, they just shadow.

**A test encoded a wrong assumption.** A Phase 3 test asserted both orders failed
identically for the replica-cap pair; in fact whichever change goes first fits
under the cap. The engine was right and the test was wrong, which is what
surfaced the branch-ordering decision in section 4.
