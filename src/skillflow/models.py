"""Versioned strict public formats; contracts are data, never executable code."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Id = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Resource(Strict):
    id: Id
    version: str
    sha256: Digest
    content: str


class Obligation(Strict):
    id: Id
    text: str
    origin_ref: str
    kind: Literal["mechanical", "observed_action", "judgment"]
    coverage: Literal["complete", "partial", "absent"]
    verifier: Literal["text", "json", "source", "connector", "review"] | None
    verifier_version: Literal["1"] = "1"
    role: Literal["controller", "connector", "reviewer"]
    parameters: dict = Field(default_factory=dict)


class Step(Strict):
    id: Id
    obligations: list[Obligation] = Field(min_length=1)
    depends_on: list[Id] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=lambda: ["artifact"])
    correction_budget: int = Field(default=3, ge=1, le=100)
    allowed_corrections: list[
        Literal["artifact_submit", "message_submit", "source_open", "piece_attach", "retry"]
    ] = Field(default_factory=lambda: ["artifact_submit", "retry"])
    branch: Id | None = None
    branch_value: str | None = None


class SourcePolicy(Strict):
    id: Id
    url: str
    max_age_seconds: int = Field(ge=1, le=31_536_000)
    media_types: list[str] = Field(default_factory=lambda: ["text/plain", "application/json"])
    sha256: Digest | None = None


class ChildSlot(Strict):
    id: Id
    contract: str
    required: bool = True


class Contract(Strict):
    schema_version: Literal["1"]
    id: Id
    version: Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
    origin: str
    instructions: str
    resources: list[Resource] = Field(default_factory=list)
    outputs: list[Literal["text/plain", "text/markdown", "application/json"]]
    steps: list[Step]
    sources: list[SourcePolicy] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    children: list[ChildSlot] = Field(default_factory=list)
    branches: dict[str, list[str]] = Field(default_factory=dict)
    review_required: bool = True
    reviewers: list[Id] = Field(default_factory=list)
    tools: list[Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]] = Field(
        default_factory=list
    )
    human_approval_required: bool = True
    legal: bool = False
    operation_prerequisites: dict[
        Literal[
            "source_open",
            "artifact_submit",
            "message_submit",
            "delegation_request",
            "skill_request",
        ],
        list[Id],
    ] = Field(default_factory=dict, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def coherent(self):
        from .util import sha256

        ids = [s.id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate step")
        obligations = [o.id for s in self.steps for o in s.obligations]
        if len(set(obligations)) != len(obligations):
            raise ValueError("duplicate obligation")
        for field in (self.resources, self.sources, self.children):
            if len({x.id for x in field}) != len(field):
                raise ValueError("duplicate resource, source or child slot")
        for resource in self.resources:
            if sha256(resource.content.encode()) != resource.sha256:
                raise ValueError("resource fingerprint mismatch")
        if self.legal and not (self.human_approval_required and self.review_required):
            raise ValueError("legal contracts require review and human approval")
        if any(not values or len(set(values)) != len(values) for values in self.branches.values()):
            raise ValueError("empty or ambiguous branch")
        graph = {s.id: s.depends_on for s in self.steps}
        for dependencies in self.operation_prerequisites.values():
            if not dependencies or len(set(dependencies)) != len(dependencies):
                raise ValueError("Operation prerequisites must be nonempty and unique")
            if not set(dependencies) <= set(graph):
                raise ValueError("Unknown operation prerequisite")
        done = set()
        for step in self.steps:
            if any(dep not in graph for dep in step.depends_on):
                raise ValueError("unknown dependency")
            # A `piece:<label>` is a case document the step reasons about: the letter, the
            # internal rule, the statement of facts. The step listing it is the
            # declaration, so no separate list is needed. A source stays different: it is
            # acquired by the collector from a pinned URL, so it must be declared.
            if not step.inputs or any(
                i not in ("artifact", "context", "message")
                and i not in {"source:" + s.id for s in self.sources}
                and not re.fullmatch(r"piece:[a-z0-9][a-z0-9-]{0,62}", i)
                for i in step.inputs
            ):
                raise ValueError("unknown or empty input set")
            if not self.outputs and "artifact" in step.inputs:
                raise ValueError("Outputless workflow cannot require an artifact")
            if step.branch is not None and step.branch_value not in self.branches.get(
                step.branch, []
            ):
                raise ValueError("unknown branch decision")
            if (step.branch is None) != (step.branch_value is None):
                raise ValueError("incomplete branch condition")
            for dep in step.depends_on:
                parent = next(s for s in self.steps if s.id == dep)
                if parent.branch and (parent.branch, parent.branch_value) != (
                    step.branch,
                    step.branch_value,
                ):
                    raise ValueError("dependency may be excluded; unreachable step")
            for obligation in step.obligations:
                role = {
                    "text": "controller",
                    "json": "controller",
                    "source": "controller",
                    "connector": "connector",
                    "review": "reviewer",
                }
                kind = {
                    "text": "mechanical",
                    "json": "mechanical",
                    "source": "mechanical",
                    "connector": "observed_action",
                    "review": "judgment",
                }
                if obligation.coverage != "absent" and obligation.verifier is None:
                    raise ValueError("covered obligation without verifier")
                if obligation.verifier and (
                    role[obligation.verifier] != obligation.role
                    or kind[obligation.verifier] != obligation.kind
                ):
                    raise ValueError("verifier role/kind mismatch")
                p = obligation.parameters
                allowed = {
                    "text": {"contains", "min_length"},
                    "json": {"schema"},
                    "source": set(),
                    "connector": set(),
                    "review": set(),
                    None: set(),
                }
                if set(p) - allowed[obligation.verifier]:
                    raise ValueError("unknown verifier parameter")
                if obligation.verifier == "text":
                    if (
                        not isinstance(p.get("contains", []), list)
                        or not all(isinstance(v, str) for v in p.get("contains", []))
                        or type(p.get("min_length", 1)) is not int
                        or p.get("min_length", 1) < 1
                    ):
                        raise ValueError("invalid text verifier parameters")
                if obligation.verifier == "json":
                    from jsonschema import Draft202012Validator

                    Draft202012Validator.check_schema(p.get("schema", {}))
                    if '"$ref"' in str(p).replace("'", '"'):
                        raise ValueError("JSON verifier schemas must be self-contained")
                if obligation.verifier == "source" and not any(
                    i.startswith("source:") for i in step.inputs
                ):
                    raise ValueError("source verifier requires a source input")
                if (
                    obligation.verifier in ("text", "json")
                    and len({"artifact", "message"} & set(step.inputs)) != 1
                ):
                    raise ValueError(
                        "text/json verifier requires exactly one artifact or message input"
                    )
        while len(done) < len(graph):
            ready = {k for k, deps in graph.items() if k not in done and set(deps) <= done}
            if not ready:
                raise ValueError("accidental dependency cycle")
            done |= ready
        return self


class ReviewVerdict(Strict):
    verdict: Literal["JUSTE", "FAUX", "NON_VERIFIE"]
    reason: str = Field(min_length=1, max_length=100_000)
    references: list[str]
    obligations: dict[str, Literal["JUSTE", "FAUX", "NON_VERIFIE"]]


class Call(Strict):
    action: str = Field(min_length=1, max_length=64)
    arguments: dict = Field(default_factory=dict)
    revision: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
