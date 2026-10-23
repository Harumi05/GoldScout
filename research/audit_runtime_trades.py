"""Read-only runtime evidence audit; never authorizes or changes trading.

MT5 timestamps are server-wall labels, not proven UTC. The requested dates
are explicit calendar labels in that same clock; no sessions are inferred.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path

from dashboard.demo_trade_ledger import ALIASES, project_events


def server_time(value):
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            return datetime(1970, 1, 1) + timedelta(seconds=value)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value[:10].replace('.', '-') + value[10:])
            return parsed if parsed.tzinfo is None else None
    except (ValueError, OverflowError):
        pass
    return None


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('nonfinite JSON constant')


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError('nonfinite JSON number')
    return parsed


def _load(path, *, jsonl=False):
    """Hash and parse the same bytes; report input changes and malformed rows."""
    metadata = {'file': path.name, 'status': 'MISSING', 'records': 0}
    try:
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
    except FileNotFoundError:
        return [], metadata
    except OSError:
        return [], {**metadata, 'status': 'READ_ERROR'}
    metadata.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    if ((before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or len(content) != before.st_size):
        return [], {**metadata, 'status': 'CHANGED_DURING_READ'}
    try:
        text = content.decode('utf-8-sig')
    except UnicodeError:
        return [], {**metadata, 'status': 'INVALID_UTF8'}
    rows, errors = [], []
    for number, line in enumerate(text.splitlines() if jsonl else [text], 1):
        if jsonl and not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=_unique_fields,
                             parse_constant=_reject_constant, parse_float=_finite_float)
            if not isinstance(row, dict):
                raise ValueError('record is not an object')
            rows.append(row)
        except ValueError:
            errors.append(number)
    metadata.update(records=len(rows), status='INVALID_JSON' if errors else 'OK')
    if errors:
        metadata['invalid_lines'] = errors[:20]
        metadata['invalid_line_count'] = len(errors)
    return rows, metadata


def _row_time(row):
    for field in ('close_time', 'timestamp', 'captured_at', 'evaluated_at', 'updated_at'):
        parsed = server_time(row.get(field))
        if parsed is not None:
            return parsed
    return None


def _coverage(rows, start, end):
    times = [parsed for row in rows if (parsed := _row_time(row)) is not None]
    return {
        'first_server_label': min(times).isoformat() if times else None,
        'last_server_label': max(times).isoformat() if times else None,
        'records_in_requested_dates': sum(start <= value.date() <= end for value in times),
        'records_without_time': len(rows) - len(times),
        'latest_before_requested_dates': max(times).date() < start if times else None,
        'records_after_requested_dates': sum(value.date() > end for value in times),
    }


def _number(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    except OverflowError:
        return False


def _metrics(rows):
    pnl = [row['net_pnl'] for row in rows if _number(row.get('net_pnl'))]
    r = [row['realized_r_net'] for row in rows
         if _number(row.get('realized_r_net'))
         and _number(row.get('initial_risk_account_currency'))
         and row['initial_risk_account_currency'] > 0
         and _number(row.get('net_pnl'))
         and abs(row['net_pnl'] / row['initial_risk_account_currency'] - row['realized_r_net']) <= .001]
    wins, losses = sum(v for v in pnl if v > 0), -sum(v for v in pnl if v < 0)
    complete = bool(rows) and len(pnl) == len(rows)
    return {
        'closed_trades': len(rows), 'net_complete_trades': len(pnl),
        'net_unknown_trades': len(rows) - len(pnl), 'r_known_trades': len(r),
        'net_pnl': sum(pnl) if complete else None,
        'profit_factor': wins / losses if complete and losses else None,
        'expectancy_r_known_subset': sum(r) / len(r) if r else None,
    }


def _demo_mode_proven(rows):
    # Current serializers omit account_mode on RESERVED, POSITION_CLOSED and
    # excursion RECONCILED rows. Require matching trade-scoped DEMO evidence,
    # never infer mode merely from schema_version or a filename.
    demo_trades = {r['trade_id'] for r in rows
                   if r.get('account_mode') == 'DEMO' and isinstance(r.get('trade_id'), str)}
    for row in rows:
        if row.get('source') != 'MT5':
            return False
        if 'account_mode' in row:
            if row['account_mode'] != 'DEMO':
                return False
        elif (row.get('event') not in ('RESERVED', 'POSITION_CLOSED', 'RECONCILED')
              or not isinstance(row.get('trade_id'), str)
              or row['trade_id'] not in demo_trades):
            return False
    return True


def audit(input_dir: Path, *, as_of_date: date, days: int = 7,
          account_login: int | None = None):
    if not 1 <= days <= 366:
        raise ValueError('days must be between 1 and 366')
    start = as_of_date - timedelta(days=days - 1)
    sources, rows_by_name = {}, {}
    for filename in ('market_execution_events.jsonl', 'market_observations.jsonl',
                     'market_outcomes.jsonl', 'xau_goldscout_dashboard.json'):
        rows, metadata = _load(input_dir / filename, jsonl=filename.endswith('.jsonl'))
        sources[filename] = {**metadata, **_coverage(rows, start, as_of_date)}
        rows_by_name[filename] = rows
    events = rows_by_name['market_execution_events.jsonl']
    ledger = sources['market_execution_events.jsonl']
    legacy = [r for r in events if type(r.get('schema_version')) is not int or r['schema_version'] != 2]
    v2 = [r for r in events if type(r.get('schema_version')) is int and r['schema_version'] == 2]
    kinds = Counter(ALIASES.get(r.get('event'), r.get('event', 'UNKNOWN'))
                    for r in events if isinstance(r.get('event', 'UNKNOWN'), str))
    warnings = []
    if legacy:
        warnings.append('LEGACY_EVENTS_EXCLUDED_FROM_VALIDATED_STATISTICS')
    if ledger['latest_before_requested_dates']:
        warnings.append('LEDGER_ENDS_BEFORE_REQUESTED_DATES')
    identity_ok = lambda v: type(v) is int and 0 < v <= 2**64 - 1
    accounts = {r['account_login'] for r in v2 if identity_ok(r.get('account_login'))}
    selected = account_login if account_login is not None else next(iter(accounts)) if len(accounts) == 1 else None
    status, projection, period_stats, selected_coverage = 'NO_V2_EVIDENCE', None, None, None
    if ledger['status'] != 'OK':
        status = ledger['status']
    elif v2:
        if any(not identity_ok(r.get('account_login')) for r in v2):
            status = 'ACCOUNT_IDENTITY_MISSING'
        elif selected is None:
            status = 'ACCOUNT_SELECTION_REQUIRED'
        elif selected not in accounts:
            status = 'SELECTED_ACCOUNT_NOT_FOUND'
        else:
            selected_rows = [r for r in v2 if r['account_login'] == selected]
            selected_coverage = _coverage(selected_rows, start, as_of_date)
            seen, conflicting = {}, False
            for row in selected_rows:
                event_id = row.get('event_id')
                if not isinstance(event_id, str) or not event_id:
                    conflicting = True
                    break
                if event_id in seen and seen[event_id] != row:
                    conflicting = True
                    break
                seen[event_id] = row
            if conflicting:
                status = 'EVENT_IDENTITY_CONFLICT'
            elif not _demo_mode_proven(selected_rows):
                status = 'SOURCE_OR_DEMO_MODE_UNVERIFIED'
            elif any(not isinstance(r.get('event'), str) for r in selected_rows):
                status = 'INVALID_EVENT_TYPE'
            else:
                projection = project_events(selected_rows, account_login=selected)
                status = 'V2_PROJECTION_AVAILABLE' if projection['status'] == 'OK' else 'CONFLICT'
                closes = projection['closed_trades']
                if any(_row_time(r) is None for r in closes):
                    status = 'CLOSE_TIME_UNKNOWN'
                elif status == 'V2_PROJECTION_AVAILABLE':
                    period = [r for r in closes if start <= _row_time(r).date() <= as_of_date]
                    period_stats = _metrics(period)
    snapshot_rows = rows_by_name['xau_goldscout_dashboard.json']
    snapshot = snapshot_rows[0] if snapshot_rows and sources['xau_goldscout_dashboard.json']['status'] == 'OK' else {}
    reported_closes = snapshot.get('closed_trades')
    reported_closes = [r for r in reported_closes if isinstance(r, dict)] if isinstance(reported_closes, list) else []
    if snapshot and not isinstance(snapshot.get('risk_budget'), dict):
        warnings.append('SNAPSHOT_MISSING_RISK_BUDGET_V2')
    if snapshot and len(reported_closes) != kinds['POSITION_CLOSED']:
        warnings.append('SOURCE_CLOSE_COUNTS_DIFFER_NOT_PROOF_OF_MISSING_TRADES')
    ready = (status == 'V2_PROJECTION_AVAILABLE'
             and selected_coverage['records_in_requested_dates'] > 0
             and selected_coverage['records_after_requested_dates'] == 0)
    return {
        'observer_only': True, 'score_effect': 0, 'strategy_validated': False,
        'time_convention': 'MT5_SERVER_WALL_LABELS_UTC_OFFSET_UNVERIFIED',
        'requested_dates': {'start_inclusive': start.isoformat(), 'end_inclusive': as_of_date.isoformat()},
        'audit_status': status, 'recent_v2_evidence_available': ready,
        'sources': sources, 'event_counts': dict(kinds),
        'legacy_events': len(legacy), 'v2_events': len(v2),
        'distinct_v2_accounts': len(accounts),
        'selected_v2_coverage': selected_coverage,
        'ledger_conflicts': projection['ledger_conflicts'] if projection else None,
        'requested_period_v2_stats': period_stats,
        'unverified_snapshot': {
            'reported_close_rows': len(reported_closes),
            'close_coverage': _coverage(reported_closes, start, as_of_date),
            'unknown_setup_rows': sum(r.get('setup') in (None, '', 'UNKNOWN') for r in reported_closes),
            'net_pnl': None, 'profit_factor': None,
            'reason': 'NOT_A_RECONCILED_LEDGER_OR_PROVEN_ACCOUNT_SCOPE',
        },
        'warnings': warnings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', required=True, type=Path)
    parser.add_argument('--as-of-date', required=True, type=date.fromisoformat,
                        help='Explicit end calendar date in MT5 server-wall labels, not a UTC claim')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--account-login', type=int)
    args = parser.parse_args()
    if not 1 <= args.days <= 366:
        parser.error('--days must be between 1 and 366')
    result = audit(args.input_dir, as_of_date=args.as_of_date, days=args.days,
                   account_login=args.account_login)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result['recent_v2_evidence_available'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
