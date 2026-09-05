"""Declarative vocabulary: Resource, Change, Precondition, Postcondition, Invariant."""

import operator
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

# The only comparison operators a precondition may use.
OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    "<=": operator.le,
    ">=": operator.ge,
}

Operator = Literal["==", "!=", "<=", ">="]


class Resource(BaseModel):
    """One declarative infra resource, e.g. a database or a security group."""

    id: str
    type: str
    fields: dict[str, Any] = Field(default_factory=dict)


class Precondition(BaseModel):
    """A check the target resource must satisfy before a change may apply."""

    field: str
    op: Operator
    value: Any

    def holds(self, resource: Resource) -> bool:
        if self.field not in resource.fields:
            return False
        return OPERATORS[self.op](resource.fields[self.field], self.value)

    def describe(self) -> str:
        return f"{self.field} {self.op} {self.value!r}"


class Postcondition(BaseModel):
    """A field the change sets to a target value."""

    field: str
    value: Any

    def describe(self) -> str:
        return f"{self.field} = {self.value!r}"


class Change(BaseModel):
    """A proposed change to exactly one resource, with explicit semantics."""

    id: str
    resource_id: str
    preconditions: list[Precondition] = Field(default_factory=list)
    postconditions: list[Postcondition] = Field(min_length=1)
    description: str
    origin: str

    def touched_fields(self) -> set[str]:
        return {p.field for p in self.postconditions}


class InvariantResult(BaseModel):
    """Outcome of checking one invariant against a whole infra state."""

    name: str
    passed: bool
    reason: str | None = None


class Invariant(BaseModel):
    """A rule that must hold across the whole infra state, per-resource or cross-resource.

    `predicate` takes all resources and returns a failure reason, or None if it holds.
    """

    name: str
    description: str
    # Excluded from serialization: a rule travels over the wire as its name and
    # description, never as the code behind it.
    predicate: Callable[[dict[str, Resource]], str | None] = Field(exclude=True)

    def check(self, resources: dict[str, Resource]) -> InvariantResult:
        reason = self.predicate(resources)
        return InvariantResult(name=self.name, passed=reason is None, reason=reason)
