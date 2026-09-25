"""The declaration a change is checked under.

A `Node` says what kind of change is being judged (`test`, `impl` or
`refactor`), which requirements it binds with what accepted and rejected
examples, which files it may touch, and the thresholds the checks apply.
The audit builds exactly one, `audit.audit_node()`: a `refactor` with a
placeholder requirement, no target files, and every threshold at the value
the schema pins. The models validate their own invariants (a near-miss
reject per requirement, repo-relative target paths) so the checks can trust
what they are handed.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmptyStr = Annotated[str, Field(min_length=1)]
# min_length=1 admits "   ", which states nothing. Requirement statements
# are the one field whose whole purpose is to be readable by a test
# author, so blankness has to be rejected rather than counted.
Statement = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NodeId = Annotated[str, Field(min_length=1, max_length=64)]
ReasoningBudget = Literal["zero", "low", "medium", "xhigh"]
# The kill rates a declaration may demand; 85 is the floor. PEP 586 forbids
# float Literals for the type checker; pydantic accepts them as an enum.
KillThreshold = Literal[85.0, 90.0, 95.0, 100.0]  # type: ignore[valid-type]
NodeKind = Literal["test", "impl", "refactor"]
# The one shape a requirement id may take; `gates.REQUIREMENT_CITATION`
# looks for the same shape in test sources.
RequirementId = Annotated[str, Field(pattern=r"^REQ-\d{3}$")]
# How far, in edits, a reject may sit from its nearest accept. Lives here
# rather than in gates.py because dag sits below gates in the layering and
# the validator that enforces it is `Requirement`'s own.
REQ_NEAR_MISS_K: Final = 3


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance: insertions, deletions and substitutions, each cost 1."""
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, start=1):
        current = [i]
        for j, y in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


class Requirement(BaseModel):
    """One acceptance criterion: an ID, what it requires, and examples either way.

    A bare ID states nothing, so no check could tell whether a test tests
    it; the statement and the examples are what make the binding checkable
    by something other than the test's own author.
    """

    model_config = ConfigDict(extra="forbid")

    id: RequirementId
    statement: Statement
    # Concrete inputs the statement admits and refuses, at least one each.
    # A reject far from every accept (`"user"` against `user@example.com`)
    # rejects nothing a lazy validator would not; a near-miss is what
    # tells the requirement from a looser one, so `_rejects_are_near_misses`
    # bounds the edit distance.
    accepts: list[str] = Field(min_length=1)
    rejects: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _rejects_are_near_misses(self) -> Requirement:
        """Every reject sits within `REQ_NEAR_MISS_K` edits of some accept."""
        for reject in self.rejects:
            nearest = min(self.accepts, key=lambda accept: edit_distance(reject, accept))
            distance = edit_distance(reject, nearest)
            if distance > REQ_NEAR_MISS_K:
                msg = (
                    f"{self.id}: reject {reject!r} is {distance} edits from its nearest "
                    f"accept {nearest!r}; a near-miss is within {REQ_NEAR_MISS_K}"
                )
                raise ValueError(msg)
        return self

    @property
    def examples(self) -> list[tuple[str, str, str]]:
        """`(id, "accepts"|"rejects", text)` for every example, accepts first."""
        return [(self.id, "accepts", text) for text in self.accepts] + [
            (self.id, "rejects", text) for text in self.rejects
        ]


class MutationSample(BaseModel):
    """What the mutation check runs over and the kill rate it demands."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["changed-lines"]
    # Kept for the shape's sake: `evidence.mutation_sample` no longer
    # truncates to it, and every changed-line mutant mutmut decides enters
    # the population. The wall-clock bound is `evidence._MUTATION_TIMEOUT_S`.
    max_mutants: Literal[100] = 100
    # The kill rate a change must clear once the sample is large enough
    # (`gates.MIN_SIGNIFICANT_MUTANTS`); the enum floors it at 85 while
    # still permitting a stricter value.
    kill_threshold: KillThreshold = 85.0


class DeterministicGate(BaseModel):
    """The test command and the thresholds the checks apply."""

    model_config = ConfigDict(extra="forbid")

    test_command: NonEmptyStr
    # Every changed line must be executed: the only value the schema admits,
    # so no declaration can lower the coverage bar. PEP 586 forbids float
    # Literals for the type checker; pydantic accepts them as an enum.
    changed_line_coverage_min: Literal[100.0] = 100.0  # type: ignore[valid-type]
    # Red-phase cannot be waived: the only value the schema admits.
    red_phase_required: Literal[True] = True
    mutation_sample: MutationSample


class ExecutionConstraints(BaseModel):
    """What the author of the change was allowed to do; `allowed_tools` feeds `node-scope`."""

    model_config = ConfigDict(extra="forbid")

    reasoning_budget: ReasoningBudget
    # `node-scope` reads this: a change may create files only when
    # `write_file` is listed.
    allowed_tools: list[NonEmptyStr] = Field(min_length=1)
    max_context_tokens: int = Field(ge=8000, le=30000)


class Node(BaseModel):
    """One declared change: its kind, requirements, permitted files and thresholds."""

    model_config = ConfigDict(extra="forbid")

    id: NodeId
    # The test/implementation split: an `impl` change may not edit tests
    # and a `test` change may not ship the implementation, so the tests a
    # change is judged by are never the tests it just rewrote. `refactor`
    # is the behaviour-preserving case, which has to move code and its
    # tests together.
    kind: NodeKind
    dependencies: list[NonEmptyStr]
    task_prompt: NonEmptyStr
    requirements: list[Requirement] = Field(min_length=1)
    execution_constraints: ExecutionConstraints
    deterministic_gate: DeterministicGate
    # Repo-relative files this change may touch. Empty means unrestricted,
    # so an omitted field changes nothing and a declared list can only
    # narrow the scope. Validated by `_repo_relative_posix` rather than a
    # regex `pattern`: no lookahead-free regex says "no `..` segment".
    target_files: list[NonEmptyStr] = Field(default_factory=list)

    @field_validator("target_files")
    @classmethod
    def _repo_relative_posix(cls, paths: list[str]) -> list[str]:
        """Each entry is a forward-slash path with no empty, `.` or `..` segment and no padding."""
        for path in paths:
            if (
                "\\" in path
                or path != path.strip()
                or any(segment in ("", ".", "..") for segment in path.split("/"))
            ):
                msg = (
                    f"target_files entry {path!r} must be a repo-relative POSIX path "
                    "without empty, '.' or '..' segments"
                )
                raise ValueError(msg)
        return paths

    @property
    def requirement_ids(self) -> list[str]:
        """The declared requirement IDs, in order, for the requirement-binding check."""
        return [requirement.id for requirement in self.requirements]

    @property
    def requirement_examples(self) -> list[tuple[str, str, str]]:
        """Every accept and reject the requirements cite, for the requirement-binding check."""
        return [example for requirement in self.requirements for example in requirement.examples]
