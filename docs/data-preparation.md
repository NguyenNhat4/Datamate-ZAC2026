# Data profiling and preparation

The preparation layer reads every supported business file and publishes a typed DuckDB database with a documented schema. It does not read questions or reference answers. SQL question answering is a separate consumer; the existing Python agent is unchanged.

## Commands

```bash
make profile ARGS="--business shop_directory"
make prepare ARGS="--business shop_directory"
make prepare
```

`SPLIT` defaults to `data/train`. Omit `--business` to process every business directory. `OUT` selects an artifact directory outside the source split. Profile reports default to `artifacts/profiles/`; prepared bundles default to `artifacts/prepared/`. These directories are Git-ignored and may contain real business records.

An already running local model is optional:

```bash
make prepare ARGS="--business shop_directory --model local-coder-7b --base-url http://127.0.0.1:8000/v1"
```

The model receives bounded source samples and relevant documentation only when layout or meaning is ambiguous. Suggestions are declarative JSON, with one correction opportunity. It cannot run generated code. Only a loopback HTTP model endpoint is accepted. Model weights/dependencies must already be provisioned before inference; preparation does not download them.

Reuse or correct the saved extraction specification:

```bash
make prepare ARGS="--business shop_directory --spec artifacts/prepared/BUNDLE/spec.json"
```

The specification is tied to source/region identifiers, not question identifiers. Changing it creates a different immutable bundle. The generated schema is not permission to access private database namespaces: the future SQL executor must enforce the exported relation/column allowlist.

## Public interfaces

`tabular_agent.preparation` exports:

- `profile_business(business, rulebook, calendar, *, config, spec, proposal_adapter) -> BusinessProfile`
- `prepare_business(business, rulebook, calendar, *, output, config, spec, proposal_adapter) -> PreparedBusiness`
- `load_agent_schema(bundle) -> AgentSchema`

`PreparationConfig` controls batch size, sample size, preparation deadline, DuckDB memory limit and model configuration. Defaults are 2,000 rows per batch, 128 discovery samples, a 600-second business deadline, and a 1 GiB DuckDB memory limit. The memory limit controls DuckDB, not total Python memory; measurements label process peak RSS separately.

All contracts carry version `1` and reject unknown top-level fields. `PreparationSpec` contains source layout overrides and region-specific semantic/parser specifications. Original paths remain authoritative even when display names repair filename encoding damage.

## Full-scan guarantees

Discovery samples suggest headers and parsers; full source scans establish row accounting and conversion outcomes. Each physical source row is classified as header/title, blank, record, summary, note, or unresolved. Quoted CSV multiline cells retain their source starting line coordinates.

Typed parsing handles mixed date representations, explicit serial-date epochs, Vietnamese dates, Decimal money and quantities, and preserved textual identifiers. Missing/blank values remain distinguishable in private profiles. Failed conversions withhold the affected field and dependent views rather than turning failures into ordinary SQL nulls. An unresolved source partition withholds its related complete table union.

Compatible monthly/file partitions are assembled before exact same-order export deduplication. Deduplication requires the current rulebook's applicable clause and real order identity. Orderless identical recordings remain separate. `private.lineage` retains every original record reference, including collapsed duplicate groups; `private.row_audit` retains nonrecord and unresolved source rows with reasons.

Keys and many-to-one relationships are tested against full data. Ordinary sales views require resolved current rules, recognized statuses, and supported whole-order return evidence. Invoice totals are reconciled with cleaned line amounts when both definitions and relationships are supported. No mismatching amount is overwritten merely to make arithmetic agree.

Observed event ranges are distinct from declared source coverage. Inventory snapshots retain their effective date; they do not establish arbitrary historical inventory. Preparation keeps the complete timeline and has no dependence on the system date or a particular question date.

## Bundle contents and statuses

Each content-addressed bundle contains:

| Artifact | Purpose |
|---|---|
| `data.duckdb` | `clean` source relations, supported `semantic` views, and private audit/provenance |
| `profile.json` | Private full-column statistics, representation counts, issues and bounded diagnostic examples |
| `spec.json` | Saved declarative extraction, mapping and parser decisions |
| `schema.json`, `schema.md` | Documented queryable relations and capabilities; no record samples |
| `validation.json` | Quality issues, relationship checks and reconciliations |
| `manifest.json` | Input/code/dependency/configuration hashes, artifact integrity hashes and measurements |

Input content, documentation, rulebook, calendar, specification, preparation code, dependency versions and model configuration participate in cache identity. Bundles publish atomically after processing. Existing bundles are integrity checked before reuse. Inputs changing during the build prevent publication.

- `ready`: published fields passed declared checks and no blocking issue remains.
- `partial`: some trustworthy relations exist, but blocked information/capabilities are documented.
- `blocked`: no trustworthy business relation can be published.

Capabilities separately distinguish `available`, `missing_in_source`, `blocked_by_quality` and `unresolved_mapping`. A preparation failure is not evidence for an `unanswerable` answer. Readable clean tables alone do not prove a business capability.

## Inspection evidence and validation

The initial read-only training inspection found 128 physical business data files: 107 Excel workbooks with 302 sheets, 20 CSV files, and one SQLite database with 17 tables. Eight filenames had recoverable display encoding damage. Worksheet dimensions were absent, so row counts require streaming.

One inspected orderless workbook contains 526 embedded summaries, including daily and monthly summaries. Another sheet contains 347 daily summaries. Summary matching uses both control labels and record shape; product names containing similar words are retained when record identity/date evidence is present.

The inspected SQLite sales table contained 64,149 source rows and 62,941 distinct same-order records. After applying the documented export-duplicate rule, all 33,630 order goods totals matched their cleaned line sums. Its mixed order-date representations agreed with ISO creation dates over the complete order table. These are development checks, not runtime constants or answer branches.

Run `make test` for the current scripted suite plus preparation fixtures. `make test-sandbox` also runs the existing isolated-worker checks. Preparation fixtures cover layouts, summaries, malformed rows, dates, money, grains, duplicates, joins, caching, schema privacy and source preservation. Real-data preparation reports describe the actual checked dataset and remaining issues; they do not establish model accuracy or final evaluation-profile compliance.
