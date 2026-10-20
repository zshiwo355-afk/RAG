"""The migration entrypoint must never pick an implicit local database."""
import importlib.util
import json
from pathlib import Path

import pytest

from test_knowledge_processing import system

pytestmark = pytest.mark.offline


def command():
    path = Path(__file__).resolve().parents[1] / "scripts/knowledge_processing.py"
    spec = importlib.util.spec_from_file_location("role_migration_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("operation", ["init-role-assignments", "migrate-role-assignments", "review-published-roles"])
def test_migration_refuses_implicit_or_missing_database(tmp_path, capsys, operation):
    cli = command()
    assert cli.main([operation]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "explicit_existing_database_required"
    missing = tmp_path / "must-not-create.db"
    assert cli.main(["--sqlite", str(missing), operation]) == 1
    assert not missing.exists()


@pytest.mark.parametrize("operation,method", [("init-role-assignments", "initialize_role_assignments"),
                                              ("migrate-role-assignments", "migrate_legacy_item_roles")])
def test_migration_preview_does_not_apply_or_run_worker(tmp_path, monkeypatch, capsys, operation, method):
    cli = command()
    target = tmp_path / "existing.db"
    target.write_bytes(b"existing-database")
    calls = []

    class Store:
        def __init__(self, path):
            assert path == target

    def invoke(self, *, apply=False):
        calls.append(apply)
        return {"applied": apply, "items": []}

    setattr(Store, method, invoke)
    monkeypatch.setattr(cli, "ProcessingStore", Store)
    assert cli.main(["--sqlite", str(target), operation]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
    assert calls == [False]
    assert cli.main(["--sqlite", str(target), operation, "--execute"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    assert calls == [False, True]
    assert target.read_bytes() == b"existing-database"


def test_published_review_has_no_write_option_and_checks_page_bounds(tmp_path, capsys):
    cli = command()
    with pytest.raises(SystemExit):
        cli.main(["review-published-roles", "--execute"])
    capsys.readouterr()
    target = tmp_path / "existing.db"
    target.write_bytes(b"existing-database")
    for page in (["--limit", "0"], ["--limit", "1001"], ["--offset", "-1"]):
        assert cli.main(["--sqlite", str(target), "review-published-roles", *page]) == 1
        assert json.loads(capsys.readouterr().out)["error_code"] == "invalid_review_page"
    assert target.read_bytes() == b"existing-database"


def test_cli_previews_and_restores_a_real_legacy_selection_without_reupload(system, capsys):
    from rag_app.knowledge_item_assignments import TABLES
    from test_knowledge_processing_phase3 import enable, receipt_job
    from test_knowledge_uploader_role_selection import blocked
    from test_knowledge_uploader_roles import published_record

    enable(system)
    role = system.catalogue.register_role("共享中心", "流程专员")
    receipt, job = blocked(system)
    with system.store._connection(write=True) as connection:
        system.store._event(connection, job["job_id"], "uploader_role_selected", role["role_id"], "original-reviewer")
        for table in TABLES:
            connection.execute("DROP TABLE " + table)
    cli = command()
    before = receipt_job(system, receipt)
    original_receipt = system.receipts.get(receipt["principal"], receipt["receipt_id"])
    arguments = ["--sqlite", str(system.store.path)]
    assert cli.main([*arguments, "init-role-assignments"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert not preview["ready"] and not preview["applied"]
    assert receipt_job(system, receipt) == before
    assert cli.main([*arguments, "init-role-assignments", "--execute"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"]
    assert receipt_job(system, receipt) == before
    assert cli.main([*arguments, "migrate-role-assignments"]) == 0
    migration = json.loads(capsys.readouterr().out)
    assert migration["counts"]["eligible"] == 1
    assert migration["items"][0]["will_requeue"] is True
    assert receipt_job(system, receipt) == before
    assert cli.main([*arguments, "migrate-role-assignments", "--execute"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] and applied["items"][0]["requeued"] is True
    assert system.service.run_once()
    record = published_record(system, receipt)
    assert record["role_id"] == role["role_id"]
    assert system.receipts.get(receipt["principal"], receipt["receipt_id"]) == original_receipt
    assert cli.main([*arguments, "migrate-role-assignments"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["already_migrated"] == 1
    assert cli.main([*arguments, "review-published-roles"]) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["status"] == "read_only" and review["scanned_count"] == 1 and not review["has_more"]
    assert system.catalogue.get_published(record["knowledge_id"]) == record
