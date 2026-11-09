"""Full-scan preparation with private provenance and fail-closed publication."""
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re
import resource
import shutil
import tempfile
import time
from typing import Any

from .contracts import (VERSION, AgentSchema, BusinessProfile, ColumnProfile, Issue,
                        PreparationConfig, PreparationSpec, PreparedBusiness, TableProfile)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def _write(path, value):
    path.write_text(_json(value) + '\n', encoding='utf-8')


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _q(value):
    return '"' + value.replace('"', '""') + '"'


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def _inputs(business, rulebook, calendar):
    business, rulebook, calendar = map(lambda p: Path(p).absolute(), (business, rulebook, calendar))
    if business.is_symlink() or not business.is_dir():
        raise ValueError('Business must be a real directory')
    if any(p.is_symlink() for p in business.rglob('*')):
        raise ValueError('Symlinked input is not supported')
    for p in (rulebook, calendar):
        if p.is_symlink() or not p.is_file():
            raise ValueError('Rulebook and calendar must be nonsymlink files')
    readme_path = business / 'README.md'
    readme = readme_path.read_text(encoding='utf-8') if readme_path.exists() else ''
    return business.resolve(), rulebook.resolve(), calendar.resolve(), readme


def _identity(business, rulebook, calendar, config, spec):
    from .readers import discover_sources
    sources = discover_sources(business)
    files = {s.path: s.source_hash for s in sources}
    files['README.md'] = _hash(business / 'README.md') if (business / 'README.md').exists() else None
    code = {p.name: _hash(p) for p in Path(__file__).parent.glob('*.py')}
    from importlib.metadata import version
    dependencies={name:version(name) for name in ('duckdb','pandera','pandas','openpyxl','pydantic')}
    evidence = {'files': files, 'rulebook': _hash(rulebook), 'calendar': _hash(calendar),
                'code': code, 'dependencies':dependencies,'config': config.model_dump(), 'requested_spec': spec.model_dump()}
    fingerprint = hashlib.sha256(_json(evidence).encode()).hexdigest()
    return sources, evidence, fingerprint


def _names(labels):
    from .parsers import normalize_name
    counts = Counter()
    names = []
    for i, label in enumerate(labels):
        base = normalize_name(label) or f'column_{i + 1}'
        if base.startswith('_'):
            base = 'field' + base
        counts[base] += 1
        names.append(base if counts[base] == 1 else f'{base}_{counts[base]}')
    # A source may itself contain labels ending in _2.
    used = set()
    for i, name in enumerate(names):
        candidate = name
        while candidate in used:
            candidate += '_field'
        names[i] = candidate
        used.add(candidate)
    return names


def _insert(connection, table, columns, batch):
    if not batch:
        return
    import pandas as pd
    frame = pd.DataFrame(batch, columns=columns, dtype=object)
    connection.register('_preparation_batch', frame)
    try:
        connection.execute(f'INSERT INTO {table} SELECT * FROM _preparation_batch')
    finally:
        connection.unregister('_preparation_batch')
    batch.clear()


def _corroborated_epoch(connection, raw, labels, names, date_index):
    """Serial dates outside XLSX need agreement with a real dated field."""
    from .parsers import normalize_name
    possible = [i for i, label in enumerate(labels) if i != date_index and
                any(x in normalize_name(label).replace('_', ' ') for x in ('thoi gian', 'gio tao', 'created', 'timestamp'))]
    from openpyxl.utils.datetime import from_excel
    for other in possible:
        compared = 0
        failed = False
        query = f'SELECT {_q(names[date_index])}, {_q(names[other])} FROM {raw}'
        cursor = connection.execute(query)
        while rows := cursor.fetchmany(4096):
            for value, stamp in rows:
                if value and re.fullmatch(r'\d{5}(?:\.\d+)?', value.strip()):
                    try:
                        proposed = from_excel(float(value)).date()
                        actual = datetime.fromisoformat(stamp).date()
                    except (ValueError, TypeError, OverflowError, AttributeError):
                        failed = True
                        break
                    compared += 1
                    if proposed != actual:
                        failed = True
                        break
            if failed:
                break
        if compared and not failed:
            return '1900', {'kind': 'cross_field_date_agreement', 'column': names[other],
                            'serial_values_checked': compared, 'mismatches': 0}
    return None, None


