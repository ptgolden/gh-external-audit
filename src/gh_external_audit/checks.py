from typing import Any, Iterable

from .models import ProblemRecord


def check_node_runtime(metadata: dict[str, Any]) -> Iterable[ProblemRecord]:
    """Flag JavaScript actions running on Node older than version 24."""
    runs = metadata.get("runs")
    if not isinstance(runs, dict):
        return
    using = runs.get("using")
    if not isinstance(using, str):
        return
    runtime = using.lower()
    if runtime.startswith("node"):
        version = runtime.removeprefix("node")
        if version.isdigit() and int(version) < 24:
            yield ProblemRecord(code="node_lt_24", detail=using)


ACTION_CHECKS = [check_node_runtime]


def audit_action(metadata: dict[str, Any]) -> Iterable[ProblemRecord]:
    """Run every action-level check on parsed action metadata."""
    for check in ACTION_CHECKS:
        yield from check(metadata)
