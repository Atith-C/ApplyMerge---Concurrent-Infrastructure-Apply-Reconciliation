# Demo script

Four minutes. Everything below is real: real GitHub accounts, a real repository,
real commits. Nothing is staged in advance except the starting state.

## Before you start

```powershell
# .env
APPLYMERGE_BACKEND=github
APPLYMERGE_REPO=Atith-C/applymerge-state
APPLYMERGE_TOKEN=github_pat_...      # reads: 5,000/hour instead of 60

uvicorn apply_merge.api:app --port 8000
```

- **Chrome** signed in as your main account, **incognito** as the second. One
  browser holds one GitHub session, so you need two.
- A third tab on `github.com/<owner>/applymerge-state/commits/main`.
- **Check the headroom.** Open `/live` and add up the `replicas` across both
  databases. The cap is 10. **If the total is already 10 there is no room to scale
  and every demo below fails.** Scale `db-primary` down to 3 first — a decrease can
  never breach the cap.

**Target starting state:** `db-primary.replicas = 3`, `db-replica.replicas = 4`,
`status = active`, `tier = silver`. Total 7, headroom 3.

---

## 0:00 — What this is

> "Two operators change the same infrastructure at the same time. The question
> isn't *who wins* — it's whether both changes can survive, and how you'd know."

Show both browsers side by side. Both say **working from the same commit sha**.

> "That's not a simulation. Both consoles are pinned to the same commit in a real
> repository. That's what makes them concurrent."

---

## 0:30 — CONFLICT

**Chrome:** `db-primary.replicas` → **5** → Apply

> **APPLIED.** Point at the version chain — the sha is now clickable.

**Switch to the GitHub tab. Refresh.** There's a new commit, authored by you.
Open it:

```
Preconditions: replicas == 3
Postconditions: replicas = 5
Invariants confirmed: replica_cap, replicas_non_negative, ssh_not_public
```

> "Nobody typed those preconditions. The system derived them from what I was
> looking at when I decided."

**Incognito** — still on the old sha, hasn't refreshed: `db-primary.replicas` → **8** → Apply

> **CONFLICT.**

> "There is no state in which replicas is both 5 and 8. Averaging to 6 invents a
> number nobody asked for; picking a side discards a declared intent. So nothing
> is applied."

Point at the extra line: 8 + 4 = 12 would also breach the cap.

> "It even tells you that choosing *his* side wouldn't have worked either."

**And the payoff:** point at *"Your change was kept — pull request #N"*. Open it.

> "The rejected change isn't gone. It's a branch cut from the version he was
> actually working on, so the diff **is** the disagreement. GitHub shows it as
> conflicting — same verdict, reached independently."

---

## 1:30 — MERGED

Refresh incognito so both are current.

| Chrome | Incognito |
| --- | --- |
| `sg-web.owner` → `platform-team` | `sg-web.port` → `8443` |

Apply Chrome, then incognito.

> **MERGED**, marked *concurrent with*.

> "Same resource. Both applied. They wrote different fields and neither read what
> the other wrote, so the order genuinely doesn't matter — and we checked by
> running both orders, not by assuming."

Two commits on GitHub, two different authors, both intents present.

---

## 2:15 — ORDER_DEPENDENT — the surprising one

Refresh both.

**Chrome:** `db-primary.status` → `maintenance` → Apply → **APPLIED**

**Incognito:** `db-primary.tier` → `gold`, then **click 🔒 pin on `status`**

Before applying, point at *Will be submitted as*:

```
POSTCONDITIONS   tier = "gold"
PRECONDITIONS    tier == "silver"     automatic
                 status == "active"   pinned
```

> "That pin is me saying: promote this database, but only while it's actually
> serving traffic."

Apply.

> **ORDER-DEPENDENT.**

> "These two write **different fields** — `status` and `tier`. Every field-level
> merge tool on earth calls that safe. It isn't."

Use the **Theirs first / Ours first** toggle.

> "His first: the database goes into maintenance, my promotion is refused. Mine
> first: both succeed. Same two changes, two different worlds."

> "Nothing in the declarative model says who came first — so we refuse to pick,
> and hand you both candidate states."

**Then remove the pin and do it again.** It merges.

> "That one click is the whole difference between 'these commute' and 'the order
> decides'."

---

## 3:00 — INVARIANT_REJECTED — the one nothing else can do

Refresh both. Ensure headroom (total ≤ 7).

| Chrome | Incognito |
| --- | --- |
| `db-primary.replicas` → **5** | `db-replica.replicas` → **6** |

Apply Chrome → **APPLIED**. Apply incognito.

> **INVARIANT-REJECTED.**

> "Different resources. Different fields. Nothing contested — a conflict detector
> has nothing to find here. Each change is fine alone: 5 + 4 is 9, 3 + 6 is 9.
> **Together they need 11, and the cap is 10.**"

Point at the arithmetic, itemised by contributor.

> "Your policy engine checks one change at a time. Both of these pass it. We check
> the **combination** — and that class of bug is invisible to every per-change
> tool on the market."

> "Neither operator did anything wrong, so neither change is discarded and neither
> request is quietly trimmed to fit. Both are parked as pull requests."

---

## 3:45 — Close

Show the GitHub tab: the commit list, two accounts, every message carrying its
preconditions and invariants. Then the pull requests tab.

> "Every commit in this repository satisfies every declared invariant — because
> the ones that didn't were never written. And nothing was thrown away: every
> refusal is still open as a pull request."

> "Git gives you optimistic concurrency over bytes. We do it over meaning."

---

# If something goes wrong

## The network dies, or GitHub rate-limits you

```
APPLYMERGE_BACKEND=memory
```

Restart. **Everything still works** — two consoles on one page, four preset
buttons that stage each outcome, no network, no sign-in. You lose the real commits
and the pull requests; you keep the entire argument.

The presets are the fastest path: **Reset world**, click a scenario, Apply left,
Apply right. Say plainly: *"This is the same engine — it just isn't writing to
GitHub right now."*

## Everything is rejected as INVARIANT_VIOLATED

The fleet is at the cap. Scale `db-primary` **down** to 3 and continue. A decrease
can never breach an upper bound.

## A rejection says the pull request couldn't be opened

The verdict is still correct and still on screen — that's by design; parking is
best-effort and can never cost you an answer. Carry on and don't mention it.

## A console is stuck showing a stale version

Click **refresh to v_N_** in the amber bar. It rebases that console onto the live
state and discards its edits.

---

# Questions you will get

**"Isn't this just git merge?"**
> Git merges text. It would happily merge `replicas: 5` and `replicas: 6` if they
> were on different lines, and it has no idea a fleet-wide cap exists. Our
> invariant-rejected case has **zero** textual overlap and still must be refused.

**"Isn't this just OPA / Sentinel / a policy engine?"**
> Those evaluate one change against policy. We evaluate **two pending changes
> together.** Each of ours passes policy individually; the combination doesn't.

**"Why not just lock the state?"**
> That's what Terraform does, and it's why one engineer waits while another applies
> something unrelated. We answer *whether* you need to serialise — and the
> order-dependent case shows that field-disjointness isn't enough to prove you
> don't.

**"What if an operator edits the file directly on GitHub?"**
> We notice. Its writes are reconstructed by diffing against the parent, and its
> reads are recorded as unknown — because a read leaves no trace in a diff. That's
> also why our own commits carry a machine-readable trailer.

**"Is any of this AI?"**
> No, and deliberately. The problem statement's premise is that merging is only
> meaningful when semantics are explicit rather than guessed. Every verdict here is
> a deterministic function of declared fields, and 144 tests pin it down.
