"""Deterministic question kinds answered by fixed queries; the model handles everything else."""
from .catalogue import answer, analyze
from .context import Context

__all__ = ['answer', 'analyze', 'Context']
