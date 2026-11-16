"""Cross-source entity resolution and versioned master data."""
from .service import EntityService
from .storage import BatchConflict, RevisionConflict

__all__ = ["EntityService", "BatchConflict", "RevisionConflict"]
