"""Cross-platform upgrade/restore workflow through real CLI subprocesses."""
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from hub.history.projector import CoreRecord
from hub.history.schema import SQL, VERSION
from hub.history.store import HistoryStore
from hub.registry import load_registry
from test_backend import NODE_ID


class ReleaseTests(unittest.TestCase):
    def test_legacy_upgrade_backup_restore_and_registry_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            registry=root/'nodes.json'
            registry.write_text(json.dumps({'nodes':[dict(node_id=NODE_ID,name='Before upgrade',endpoint='http://192.0.2.1:9797',enabled=False)]}),encoding='utf-8')
            db=root/'history.db'
            connection=sqlite3.connect(str(db))
            connection.executescript(SQL)
            connection.execute('INSERT INTO core_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                CoreRecord(NODE_ID,'legacy',1,1000,1,5000,'ready',0,None,50,None,45).values())
            connection.commit();connection.close()
            old=root/'before.db'
            subprocess.run([sys.executable,'-m','hub','backup','--db',str(db),'--output',str(old)],check=True,stdout=subprocess.PIPE)
            store=HistoryStore(db).open()
            try:
                self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0],VERSION)
                row=store.connection.execute('SELECT node_id,cpu_percent,gpu_percent FROM core_samples').fetchone()
                self.assertEqual(row,(NODE_ID,0,None))
                current=root/'current.db'
                subprocess.run([sys.executable,'-m','hub','backup','--db',str(db),'--output',str(current)],check=True,stdout=subprocess.PIPE)
            finally: store.close()
            subprocess.run([sys.executable,'-m','hub','nodes','--file',str(registry),'rename',NODE_ID,'After upgrade'],check=True,stdout=subprocess.PIPE)
            self.assertEqual(load_registry(registry)[0].node_id,NODE_ID)
            self.assertEqual(load_registry(registry)[0].name,'After upgrade')
            restored=root/'restored.db'
            shutil.copyfile(old,restored)
            connection=sqlite3.connect(str(restored))
            try:
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0],1)
                self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0],'ok')
                self.assertEqual(connection.execute('SELECT node_id FROM core_samples').fetchone()[0],NODE_ID)
            finally: connection.close()
            reopened=HistoryStore(current).open()
            try: self.assertEqual(reopened.connection.execute('SELECT count(*) FROM core_samples').fetchone()[0],1)
            finally: reopened.close()