def _scan_table(connection, table, readme, rulebook, config, spec, profile, deadline):
    from .parsers import infer_parser, parse_value, representation, text_key
    from .semantics import propose_table, column_description
    source = asdict(table.source)
    names = _names(table.labels)
    raw_name = 'r_' + hashlib.sha256((table.source.source_id + table.region_id).encode()).hexdigest()[:20]
    raw = 'private.' + _q(raw_name)
    columns = ['_record_id', '_source_id', '_row', '_raw_fingerprint', *names]
    connection.execute(f'CREATE TABLE {raw} (_record_id VARCHAR, _source_id VARCHAR, _row BIGINT, '
                       f'_raw_fingerprint VARCHAR{"," if names else ""}' +
                       ','.join(f'{_q(n)} VARCHAR' for n in names) + ')')
    accounting = Counter()
    types = [Counter() for _ in names]
    batch, audit = [], []
    records = 0
    for row in table.rows:
        if time.monotonic() > deadline:
            raise TimeoutError('Preparation deadline exceeded')
        kind = row.classification
        accounting[kind] += 1
        values = list(row.values)
        if kind == 'record' and any(v not in (None, '') for v in values[len(names):]):
            accounting['record'] -= 1
            accounting['unresolved'] += 1
            kind = 'unresolved'
        if kind != 'record':
            audit.append((table.source.source_id, table.region_id, row.number, kind,
                          row.reason or kind, _json(values)))
        else:
            values = (values + [None] * len(names))[:len(names)]
            for i, value in enumerate(values):
                types[i][type(value).__name__] += 1
            identity = hashlib.sha256(f'{table.source.source_id}:{table.region_id}:{row.number}'.encode()).hexdigest()
            signature = hashlib.sha256(_json(values).encode()).hexdigest()
            batch.append((identity, table.source.source_id, row.number, signature,
                          *[str(v) if v is not None else None for v in values]))
            records += 1
        if len(batch) >= config.batch_size:
            _insert(connection, raw, columns, batch)
        if len(audit) >= config.batch_size:
            _insert(connection, 'private.row_audit', ['source_id', 'region_id', 'row_number',
                    'classification', 'reason', 'values_json'], audit)
    _insert(connection, raw, columns, batch)
    _insert(connection, 'private.row_audit', ['source_id', 'region_id', 'row_number',
            'classification', 'reason', 'values_json'], audit)
    proposal = propose_table(table.labels, source_name=(table.source.display_alias or table.source.path)+'::'+(table.source.table or table.source.sheet or ''),
                             readme=readme, rulebook=rulebook)
    overrides = spec.tables.get(table.region_id, {})
    if 'roles' in overrides:
        if not all(isinstance(i, int) and 0 <= i < len(names) for i in overrides['roles'].values()):
            raise ValueError('Semantic role overrides must reference real column positions')
        proposal['roles'] = overrides['roles']
    for key in ('grain', 'duplicate_policy', 'key_roles'):
        if key in overrides:
            proposal[key] = overrides[key]
    roles = {role: names[i] for role, i in sorted(proposal.get('roles', {}).items(),key=lambda item:(item[1],item[0])) if isinstance(i, int) and 0 <= i < len(names)}
    base = table.source.table or Path(table.source.display_alias or table.source.path).stem
    base = re.sub(r'(?:[_ -](?:\d{1,4}))+\Z', '', base)
    result = TableProfile(source_id=table.source.source_id, region_id=table.region_id,
                          source=source, name=_names([base])[0], labels=table.labels,
                          header_rows=table.header_rows, extraction=table.metadata,
                          accounting=dict(accounting), records=records,
                          grain=proposal.get('grain', 'unknown'), roles=roles,
                          candidate_keys=[roles[k] for k in proposal.get('key_roles', []) if k in roles],
                          duplicate_policy=proposal.get('duplicate_policy', 'preserve'),
                          evidence=proposal.get('evidence', []))
    if accounting['unresolved']:
        profile.issues.append(Issue(code='UNRESOLVED_ROWS', severity='blocked', source_id=result.source_id,
                                    message='Record-like rows are unresolved; this region cannot be published complete.',
                                    count=accounting['unresolved']))
    # The proposal is persisted separately from the factual profile.
    spec.tables.setdefault(table.region_id, {}).update({
        'roles': proposal.get('roles', {}), 'grain': result.grain,
        'duplicate_policy': result.duplicate_policy, 'key_roles': proposal.get('key_roles', [])})
    parser_overrides = overrides.get('parsers', {})
    for i, (label, name) in enumerate(zip(table.labels, names)):
        selected = connection.execute(f'SELECT {_q(name)} FROM {raw} WHERE {_q(name)} IS NOT NULL '
                                      f'AND trim({_q(name)})<>\'\' LIMIT ?', [config.sample_rows]).fetchall()
        samples = [r[0] for r in selected]
        description = column_description(label, source_name=(table.source.display_alias or table.source.path)+'::'+(table.source.table or table.source.sheet or ''), readme=readme)
        role = next((r for r, column in roles.items() if column == name), None)
        epoch = table.source.epoch
        epoch_evidence = None
        if epoch is None and (role and ('date' in role or role in ('created_at', 'expiry_date')) or
                              any(x in text_key(label) for x in ('ngay', 'date', 'thoi gian'))):
            epoch, epoch_evidence = _corroborated_epoch(connection, raw, table.labels, names, i)
        year_match = re.findall(r'(?:19|20)\d{2}', table.source.sheet or '')
        year = int(year_match[0]) if len(set(year_match)) == 1 and 'nam cua sheet' in text_key(rulebook) else None
        if role and any(k in role for k in ('amount', 'price', 'cost', 'total')):
            description += ' Monetary value in VND; tiền bằng đồng.'
        parser = parser_overrides.get(name) or infer_parser(label, samples, epoch=epoch, year=year, description=description)
        if role and (role.endswith('_id') or role == 'line_number'):
            parser = {'kind': 'identifier', 'status': 'validated', 'evidence': ['entity identifier role']}
        if epoch_evidence:
            parser.setdefault('corroboration', []).append(epoch_evidence)
        column = ColumnProfile(name=name, label=label, position=i, description=description, role=role,
                               parser=parser, source_types=dict(types[i]))
        total, missing, blank, distinct = connection.execute(
            f'SELECT count(*), count(*) FILTER(WHERE {_q(name)} IS NULL), '
            f'count(*) FILTER(WHERE {_q(name)} IS NOT NULL AND trim({_q(name)})=\'\'), '
            f'count(DISTINCT {_q(name)}) FILTER(WHERE {_q(name)} IS NOT NULL AND trim({_q(name)})<>\'\') FROM {raw}').fetchone()
        column.missing, column.blank, column.distinct = missing, blank, distinct
        column.populated = total - missing - blank
        column.all_empty = column.populated == 0
        column.constant = distinct == 1
        column.unique = distinct == total and bool(total)
        column.frequent_values = [{'value': value[:160], 'count': count} for value, count in
            connection.execute(f'SELECT {_q(name)}, count(*) n FROM {raw} WHERE {_q(name)} IS NOT NULL '
                               f'AND trim({_q(name)})<>\'\' GROUP BY 1 ORDER BY n DESC, 1 LIMIT 12').fetchall()]
        result.columns.append(column)
    typed_name = raw_name + '_typed'
    typed = 'private.' + _q(typed_name)
    connection.execute(f'CREATE TABLE {typed} AS SELECT * FROM {raw} WHERE FALSE')
    cursor = connection.cursor()
    cursor.execute(f'SELECT * FROM {raw} ORDER BY _row')
    typed_batch = []
    while rows := cursor.fetchmany(config.batch_size):
        if time.monotonic() > deadline:
            raise TimeoutError('Preparation deadline exceeded')
        for row in rows:
            parsed_row = list(row[:4])
            for col, value in zip(result.columns, row[4:]):
                form = representation(value)
                col.representations[form] = col.representations.get(form, 0) + 1
                try:
                    parsed = parse_value(value, col.parser)
                    if isinstance(parsed,datetime):
                        if parsed.utcoffset() is None:col.naive_timestamps+=1
                        else:col.aware_timestamps+=1
                    if isinstance(parsed, Decimal):
                        with localcontext() as ctx:
                            ctx.prec = 80
                            places = max(0, -parsed.as_tuple().exponent)
                            digits = max(1, parsed.adjusted() + 1)
                            col.scale = max(col.scale, places)
                            col.precision = max(col.precision, digits + places)
                            if digits + places > 38:
                                raise ValueError('Decimal exceeds DuckDB precision without loss')
                        col.negative += parsed < 0
                        col.zero += parsed == 0
                    if parsed is not None and not isinstance(parsed, bool) and not isinstance(parsed, str):
                        as_text = str(parsed)
                        if col.parser['kind'] in ('date', 'datetime'):
                            col.minimum = min(col.minimum or as_text, as_text)
                            col.maximum = max(col.maximum or as_text, as_text)
                        elif isinstance(parsed, Decimal):
                            col.minimum = str(min(Decimal(col.minimum) if col.minimum else parsed, parsed))
                            col.maximum = str(max(Decimal(col.maximum) if col.maximum else parsed, parsed))
                    parsed_row.append(str(parsed) if parsed is not None else None)
                except (ValueError, TypeError, OverflowError) as error:
                    col.parse_failures += 1
                    if len(col.failure_examples) < 5:
                        col.failure_examples.append({'row': row[2], 'value': str(value)[:160], 'reason': str(error)[:240]})
                    parsed_row.append(None)
            typed_batch.append(tuple(parsed_row))
        _insert(connection, typed, columns, typed_batch)
    cursor.close()
    for col in result.columns:
        col.usable = bool(col.populated) and col.parse_failures == 0 and col.parser.get('status') != 'unresolved'
        if col.usable:
            col.parser['status'] = 'validated'
        kind = col.parser['kind']
        col.sql_type = {'date': 'DATE', 'datetime': 'TIMESTAMP', 'boolean': 'BOOLEAN'}.get(kind, 'VARCHAR')
        if kind in ('decimal', 'quantity'):
            # Combined scale and integer width must fit, even when maxima came from different rows.
            whole = max(1, Decimal(col.maximum).adjusted() + 1 if col.maximum else 1,
                        Decimal(col.minimum).adjusted() + 1 if col.minimum else 1)
            if whole + col.scale > 38:
                col.usable = False
                profile.issues.append(Issue(code='DECIMAL_OVERFLOW', severity='blocked', relation=result.name,
                                            column=col.name, message='Combined decimal range/scale exceeds 38 digits.'))
            col.sql_type = f'DECIMAL(38,{min(col.scale, 37)})'
        if col.aware_timestamps:
            col.sql_type='TIMESTAMPTZ'
            if col.naive_timestamps:
                col.usable=False
                profile.issues.append(Issue(code='MIXED_TIMESTAMP_ZONES',severity='blocked',relation=result.name,column=col.name,
                                            message='Mixed zoned and unzoned timestamps need an explicit source timezone policy.'))
        if col.parse_failures:
            profile.issues.append(Issue(code='PARSE_FAILURE', severity='blocked', source_id=result.source_id,
                                        relation=result.name, column=col.name, count=col.parse_failures,
                                        message='Field is excluded from queryable relations; failures remain in private audit.'))
        elif col.all_empty:
            profile.issues.append(Issue(code='EMPTY_FIELD', severity='info', source_id=result.source_id,
                                        relation=result.name, column=col.name, message='Source records no values for this field.'))
    layout_blocked = bool(set(table.metadata.get('layout_issues',[])) & {'weak_header_evidence','unresolved_header'})
    if layout_blocked:
        profile.issues.append(Issue(code='LAYOUT_AMBIGUOUS',severity='blocked',source_id=result.source_id,
                                    message='Header evidence is unresolved; provide a validated layout specification.'))
    result.model_assisted = bool(overrides.get('model_evidence'))
    result.status = 'blocked' if accounting['unresolved'] or layout_blocked or not any(c.usable for c in result.columns) else (
        'partial' if any(c.parse_failures for c in result.columns) else 'ready')
    spec.tables[table.region_id]['parsers'] = {c.name: c.parser for c in result.columns}
    return result, typed


