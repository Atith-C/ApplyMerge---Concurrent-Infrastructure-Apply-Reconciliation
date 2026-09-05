# ApplyMerge — Project Context

Last updated 2026-09-05. Read this before changing anything: it records **why** the
code is shaped the way it is, and which apparent oddities are deliberate.

Built in two arcs against hackathon problem statement 04:

- **Arc 1 — the engine** (`applymerge_execution_plan.md`, phases 0–8). A reconciliation
  engine plus four canned demo scenarios. 48 tests.
- **Arc 2 — live concurrent editing** (phases 1–6 of a second plan). Two operators
  editing the same state, changes *derived* from their edits rather than written by
  hand. 84 tests.

Status: **both arcs complete, 84/84 passing**, committed and pushed. Only relative
postconditions (arc 2, optional phase 7) and `DEMO_SCRIPT.md` remain unbuilt, both
deferred by the user rather than blocked.

Repo: <https://github.com/Atith-C/ApplyMerge---Concurrent-Infrastructure-Apply-Reconciliation>

---

## 1. What the project is

A simulated infrastructure state where two concurrent, declarative changes are
reconciled into exactly one of four explainable outcomes. It never guesses at
operator intent: anything that is not a clean merge is rejected with a reason built
from the declarative model itself.

The central claim: **merging is only meaningful when each change carries explicit
semantics** — preconditions (what must be true before), postconditions (what it
sets), and invariants (what must remain true of the whole system).

Arc 2 added the answer to *"where do those semantics come from?"* — an operator
edits a working copy, and the diff against the snapshot they were handed becomes
the change. Postconditions come from what moved; preconditions come from what they
were looking at.

## 2. File map

```
apply_merge/
  models.py         Resource, Precondition, Postcondition, Change, Invariant, InvariantResult
  invariants.py     replica_cap + ssh_not_public definitions, check_all(), failures()
  engine.py         InfraState, apply_single(), commute_check(), conflict_check(),
                    order_check(), diff_states(), apply_sequence(), reconcile()
  session.py        World (versioning, snapshots, history), Session, Edit, Lock,
                    derive_change(), submit(), Submission            [arc 2]
  scenarios.py      base_state() + four scenario builders + SCENARIOS registry
  api.py            FastAPI: eleven routes, see section 11
  test_engine.py    48 tests — the engine and the pure scenario endpoints
  test_session.py   36 tests — sessions, derivation, live concurrent editing  [arc 2]
  frontend/
    index.html      the live two-operator console                    [arc 2]
    scenarios.html  the original canned walkthrough, served at /scenarios-view
README.md           user-facing: architecture diagrams, the model, how to read a result
CONTEXT.md          this file
requirements.txt    pydantic, fastapi, uvicorn, pytest, httpx
.gitignore          __pycache__, venvs, .claude/, .claude-flow/, graphify-out/
.vscode/            interpreter pin for the editor only; no effect on the code
```

1,306 lines of source (engine 431, session 360, api 216, scenarios 148, models 89,
invariants 62), 985 of frontend, 1,092 of tests, 366 of README.

## 3. Running it

```
pip install -r requirements.txt
uvicorn apply_merge.api:app --port 8000     # console at /, Swagger at /docs
pytest -q -p no:cacheprovider               # the -p flag silences a cache
                                            # permission warning on this drive
```

Python 3.12.10 on Windows. No database, no Docker, no async, no websockets.

**Using the console:** name two operators, click Open — both start at `v0`. Edit any
value; edited fields turn amber. Optionally **🔒 pin** a field you rely on but are
not changing. "Your change" previews exactly what will be submitted. Apply on one
side, then the other; the second is the one being reconciled. Four **preset**
buttons stage each outcome.

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
fine" — false, since no order applies both. Found by a failing test in arc 1 phase 3
and fixed deliberately.

---

## 5. The live session layer (arc 2)

### 5.1 The concurrency model is a version stamp

