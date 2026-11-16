"""Independent relationship hierarchy computation.

This package is deliberately separate from the entity resolution flow:
ingest/resolve/merge/split never read or write relationship data, and the
relationship engine only reads a pinned entity snapshot through a read-only
connection.
"""
from .artifact import RelationEngine, RelationStore, load_pinned_snapshot
from .contract import (
    ROOT,
    RULE_VERSION,
    Assertion,
    PinnedSnapshot,
    RelationBatchRejected,
    assertion_digest,
    record_digest,
)
from .solver import (
    BudgetExhausted,
    branch_and_bound,
    edmonds_contract,
    enumerate_all_forests,
    enumerate_forests,
)
from .timeline import merge_intervals, solve_timeline
from .verify import VerificationError, verify_segment

__all__ = [
    "RelationEngine",
    "RelationStore",
    "load_pinned_snapshot",
    "Assertion",
    "PinnedSnapshot",
    "RelationBatchRejected",
    "BudgetExhausted",
    "VerificationError",
    "ROOT",
    "RULE_VERSION",
    "record_digest",
    "assertion_digest",
    "edmonds_contract",
    "branch_and_bound",
    "enumerate_forests",
    "enumerate_all_forests",
    "solve_timeline",
    "merge_intervals",
    "verify_segment",
]