def _publish_groups(connection, profile):
    """Assemble compatible partitions, then deduplicate with original fingerprints."""
    groups = {}
    blocked_families={t.name for t in profile.tables if t.status=='blocked'}
    from .parsers import normalize_name
    for issue in profile.issues:
        if issue.code not in ('SOURCE_SCAN_FAILED','EMPTY_SOURCE'):continue
        source=next((s for s in profile.inventory if s['source_id']==issue.source_id),None)
        if source:
            label=source.get('table') or Path(source.get('display_alias') or source['path']).stem
            blocked_families.add(normalize_name(re.sub(r'(?:[_ -](?:\d{1,4}))+\Z','',label)))
    for table in profile.tables:
        if table.status == 'blocked' or table.name in blocked_families:
            if table.status!='blocked':
                profile.issues.append(Issue(code='INCOMPLETE_PARTITIONS',severity='blocked',source_id=table.source_id,
                                            relation=table.name,message='A related source partition is blocked; complete union is withheld.'))
            continue
        signature = (table.name, table.grain, tuple(c.name for c in table.columns),
                     tuple(sorted(table.roles.items())), table.duplicate_policy)
        groups.setdefault(signature, []).append(table)
    relations = []
    used_names = set()
    for tables in groups.values():
        first = tables[0]
        name = first.name
        if name in used_names:
            name += '_' + hashlib.sha256(first.region_id.encode()).hexdigest()[:8]
        used_names.add(name)
        good, blocked, absent = [], [], []
        for position, original in enumerate(first.columns):
            parts = [t.columns[position] for t in tables]
            populated = [c for c in parts if not c.all_empty]
            if not populated:
                absent.append(original.name)
                continue
            kinds = {c.parser['kind'] for c in populated}
            if any(not c.usable for c in populated) or len(kinds)>1:
                blocked.append(original.name)
                profile.issues.append(Issue(code='PARTITION_FIELD_BLOCKED',severity='blocked',relation=name,column=original.name,
                                            message='A partition has unresolved values/types; the field is withheld from every partition.'))
                continue
            combined = populated[0].model_copy(deep=True)
            combined.missing=sum(c.missing for c in parts)
            combined.blank=sum(c.blank for c in parts)
            if kinds <= {'decimal','quantity'}:
                scale=max(c.scale for c in populated)
                whole=max(max(1,Decimal(v).adjusted()+1) for c in populated for v in (c.minimum,c.maximum) if v is not None)
                if whole+scale>38:
                    blocked.append(original.name)
                    profile.issues.append(Issue(code='PARTITION_DECIMAL_OVERFLOW',severity='blocked',relation=name,column=original.name,
                                                message='Combined partitions exceed exact decimal precision.'))
                    continue
                combined.sql_type=f'DECIMAL(38,{scale})'
            good.append(combined)
        if not good:
            continue
        raw_names = ['private.' + _q('r_' + hashlib.sha256((t.source_id + t.region_id).encode()).hexdigest()[:20] + '_typed') for t in tables]
        union = ' UNION ALL '.join(f'SELECT * FROM {t}' for t in raw_names)
        stage = 'private.' + _q('g_' + name)
        selected = ','.join(f'CAST({_q(c.name)} AS {c.sql_type}) AS {_q(c.name)}' for c in good)
        connection.execute(f'CREATE TABLE {stage} AS SELECT _record_id,_source_id,_row,_raw_fingerprint,{selected} FROM ({union})')
        order = first.roles.get('order_id')
        dedup = first.duplicate_policy == 'same_order_exact' and order in {c.name for c in good}
        if dedup:
            canonical = (f'CASE WHEN { _q(order)} IS NOT NULL AND trim(CAST({_q(order)} AS VARCHAR))<>\'\' '
                         'THEN first_value(_record_id) OVER(PARTITION BY _raw_fingerprint ORDER BY _record_id) '
                         'ELSE _record_id END')
        else:
            canonical = '_record_id'
        connection.execute(f'INSERT INTO private.lineage SELECT ?, {canonical}, _record_id,_source_id,_row FROM {stage}', [name])
        # Provenance is privately retained; clean relations expose no audit samples or technical pseudo-IDs.
        data = 'private.' + _q('data_' + name)
        connection.execute(f'CREATE TABLE {data} AS SELECT s.* FROM {stage} s JOIN '
                           '(SELECT DISTINCT canonical_record_id FROM private.lineage WHERE relation_name=?) l '
                           'ON s._record_id=l.canonical_record_id', [name])
        columns = ','.join(_q(c.name) for c in good)
        connection.execute(f'CREATE VIEW clean.{_q(name)} AS SELECT {columns} FROM {data}')
        before = connection.execute(f'SELECT count(*) FROM {stage}').fetchone()[0]
        after = connection.execute(f'SELECT count(*) FROM {data}').fetchone()[0]
        for table in tables:
            table.duplicates_removed = before - after if table is first else 0
        keys = [k for k in first.candidate_keys if k in {c.name for c in good}]
        verified_keys = []
        if keys:
            fields = ','.join(_q(k) for k in keys)
            unique = connection.execute(f'SELECT count(*) FROM (SELECT {fields} FROM {data} GROUP BY {fields})').fetchone()[0] == after
            nulls = connection.execute(f'SELECT count(*) FROM {data} WHERE ' + ' OR '.join(f'{_q(k)} IS NULL OR trim(CAST({_q(k)} AS VARCHAR))=\'\'' for k in keys)).fetchone()[0]
            if unique and not nulls:
                verified_keys = keys
            else:
                profile.issues.append(Issue(code='KEY_CONFLICT', severity='warning', relation=name,
                                            message='Candidate key is not unique/nonmissing after allowed deduplication.'))
        relations.append({'name': 'clean.' + name, 'grain': first.grain, 'rows': after,
                          'keys': verified_keys, 'roles': {r: c for r,c in first.roles.items() if c in {x.name for x in good}},
                          'evidence': first.evidence,
                          'columns': [{'name': c.name, 'type': c.sql_type, 'meaning': c.description or c.label,
                                       'source_alias': c.label, 'role': c.role, 'nullable': bool(c.missing+c.blank),
                                       'missing_count':c.missing,'blank_count':c.blank,
                                       'null_meaning':'Missing/blank source value; never an inferred zero.'} for c in good],
                          'source_regions': [t.region_id for t in tables],
                          'blocked_columns': blocked,
                          'absent_columns': absent})
    return relations


def _relation_sql(relation):
    return '.'.join(_q(part) for part in relation['name'].split('.'))