`World` holds the live state plus `version`, `snapshots[v]` (the state at each
version), `committed[v]` (the change that produced it), `sessions`, and `history`.

A session records the version that was live when it opened. **Two sessions holding
the same base version overlapped in time** — that stamp is the entire concurrency
model, and it needs no clock. This closes what used to be the project's one partial
requirement (see 9.1).

### 5.2 The submit decision path

```
base == live   -> nothing overlapped -> apply to live, commit if it holds
base <  live   -> reconcile against each change committed since, from the
                  session's own snapshot as the common ancestor.
                  First non-MERGED verdict is the answer, nothing applied.
                  All merged -> apply to live (re-checking preconditions and
                  every invariant against reality, not the snapshot).
```

A commit advances only *that* session's base version. Everyone else stays stale,
which is the point.

### 5.3 Change derivation — postconditions are free, preconditions are not

`derive_change(snapshot, edit, origin)`:

- **Postconditions** ← every field whose submitted value differs from the snapshot.
- **Preconditions** ← the snapshot value of each written field. This is an optimistic
  lock meaning *"I decided this while looking at 3"*, and it needs no engine support:
  a stale write fails its own precondition through the ordinary path.
- **Plus** the snapshot value of every **pinned** field.

**The pin is not decoration.** With derived preconditions alone, `MERGED`, `CONFLICT`
and `INVARIANT_REJECTED` all still classify correctly, but `ORDER_DEPENDENT` becomes
*unreachable* — nothing would ever read a field it does not write. The pin creates
the read. `test_a_pin_is_what_makes_a_pair_order_dependent` and
`test_without_the_pin_the_same_pair_merges` are the paired proof.

### 5.4 Input validation at the boundary

`_coerce` matches a submitted value to the field's existing type. A browser sends
`"5"`; left as a string it would reach `_replica_cap`'s `sum()` and 500. Six
`EditError` cases, all with usable messages: nothing changed, two resources touched,
unknown resource, unknown field, wrong type, pin on another resource. `EditError`
subclasses `ValueError` and maps to a 400.

### 5.5 A pin must be on the resource being edited

`apply_single` evaluates every precondition against the change's **target** resource
only. A pin on a different resource would be silently checked against the wrong one,
so it is refused with an explanation. The console also warns before submitting.

---

## 6. Design decisions and why (do not silently "fix" these)

### 6.1 A fourth outcome was added beyond the plan

The plan specified exactly three. `INVARIANT_REJECTED` was added after discussion
because two non-overlapping changes that break a shared invariant are neither a
field conflict nor genuinely order-dependent. Labelling that `CONFLICT` makes a
judge hunt for an overlapping field that does not exist.

It is also the only scenario where **invariants do the rejecting** — the other three
reject on fields and preconditions. Without it, the declared-invariant machinery
never visibly does any work.

### 6.2 `commutes: true` on rejected results is intentional

In `scenario_order_dependent`, Alice writes `tier` and Bob writes `status` —
genuinely disjoint — yet the result is a rejection. `commute_check` reports `true`
**and** the verdict is `ORDER_DEPENDENT`.

That contradiction is the sharpest teaching moment in the demo: field-overlap
analysis only sees *writes*, and Alice's dependency on `status` lives in her
**precondition**, a read. This is why `order_check` actually executes both orders
instead of reasoning about fields.

Do not "fix" `commutes` to `false` here. It is factually true and deliberately
displayed.

### 6.3 `holds()` and `check()` live on the models, not the engine

`Precondition.holds()` and `Invariant.check()` are methods on `models.py` types
rather than functions in `engine.py`. Flagged and approved.

Reason: `>=` semantics belong to the precondition, and `order_check` needs to
evaluate preconditions against *hypothetical* states — a second call site that must
agree with the first exactly. Keeping it on the model means `apply_single` reads as
orchestration.

### 6.4 `Invariant.predicate` is `Field(exclude=True)`

