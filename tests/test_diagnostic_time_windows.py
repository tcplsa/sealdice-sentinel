import json
from datetime import UTC, datetime, timedelta, timezone

from sealdice_sentinel.adapters.diagnostic_store import DiagnosticStore


def test_legacy_offset_evidence_does_not_enter_a_later_utc_window(tmp_path):
    store = DiagnosticStore(tmp_path / 'evidence.db')
    store.initialize_evidence()
    # The old error occurred at 04:36 UTC, before the 06:14 UTC capture window.
    with store.connect() as db:
        db.executemany('INSERT INTO observations(at,scope,kind,payload) VALUES (?,?,?,?)', [
            ('2026-10-08T12:36:15.263000+08:00', 'default', 'log',
             json.dumps({'event': 'qq_auth_error'})),
            ('2026-10-08T14:20:24+08:00', 'default', 'health',
             json.dumps({'event': 'current_timeout'})),
            ('2026-10-08T02:21:00-04:00', 'server', 'network',
             json.dumps({'event': 'current_network'})),
        ])
    start = datetime(2026, 10, 8, 6, 14, tzinfo=UTC)
    for cutoff in (start, start.astimezone(timezone(timedelta(hours=8)))):
        rows = store.observations(cutoff, 'default')
        assert [x['event'] for x in rows] == ['current_timeout', 'current_network']
        assert all(datetime.fromisoformat(x['at']).utcoffset() == timedelta(0) for x in rows)


def test_resource_windows_support_legacy_offsets_and_non_utc_cutoffs(tmp_path):
    store = DiagnosticStore(tmp_path / 'resources.db')
    store.initialize_resources()
    with store.connect() as db:
        for timestamp, value in [('2026-10-08T12:36:15+08:00', 'old'),
                                 ('2026-10-08T14:20:24+08:00', 'current')]:
            db.execute('INSERT INTO resource_samples(at,payload) VALUES (?,?)',
                       (timestamp, json.dumps({'at': timestamp, 'value': value})))
    start = datetime(2026, 10, 8, 14, 14, tzinfo=timezone(timedelta(hours=8)))
    assert [x['value'] for x in store.resources(start)] == ['current']


def test_microsecond_cutoff_is_exact_even_when_sqlite_rounds_timestamps(tmp_path):
    store = DiagnosticStore(tmp_path / 'evidence.db')
    store.initialize_evidence()
    start = datetime(2026, 10, 8, 6, 14, 0, 200, tzinfo=UTC)
    store.append_observation(start - timedelta(microseconds=1), 'default', 'log', {'value': 'old'})
    store.append_observation(start, 'default', 'log', {'value': 'boundary'})
    assert [x['value'] for x in store.observations(start)] == ['boundary']


def test_new_evidence_uses_utc_and_payload_cannot_replace_its_timestamp(tmp_path):
    store = DiagnosticStore(tmp_path / 'evidence.db')
    store.initialize_evidence()
    instant = datetime(2026, 10, 8, 14, 20, tzinfo=timezone(timedelta(hours=8)))
    store.append_observation(instant, 'default', 'log',
                             {'at': '2099-01-01T00:00:00Z', 'scope': 'wrong', 'kind': 'wrong'})
    with store.connect(readonly=True) as db:
        assert db.execute('SELECT at FROM observations').fetchone()[0] == '2026-10-08T06:20:00+00:00'
    assert store.observations(instant)[0] == {
        'at': '2026-10-08T06:20:00+00:00', 'scope': 'default', 'kind': 'log',
    }


def test_report_order_and_cooldown_use_instants_not_offset_text(tmp_path):
    store = DiagnosticStore(tmp_path / 'evidence.db')
    store.initialize_evidence()
    now = datetime.now(UTC)
    times = [now.astimezone(timezone(timedelta(hours=8))), now + timedelta(seconds=1)]
    with store.connect() as db:
        for i, at in enumerate(times):
            payload = {'id': str(i), 'scope': 'default', 'created_at': at.isoformat(),
                       'due_at': at.isoformat(), 'state': 'complete'}
            db.execute('INSERT INTO reports VALUES (?,?,?,?,?,?)',
                       (str(i), 'default', at.isoformat(), at.isoformat(), 'complete',
                        json.dumps(payload)))
    assert [x['id'] for x in store.reports()] == ['1', '0']
    assert store.last_capture('default') == now + timedelta(seconds=1)