def _relations_and_semantics(connection, profile, relations, readme, rulebook):
    """Publish only semantic operations whose complete dependencies were checked."""
    from .semantics import classify_status, return_evidence_policy, sales_eligibility_policy, _words
    from .parsers import text_key

    def issue(code, message, relation=None, count=1):
        profile.issues.append(Issue(code=code, severity='blocked', relation=relation,
                                    count=count, message=message))

    def linked(child, parent, complete=True):
        return any(r['child'] == child['name'] and r['parent'] == parent['name']
                   and r['validated'] and (not complete or r.get('null_child_keys', 0) == 0)
                   for r in profile.relationships)

    # Verified canonical parents are the only source of advertised foreign keys.
    for child in relations:
        for role, expected in {'order_id': 'order', 'product_id': 'product',
                               'customer_id': 'customer', 'receipt_id': 'receipt', 'lot_id': 'lot'}.items():
            key = child['roles'].get(role)
            parents = [r for r in relations if r is not child and r['grain'] == expected
                       and r['keys'] == [r['roles'].get(role)]]
            if key is None or len(parents) != 1:
                continue
            parent = parents[0]
            pk = parent['roles'][role]
            missing, orphans = connection.execute(
                f'SELECT count(*) FILTER(WHERE c.{_q(key)} IS NULL OR trim(CAST(c.{_q(key)} AS VARCHAR))=\'\'), '
                f'count(*) FILTER(WHERE c.{_q(key)} IS NOT NULL AND trim(CAST(c.{_q(key)} AS VARCHAR))<>\'\' '
                f'AND p.{_q(pk)} IS NULL) FROM {_relation_sql(child)} c LEFT JOIN {_relation_sql(parent)} p '
                f'ON c.{_q(key)}=p.{_q(pk)}').fetchone()
            profile.relationships.append({'child': child['name'], 'child_column': key,
                'parent': parent['name'], 'parent_column': pk, 'cardinality': 'many_to_one',
                'null_child_keys': missing, 'unmatched_nonnull_keys': orphans,
                'validated': orphans == 0, 'join_multiplier': 1})
            if orphans:
                issue('ORPHAN_KEYS', 'Unmatched nonnull keys prevent a complete relationship.', child['name'], orphans)

    orders = [r for r in relations if r['grain'] == 'order' and r['keys'] == [r['roles'].get('order_id')]]
    items = [r for r in relations if r['grain'] == 'order_item' and 'order_id' in r['roles']]
    for order in orders:
        if 'order_goods_amount' not in order['roles']:
            continue
        for item in items:
            if not linked(item, order) or 'line_amount' not in item['roles']:
                continue
            o, i = order['roles'], item['roles']
            n, wrong, missing = connection.execute(
                f'SELECT count(*), count(*) FILTER(WHERE o.{_q(o["order_goods_amount"])} IS NULL '
                f'OR x.total IS NULL OR o.{_q(o["order_goods_amount"])}<>x.total), '
                f'count(*) FILTER(WHERE x.id IS NULL) FROM {_relation_sql(order)} o LEFT JOIN '
                f'(SELECT {_q(i["order_id"])} id,SUM({_q(i["line_amount"])}) total '
                f'FROM {_relation_sql(item)} GROUP BY 1) x ON o.{_q(o["order_id"])}=x.id').fetchone()
            profile.reconciliations.append({'kind': 'order_goods_vs_lines', 'orders': order['name'],
                'items': item['name'], 'compared': n, 'mismatches': wrong,
                'missing_line_orders': missing, 'validated': bool(n) and wrong == 0})

    semantic = []

    def publish(name, grain, sql, sources, meaning, evidence=None, keys=None):
        connection.execute('CREATE VIEW ' + name + ' AS ' + sql)
        descriptions = {}
        for source in sources:
            by_name = {c['name']: c for c in source['columns']}
            for role, column in source.get('roles', {}).items():
                descriptions.setdefault(role, by_name.get(column, {}))
        columns = []
        for col, kind, nullable, *_ in connection.execute('DESCRIBE ' + name).fetchall():
            origin = descriptions.get(col, {})
            field = {'name': col, 'type': kind, 'meaning': origin.get('meaning', col.replace('_', ' ')),
                     'nullable': nullable == 'YES', 'role': col}
            if origin.get('source_alias'):
                field['source_alias'] = origin['source_alias']
            if col == 'base_quantity':
                field['meaning'] = 'Quantity in the verified product base unit; not a count of orders or rows.'
                field['unit'] = 'product base unit'
            columns.append(field)
        relation = {'name': name, 'grain': grain, 'meaning': meaning, 'columns': columns,
                    'keys': keys or [], 'roles': {c['name']: c['name'] for c in columns},
                    'source_relations': [s['name'] for s in sources]}
        if evidence:
            relation['evidence'] = evidence
        semantic.append(relation)
        return relation

    policy = sales_eligibility_policy(rulebook)
    policy_ok = all(policy.get(key) is True for key in
                    ('exclude_cancelled', 'exclude_returned', 'whole_order_returns'))
    if not policy_ok:
        return semantic
    primary = orders[0] if len(orders) == 1 else None
    bases = [primary] if primary else items if not orders else []
    if len(bases) != 1:
        if orders or items:
            issue('ORDER_SYSTEM_AMBIGUOUS', 'No unique validated order system can establish whole-order eligibility.')
        return semantic
    base = bases[0]
    roles = base['roles']
    oid, status = roles.get('order_id'), roles.get('status')
    if not oid or not status:
        return semantic
    bq = _relation_sql(base)
    missing_ids = connection.execute(f'SELECT count(*) FROM {bq} WHERE {_q(oid)} IS NULL '
                                    f'OR trim(CAST({_q(oid)} AS VARCHAR))=\'\'').fetchone()[0]
    if missing_ids:
        issue('MISSING_ORDER_ID', 'Missing order identifiers prevent complete whole-order filtering.', base['name'], missing_ids)
        return semantic
    values = [r[0] for r in connection.execute(f'SELECT DISTINCT {_q(status)} FROM {bq}').fetchall()]
    cases = []
    for value in values:
        if value is None or not str(value).strip():
            continue
        state = classify_status(value)
        if text_key(str(value)) in {'shipping', 'in delivery', 'dang giao', 'dang giao hang'} and policy.get('include_shipping') is not True:
            state = 'unknown'
        cases.append(f'WHEN {_q(status)}={_lit(value)} THEN {_lit(state)}')
    expression = 'CASE ' + ' '.join(cases) + " ELSE 'missing' END" if cases else "'missing'"
    # Unknown nonempty labels remain failures. Only explicitly documented grouped
    # blanks may inherit an established status of the same real order.
    inheritance = 'o de trong o cac dong sau cua cung mot nhom dong' in _words(rulebook)
    fragments = [f'SELECT {_q(oid)} order_id,{expression} state FROM {bq}']
    return_policy = return_evidence_policy(readme)
    returns_blocked = False
    for table in profile.tables:
        if table.grain == 'return' and table.status == 'blocked':
            returns_blocked = True
        for role in ('return_quantity', 'quantity'):
            required = return_policy['positive_return_quantity_is_return'] if role == 'return_quantity' else return_policy['negative_quantity_is_return']
            column = table.roles.get(role)
            if required and column and any(c.name == column and not c.usable and not c.all_empty for c in table.columns):
                returns_blocked = True
    for relation in relations:
        rr = relation['roles']
        if 'order_id' not in rr:
            continue
        condition = None
        if relation['grain'] == 'return':
            condition = 'TRUE'
        elif 'return_quantity' in rr and return_policy['positive_return_quantity_is_return']:
            condition = f'{_q(rr["return_quantity"])}>0'
        elif 'quantity' in rr and return_policy['negative_quantity_is_return'] and relation['grain'] == 'order_item':
            condition = f'{_q(rr["quantity"])}<0'
        if condition is None:
            continue
        if relation is not base:
            if primary and not linked(relation, primary):
                returns_blocked = True
                issue('RETURN_LINK_UNRESOLVED', 'Return evidence is not completely linked to the canonical orders.', relation['name'])
                continue
            if not primary:
                # A single line-based system has a derived order namespace.
                missing, unmatched = connection.execute(
                    f'SELECT count(*) FILTER(WHERE r.{_q(rr["order_id"])} IS NULL), '
                    f'count(*) FILTER(WHERE r.{_q(rr["order_id"])} IS NOT NULL AND b.id IS NULL) '
                    f'FROM {_relation_sql(relation)} r LEFT JOIN (SELECT DISTINCT {_q(oid)} id FROM {bq}) b '
                    f'ON r.{_q(rr["order_id"])}=b.id WHERE {condition}').fetchone()
                if missing or unmatched:
                    returns_blocked = True
                    issue('RETURN_LINK_UNRESOLVED', 'Return evidence contains unknown order identities.', relation['name'])
                    continue
        fragments.append(f'SELECT {_q(rr["order_id"])} order_id,\'returned\' state '
                         f'FROM {_relation_sql(relation)} WHERE {condition}')
    if returns_blocked:
        issue('RETURN_EVIDENCE_BLOCKED', 'Incomplete return evidence prevents eligible sales publication.')
        return semantic
    unknown_sql = "bool_or(state='unknown') OR (bool_or(state='missing') AND NOT bool_or(state IN ('active','cancelled','returned')))" if inheritance else "bool_or(state IN ('unknown','missing'))"
    connection.execute('CREATE TABLE private.order_flags AS SELECT order_id, '
        "bool_or(state='cancelled') cancelled,bool_or(state='returned') returned," + unknown_sql +
        ' unknown FROM (' + ' UNION ALL '.join(fragments) + ') GROUP BY order_id')
    unknown = connection.execute('SELECT count(*) FROM private.order_flags WHERE unknown').fetchone()[0]
    if unknown:
        issue('UNKNOWN_ORDER_STATUS', 'Eligible sales are blocked by unresolved order status meanings.', base['name'], unknown)
        return semantic
    if primary:
        selected = ','.join(f'b.{_q(column)} AS {_q(role)}' for role, column in roles.items())
        sql = f'SELECT {selected}, f.cancelled, f.returned FROM {bq} b JOIN private.order_flags f ON b.{_q(oid)}=f.order_id'
    else:
        when = roles.get('order_date')
        if not when:
            issue('ORDER_GRAIN_UNRESOLVED', 'Line-based orders have no verified order date.', base['name'])
            return semantic
        missing_test = 'count(' + _q(when) + ')=0' if inheritance else 'count(' + _q(when) + ')<>count(*)'
        conflicts = connection.execute(f'SELECT count(*) FROM (SELECT {_q(oid)} FROM {bq} GROUP BY 1 '
            f'HAVING count(DISTINCT {_q(when)})<>1 OR {missing_test})').fetchone()[0]
        if conflicts:
            issue('ORDER_GRAIN_UNRESOLVED', 'Order dates are missing or do not functionally depend on order IDs.', base['name'], conflicts)
            return semantic
        sql = (f'SELECT b.{_q(oid)} order_id, MIN(b.{_q(when)}) order_date, '
               f'bool_or(f.cancelled) cancelled,bool_or(f.returned) returned FROM {bq} b '
               f'JOIN private.order_flags f ON b.{_q(oid)}=f.order_id GROUP BY 1')
    order_view = publish('semantic.orders', 'order', sql, [base],
                        'Verified order facts across the complete timeline.', keys=['order_id'])
    eligible = publish('semantic.eligible_orders', 'order',
        'SELECT * FROM semantic.orders WHERE NOT cancelled AND NOT returned', [order_view],
        'Ordinary sales orders under the supplied rulebook; explicit questions may require all orders.', policy, ['order_id'])
    candidates = [r for r in items if linked(r, primary)] if primary else [base]
    if len(candidates) == 1:
        item = candidates[0]
        selected = ','.join(f'i.{_q(column)} AS {_q(role)}' for role, column in item['roles'].items())
        sales = publish('semantic.eligible_sales_lines', 'order_item',
            f'SELECT {selected} FROM {_relation_sql(item)} i JOIN semantic.eligible_orders o '
            f'ON i.{_q(item["roles"]["order_id"])}=o.order_id', [item],
            'Recorded sales fields with entire cancelled/returned orders excluded.', policy)
        _publish_product_quantities(connection, profile, relations, semantic, sales, readme, rulebook, publish)
    return semantic