A `Callable` cannot be serialized. Rather than dropping invariants from API
responses, the predicate is excluded and the rule travels as **name + description**.
`/state` therefore shows a judge the rules in plain English while the code behind
them stays server-side.

### 6.5 `NO_SUCH_RESOURCE` exists though no scenario produces it

A change naming a missing resource has to go somewhere. Inventing the resource would
be exactly the guess the problem statement forbids; raising would make it the one
failure mode that escapes the `ApplyResult` trail and 500s the API. Defensive,
tested, never hit by the demo.

### 6.6 Postconditions are assignments only

No expressions, no "increment by 2". This is what makes "same field, different
values" decidable. With arithmetic postconditions, two changes could both be
satisfiable in a merged state and the engine would be back to guessing.

*(Arc 2 phase 7 would relax this deliberately — `add` alongside `set` — so that two
increments genuinely commute. Not built.)*

### 6.7 Invariants take the whole resource dict

One predicate signature, `dict[str, Resource] -> str | None`, covers both
per-resource rules (`ssh_not_public`) and cross-resource rules (`replica_cap`).
Returning `str | None` rather than `(bool, str)` makes "failed but forgot the
reason" unrepresentable.

### 6.8 `base_state()` is duplicated in `test_engine.py` and `scenarios.py`

Deliberate, not debt. The test fixture is a minimal two-database world for engine
unit tests; the scenario one is the richer demo world with a security group. Merging
them would couple engine tests to demo data.

### 6.9 `state` is unused in `commute_check` and `conflict_check`

Both decide purely from the two changes. The parameter is kept so all four checks
share the `(state, change_a, change_b)` shape. Removing it saves nothing and makes
`engine.py` read inconsistently.

### 6.10 Scenario runs are pure

`POST /scenarios/{name}/run` reconciles against the scenario's **own** initial state
and never touches the live world. Demos can run in any order, repeatedly, and always
agree.

### 6.11 The order toggle needs no second API call

The response already carries `order.state_ab`, `state_ba`, `results_ab`,
`results_ba` and `diverging_fields`. "Theirs first / Ours first / Show both" is a
client-side switch over one response, so both orders are provably the same
computation.

### 6.12 `Change` has `origin` but no `timestamp`

A timestamp would invite "just apply the earlier one", the exact guess the statement
forbids. Concurrency is expressed by the version stamp on the *session*, not by a
clock on the change.

### 6.13 `/state` and `/reset` were rewired onto the `World` (arc 2)

Keeping the old module-level `_state` alongside a versioned live state would have
given two "live" states that silently disagree. There is one live state. `/reset`
rewinds to v0 and **drops every session** — a reset that left sessions pinned to
versions that no longer exist would be a lie. Request/response shapes unchanged, so
the arc-1 tests held.

### 6.14 The console uses `sessionStorage`, not `localStorage`

`localStorage` is shared across tabs of one origin — two tabs would land on the same
session and the demo would collapse into sequential edits. `sessionStorage` is
per-tab. (Now largely moot since both consoles live on one page, but the restore
path still uses it.)

### 6.15 Both operators are on one page, not two tabs

Changed at the user's request after phase 4. Each console keeps its own session id,
base version, working copy, pins and verdict; applying on one immediately re-renders
the other so it visibly goes stale. Easier to demo than two windows, and it makes
the preset buttons possible.

### 6.16 Presets are client-side, not `POST /stage/{scenario}`

The planned staging endpoint was never built. A preset resets the world, reopens
both sessions at v0 and fills both drafts — all from the browser. The staged edits
then go through the same diff → derive → reconcile path as anything typed by hand,
so a preset is a shortcut, not a mode, and editing a staged value before applying
changes the verdict.

### 6.17 `scenarios.html` was kept, not deleted

The original single-page walkthrough still works and is served at `/scenarios-view`,
because `/stage` did not exist when the console replaced it and a working demo
should not be thrown away mid-rework. It drives the pure `/scenarios` endpoints.

### 6.18 A concurrent commit is badged MERGED, not APPLIED

