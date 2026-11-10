"""Read-only source profiling and reproducible prepared business databases."""
from .contracts import (AgentSchema, BusinessProfile, PreparationConfig,
                        PreparationSpec, PreparedBusiness)


def profile_business(*args, **kwargs):
    """Inspect all business sources without reading questions or references."""
    from .pipeline import profile_business as operation
    return operation(*args, **kwargs)


def prepare_business(*args, **kwargs):
    """Publish or reuse a validated, content-addressed preparation bundle."""
    from .pipeline import prepare_business as operation
    return operation(*args, **kwargs)


def load_agent_schema(*args, **kwargs):
    """Load only the schema; private audit examples are never included."""
    from .pipeline import load_agent_schema as operation
    return operation(*args, **kwargs)


__all__ = ['AgentSchema', 'BusinessProfile', 'PreparationConfig', 'PreparationSpec',
           'PreparedBusiness', 'profile_business', 'prepare_business', 'load_agent_schema']