def _publish_product_quantities(connection, profile, relations, semantic, sales, readme, rulebook, publish):
    """Use recorded base quantities or fully checked explicit unit equations."""
    from .semantics import readme_unit_conversions, _words
    from .parsers import text_key
    roles = sales['roles']
    if not ({'product_id', 'product_name'} & roles.keys()):
        return
    if 'base_quantity' in roles:
        missing = connection.execute('SELECT count(*) FROM semantic.eligible_sales_lines WHERE base_quantity IS NULL').fetchone()[0]
        if missing:
            profile.issues.append(Issue(code='BASE_QUANTITY_MISSING', severity='blocked',
                relation='semantic.product_quantities', count=missing,
                message='Eligible sales contain missing base quantities; aggregation would omit unknown values.'))
            return
        publish('semantic.product_quantities', 'order_item', 'SELECT * FROM semantic.eligible_sales_lines', [sales],
                'Eligible sales with recorded, validated product base quantities.')
        return
    if not {'quantity', 'unit'} <= roles.keys():
        return
    identity = 'product_id' if 'product_id' in roles else 'product_name'
    conversions = [r for r in relations if r['grain'] == 'unit_conversion' and
        {identity, 'unit', 'conversion_factor'} <= r['roles'].keys() and
        set(r['keys']) == {r['roles'][identity], r['roles']['unit']}]
    equations = readme_unit_conversions(readme)
    if equations['issues']:
        profile.issues.append(Issue(code='CONVERSION_RULE_CONFLICT', severity='blocked',
            relation='semantic.product_quantities',
            message='Explicit README conversions contain invalid or conflicting equations.'))
        return
    if len(conversions) > 1:
        return
    select = None
    sources = [sales]
    evidence = {'kind': 'validated_unit_equations'}
    if conversions:
        conversion = conversions[0]
        cr = conversion['roles']
        bad = connection.execute(f'SELECT count(*) FROM {_relation_sql(conversion)} '
            f'WHERE {_q(cr["conversion_factor"])} IS NULL OR {_q(cr["conversion_factor"])}<=0').fetchone()[0]
        if bad:
            return
        target = f'CAST({_q(cr["base_unit"])} AS VARCHAR)' if 'base_unit' in cr else 'NULL::VARCHAR'
        select = (f'SELECT CAST({_q(cr[identity])} AS VARCHAR) product_key, '
                  f'CAST({_q(cr["unit"])} AS VARCHAR) selling_unit, {_q(cr["conversion_factor"])} factor, '
                  f'{target} base_unit FROM {_relation_sql(conversion)}')
        sources.append(conversion)
    elif equations['status'] == 'resolved' and identity == 'product_name':
        data = equations['conversions']
        scale = max(max(0, -Decimal(r['factor']).as_tuple().exponent) for r in data)
        whole = max(max(1, Decimal(r['factor']).adjusted() + 1) for r in data)
        if scale + whole > 38:
            return
        connection.execute(f'CREATE TABLE private.unit_equations(product_key VARCHAR,selling_unit VARCHAR,factor DECIMAL(38,{scale}),base_unit VARCHAR)')
        connection.executemany('INSERT INTO private.unit_equations VALUES (?,?,?,?)',
            [(r['product_name'], r['from_unit'], Decimal(r['factor']), r['base_unit']) for r in data])
        select = 'SELECT * FROM private.unit_equations'
        evidence = {'kind': 'explicit_readme_unit_equations', 'equation_count': len(data)}
    if select is None:
        return
    duplicates = connection.execute('SELECT count(*) FROM (SELECT lower(trim(product_key)),lower(trim(selling_unit)) '
        f'FROM ({select}) GROUP BY 1,2 HAVING count(*)>1)').fetchone()[0]
    if duplicates:
        profile.issues.append(Issue(code='CONVERSION_KEY_CONFLICT', severity='blocked',
            relation='semantic.product_quantities', count=duplicates,
            message='Conversion keys conflict after safe whitespace/case normalization.'))
        return
    # Canonical product base units can corroborate factor-one rows.
    catalogues = [r for r in relations if r['grain'] == 'product' and
                  {identity, 'base_unit'} <= r['roles'].keys() and r['keys'] == [r['roles'][identity]]]
    catalogue = catalogues[0] if len(catalogues) == 1 else None
    product_match = (f'lower(trim(CAST(s.{_q(identity)} AS VARCHAR)))=lower(trim(c.product_key))'
                     if identity == 'product_name' else f'CAST(s.{_q(identity)} AS VARCHAR)=c.product_key')
    join = (f'LEFT JOIN ({select}) c ON {product_match} '
            f'AND lower(trim(s.unit))=lower(trim(c.selling_unit))')
    factor = 'c.factor'
    base_unit = 'c.base_unit'
    if catalogue:
        cr = catalogue['roles']
        join += (f' LEFT JOIN {_relation_sql(catalogue)} p ON s.{_q(identity)}=p.{_q(cr[identity])}')
        factor = f'CASE WHEN lower(trim(s.unit))=lower(trim(p.{_q(cr["base_unit"])})) THEN 1 ELSE c.factor END'
        base_unit = f'COALESCE(c.base_unit, CAST(p.{_q(cr["base_unit"])} AS VARCHAR))'
        sources.append(catalogue)
        conflicting = connection.execute('SELECT count(*) FROM semantic.eligible_sales_lines s ' + join +
            f' WHERE (c.base_unit IS NOT NULL AND p.{_q(cr["base_unit"])} IS NOT NULL '
            f'AND lower(trim(c.base_unit))<>lower(trim(p.{_q(cr["base_unit"])}))) '
            f'OR (lower(trim(s.unit))=lower(trim(p.{_q(cr["base_unit"])})) AND c.factor<>1)').fetchone()[0]
        if conflicting:
            profile.issues.append(Issue(code='CONVERSION_BASE_UNIT_CONFLICT', severity='blocked',
                relation='semantic.product_quantities', count=conflicting,
                message='Conversion equations conflict with verified product base units.'))
            return
    elif 'don vi khong co dong quy doi nao la don vi co ban he so 1' in _words(rulebook):
        # This fallback is permitted only by the current supplied rule and a
        # complete conversion source. Failed conversion tables cannot disappear.
        blocked = any(t.grain == 'unit_conversion' and (t.status == 'blocked' or
            any(c.role == 'conversion_factor' and not c.usable for c in t.columns)) for t in profile.tables)
        if not blocked:
            factor = 'COALESCE(c.factor,1)'
            base_unit = 'COALESCE(c.base_unit,CAST(s.unit AS VARCHAR))'
    missing = connection.execute('SELECT count(*) FROM semantic.eligible_sales_lines s ' + join +
        f' WHERE ({factor}) IS NULL OR s.quantity IS NULL OR s.unit IS NULL OR s.{_q(identity)} IS NULL').fetchone()[0]
    if missing:
        profile.issues.append(Issue(code='UNIT_MAPPING_INCOMPLETE', severity='blocked', count=missing,
            relation='semantic.product_quantities',
            message='Base quantities are unavailable because some eligible sales lack verified unit mappings.'))
        return
    select_sales = 's.* EXCLUDE (base_unit)' if 'base_unit' in sales['roles'] else 's.*'
    sql = ('SELECT ' + select_sales + ',s.quantity*(' + factor + ') AS base_quantity,' + base_unit +
           ' AS base_unit FROM semantic.eligible_sales_lines s ' + join)
    try:
        # Evaluate the entire conversion expression before publishing its view.
        connection.execute('SELECT count(base_quantity),min(base_quantity),max(base_quantity) FROM (' + sql + ')').fetchone()
    except Exception as error:
        profile.issues.append(Issue(code='UNIT_CONVERSION_FAILED', severity='blocked',
            relation='semantic.product_quantities',
            message='Exact base-quantity conversion failed: ' + type(error).__name__))
        return
    publish('semantic.product_quantities', 'order_item', sql, sources,
            'Eligible sales quantities converted using verified product-specific equations.', evidence)