`Submission.outcome` is `APPLIED` when the change lands, whether or not it merged
first. The console overrides the badge to **MERGED** when `concurrent && committed`,
reserving `APPLIED` for a change that had nothing to reconcile against. The
underlying data is unchanged; only the label is.

---

## 7. Explanation format

Every explanation is a compiler-style block: headline, then indented detail. The UI
renders it with `whitespace-pre-line` in a monospace block.

```
INVARIANT_REJECTED: alice-scale-primary (Alice) and bob-scale-replica (Bob) each apply cleanly alone, but no order applies both.
  Invariant replica_cap: total replicas = 11 (db-primary=5, db-replica=6), cap is 10
  A-then-B   alice-scale-primary: APPLIED, bob-scale-replica: INVARIANT_VIOLATED
  B-then-A   bob-scale-replica: APPLIED, alice-scale-primary: INVARIANT_VIOLATED
  Whichever change is applied first fits; the second never does.
  Neither change is at fault, so neither is discarded.
```

A submission that overlapped is prefixed with the reason it overlapped:

```
Your edit was built on v0, but v1 is live: it overlapped with atith-status-f7b3 (atith).
```

Every value comes from the declarative model: resource ids, field names, declared
values, invariant names, and the invariant's own failure reason.

**~18 tests assert on explanation substrings.** Any rewording breaks its test — that
is the tests working, not a regression. Change strings and assertions together.

`CONFLICT` also carries a computed note: if picking a side would itself violate an
invariant, `reconcile` runs that change alone and says so. Only appears when true.

---

## 8. The four demo scenarios

| scenario | changes | outcome | the point |
| --- | --- | --- | --- |
| `safe_merge` | Alice tags `sg-web.owner`, Bob moves `sg-web.port` 443&rarr;8443 | MERGED | same resource, disjoint fields, both intents survive |
| `conflict` | Alice sets `db-primary.replicas`=5, Bob=8 | CONFLICT | direct field contest; Bob's 8 would *also* breach the cap, so picking a side is no escape |
| `order_dependent` | Alice `tier=gold` **while active**, Bob `status=maintenance` | ORDER_DEPENDENT | disjoint writes, `commutes: true`, still order-sensitive via a precondition read |
| `invariant_rejected` | Alice db-primary 3&rarr;5, Bob db-replica 4&rarr;6 | INVARIANT_REJECTED | different resources, zero overlap, rejected purely by a cross-resource rule |

One definition drives three consumers: the preset buttons, the pure `/scenarios`
endpoints, and the test suite.

Shared world (`base_state()`): `db-primary` (3 replicas, active, silver),
`db-replica` (4 replicas, active, bronze), `sg-web` (port 443, ssh_cidr 10.0.0.0/8,
owner unassigned). Invariants: `replica_cap` (total &le; 10), `ssh_not_public`.

7 of 10 replicas are used at rest — that headroom is what makes `invariant_rejected`
work (5+6=11) while each change alone fits.

In the console, `order_dependent` run a second time with the pin removed **merges**.
That one click is the whole difference between "these commute" and "the order
decides", and it is the sharpest thing to show an audience.

---

## 9. Known gaps

### 9.1 Time — now modelled, was the one partial requirement

Arc 1 had no clock and expressed concurrency only by refusing to order the pair.
Arc 2 closed this: two sessions holding the same base version provably overlapped,
and the stale-base branch is what triggers reconciliation. If asked "where is time
in your model?", the answer is now "the version stamp on a session".

### 9.2 One resource per submission

`Change.resource_id` is a single id, so an edit spanning two resources is refused
with an explanation rather than split. The console disables Apply and says why.
Lifting this is a model change, not a plumbing change, and it is the largest single
obstacle to consuming real Terraform plans.

### 9.3 Postconditions are assignment-only

`replicas = 5`, never `replicas += 2`. Two relative increments genuinely commute
where two assignments never can — the strongest possible answer to the statement's
"whether their effects commute" — and it cannot currently be expressed. This is arc
2's optional phase 7.

