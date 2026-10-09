#!/usr/bin/env python3
"""Back up the RAG database and verify restoration in a newly created database."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit
from uuid import uuid4


CONTAINER = "rag-company-postgres"
SOURCE_DATABASE = "company_knowledge"


def connection_parts(url):
    parsed = urlsplit(url)
    allowed_query = {"sslmode", "connect_timeout", "application_name"}
    if (parsed.scheme not in {"postgresql", "postgres"} or parsed.hostname != "127.0.0.1"
            or parsed.port != 65432 or parsed.path != "/" + SOURCE_DATABASE or parsed.fragment
            or not parsed.username or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", unquote(parsed.username))
            or any(key not in allowed_query for key, _ in parse_qsl(parsed.query))):
        raise ValueError("unexpected_source_database")
    return parsed


def command(arguments, parsed, *, stdin=None, stdout=None):
    # Docker imports these two environment values by name; no credential is an argv value.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment.update(PGUSER=unquote(parsed.username), PGPASSWORD=unquote(parsed.password or ""))
    result = subprocess.run(
        ["docker", "exec", "-i", "--env", "PGUSER", "--env", "PGPASSWORD",
         "--env", "PGHOST=/var/run/postgresql", "--env", "PGPORT=5432", CONTAINER, *arguments],
        stdin=stdin, stdout=stdout if stdout is not None else subprocess.DEVNULL,
        stderr=subprocess.PIPE, env=environment, timeout=600, check=False,
    )
    if result.returncode:
        raise RuntimeError("native_postgres_command_failed")


def database_fingerprint(connection):
    from psycopg import sql

    connection.execute("SET LOCAL TIME ZONE 'UTC'")
    tables = connection.execute("""SELECT n.nspname, c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
        ORDER BY n.nspname, c.relname""").fetchall()
    counts = {}
    for schema, table in tables:
        count = connection.execute(sql.SQL("SELECT COUNT(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))).fetchone()[0]
        counts[schema + "." + table] = count
    if not {"public.knowledge_assets", "public.knowledge_revisions"}.issubset(counts):
        raise RuntimeError("source_catalogue_not_found")
    hashes = {}
    for table, order in (("knowledge_assets", "knowledge_id"), ("knowledge_revisions", "knowledge_id, revision"),
                         ("knowledge_upload_receipts", "receipt_id")):
        if "public." + table not in counts:
            continue
        digest = hashlib.sha256()
        rows = connection.execute(sql.SQL("SELECT row_to_json(t)::text FROM public.{} t ORDER BY " + order).format(sql.Identifier(table)))
        for row in rows:
            digest.update(row[0].encode("utf-8") + b"\n")
        hashes[table] = digest.hexdigest()
    return {"table_rows": counts, "catalogue_sha256": hashes}


def save_manifest(directory, value):
    path = directory / "restore-manifest.json"
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def run_check(env_file, directory, *, check_receipts=False):
    from dotenv import dotenv_values
    import psycopg
    from psycopg import sql

    # Only the explicitly selected file supplies the source URL; shell defaults cannot redirect it.
    values = dotenv_values(env_file)
    url = values.get("KNOWLEDGE_DATABASE_URL") or ""
    parsed = connection_parts(url)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    directory.chmod(0o700)
    validation_db = "receipt_restore_" + uuid4().hex
    restored_url = urlunsplit(parsed._replace(path="/" + validation_db))
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "container": CONTAINER,
                "source_database": SOURCE_DATABASE, "validation_database": validation_db,
                "stage": "created", "restore_verified": False, "validation_database_created": False,
                "validation_database_removed": False, "source_catalogue_unchanged": None,
                "receipt_postgres_verified": None}
    save_manifest(directory, manifest)
    source = target = admin = None
    created = False
    problem = cleanup_problem = None
    dump = directory / "database.dump"
    try:
        source = psycopg.connect(url, autocommit=True, connect_timeout=10)
        source.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        source.execute("SET LOCAL statement_timeout='120s'")
        source.execute("SET LOCAL lock_timeout='5s'")
        baseline = database_fingerprint(source)
        snapshot = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
        manifest.update(stage="dump", baseline=baseline)
        save_manifest(directory, manifest)
        descriptor = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            command(["pg_dump", "--no-password", "--dbname=" + SOURCE_DATABASE, "--format=custom", "--no-owner", "--no-privileges",
                     "--snapshot=" + snapshot], parsed, stdout=stream)
        source.rollback()
        source.close()
        source = None
        digest = hashlib.sha256()
        with dump.open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                digest.update(block)
        manifest.update(backup_sha256=digest.hexdigest(), backup_bytes=dump.stat().st_size, stage="create_validation_database")
        save_manifest(directory, manifest)
        admin = psycopg.connect(url, autocommit=True, connect_timeout=10)
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(validation_db)))
        created = True
        manifest.update(validation_database_created=True, stage="restore")
        save_manifest(directory, manifest)
        with dump.open("rb") as stream:
            command(["pg_restore", "--no-password", "--dbname=" + validation_db, "--exit-on-error", "--single-transaction",
                     "--no-owner", "--no-privileges"], parsed, stdin=stream)
        target = psycopg.connect(restored_url, autocommit=True, connect_timeout=10)
        target.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        target.execute("SET LOCAL statement_timeout='120s'")
        restored = database_fingerprint(target)
        if restored != baseline:
            raise RuntimeError("restored_snapshot_mismatch")
        target.rollback()
        target.close()
        target = None
        if check_receipts:
            manifest.update(stage="receipt_postgres_test", receipt_postgres_verified=False)
            save_manifest(directory, manifest)
            root = Path(__file__).resolve().parents[1]
            environment = {**os.environ, "KNOWLEDGE_TEST_DATABASE_URL": restored_url,
                           "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                     str(root / "tests" / "test_knowledge_receipts_postgres.py")],
                                    cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=180, check=False)
            if result.returncode:
                raise RuntimeError("receipt_postgres_test_failed")
            manifest["receipt_postgres_verified"] = True
        manifest.update(restore_verified=True, restored=restored, stage="source_recheck")
        source = psycopg.connect(url, autocommit=True, connect_timeout=10)
        source.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        after = database_fingerprint(source)
        # A concurrent legitimate publication may change the source. The restore still matches its exported snapshot.
        manifest["source_catalogue_unchanged"] = after["catalogue_sha256"] == baseline["catalogue_sha256"]
        source.rollback()
        source.close()
        source = None
    except BaseException as exc:
        problem = exc
        manifest["error_code"] = "backup_verification_failed"
    finally:
        for connection in (source, target):
            if connection is not None:
                connection.close()
        if created:
            try:
                if not re.fullmatch(r"receipt_restore_[0-9a-f]{32}", validation_db) or validation_db == SOURCE_DATABASE:
                    raise RuntimeError("unsafe_cleanup_target")
                if admin is None or admin.closed:
                    admin = psycopg.connect(url, autocommit=True, connect_timeout=10)
                # No --clean and no FORCE: only the random database we successfully created can be dropped.
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(validation_db)))
                manifest["validation_database_removed"] = True
            except Exception as exc:
                cleanup_problem = exc
                manifest["cleanup_error_code"] = "validation_database_cleanup_failed"
        if admin is not None:
            admin.close()
        if problem is None and cleanup_problem is None:
            manifest["stage"] = "verified"
        save_manifest(directory, manifest)
    result = {key: manifest.get(key) for key in ("stage", "backup_sha256", "backup_bytes", "restore_verified",
                                               "source_catalogue_unchanged", "validation_database_removed", "receipt_postgres_verified")}
    result["ok"] = problem is None and cleanup_problem is None
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True, help="New directory under an existing private backup parent.")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--check-receipts", action="store_true", help="Run receipt concurrency tests inside the disposable restored database.")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"ok": True, "status": "dry_run", "source_database": SOURCE_DATABASE, "container": CONTAINER,
                          "source_tables_written": False, "cloud_upload": False, "backup_scope": "entire_database"}))
        return 0
    try:
        return run_check(args.env_file, args.out_dir, check_receipts=args.check_receipts)
    except Exception:
        print(json.dumps({"ok": False, "error_code": "backup_preflight_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
