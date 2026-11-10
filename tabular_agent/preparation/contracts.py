"""Versioned preparation contracts. Internal profiles must never become prompts."""
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

VERSION = '1'


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal['1'] = VERSION


class Issue(Contract):
    code: str
    severity: Literal['info', 'warning', 'blocked'] = 'warning'
    message: str
    source_id: str | None = None
    relation: str | None = None
    column: str | None = None
    count: int = 1


class ColumnProfile(Contract):
    name: str
    label: str
    position: int
    description: str = ''
    role: str | None = None
    parser: dict[str, Any] = Field(default_factory=dict)
    source_types: dict[str, int] = Field(default_factory=dict)
    representations: dict[str, int] = Field(default_factory=dict)
    missing: int = 0
    blank: int = 0
    populated: int = 0
    distinct: int = 0
    parse_failures: int = 0
    failure_examples: list[dict[str, Any]] = Field(default_factory=list)
    frequent_values: list[dict[str, Any]] = Field(default_factory=list)
    minimum: str | None = None
    maximum: str | None = None
    negative: int = 0
    zero: int = 0
    precision: int = 0
    scale: int = 0
    aware_timestamps: int = 0
    naive_timestamps: int = 0
    all_empty: bool = False
    constant: bool = False
    unique: bool = False
    usable: bool = False
    sql_type: str = 'VARCHAR'


class TableProfile(Contract):
    source_id: str
    region_id: str
    source: dict[str, Any]
    name: str
    labels: list[str]
    header_rows: list[int]
    extraction: dict[str, Any] = Field(default_factory=dict)
    accounting: dict[str, int] = Field(default_factory=dict)
    columns: list[ColumnProfile] = Field(default_factory=list)
    grain: str = 'unknown'
    roles: dict[str, str] = Field(default_factory=dict)
    candidate_keys: list[str] = Field(default_factory=list)
    duplicate_policy: str = 'preserve'
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal['ready', 'partial', 'blocked'] = 'blocked'
    records: int = 0
    duplicates_removed: int = 0
    model_assisted: bool = False


class BusinessProfile(Contract):
    business: str
    fingerprint: str
    inventory: list[dict[str, Any]] = Field(default_factory=list)
    tables: list[TableProfile] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    reconciliations: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    measurements: dict[str, Any] = Field(default_factory=dict)
    status: Literal['ready', 'partial', 'blocked'] = 'blocked'


class PreparationSpec(Contract):
    """Overrides are source/region keyed, never question or business keyed."""
    layouts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    tables: dict[str, dict[str, Any]] = Field(default_factory=dict)
    model_configuration: dict[str, Any] = Field(default_factory=dict)


class AgentSchema(Contract):
    dialect: str = 'duckdb'
    relations: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rules: str = ''
    temporal_coverage: dict[str, Any] = Field(default_factory=dict)
    supported_functions: list[str] = Field(default_factory=lambda: [
        'COUNT', 'SUM', 'MIN', 'MAX', 'CASE', 'COALESCE', 'DATE_TRUNC',
        'EXTRACT', 'ROW_NUMBER', 'LAG', 'LEAD'])
    limitations: list[str] = Field(default_factory=list)


class PreparedBusiness(Contract):
    bundle_id: str
    path: Path
    database: Path
    status: Literal['ready', 'partial', 'blocked']
    schema_path: Path
    profile_path: Path
    cache_hit: bool = False
    measurements: dict[str, Any] = Field(default_factory=dict)


class PreparationConfig(Contract):
    batch_size: int = Field(default=2000, ge=1, le=20000)
    sample_rows: int = Field(default=128, ge=16, le=512)
    memory_limit: str = '1GB'
    deadline_seconds: float = Field(default=600, gt=0)
    model_configuration: dict[str, Any] = Field(default_factory=dict)