### 9.4 Merge base is approximated for more than one missed change

A session that missed several changes is reconciled against each of them from its
own snapshot. Strictly, each missed change was built on the one before, not on that
snapshot. With two operators the missed list is almost always length one. Marked in
the code.

### 9.5 Preconditions are single-field comparisons

Four operators (`==`, `!=`, `<=`, `>=`), one field, one literal. No compound
conditions, no cross-resource preconditions.

### 9.6 No drift, no partial application

The recorded state is assumed to be the truth, and an apply is all-or-nothing. Real
infrastructure is neither.

### 9.7 Sessions never expire

One abandoned session pins the snapshot history at its base version forever, because
the prune floor is the oldest base version anyone holds. Fine for a demo; a
last-seen timestamp would fix it.

### 9.8 Two operators making the identical edit are reported as a CONFLICT

If both set `owner="platform-team"`, whoever lands first invalidates the other's
optimistic lock, so no order applies both and the pair reads as a `CONFLICT` — even
though the second operator's intent is already satisfied. Recorded in
`test_two_operators_making_the_identical_edit_are_reported_as_a_conflict`,
deliberately *documented rather than endorsed*. A friendlier answer would be a no-op
outcome; that was flagged to the user and left undecided.

### 9.9 `DEMO_SCRIPT.md` not written

Arc 1 phase 8's second deliverable, explicitly skipped. The final regression (its
first deliverable) was completed.

---

## 10. Test coverage

84 tests in two files.

**`test_engine.py` — 48**, sectioned by arc-1 phase.

| section | covers |
| --- | --- |
| Phase 1 | model validation: four operators, missing field, rejected operator, empty postconditions, `touched_fields`, invariant both ways |
| Phase 2 | apply: success, precondition rejection, invariant rejection, unknown resource, original state untouched |
| Phase 3 | commute × 3, conflict × 3, all four `reconcile` outcomes, `order_check` negative |
| Phase 4 | four scenarios vs expected outcome (parametrized, prints explanations), per-scenario content, initial states untouched, registry covers all outcomes |
| Phase 5 | every pure endpoint via `TestClient`, no `predicate` leaking, both candidate states in one call, purity, 404 |
| Coverage | mutual-precondition `CONFLICT` branch; all four outcomes reachable |

**`test_session.py` — 36**, the live layer.

| section | covers |
| --- | --- |
| Versioning | v0 start, shared base version, late session starts current, `changes_since` ordering, prune floor, reset drops sessions |
| Derivation | postconditions from the diff, optimistic-lock preconditions, unchanged fields ignored, pins, pin/write dedupe, `"5"` → `5`, five rejection cases, foreign-pin refusal |
| Submission | **all four outcomes through live concurrent editing**, lone invariant rejection, the pin/no-pin pair, refresh-and-retry, history records rejections |
| Over HTTP | session open, two consoles colliding, 400 with the reason, 404 unknown session, refresh, `/live`, `/` serves the console, `/scenarios-view` survives |

All nine `outcome=` branches in `engine.py` are exercised. Assertions check
explanation **content**, not just labels.

One persistent warning from Starlette (`TestClient` prefers `httpx2`) — library
internals, not ours.

---

## 11. Endpoints

| method | path | purpose |
| --- | --- | --- |
| POST | `/session` | open an operator session, pinned to the live version |
| GET | `/session/{id}` | base version against live |
| POST | `/session/{id}/submit` | submit an edited manifest; applied or reconciled |
| POST | `/session/{id}/refresh` | rebase onto live after a rejection |
| GET | `/live` | live state, version, history, connected operators |
| GET | `/state` | current resources and declared invariants |
| POST | `/reset` | rewind to v0, dropping every session |
| GET | `/scenarios` | the four canned cases |
| POST | `/scenarios/{name}/run` | reconcile that scenario, pure |
| GET | `/` | the live two-operator console |
| GET | `/scenarios-view` | the canned walkthrough |

