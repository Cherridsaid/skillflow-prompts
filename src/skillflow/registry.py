"""Read-only approved contracts and principals loaded by the controller owner."""

import secrets
from dataclasses import dataclass
from pathlib import Path

from .models import Contract
from .util import canonical, require, sha256, strict_json


@dataclass(frozen=True)
class Principal:
    id: str
    role: str
    case_keys: tuple[str, ...]


def covers(principal, case_key):
    """Exact membership, the global "*", or an operator prefix grant such as "auto:claude:*".

    A literal key never matches by prefix: only a grant ending in "*" widens scope.
    """
    for granted in principal.case_keys:
        if granted == case_key or granted == "*":
            return True
        if granted.endswith("*") and len(granted) > 1 and case_key.startswith(granted[:-1]):
            return True
    return False


class Registry:
    def __init__(self, documents):
        self.contracts = {}
        for document in documents:
            contract = Contract.model_validate(document)
            ref = f"{contract.id}@{contract.version}"
            require(ref not in self.contracts, "CONTRACT_COLLISION", "Duplicate contract reference")
            self.contracts[ref] = contract
        graph = {}
        for ref, contract in self.contracts.items():
            graph[ref] = contract.dependencies + [c.contract for c in contract.children]
            require(
                set(graph[ref]) <= set(self.contracts),
                "UNKNOWN_SKILL",
                "Unresolved contract reference",
            )
        visited = set()
        while len(visited) < len(graph):
            ready = {k for k, deps in graph.items() if k not in visited and set(deps) <= visited}
            require(ready, "CONTRACT_CYCLE", "Cross-skill dependency or child cycle")
            visited |= ready

    @classmethod
    def from_directory(cls, path):
        return cls([strict_json(p.read_bytes()) for p in sorted(Path(path).glob("*.json"))])

    def get(self, ref):
        require(ref in self.contracts, "UNKNOWN_SKILL", "Skill/version is not approved")
        return self.contracts[ref].model_copy(deep=True)

    def fingerprint(self, ref):
        return sha256(canonical(self.get(ref).model_dump()))


def authenticate(token, config):
    digest = sha256(token.encode())
    for item in config["principals"]:
        if secrets.compare_digest(digest, item["token_sha256"]):
            return Principal(item["id"], item["role"], tuple(item["case_keys"]))
    require(False, "UNAUTHENTICATED", "Valid scoped credentials required")