def _schema(connection, profile, relations, readme, rulebook):
    from .parsers import text_key
    semantic = _relations_and_semantics(connection, profile, relations, readme, rulebook)
    by_name = {r['name']: r for r in relations + semantic}
    clean_roles = {role for r in relations for role in r['roles']}
    documented_roles, populated_roles, blocked_roles = set(), set(), set()
    for table in profile.tables:
        for role, name in table.roles.items():
            documented_roles.add(role)
            field = next((c for c in table.columns if c.name == name), None)
            if field is None or not field.all_empty:
                populated_roles.add(role)
            if field and not field.all_empty and (not field.usable or table.status == 'blocked'):
                blocked_roles.add(role)
    if not profile.tables:
        populated_roles = clean_roles.copy()
    required = {'order_count': {'order_id', 'order_date'}, 'revenue': {'line_amount'},
        'product_quantities': {'base_quantity'}, 'payment_history': {'payment_date', 'payment_amount'},
        'historical_debt': {'order_id', 'order_date', 'order_total', 'payment_date', 'payment_amount'},
        'historical_inventory': {'inventory_date', 'inventory_quantity'}}
    targets = {'order_count': 'semantic.eligible_orders', 'revenue': 'semantic.eligible_sales_lines',
               'product_quantities': 'semantic.product_quantities'}
    capabilities = {}
    for name, roles in required.items():
        target = targets.get(name)
        relation = by_name.get(target)
        available = relation is not None and roles <= relation['roles'].keys()
        if name == 'payment_history':
            candidates = [r for r in relations if r['grain'] == 'payment' and roles <= r['roles'].keys()
                          and r['keys'] == [r['roles'].get('payment_id')]]
            available = len(candidates) == 1
            target = candidates[0]['name'] if available else None
        elif name == 'historical_debt':
            order = by_name.get('semantic.orders')
            payments = [r for r in relations if r['grain'] == 'payment' and {'payment_date', 'payment_amount', 'order_id'} <= r['roles'].keys()]
            order_sources = order.get('source_relations', []) if order else []
            connected = [p for p in payments if any(link['child'] == p['name'] and link['parent'] in order_sources
                and link['validated'] and link.get('null_child_keys', 0) == 0 for link in profile.relationships)]
            # Debt is not merely total minus payments: COD/channel exclusions
            # and cancellation semantics must be mapped before claiming support.
            available = bool(order and {'order_total', 'order_date', 'payment_method'} <= order['roles'].keys() and len(connected) == 1)
            target = order['name'] if available else None
        elif name == 'historical_inventory':
            candidates = [r for r in relations if r['grain'] == 'inventory_movement' and roles <= r['roles'].keys()
                          and {'product_id', 'product_name'} & r['roles'].keys()]
            meaning_resolved = 'ton sau lan' in text_key(readme) or 'inventory balance after' in text_key(readme)
            available = len(candidates) == 1 and meaning_resolved
            target = candidates[0]['name'] if available else None
        if available:
            state = 'available'
        elif roles & blocked_roles:
            state = 'blocked_by_quality'
        elif name == 'product_quantities' and {'quantity', 'unit'} <= populated_roles:
            state = 'unresolved_mapping'
        elif not roles <= populated_roles:
            state = 'missing_in_source'
        else:
            state = 'unresolved_mapping'
        capabilities[name] = {'status': state, 'required_roles': sorted(roles), 'relation': target if available else None}
    coverage = {}
    for table in profile.tables:
        for column in table.columns:
            if column.usable and column.parser.get('kind') in ('date', 'datetime'):
                coverage[f'{table.region_id}:{column.name}'] = {'observed_min': column.minimum,
                    'observed_max': column.maximum, 'role': column.role,
                    'meaning': 'Observed events, not a guarantee of continuous coverage.'}
    cutoff = re.search(r'(?:đến hết|den het)\s+(\d{1,2}/\d{1,2}/\d{4})', readme, re.I)
    if cutoff:
        try:
            ending = datetime.strptime(cutoff[1], '%d/%m/%Y').date().isoformat()
        except ValueError:
            ending = None
        if ending:
            coverage['declared_end'] = {'date': ending, 'evidence': cutoff[0],
                'meaning': 'README cutoff; not evidence of historical snapshot availability.'}
            for relation in relations:
                if relation['grain'] != 'inventory_snapshot':
                    continue
                aliases = [c.get('source_alias', c['name']) for c in relation['columns']
                           if c.get('role') == 'inventory_quantity' or relation['roles'].get('inventory_quantity') == c['name']]
                statements = [line for line in readme.splitlines() if 'ton cuoi ngay cuoi co du lieu' in text_key(line)
                              and any(text_key(alias) in text_key(line) for alias in aliases)]
                if statements:
                    relation['snapshot_as_of'] = ending
                    relation['temporal_semantics'] = {'kind': 'snapshot', 'as_of_date': ending,
                        'evidence': statements, 'historical_reconstruction': False}
                    coverage[relation['name']] = relation['temporal_semantics']
    return AgentSchema(relations=relations + semantic, relationships=profile.relationships,
        capabilities=capabilities, rules=rulebook, temporal_coverage=coverage,
        limitations=['Only listed clean/semantic relations and columns are queryable.',
            'Preparation-blocked information requires abstention, not unanswerable.',
            'Snapshot balances apply only at their documented date; they do not establish historical balances.',
            'No query results or record samples are part of this schema.'])


