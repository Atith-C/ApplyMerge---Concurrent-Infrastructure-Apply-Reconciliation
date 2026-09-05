"""Invariant definitions and the checker that runs them against whole infra state."""

from apply_merge.models import Invariant, InvariantResult, Resource

REPLICA_CAP = 10


def _total_replicas(resources: dict[str, Resource]) -> int:
    return sum(
        r.fields.get("replicas", 0) for r in resources.values() if r.type == "database"
    )


def _replica_cap(resources: dict[str, Resource]) -> str | None:
    """Cross-resource: the whole fleet shares one replica budget."""
    total = _total_replicas(resources)
    if total <= REPLICA_CAP:
        return None
    contributors = ", ".join(
        f"{r.id}={r.fields.get('replicas', 0)}"
        for r in sorted(resources.values(), key=lambda r: r.id)
        if r.type == "database"
    )
    return f"total replicas = {total} ({contributors}), cap is {REPLICA_CAP}"


def _ssh_not_public(resources: dict[str, Resource]) -> str | None:
    """Per-resource: no security group may expose SSH to the whole internet."""
    offenders = [
        r.id
        for r in sorted(resources.values(), key=lambda r: r.id)
        if r.type == "security_group" and r.fields.get("ssh_cidr") == "0.0.0.0/0"
    ]
    if not offenders:
        return None
    return f"{', '.join(offenders)} allows SSH from 0.0.0.0/0"


replica_cap = Invariant(
    name="replica_cap",
    description=f"Total replicas across all databases must be <= {REPLICA_CAP}",
    predicate=_replica_cap,
)

ssh_not_public = Invariant(
    name="ssh_not_public",
    description="No security group may allow SSH (port 22) from 0.0.0.0/0",
    predicate=_ssh_not_public,
)

DEFAULT_INVARIANTS = [replica_cap, ssh_not_public]


def check_all(
    resources: dict[str, Resource], invariants: list[Invariant]
) -> list[InvariantResult]:
    """Run every invariant against the whole state. Order is the declared order."""
    return [inv.check(resources) for inv in invariants]


def failures(results: list[InvariantResult]) -> list[InvariantResult]:
    return [r for r in results if not r.passed]
