import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_IMPORT_ROOT = tempfile.TemporaryDirectory()
os.environ['SECRET_KEY'] = 'unit-test-secret'
os.environ['SYNC_API_KEY'] = 'unit-test-sync-key'
os.environ['ADMIN_PASSWORD'] = 'unit-test-admin-password'
os.environ['DATABASE_PATH'] = str(Path(_IMPORT_ROOT.name) / 'import.db')

import server


class ShiftSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        server.DB_FILE = str(Path(self.temp_dir.name) / 'cloud_test.db')
        server.app.config['TESTING'] = True
        server.init_cloud_db()
        self.client = server.app.test_client()
        with self.client.session_transaction() as session:
            session.update({
                'user_id': 1,
                'username': 'Aliaksei Patonich',
                'role': 'user',
                'employee_id': 1
            })
        self.sync_headers = {'X-Sync-Key': 'unit-test-sync-key'}
        self.base_payload = {
            'request_id': 'request-001',
            'date': '2026-10-11',
            'employee': 'Aliaksei Patonich',
            'company': 'Privat',
            'object_name': 'Bygg och renovering',
            'hours': 8,
            'rate': 0,
            'transport': 0,
            'comment': 'test shift'
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def submit(self, payload=None, client=None):
        return (client or self.client).post('/submit-shift', json=payload or self.base_payload)

    def database(self):
        return server.get_db_connection()

    def test_request_id_is_idempotent_and_payload_mismatch_conflicts(self):
        self.assertEqual(self.submit().status_code, 201)
        repeated = self.submit()
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.json['idempotent'])
        changed = dict(self.base_payload, comment='different payload')
        self.assertEqual(self.submit(changed).status_code, 409)

        conn = self.database()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM incoming_submissions').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM cloud_shifts').fetchone()[0], 0)
        conn.close()

    def test_concurrent_same_request_creates_one_queue_row(self):
        def post_once(_):
            client = server.app.test_client()
            with client.session_transaction() as session:
                session.update({
                    'user_id': 1, 'username': 'Aliaksei Patonich',
                    'role': 'user', 'employee_id': 1
                })
            return self.submit(client=client).status_code

        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(post_once, range(16)))
        self.assertTrue(all(status in (200, 201) for status in statuses), statuses)
        conn = self.database()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM incoming_submissions').fetchone()[0], 1)
        conn.close()

    def test_submission_id_ack_after_local_commit_and_legacy_numeric_id(self):
        self.submit()
        queued = self.client.get('/get-unsynced', headers=self.sync_headers).json['shifts']
        submission = next(row for row in queued if row['request_id'] == 'request-001')
        self.assertEqual(submission['id'], 'submission:1')

        local = sqlite3.connect(Path(self.temp_dir.name) / 'local_test.db')
        local.execute('CREATE TABLE received (sync_id TEXT PRIMARY KEY)')
        local.execute('INSERT INTO received VALUES (?)', (submission['sync_id'],))
        local.commit()
        local.close()
        ack = self.client.post('/mark-synced', headers=self.sync_headers, json={'ids': [submission['id']]})
        self.assertEqual(ack.status_code, 200)
        self.assertFalse(any(row['id'] == submission['id'] for row in self.client.get('/get-unsynced', headers=self.sync_headers).json['shifts']))

        conn = self.database()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO cloud_shifts (date, employee, company, object_name, hours, rate, transport, comment, synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        ''', ('2026-10-10', 'Aliaksei Patonich', 'Privat', 'Bygg och renovering', 7, 0, 0, 'legacy'))
        legacy_id = cursor.lastrowid
        conn.commit()
        conn.close()
        server.init_cloud_db()
        legacy = next(row for row in self.client.get('/get-unsynced', headers=self.sync_headers).json['shifts'] if row['id'] == legacy_id)
        self.assertEqual(legacy['source'], 'legacy')
        self.assertEqual(self.client.post('/mark-synced', headers=self.sync_headers, json={'ids': [legacy_id]}).status_code, 200)
        conn = self.database()
        self.assertEqual(conn.execute('SELECT synced FROM cloud_shifts WHERE id = ?', (legacy_id,)).fetchone()[0], 1)
        conn.close()

    def test_legacy_schema_without_request_id_migrates_without_losing_rows(self):
        server.DB_FILE = str(Path(self.temp_dir.name) / 'legacy_schema.db')
        legacy = sqlite3.connect(server.DB_FILE)
        legacy.execute('''
            CREATE TABLE cloud_shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, employee TEXT NOT NULL,
                company TEXT NOT NULL, object_name TEXT NOT NULL, hours REAL NOT NULL,
                rate REAL NOT NULL, transport REAL DEFAULT 0, comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, synced INTEGER DEFAULT 0
            )
        ''')
        legacy.execute('''
            INSERT INTO cloud_shifts (date, employee, company, object_name, hours, rate)
            VALUES ('2026-10-11', 'Legacy Employee', 'Privat', 'Old Object', 8, 0)
        ''')
        legacy.commit()
        legacy.close()

        server.init_cloud_db()
        conn = self.database()
        row = conn.execute("SELECT request_id, sync_id, source FROM incoming_submissions").fetchone()
        self.assertEqual(tuple(row), ('legacy-cloud:1', 'legacy-cloud:1', 'legacy'))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM cloud_shifts').fetchone()[0], 1)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        conn.close()

    def test_versions_scope_and_tombstone_prevent_resurrection(self):
        self.submit()
        queued = self.client.get('/get-unsynced', headers=self.sync_headers).json['shifts'][0]
        self.client.post('/mark-synced', headers=self.sync_headers, json={'ids': [queued['id']]})

        edited_shift = dict(self.base_payload, hours=9, comment='admin update')
        edited = self.client.post('/sync/push', headers=self.sync_headers, json={'changes': [{
            'operation_id': 'edit-1', 'sync_id': queued['sync_id'], 'operation': 'upsert',
            'base_version': queued['version'], 'shift': edited_shift
        }]})
        self.assertTrue(edited.json['results'][0]['ok'], edited.json)
        version = edited.json['results'][0]['version']
        repeated_edit = self.client.post('/sync/push', headers=self.sync_headers, json={'changes': [{
            'operation_id': 'edit-1', 'sync_id': queued['sync_id'], 'operation': 'upsert',
            'base_version': queued['version'], 'shift': edited_shift
        }]})
        self.assertTrue(repeated_edit.json['results'][0]['idempotent'], repeated_edit.json)

        stale = self.client.post('/sync/push', headers=self.sync_headers, json={'changes': [{
            'operation_id': 'stale-1', 'sync_id': queued['sync_id'], 'operation': 'upsert',
            'base_version': queued['version'], 'shift': self.base_payload
        }]})
        self.assertEqual(stale.json['results'][0]['reason'], 'stale_version')

        history = self.client.post('/check-employee-shifts', json={'employee': 'someone else', 'month': '2026-10'}).json
        self.assertEqual(history['recent_shifts'][0]['hours'], 9)
        self.assertEqual(history['recent_shifts'][0]['comment'], 'admin update')

        deleted = self.client.post('/sync/push', headers=self.sync_headers, json={'changes': [{
            'operation_id': 'delete-1', 'sync_id': queued['sync_id'], 'operation': 'delete',
            'base_version': version, 'shift': edited_shift
        }]})
        self.assertTrue(deleted.json['results'][0]['ok'], deleted.json)
        history = self.client.post('/check-employee-shifts', json={'employee': 'Aliaksei Patonich', 'month': '2026-10'}).json
        self.assertEqual(history['recent_shifts'], [])
        replay = self.client.post('/sync/push', headers=self.sync_headers, json={'changes': [{
            'operation_id': 'resurrect-1', 'sync_id': queued['sync_id'], 'operation': 'upsert',
            'base_version': version, 'shift': edited_shift
        }]})
        self.assertEqual(replay.json['results'][0]['reason'], 'deleted')

    def test_sync_auth_and_old_snapshot_cannot_delete(self):
        self.submit()
        self.assertEqual(self.client.get('/sync/pull').status_code, 403)
        self.assertEqual(self.client.post('/sync/push', json={'changes': []}).status_code, 403)

        queued = self.client.get('/get-unsynced', headers=self.sync_headers).json['shifts'][0]
        self.client.post('/mark-synced', headers=self.sync_headers, json={'ids': [queued['id']]})
        deleted = self.client.post('/sync/push', headers=self.sync_headers, json={'changes': [{
            'operation_id': 'delete-before-stale-snapshot', 'sync_id': queued['sync_id'],
            'operation': 'delete', 'base_version': queued['version'], 'shift': self.base_payload
        }]})
        self.assertTrue(deleted.json['results'][0]['ok'], deleted.json)

        snapshot = self.client.post('/sync-desktop-data', headers=self.sync_headers, json={
            'shifts': [dict(self.base_payload, request_id='')]
        })
        self.assertEqual(snapshot.status_code, 200)
        history = self.client.post('/check-employee-shifts', json={'employee': 'Aliaksei Patonich', 'month': '2026-10'}).json
        self.assertEqual(history['recent_shifts'], [])

    def test_user_export_is_scoped_to_session_employee(self):
        self.submit()
        conn = self.database()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO meta_employees (name, salary_rate, is_active) VALUES (?, 0, 1)", ('Second Employee',))
        second_employee_id = cursor.lastrowid
        conn.commit()
        conn.close()

        other_client = server.app.test_client()
        with other_client.session_transaction() as session:
            session.update({'user_id': 2, 'username': 'Second Employee', 'role': 'user', 'employee_id': second_employee_id})
        response = other_client.post('/export-shifts-csv', json={'employee': 'Aliaksei Patonich', 'month': '2026-10'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'Aliaksei Patonich', response.data)
        history = other_client.post('/check-employee-shifts', json={'employee': 'Aliaksei Patonich', 'month': '2026-10'}).json
        self.assertEqual(history['recent_shifts'], [])

    def test_desktop_outbox_survives_offline_and_syncs_tombstone(self):
        import app as desktop
        import requests

        previous_db = desktop.DB_FILE
        previous_url = desktop.CLOUD_URL
        previous_key = desktop.SYNC_API_KEY
        desktop.DB_FILE = str(Path(self.temp_dir.name) / 'desktop_test.db')
        desktop.CLOUD_URL = 'http://in-process'
        desktop.SYNC_API_KEY = 'unit-test-sync-key'
        desktop.init_db_once()

        class Response:
            def __init__(self, response):
                self.response = response
                self.status_code = response.status_code

            def json(self):
                return self.response.get_json()

            def raise_for_status(self):
                if self.response.status_code >= 400:
                    raise RuntimeError(self.response.get_data(as_text=True))

        def route_get(url, **kwargs):
            response = server.app.test_client().get(
                '/'.join(url.split('/')[3:]), headers=kwargs.get('headers'),
                query_string=kwargs.get('params')
            )
            return Response(response)

        def route_post(url, **kwargs):
            response = server.app.test_client().post(
                '/'.join(url.split('/')[3:]), headers=kwargs.get('headers'), json=kwargs.get('json')
            )
            return Response(response)

        original_get = desktop.requests.get
        original_post = desktop.requests.post
        try:
            desktop.requests.get = route_get
            desktop.requests.post = route_post
            sync_id = 'desktop-offline-test'
            payload = {
                'date': '2026-10-11', 'employee': 'Aliaksei Patonich', 'company': 'Privat',
                'object_name': 'Bygg och renovering', 'hours': 8, 'rate': 0, 'transport': 0,
                'comment': 'offline test', 'invoiced': 0
            }
            conn = desktop.get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO shifts (date, employee, company, object_name, hours, rate, transport, comment, invoiced, sync_id, sync_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0)
            ''', (payload['date'], payload['employee'], payload['company'], payload['object_name'], payload['hours'], 0, 0, payload['comment'], sync_id))
            desktop.enqueue_shift_operation(cursor, sync_id, 'upsert', 0, payload)
            cursor.execute("INSERT INTO balance (date, employee, description, change_amount) VALUES (?, ?, ?, ?)", ('2026-10-11', 'Aliaksei Patonich', 'private-test', 123))
            conn.commit()
            conn.close()

            window = desktop.SmartReportApp.__new__(desktop.SmartReportApp)
            window.perform_sync_logic()
            conn = desktop.get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM shifts WHERE sync_id = ?", (sync_id,))
            local_shift = cursor.fetchone()
            payload['hours'] = 9
            cursor.execute("UPDATE shifts SET hours = 9 WHERE sync_id = ?", (sync_id,))
            desktop.enqueue_shift_operation(cursor, sync_id, 'upsert', local_shift['sync_version'], payload)
            conn.commit()
            conn.close()

            desktop.requests.get = lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError('offline test'))
            with self.assertRaises(requests.ConnectionError):
                window.perform_sync_logic()
            conn = desktop.get_db_connection()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM shift_sync_outbox WHERE state = 'pending'").fetchone()[0], 1)
            conn.close()

            desktop.init_db_once()
            conn = desktop.get_db_connection()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM shift_sync_outbox WHERE state = 'pending'").fetchone()[0], 1)
            conn.close()
            window = desktop.SmartReportApp.__new__(desktop.SmartReportApp)

            desktop.requests.get = route_get
            window.perform_sync_logic()
            conn = desktop.get_db_connection()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM shift_sync_outbox WHERE state = 'pending'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT change_amount FROM balance").fetchone()[0], 123)
            conn.close()

            remote = self.database()
            self.assertEqual(remote.execute("SELECT hours FROM incoming_submissions WHERE sync_id = ?", (sync_id,)).fetchone()[0], 9)
            self.assertIsNone(remote.execute("SELECT name FROM sqlite_master WHERE name = 'balance'").fetchone())
            remote.close()

            conn = desktop.get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM shifts WHERE sync_id = ?", (sync_id,))
            local_shift = cursor.fetchone()
            desktop.enqueue_shift_operation(cursor, sync_id, 'delete', local_shift['sync_version'], desktop.shift_payload(local_shift))
            cursor.execute("DELETE FROM shifts WHERE sync_id = ?", (sync_id,))
            conn.commit()
            conn.close()
            window.perform_sync_logic()
            history = self.client.post('/check-employee-shifts', json={'employee': 'Aliaksei Patonich', 'month': '2026-10'}).json
            self.assertEqual(history['recent_shifts'], [])
        finally:
            desktop.requests.get = original_get
            desktop.requests.post = original_post
            desktop.DB_FILE = previous_db
            desktop.CLOUD_URL = previous_url
            desktop.SYNC_API_KEY = previous_key

    def test_remote_insert_preserves_sync_id_and_integer_version(self):
        import app as desktop

        previous_db = desktop.DB_FILE
        desktop.DB_FILE = str(Path(self.temp_dir.name) / 'desktop_receive.db')
        desktop.init_db_once()
        window = desktop.SmartReportApp.__new__(desktop.SmartReportApp)
        conn = desktop.get_db_connection()
        cursor = conn.cursor()
        cursor.execute('BEGIN IMMEDIATE')
        window.apply_remote_shift(cursor, {
            'sync_id': 'remote-stable-id', 'source': 'web', 'version': 7, 'change_seq': 7,
            'deleted': False, 'date': '2026-10-11', 'employee': 'Aliaksei Patonich',
            'company': 'Privat', 'object_name': 'Bygg och renovering', 'hours': 8,
            'rate': 0, 'transport': 0, 'comment': 'received', 'invoiced': 0
        })
        conn.commit()
        row = conn.execute('SELECT sync_id, sync_version FROM shifts').fetchone()
        self.assertEqual(tuple(row), ('remote-stable-id', 7))
        conn.close()
        desktop.DB_FILE = previous_db

    def test_authoritative_admin_edit_wins_against_pending_user_edit(self):
        import app as desktop

        previous_db = desktop.DB_FILE
        desktop.DB_FILE = str(Path(self.temp_dir.name) / 'desktop_conflict.db')
        desktop.init_db_once()
        conn = desktop.get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO shifts (date, employee, company, object_name, hours, rate, transport, comment, invoiced, sync_id, sync_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1)
        ''', ('2026-10-11', 'Aliaksei Patonich', 'Privat', 'Bygg och renovering', 9, 0, 0, 'offline user edit', 'admin-conflict'))
        desktop.enqueue_shift_operation(cursor, 'admin-conflict', 'upsert', 1, {
            'date': '2026-10-11', 'employee': 'Aliaksei Patonich', 'company': 'Privat',
            'object_name': 'Bygg och renovering', 'hours': 9, 'rate': 0, 'transport': 0,
            'comment': 'offline user edit', 'invoiced': 0
        })
        window = desktop.SmartReportApp.__new__(desktop.SmartReportApp)
        window.apply_remote_shift(cursor, {
            'sync_id': 'admin-conflict', 'version': 2, 'change_seq': 2, 'deleted': False,
            'date': '2026-10-11', 'employee': 'Aliaksei Patonich', 'company': 'Privat',
            'object_name': 'Bygg och renovering', 'hours': 8, 'rate': 0, 'transport': 0,
            'comment': 'admin edit', 'invoiced': 0
        })
        row = cursor.execute("SELECT hours, comment, sync_version FROM shifts WHERE sync_id = ?", ('admin-conflict',)).fetchone()
        state = cursor.execute("SELECT state FROM shift_sync_outbox WHERE sync_id = ?", ('admin-conflict',)).fetchone()[0]
        self.assertEqual(tuple(row), (8.0, 'admin edit', 2))
        self.assertEqual(state, 'conflict')
        conn.commit()
        conn.close()
        desktop.DB_FILE = previous_db

    def test_authoritative_admin_delete_wins_against_pending_user_edit(self):
        import app as desktop

        previous_db = desktop.DB_FILE
        desktop.DB_FILE = str(Path(self.temp_dir.name) / 'desktop_delete_conflict.db')
        desktop.init_db_once()
        conn = desktop.get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO shifts (date, employee, company, object_name, hours, rate, transport, comment, invoiced, sync_id, sync_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1)
        ''', ('2026-10-11', 'Aliaksei Patonich', 'Privat', 'Bygg och renovering', 9, 0, 0, 'offline user edit', 'admin-deleted'))
        desktop.enqueue_shift_operation(cursor, 'admin-deleted', 'upsert', 1, {
            'date': '2026-10-11', 'employee': 'Aliaksei Patonich', 'company': 'Privat',
            'object_name': 'Bygg och renovering', 'hours': 9, 'rate': 0, 'transport': 0,
            'comment': 'offline user edit', 'invoiced': 0
        })
        window = desktop.SmartReportApp.__new__(desktop.SmartReportApp)
        window.apply_remote_shift(cursor, {
            'sync_id': 'admin-deleted', 'version': 2, 'change_seq': 2, 'deleted': True
        })
        self.assertIsNone(cursor.execute("SELECT id FROM shifts WHERE sync_id = ?", ('admin-deleted',)).fetchone())
        self.assertEqual(cursor.execute("SELECT state FROM shift_sync_outbox WHERE sync_id = ?", ('admin-deleted',)).fetchone()[0], 'conflict')
        conn.commit()
        conn.close()
        desktop.DB_FILE = previous_db


if __name__ == '__main__':
    unittest.main()