def _build(business, rulebook, calendar, config, spec, database, proposal_adapter=None):
    import duckdb
    from .readers import iter_tables
    from .assistance import assist_table, assist_source
    started=time.monotonic();deadline=started+config.deadline_seconds
    model_calls_before=getattr(proposal_adapter,'calls',0)
    business,rulebook,calendar,readme=_inputs(business,rulebook,calendar)
    sources,evidence,fingerprint=_identity(business,rulebook,calendar,config,spec)
    rules = rulebook.read_text(encoding='utf-8')
    profile=BusinessProfile(business=business.name,fingerprint=fingerprint,inventory=[asdict(s) for s in sources],evidence=evidence)
    connection=duckdb.connect(str(database))
    connection.execute('SET memory_limit='+_lit(config.memory_limit))
    connection.execute('SET threads=1')
    connection.execute('CREATE SCHEMA private');connection.execute('CREATE SCHEMA clean');connection.execute('CREATE SCHEMA semantic')
    connection.execute('CREATE TABLE private.row_audit(source_id VARCHAR,region_id VARCHAR,row_number BIGINT,classification VARCHAR,reason VARCHAR,values_json VARCHAR)')
    connection.execute('CREATE TABLE private.lineage(relation_name VARCHAR,canonical_record_id VARCHAR,source_record_id VARCHAR,source_id VARCHAR,row_number BIGINT)')
    try:
        seen=set()
        for source in sources:
            aliases=[s for s in sources if (s.display_alias or s.path)==(source.display_alias or source.path) and s.path!=source.path]
            if aliases:
                profile.issues.append(Issue(code='DISPLAY_ALIAS_COLLISION',source_id=source.source_id,message='Distinct physical files share a display alias.'))
            before=len(profile.tables)
            try:
                assist_source(business,source,spec,proposal_adapter,config)
                tables=iter_tables(business,source,layout_overrides=spec.layouts.get(source.source_id))
                for table in tables:
                    table.region_id = source.source_id + ':' + table.region_id
                    if table.region_id in seen:
                        raise ValueError('Duplicate region identifier')
                    seen.add(table.region_id)
                    assist_table(table,readme,rules,spec,proposal_adapter,config)
                    result,_=_scan_table(connection,table,readme,rules,config,spec,profile,deadline)
                    profile.tables.append(result)
                    spec.layouts.setdefault(source.source_id, table.metadata.get('extraction',{}))
                if len(profile.tables)==before:
                    profile.issues.append(Issue(code='EMPTY_SOURCE',severity='blocked',source_id=source.source_id,message='No table region was discovered.'))
            except (ValueError,OSError,RuntimeError) as error:
                profile.issues.append(Issue(code='SOURCE_SCAN_FAILED',severity='blocked',source_id=source.source_id,message=str(error)[:500]))
        relations=_publish_groups(connection,profile)
        schema=_schema(connection,profile,relations,readme,rules)
        _validate_pandera(profile,connection,relations)
        _calendar(connection,calendar,schema,profile)
        for entry in profile.inventory:
            matching=[t for t in profile.tables if t.source_id==entry['source_id']]
            failed=any(i.source_id==entry['source_id'] and i.code in ('SOURCE_SCAN_FAILED','EMPTY_SOURCE') for i in profile.issues)
            entry['scan_complete']=bool(matching) and not failed
            entry['status']='blocked' if failed or not matching or all(t.status=='blocked' for t in matching) else (
                'partial' if any(t.status!='ready' for t in matching) else 'ready')
        profile.status='blocked' if not relations else ('partial' if any(i.severity=='blocked' for i in profile.issues) else 'ready')
        profile.measurements={'elapsed_seconds':time.monotonic()-started,'sources':len(sources),
                              'regions':len(profile.tables),'source_rows':sum(sum(t.accounting.values()) for t in profile.tables),
                              'record_rows':sum(t.records for t in profile.tables),
                              'duplicates_removed':sum(t.duplicates_removed for t in profile.tables),
                              'model_calls':getattr(proposal_adapter,'calls',0)-model_calls_before,
                              'peak_process_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                              'rss_scope':'process high-water mark, including preceding businesses',
                              'full_scan_complete':all(s['scan_complete'] for s in profile.inventory)}
        current_sources,current_evidence,_=_identity(business,rulebook,calendar,config,PreparationSpec.model_validate(evidence['requested_spec']))
        if current_evidence != evidence:
            raise RuntimeError('Source inputs changed during preparation; bundle was not published')
        return profile,schema,spec
    finally:
        connection.close()