---

## 12. Working agreement used throughout

1. One phase at a time; no work started without explicit confirmation.
2. No mid-phase test runs. Write the phase, stop, ask before running the suite.
3. Full suite only, never a subset.
4. Ambiguities and design decisions the plan does not cover get **flagged and
   discussed**, never silently improvised. Several such decisions
   (`NO_SUCH_RESOURCE`, the fourth outcome, `commutes` on rejections, model-level
   `holds()`, one-resource-per-submission, the merge-base approximation) were
   resolved this way and are recorded in sections 5 and 6.

### Incidents worth remembering

**Appends ran twice, silently.** A shell heredoc append duplicated the arc-1 phase 3
block in `engine.py` and `test_engine.py`; caught and rebuilt from the boundary. It
then recurred unnoticed for phases 4, 5 and coverage, leaving `test_engine.py` at
905 lines with 61 `def test_` statements but only 42 unique names — Python keeps the
last definition, so the duplicates were inert and the suite still reported 48. The
signal was visible and missed: 61 definitions against 48 collected tests.

**Guard:** after any append to a source file, check `ast.parse` for repeated
top-level names rather than trusting a green suite — identical duplicates do not
fail, they shadow. This guard was run after every arc-2 edit.

**A test encoded a wrong assumption.** An arc-1 phase 3 test asserted both orders
failed identically for the replica-cap pair; in fact whichever change goes first
fits under the cap. The engine was right and the test was wrong, which is what
surfaced the branch-ordering decision in section 4.

**A returned field was read after the thing it described had moved.** `_land`
advances `session.base_version` on commit, so `Submission` reported the *new*
version as the base the edit was built on. Fixed by capturing `base` before anything
mutates it. A reminder that "read it from the object" is wrong once the object is
mutated mid-function.

**Rewiring event handlers twice silently disabled a control.** The order-toggle
handler called `wirePanel()` again, attaching a second click listener to every pin
button — so after using the toggle once, a pin click fired twice and cancelled
itself out. Split into `wireOrderButtons()`. Re-attaching handlers to a subtree you
did not re-render is always a bug.

**A UI affordance failed three times before the UI was blamed.** The pin was missed,
then placed on the wrong row, then reported as broken — and the engine was correct
every time. Fixes, in order: made the pin a labelled button instead of a faded
emoji; showed pins in the change summary before Apply; and finally rendered the
**full derived change** ("will be submitted as": postconditions, automatic
preconditions, pinned preconditions) so the operator can verify intent *before*
submitting rather than reading it off the verdict. The lesson: when a declarative
system keeps producing the "wrong" answer, show the user what was actually declared.

---

## 13. Where this could go next

Discussed but not built. Recorded so the reasoning is not lost.

- **Relative postconditions** (`add` alongside `set`) — two `+1`s on the same field
  genuinely commute. The deepest remaining answer to the statement's own first
  question. ~2–3h.
- **Last-write-wins mirror** — a toggle showing what a naive system would have done
  beside every rejection. The fastest way to make the value legible. ~1–2h.
- **Live pre-verdict** — reconcile the two *uncommitted* drafts on a debounce and
  show the verdict before anyone clicks Apply. ~2–3h.
- **Invariant budget meters** — render `replica_cap` as a segmented bar that goes red
  as you type. ~1–2h.
- **Audience-written invariants** — templated rule builder in the UI, so a judge can
  constrain the system live. ~3–4h.
- **AgentArbiter** — swap the operators for LLM agents that emit `Change` objects via
  structured output; the engine stays the sole arbiter, and a rejected agent replans
  from the explanation. Shares one `propose_change()` with a natural-language intent
  compiler. ~6–8h.
- **Terraform PR gate** — `terraform plan -json` gives postconditions (`after`) and
  the optimistic lock (`before`) deterministically, and the state `serial` *is* the
  version stamp. Blocked on 9.2 (multi-resource changes).
