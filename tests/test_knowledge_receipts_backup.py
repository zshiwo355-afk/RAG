import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.offline
SPEC = importlib.util.spec_from_file_location("receipt_backup_helper", Path(__file__).parents[1] / "scripts" / "knowledge_receipts_backup.py")
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)
URL = "postgresql://rag_knowledge:synthetic-secret@127.0.0.1:65432/company_knowledge"


@pytest.mark.parametrize("url", [URL.replace("127.0.0.1", "remote.test"), URL.replace("65432", "5432"),
    URL.replace("company_knowledge", "another_project"), URL + "?host=remote.test", URL + "?service=other", URL + "?options=evil"])
def test_unexpected_database_targets_fail_before_subprocess(url):
    with pytest.raises(ValueError):
        helper.connection_parts(url)


def test_dryrun_never_reads_env_creates_files_or_connects(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(helper, "run_check", lambda *args: pytest.fail("dryrun executed"))
    target = tmp_path / "backup"
    assert helper.main(["--env-file", str(tmp_path / "missing.env"), "--out-dir", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
    assert not target.exists()


def test_database_password_only_enters_subprocess_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(helper.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    helper.command(["pg_dump", "--dbname=company_knowledge"], helper.connection_parts(URL), stdout=io.BytesIO())
    args, kwargs = calls[0]
    assert "synthetic-secret" not in repr(args)
    assert kwargs["env"]["PGPASSWORD"] == "synthetic-secret"
    assert "rag-company-postgres" in args


@pytest.mark.parametrize("restore_fails", [False, True])
def test_restore_only_cleans_its_created_database_and_retains_private_backup(tmp_path, monkeypatch, capsys, restore_fails):
    commands, connections = [], []

    class SQL(str):
        def format(self, value):
            return SQL(str(self).format(value))

    class Connection:
        closed = False

        def execute(self, query):
            commands.append(str(query))
            return SimpleNamespace(fetchone=lambda: ("synthetic-snapshot",))

        def close(self):
            self.closed = True

        def rollback(self):
            pass

    def connect(*args, **kwargs):
        value = Connection()
        connections.append(value)
        return value

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect, sql=SimpleNamespace(SQL=SQL, Identifier=lambda name: name)))
    monkeypatch.setattr(helper, "database_fingerprint", lambda conn: {"table_rows": {"public.knowledge_assets": 2}, "catalogue_sha256": {"knowledge_assets": "synthetic"}})

    def run(arguments, parsed, **kwargs):
        if arguments[0] == "pg_dump":
            assert "--snapshot=synthetic-snapshot" in arguments
            assert not any(arg.startswith("--table") for arg in arguments)
            kwargs["stdout"].write(b"synthetic-whole-database-dump")
        elif restore_fails:
            raise RuntimeError("synthetic-secret-in-driver-error")

    monkeypatch.setattr(helper, "command", run)
    environment = tmp_path / "source.env"
    environment.write_text("KNOWLEDGE_DATABASE_URL=" + URL)
    target = tmp_path / "backup"
    assert helper.run_check(environment, target) == (1 if restore_fails else 0)
    summary = json.loads(capsys.readouterr().out)
    assert summary["validation_database_removed"]
    assert "synthetic-secret" not in str(summary)
    created = next(query.split()[2] for query in commands if query.startswith("CREATE DATABASE "))
    assert created.startswith("receipt_restore_")
    assert [query for query in commands if query.startswith("DROP DATABASE ")] == ["DROP DATABASE " + created]
    assert all(connection.closed for connection in connections)
    assert target.stat().st_mode & 0o777 == 0o700
    assert (target / "database.dump").stat().st_mode & 0o777 == 0o600
    assert (target / "restore-manifest.json").stat().st_mode & 0o777 == 0o600