def _validate_pandera(profile,connection,relations):
    """Check full accounting and actual persisted values, in bounded batches."""
    import pandas as pd
    import pandera.pandas as pa
    rows=[{'rows':sum(t.accounting.values()),'records':t.records,'classified_records':t.accounting.get('record',0)} for t in profile.tables]
    if rows:
        schema=pa.DataFrameSchema({'rows':pa.Column(int,pa.Check.ge(0)),
                                   'records':pa.Column(int,pa.Check.ge(0)),
                                   'classified_records':pa.Column(int,pa.Check.ge(0))},
                                  checks=pa.Check(lambda df:(df.records==df.classified_records).all()))
        schema.validate(pd.DataFrame(rows))
    def check_kind(values,sql_type):
        present=values.dropna()
        if sql_type.startswith('DECIMAL'):
            return present.map(lambda v:isinstance(v,Decimal) and v.is_finite()).all()
        if sql_type=='DATE':return present.map(lambda v:isinstance(v,date) and not isinstance(v,datetime)).all()
        if sql_type in ('TIMESTAMP','TIMESTAMP WITH TIME ZONE','TIMESTAMPTZ'):
            return present.map(lambda v:isinstance(v,datetime)).all()
        if sql_type=='BOOLEAN':return present.map(lambda v:type(v) is bool).all()
        return present.map(lambda v:isinstance(v,str)).all()
    for relation in relations:
        fields=relation['columns']
        checks={c['name']:pa.Column(object,nullable=c['nullable'],checks=pa.Check(
                    lambda values,kind=c['type']:check_kind(values,kind))) for c in fields}
        table_schema=pa.DataFrameSchema(checks,strict=True)
        cursor=connection.cursor()
        try:
            cursor.execute('SELECT * FROM '+'.'.join(map(_q,relation['name'].split('.'))))
            while batch:=cursor.fetchmany(4096):
                table_schema.validate(pd.DataFrame(batch,columns=[c['name'] for c in fields],dtype=object))
        finally:cursor.close()


def _calendar(connection,path,schema,profile):
    import csv
    try:
        with path.open(encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream)
            date_field='date' if 'date' in (reader.fieldnames or []) else 'solar_date'
            required={date_field,'lunar_day','lunar_month','lunar_year'}
            if not required<=set(reader.fieldnames or []):
                raise ValueError('Calendar needs solar date and lunar day/month/year columns')
            records=[];seen=set()
            for row in reader:
                day=date.fromisoformat(row[date_field]);ld=int(row['lunar_day']);lm=int(row['lunar_month']);ly=int(row['lunar_year'])
                if day in seen or not 1<=ld<=30 or not 1<=lm<=12:raise ValueError('Invalid or duplicate calendar date')
                seen.add(day)
                records.append((day,ld,lm,ly,row.get('lunar_leap','false').lower()=='true',row.get('holiday','')))
        connection.execute('CREATE TABLE clean.calendar(date DATE,lunar_day INTEGER,lunar_month INTEGER,lunar_year INTEGER,lunar_leap BOOLEAN,holiday VARCHAR)')
        _insert(connection,'clean.calendar',['date','lunar_day','lunar_month','lunar_year','lunar_leap','holiday'],records)
        schema.relations.append({'name':'clean.calendar','grain':'calendar_day','keys':['date'],
                                 'meaning':'Current split solar/lunar calendar; no system-date dependence.',
                                 'columns':[{'name':r[0],'type':r[1],'meaning':r[0].replace('_',' '),'nullable':True}
                                            for r in connection.execute('DESCRIBE clean.calendar').fetchall()]})
    except (ValueError,KeyError) as error:
        profile.issues.append(Issue(code='INVALID_CALENDAR',severity='blocked',message=str(error)))


def _configuration(config,proposal_adapter):
    config=config.model_copy(deep=True) if config else PreparationConfig()
    if proposal_adapter is not None:
        settings=getattr(proposal_adapter,'configuration',None)
        if not isinstance(settings,dict) or not settings:
            raise ValueError('Proposal adapter must declare its reproducible configuration')
        config.model_configuration={**config.model_configuration,'adapter':settings}
    return config


def profile_business(business,rulebook,calendar, *, config=None,spec=None,proposal_adapter=None):
    """Read all discovered rows; return private profiles, never reference answers."""
    config=_configuration(config,proposal_adapter);spec=spec.model_copy(deep=True) if spec else PreparationSpec()
    with tempfile.TemporaryDirectory(prefix='data-profile-') as temp:
        profile,_,_=_build(business,rulebook,calendar,config,spec,Path(temp)/'profile.duckdb',proposal_adapter)
        return profile


def prepare_business(business,rulebook,calendar, *, output='artifacts/prepared',config=None,spec=None,proposal_adapter=None):
    """Atomically publish an immutable content-addressed database outside input data."""
    config=_configuration(config,proposal_adapter);spec=spec.model_copy(deep=True) if spec else PreparationSpec()
    business,rulebook,calendar,_=_inputs(business,rulebook,calendar)
    output=Path(output).absolute()
    if output.is_symlink() or any(p.is_symlink() for p in (output,*output.parents)):
        raise ValueError('Output path must not contain symlinks')
    output=output.resolve()
    if output.is_relative_to(business.parent.parent) or business.is_relative_to(output) or rulebook.is_relative_to(output) or calendar.is_relative_to(output):
        raise ValueError('Prepared output must be separate from source data')
    _,_,fingerprint=_identity(business,rulebook,calendar,config,spec)
    destination=output/fingerprint
    if destination.exists():
        manifest=json.loads((destination/'manifest.json').read_text())
        for name,digest in manifest['artifact_hashes'].items():
            if _hash(destination/name)!=digest:
                raise ValueError('Cached preparation bundle has been modified')
        return PreparedBusiness(bundle_id=fingerprint,path=destination,database=destination/'data.duckdb',
                                schema_path=destination/'schema.json',profile_path=destination/'profile.json',
                                status=manifest['status'],cache_hit=True,measurements={**manifest['measurements'],'model_calls':0})
    output.mkdir(parents=True,exist_ok=True)
    temp=Path(tempfile.mkdtemp(prefix='.preparing-',dir=output))
    try:
        profile,schema,final_spec=_build(business,rulebook,calendar,config,spec,temp/'data.duckdb',proposal_adapter)
        _write(temp/'profile.json',profile.model_dump(mode='json'))
        _write(temp/'schema.json',schema.model_dump(mode='json'))
        _write(temp/'spec.json',final_spec.model_dump(mode='json'))
        _write(temp/'validation.json',{'status':profile.status,'issues':[i.model_dump() for i in profile.issues],
                                      'relationships':profile.relationships,'reconciliations':profile.reconciliations})
        (temp/'schema.md').write_text(_schema_markdown(schema),encoding='utf-8')
        hashes={p.name:_hash(p) for p in temp.iterdir() if p.is_file()}
        _write(temp/'manifest.json',{'version':VERSION,'bundle_id':fingerprint,'status':profile.status,
                                   'inputs':profile.evidence,'measurements':profile.measurements,
                                   'artifact_hashes':hashes})
        temp.rename(destination)
        return PreparedBusiness(bundle_id=fingerprint,path=destination,database=destination/'data.duckdb',
                                schema_path=destination/'schema.json',profile_path=destination/'profile.json',
                                status=profile.status,measurements=profile.measurements)
    finally:
        if temp.exists():shutil.rmtree(temp)


def _schema_markdown(schema):
    lines=['# Prepared data schema','',f'Dialect: {schema.dialect}','']
    for r in schema.relations:
        lines += ['## '+r['name'],'', 'Grain: '+r['grain'],'', '| Column | Type | Meaning |','|---|---|---|']
        lines += ['| '+c['name']+' | '+c['type']+' | '+c.get('meaning','').replace('|','/')+' |' for c in r['columns']]
        lines.append('')
    lines += ['## Capabilities','']+[f'- {name}: {v["status"]}' for name,v in schema.capabilities.items()]
    lines += ['','## Current rules','',schema.rules]
    return '\n'.join(lines)+'\n'


def load_agent_schema(bundle):
    """Load the allowlisted schema only; audit profiles are deliberately separate."""
    path=bundle.path if isinstance(bundle,PreparedBusiness) else Path(bundle)
    manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
    expected=manifest.get('artifact_hashes',{}).get('schema.json')
    if expected is None or _hash(path/'schema.json')!=expected:
        raise ValueError('Published schema integrity check failed')
    return AgentSchema.model_validate_json((path/'schema.json').read_text(encoding='utf-8'))
