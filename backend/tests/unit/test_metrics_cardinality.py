"""Enforces the cardinality policy documented in app/core/metrics.py's module
docstring: no metric may carry a label whose value could be unbounded (a
user/org/request/trace/conversation/document id, a raw query, a filename, a
URL, or an exception message). This is a static check over every metric's
declared label *names* - it can't prove a label's *values* stay bounded at
runtime, but it catches the far more common mistake of naming a label after
exactly the kind of identifier the policy forbids."""

from prometheus_client import REGISTRY

_FORBIDDEN_LABEL_NAMES = {
    "user_id",
    "organization_id",
    "org_id",
    "request_id",
    "trace_id",
    "span_id",
    "conversation_id",
    "document_id",
    "message_id",
    "chunk_id",
    "query",
    "question",
    "filename",
    "url",
    "exception",
    "error_message",
    "message",
}


def test_no_registered_metric_uses_a_forbidden_label_name():
    violations: list[str] = []
    for collector in list(REGISTRY._collector_to_names.keys()):  # noqa: SLF001
        describe = getattr(collector, "_labelnames", None)
        if not describe:
            continue
        for label in describe:
            if label.lower() in _FORBIDDEN_LABEL_NAMES:
                name = getattr(collector, "_name", repr(collector))
                violations.append(f"{name}: label {label!r}")
    assert not violations, f"Forbidden high-cardinality labels found: {violations}"
