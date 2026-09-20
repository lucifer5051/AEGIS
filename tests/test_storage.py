"""AEGIS Local SQLite Storage Foundation - Deterministic Unit & Smoke Tests.

Prompt: 011
Scope: Validates path safety boundaries, connection lifecycle, PRAGMA policies,
transaction atomicity, monotonic migrations, checksum verification, destructive
migration backup hooks, 12-point health evaluation, and zero-secret invariants.
100% offline, zero network access, zero domain tables, temporary databases only.
"""

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config.models import (
    StoragePolicyConfig,
)
from src.storage.database import (
    SQLitePragmaConfig,
    create_connection,
    open_database,
    transaction,
)
from src.storage.errors import (
    ChecksumMismatchError,
    DestructiveMigrationError,
    ForbiddenDatabaseLocationError,
    MigrationError,
    TransactionError,
)
from src.storage.health import (
    DatabaseHealthState,
    check_database_health,
)
from src.storage.migrations import (
    Migration,
    apply_migrations,
    bootstrap_schema_migrations,
    compute_sql_checksum,
    get_applied_migrations,
    get_builtin_migrations,
    get_current_schema_version,
)
from src.storage.paths import (
    get_default_aegis_home,
    get_default_backups_dir,
    get_default_cache_dir,
    get_default_data_dir,
    get_default_db_path,
    get_default_logs_dir,
    get_default_runtime_dir,
    is_path_inside_onedrive,
    is_path_inside_recovery_repo,
    is_path_inside_repo,
    resolve_database_path,
    validate_database_path,
)


@pytest.fixture
def temp_db_dir(tmp_path: Path) -> Path:
    """Provide a clean isolated temporary directory outside OneDrive/repo."""
    db_dir = tmp_path / "sqlite_test_data"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir


@pytest.fixture
def temp_db_path(temp_db_dir: Path) -> Path:
    """Path to temporary test database."""
    return temp_db_dir / "test_aegis.db"


# ==============================================================================
# 1. PATH RESOLUTION & SAFETY TESTS
# ==============================================================================


def test_01_default_path_outside_onedrive():
    """Assert canonical default DB path is strictly outside any OneDrive directory."""
    default_path = get_default_db_path()
    assert not is_path_inside_onedrive(default_path)
    assert "onedrive" not in str(default_path).lower()


def test_02_default_path_outside_repo():
    """Assert canonical default DB path is outside Git source repository."""
    default_path = get_default_db_path()
    assert not is_path_inside_repo(default_path)


def test_03_default_path_outside_recovery():
    """Assert canonical default DB path is outside C:\\AEGIS recovery directory."""
    default_path = get_default_db_path()
    assert not is_path_inside_recovery_repo(default_path)


def test_04_default_directories_defined():
    """Assert all runtime directory helpers resolve under .aegis home."""
    home = get_default_aegis_home()
    assert get_default_data_dir() == home / "data"
    assert get_default_runtime_dir() == home / "runtime"
    assert get_default_logs_dir() == home / "logs"
    assert get_default_cache_dir() == home / "cache"
    assert get_default_backups_dir() == home / "backups"


def test_05_custom_valid_path(temp_db_path: Path):
    """Assert valid custom path outside forbidden trees resolves cleanly."""
    resolved = resolve_database_path(custom_path=temp_db_path, ensure_dir=False)
    assert resolved == temp_db_path.resolve()


def test_06_custom_path_parent_creation(temp_db_dir: Path):
    """Assert parent directory creation when ensure_dir=True."""
    nested = temp_db_dir / "nested" / "sub" / "db.sqlite"
    assert not nested.parent.exists()
    resolved = resolve_database_path(custom_path=nested, ensure_dir=True)
    assert resolved.parent.is_dir()


def test_07_forbidden_onedrive_path_rejected():
    """Assert paths inside OneDrive raise ForbiddenDatabaseLocationError."""
    forbidden = Path("C:/Users/test/OneDrive/data/aegis.db")
    with pytest.raises(ForbiddenDatabaseLocationError) as exc_info:
        validate_database_path(forbidden)
    assert "OneDrive" in str(exc_info.value)


def test_08_forbidden_repo_path_rejected():
    """Assert paths inside canonical Git repo raise ForbiddenDatabaseLocationError."""
    canonical_root = Path(__file__).resolve().parent.parent
    forbidden = canonical_root / "data" / "aegis.db"
    with pytest.raises(ForbiddenDatabaseLocationError) as exc_info:
        validate_database_path(forbidden)
    assert "Git source repository" in str(exc_info.value)


def test_09_forbidden_recovery_path_rejected():
    """Assert paths inside C:\\AEGIS raise ForbiddenDatabaseLocationError."""
    forbidden = Path("C:/AEGIS/data/aegis.db")
    with pytest.raises(ForbiddenDatabaseLocationError) as exc_info:
        validate_database_path(forbidden)
    assert "C:\\AEGIS" in str(exc_info.value)


def test_10_no_hardcoded_username():
    """Assert default path uses user profile dynamically without hard-coded usernames."""
    default_home = get_default_aegis_home()
    assert default_home == (Path.home() / ".aegis").resolve()


def test_11_windows_path_normalization(temp_db_path: Path):
    """Assert Windows forward/back slashes normalize to absolute Path object."""
    raw_str = str(temp_db_path).replace("/", "\\")
    resolved = validate_database_path(raw_str)
    assert resolved.is_absolute()
    assert resolved == temp_db_path.resolve()


# ==============================================================================
# 2. CONNECTION MANAGEMENT TESTS
# ==============================================================================


def test_12_connection_opens_file_based(temp_db_path: Path):
    """Assert file-backed SQLite connection opens and can execute query."""
    conn = create_connection(temp_db_path)
    try:
        row = conn.execute("SELECT 1 AS num;").fetchone()
        assert row["num"] == 1
    finally:
        conn.close()


def test_13_foreign_keys_on(temp_db_path: Path):
    """Assert PRAGMA foreign_keys is strictly enabled."""
    conn = create_connection(temp_db_path)
    try:
        row = conn.execute("PRAGMA foreign_keys;").fetchone()
        assert row[0] == 1
    finally:
        conn.close()


def test_14_wal_mode_configured(temp_db_path: Path):
    """Assert file-backed database sets and operates in WAL journal mode."""
    conn = create_connection(temp_db_path)
    try:
        row = conn.execute("PRAGMA journal_mode;").fetchone()
        assert str(row[0]).lower() == "wal"
    finally:
        conn.close()


def test_15_synchronous_mode(temp_db_path: Path):
    """Assert synchronous mode matches configured pragma setting."""
    cfg = SQLitePragmaConfig(synchronous="NORMAL")
    conn = create_connection(temp_db_path, pragma_config=cfg)
    try:
        row = conn.execute("PRAGMA synchronous;").fetchone()
        assert row[0] == 1  # 1 = NORMAL in SQLite
    finally:
        conn.close()


def test_16_busy_timeout(temp_db_path: Path):
    """Assert busy timeout pragma is configured to specified milliseconds."""
    cfg = SQLitePragmaConfig(busy_timeout_ms=7500)
    conn = create_connection(temp_db_path, pragma_config=cfg)
    try:
        row = conn.execute("PRAGMA busy_timeout;").fetchone()
        assert row[0] == 7500
    finally:
        conn.close()


def test_17_temp_store(temp_db_path: Path):
    """Assert temp_store pragma directs scratch buffers to MEMORY."""
    cfg = SQLitePragmaConfig(temp_store="MEMORY")
    conn = create_connection(temp_db_path, pragma_config=cfg)
    try:
        row = conn.execute("PRAGMA temp_store;").fetchone()
        assert row[0] == 2  # 2 = MEMORY in SQLite
    finally:
        conn.close()


def test_18_row_factory(temp_db_path: Path):
    """Assert row factory supports dict-like column access."""
    conn = create_connection(temp_db_path)
    try:
        row = conn.execute("SELECT 'alpha' AS key, 42 AS val;").fetchone()
        assert row["key"] == "alpha"
        assert row["val"] == 42
    finally:
        conn.close()


def test_19_repeated_open_close(temp_db_path: Path):
    """Assert repeatedly opening and closing database file is robust and corruption-free."""
    for i in range(5):
        with open_database(temp_db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS t_loop (id INT PRIMARY KEY, n INT);")
            conn.execute(f"INSERT OR REPLACE INTO t_loop VALUES ({i}, {i * 10});")
    with open_database(temp_db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM t_loop;").fetchone()[0]
        assert count == 5


def test_20_in_memory_connection():
    """Assert in-memory connection initializes cleanly with MEMORY journal mode."""
    conn = create_connection(":memory:")
    try:
        jm = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert str(jm).lower() == "memory"
        fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert fk == 1
    finally:
        conn.close()


def test_21_open_database_context_manager(temp_db_path: Path):
    """Assert scoped open_database context manager automatically closes connection."""
    captured_conn = None
    with open_database(temp_db_path) as conn:
        captured_conn = conn
        assert conn.execute("SELECT 1;").fetchone()[0] == 1
    # Check connection is closed (executing on closed connection raises ProgrammingError)
    with pytest.raises(sqlite3.ProgrammingError):
        captured_conn.execute("SELECT 1;")


# ==============================================================================
# 3. TRANSACTION TESTS
# ==============================================================================


def test_22_transaction_commit(temp_db_path: Path):
    """Assert transaction commits changes on normal block completion."""
    with open_database(temp_db_path) as conn:
        conn.execute("CREATE TABLE t_tx (id INT PRIMARY KEY, val TEXT);")
        with transaction(conn):
            conn.execute("INSERT INTO t_tx VALUES (1, 'persisted');")

        row = conn.execute("SELECT val FROM t_tx WHERE id = 1;").fetchone()
        assert row["val"] == "persisted"


def test_23_transaction_rollback(temp_db_path: Path):
    """Assert transaction rolls back uncommitted changes on exception."""
    with open_database(temp_db_path) as conn:
        conn.execute("CREATE TABLE t_rb (id INT PRIMARY KEY, val TEXT);")
        with pytest.raises(RuntimeError):
            with transaction(conn):
                conn.execute("INSERT INTO t_rb VALUES (1, 'rolled_back');")
                raise RuntimeError("Simulated failure inside transaction")

        count = conn.execute("SELECT COUNT(*) FROM t_rb;").fetchone()[0]
        assert count == 0


def test_24_transaction_exception_preservation(temp_db_path: Path):
    """Assert original exception type and message are strictly preserved."""
    with open_database(temp_db_path) as conn:
        with pytest.raises(ValueError, match="Exact original message preserved"):
            with transaction(conn):
                raise ValueError("Exact original message preserved")


def test_25_nested_transaction_rejection(temp_db_path: Path):
    """Assert nested transaction invocation raises TransactionError."""
    with open_database(temp_db_path) as conn:
        with transaction(conn):
            with pytest.raises(TransactionError, match="Nested transaction rejected"):
                with transaction(conn):
                    pass


def test_26_transaction_isolation(temp_db_path: Path):
    """Assert uncommitted transaction in one connection is not visible until commit."""
    conn1 = create_connection(temp_db_path)
    conn2 = create_connection(temp_db_path)
    try:
        conn1.execute("CREATE TABLE t_iso (id INT PRIMARY KEY, val TEXT);")
        with transaction(conn1):
            conn1.execute("INSERT INTO t_iso VALUES (1, 'staged');")
            # conn2 should not see uncommitted insert
            row = conn2.execute("SELECT * FROM t_iso WHERE id = 1;").fetchone()
            assert row is None
        # Once committed, conn2 sees it
        row = conn2.execute("SELECT val FROM t_iso WHERE id = 1;").fetchone()
        assert row["val"] == "staged"
    finally:
        conn1.close()
        conn2.close()


def test_27_repeated_transactions(temp_db_path: Path):
    """Assert sequential transactions on single connection execute cleanly."""
    with open_database(temp_db_path) as conn:
        conn.execute("CREATE TABLE t_seq (id INT PRIMARY KEY);")
        for i in range(10):
            with transaction(conn):
                conn.execute(f"INSERT INTO t_seq VALUES ({i});")
        count = conn.execute("SELECT COUNT(*) FROM t_seq;").fetchone()[0]
        assert count == 10


# ==============================================================================
# 4. MIGRATION FRAMEWORK TESTS
# ==============================================================================


def test_28_fresh_database_bootstrap(temp_db_path: Path):
    """Assert schema_migrations is bootstrapped directly on fresh database."""
    with open_database(temp_db_path) as conn:
        bootstrap_schema_migrations(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations';"
        ).fetchone()
        assert row is not None
        assert get_current_schema_version(conn) == 0


def test_29_first_migration_execution(temp_db_path: Path):
    """Assert migration 0001 applies, creates storage_metadata, and records version 1."""
    builtin = get_builtin_migrations()
    assert len(builtin) >= 1
    m1 = builtin[0]
    assert m1.version == 1
    assert m1.name == "initial_storage"

    with open_database(temp_db_path) as conn:
        applied = apply_migrations(conn, [m1])
        assert len(applied) == 1
        assert applied[0].version == 1
        assert get_current_schema_version(conn) == 1

        # Verify storage_metadata table was created
        meta_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='storage_metadata';"
        ).fetchone()
        assert meta_table is not None


def test_30_second_test_migration(temp_db_path: Path):
    """Assert applying a second migration advances version monotonically."""
    m1 = get_builtin_migrations()[0]
    sql2 = "CREATE TABLE test_table_2 (id INT PRIMARY KEY, desc TEXT);"
    m2 = Migration(
        version=2,
        migration_id="0002_test_table_2",
        name="test_table_2",
        sql=sql2,
        checksum=compute_sql_checksum(sql2),
        is_destructive=False,
    )

    with open_database(temp_db_path) as conn:
        apply_migrations(conn, [m1, m2])
        assert get_current_schema_version(conn) == 2
        applied = get_applied_migrations(conn)
        assert len(applied) == 2
        assert applied[1].migration_id == "0002_test_table_2"


def test_31_already_applied_skipped(temp_db_path: Path):
    """Assert re-running apply_migrations skips previously applied migrations without error."""
    m1 = get_builtin_migrations()[0]
    with open_database(temp_db_path) as conn:
        apply_migrations(conn, [m1])
        # Re-apply same migration set
        applied = apply_migrations(conn, [m1])
        assert len(applied) == 1
        assert get_current_schema_version(conn) == 1


def test_32_migration_monotonic_ordering(temp_db_path: Path):
    """Assert migrations with descending or out-of-order versions are rejected."""
    m_a = Migration(version=2, migration_id="0002_a", name="a", sql="SELECT 1;", checksum="c1")
    m_b = Migration(version=1, migration_id="0001_b", name="b", sql="SELECT 1;", checksum="c2")

    with open_database(temp_db_path) as conn:
        with pytest.raises(MigrationError, match="strictly ordered by ascending version"):
            apply_migrations(conn, [m_a, m_b])


def test_33_duplicate_version_rejected(temp_db_path: Path):
    """Assert duplicate migration versions raise MigrationError."""
    m1 = Migration(version=1, migration_id="0001_a", name="a", sql="SELECT 1;", checksum="c1")
    m2 = Migration(version=1, migration_id="0001_b", name="b", sql="SELECT 1;", checksum="c2")

    with open_database(temp_db_path) as conn:
        with pytest.raises(MigrationError, match="Duplicate migration version"):
            apply_migrations(conn, [m1, m2])


def test_34_duplicate_migration_id_rejected(temp_db_path: Path):
    """Assert duplicate migration IDs raise MigrationError."""
    m1 = Migration(version=1, migration_id="dup_id", name="a", sql="SELECT 1;", checksum="c1")
    m2 = Migration(version=2, migration_id="dup_id", name="b", sql="SELECT 1;", checksum="c2")

    with open_database(temp_db_path) as conn:
        with pytest.raises(MigrationError, match="Duplicate migration ID"):
            apply_migrations(conn, [m1, m2])


def test_35_checksum_verification_success(temp_db_path: Path):
    """Assert identical checksum across multiple startups validates successfully."""
    m1 = get_builtin_migrations()[0]
    with open_database(temp_db_path) as conn:
        apply_migrations(conn, [m1])
        # Re-verify with same migration object
        applied = apply_migrations(conn, [m1])
        assert applied[0].checksum == m1.checksum


def test_36_checksum_mismatch_rejected(temp_db_path: Path):
    """Assert modifying SQL of an already applied migration raises ChecksumMismatchError."""
    m1 = get_builtin_migrations()[0]
    with open_database(temp_db_path) as conn:
        apply_migrations(conn, [m1])

        # Tampered migration with different SQL but same ID and version
        tampered_sql = m1.sql + "\n-- tampered comment"
        tampered = Migration(
            version=m1.version,
            migration_id=m1.migration_id,
            name=m1.name,
            sql=tampered_sql,
            checksum=compute_sql_checksum(tampered_sql),
            is_destructive=m1.is_destructive,
        )
        with pytest.raises(ChecksumMismatchError, match="Checksum mismatch"):
            apply_migrations(conn, [tampered])


def test_37_migration_failure_rollback(temp_db_path: Path):
    """Assert invalid SQL inside migration rolls back and remains unapplied."""
    m1 = get_builtin_migrations()[0]
    broken_sql = "CREATE TABLE valid (id INT); INVALID SQL STATEMENT;"
    m_broken = Migration(
        version=2,
        migration_id="0002_broken",
        name="broken",
        sql=broken_sql,
        checksum=compute_sql_checksum(broken_sql),
    )

    with open_database(temp_db_path) as conn:
        apply_migrations(conn, [m1])
        with pytest.raises(MigrationError, match="Execution failed for migration '0002_broken'"):
            apply_migrations(conn, [m1, m_broken])

        # Assert schema_migrations still only has version 1
        assert get_current_schema_version(conn) == 1
        applied = get_applied_migrations(conn)
        assert len(applied) == 1


def test_38_failed_migration_absent_from_applied(temp_db_path: Path):
    """Assert failed migration is completely absent from schema_migrations records."""
    m_broken = Migration(
        version=1,
        migration_id="0001_bad",
        name="bad",
        sql="SYNTAX ERROR HERE;",
        checksum="bad_checksum",
    )
    with open_database(temp_db_path) as conn:
        with pytest.raises(MigrationError):
            apply_migrations(conn, [m_broken])
        applied = get_applied_migrations(conn)
        assert len(applied) == 0


def test_39_schema_version_query(temp_db_path: Path):
    """Assert get_current_schema_version returns accurate version after sequential migrations."""
    with open_database(temp_db_path) as conn:
        assert get_current_schema_version(conn) == 0
        m1 = get_builtin_migrations()[0]
        apply_migrations(conn, [m1])
        assert get_current_schema_version(conn) == 1


def test_40_destructive_migration_with_successful_backup_hook(temp_db_path: Path):
    """Assert destructive migration succeeds when backup hook returns True."""
    hook_called = False

    def mock_backup(db_p: Path) -> bool:
        nonlocal hook_called
        hook_called = True
        return True

    sql_dest = "CREATE TABLE dest_table (id INT);"
    m_dest = Migration(
        version=1,
        migration_id="0001_dest",
        name="dest",
        sql=sql_dest,
        checksum=compute_sql_checksum(sql_dest),
        is_destructive=True,
    )

    with open_database(temp_db_path) as conn:
        apply_migrations(conn, [m_dest], backup_hook=mock_backup, database_path=temp_db_path)
        assert hook_called is True
        assert get_current_schema_version(conn) == 1


def test_41_destructive_migration_without_hook_rejected(temp_db_path: Path):
    """Assert destructive migration without backup hook raises DestructiveMigrationError."""
    sql_dest = "CREATE TABLE dest_no_hook (id INT);"
    m_dest = Migration(
        version=1,
        migration_id="0001_dest_no_hook",
        name="dest_no_hook",
        sql=sql_dest,
        checksum=compute_sql_checksum(sql_dest),
        is_destructive=True,
    )

    with open_database(temp_db_path) as conn:
        with pytest.raises(DestructiveMigrationError, match="requires a registered backup hook"):
            apply_migrations(conn, [m_dest], backup_hook=None, database_path=temp_db_path)


def test_42_destructive_migration_with_failed_hook_rejected(temp_db_path: Path):
    """Assert destructive migration with failed backup hook (returning False) aborts."""
    sql_dest = "CREATE TABLE dest_fail (id INT);"
    m_dest = Migration(
        version=1,
        migration_id="0001_dest_fail",
        name="dest_fail",
        sql=sql_dest,
        checksum=compute_sql_checksum(sql_dest),
        is_destructive=True,
    )

    with open_database(temp_db_path) as conn:
        with pytest.raises(DestructiveMigrationError, match="Backup precondition failed"):
            apply_migrations(
                conn, [m_dest], backup_hook=lambda p: False, database_path=temp_db_path
            )
        assert get_current_schema_version(conn) == 0


# ==============================================================================
# 5. HEALTH CHECK & DIAGNOSTICS TESTS
# ==============================================================================


def test_43_healthy_database(temp_db_path: Path):
    """Assert properly migrated database returns HEALTHY state."""
    with open_database(temp_db_path) as conn:
        apply_migrations(conn, get_builtin_migrations())

    report = check_database_health(temp_db_path)
    assert report.state == DatabaseHealthState.HEALTHY
    assert report.exists is True
    assert report.can_connect is True
    assert report.foreign_keys_enabled is True
    assert report.journal_mode == "WAL"
    assert report.integrity_check_passed is True
    assert report.read_write_verified is True
    assert report.schema_version >= 1


def test_44_unavailable_nonexistent_database(temp_db_dir: Path):
    """Assert health check on nonexistent database file returns UNAVAILABLE."""
    nonexistent = temp_db_dir / "does_not_exist.db"
    report = check_database_health(nonexistent)
    assert report.state == DatabaseHealthState.UNAVAILABLE
    assert report.exists is False
    assert report.can_connect is False


def test_45_corrupt_database_detection(temp_db_path: Path):
    """Assert corrupted database file is detected and marked CORRUPT."""
    # Write garbage content to simulate severe file corruption
    temp_db_path.write_bytes(b"THIS IS NOT A VALID SQLITE FILE CORRUPT HEADER DATA 0123456789")
    report = check_database_health(temp_db_path)
    assert report.state == DatabaseHealthState.CORRUPT
    assert report.integrity_check_passed is False


def test_46_health_foreign_keys_and_journal_mode(temp_db_path: Path):
    """Assert health probe correctly reads foreign keys and journal mode attributes."""
    with open_database(temp_db_path) as conn:
        apply_migrations(conn, get_builtin_migrations())

    report = check_database_health(temp_db_path)
    assert report.foreign_keys_enabled is True
    assert report.journal_mode == "WAL"
    assert report.synchronous in ("NORMAL", "1")


def test_47_health_integrity_check_passed(temp_db_path: Path):
    """Assert PRAGMA integrity_check verifies structural cleanliness."""
    with open_database(temp_db_path) as conn:
        apply_migrations(conn, get_builtin_migrations())

    report = check_database_health(temp_db_path)
    assert report.integrity_check_passed is True


def test_48_health_read_write_verification(temp_db_path: Path):
    """Assert health check verifies read/write capability via temp table probe."""
    with open_database(temp_db_path) as conn:
        apply_migrations(conn, get_builtin_migrations())

    report = check_database_health(temp_db_path)
    assert report.read_write_verified is True


# ==============================================================================
# 6. SECURITY & BOUNDARY INVARIANT TESTS
# ==============================================================================


def test_49_no_secrets_in_storage_metadata(temp_db_path: Path):
    """Assert storage_metadata table contains no passwords, API keys, or tokens."""
    with open_database(temp_db_path) as conn:
        apply_migrations(conn, get_builtin_migrations())
        # Insert test non-secret metadata
        conn.execute(
            "INSERT INTO storage_metadata (key, value, created_at, updated_at) "
            "VALUES ('storage_version', '1.0.0', '2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z');"
        )
        rows = conn.execute("SELECT key, value FROM storage_metadata;").fetchall()
        for r in rows:
            lower_k = r["key"].lower()
            lower_v = r["value"].lower()
            assert "secret" not in lower_k and "secret" not in lower_v
            assert "password" not in lower_k and "password" not in lower_v
            assert "token" not in lower_k and "token" not in lower_v
            assert "api_key" not in lower_k and "api_key" not in lower_v


def test_50_no_secrets_in_storage_config():
    """Assert StoragePolicyConfig rejects secret heuristic injection."""
    with pytest.raises(ValidationError):
        StoragePolicyConfig(api_key="sk-live-123456789")  # type: ignore[call-arg]


def test_51_zero_domain_tables(temp_db_path: Path):
    """Assert only schema_migrations and storage_metadata exist in database."""
    with open_database(temp_db_path) as conn:
        apply_migrations(conn, get_builtin_migrations())
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            ).fetchall()
        ]
        assert set(tables) == {"schema_migrations", "storage_metadata"}

        # Explicitly verify business tables are absent
        forbidden_tables = [
            "projects",
            "messages",
            "memories",
            "tasks",
            "workflows",
            "audit_records",
            "permissions",
            "benchmarks",
            "credentials",
        ]
        for ft in forbidden_tables:
            assert ft not in tables


# ==============================================================================
# 7. REAL TEMPORARY DATABASE SMOKE TEST
# ==============================================================================


def test_52_live_storage_smoke_test(temp_db_dir: Path):
    """Execute complete end-to-end lifecycle on a temporary SQLite database."""
    smoke_db = temp_db_dir / "smoke_lifecycle.db"
    assert not smoke_db.exists()

    # 1. Resolve path
    resolved_path = resolve_database_path(custom_path=smoke_db, ensure_dir=True)
    assert resolved_path == smoke_db.resolve()

    # 2. Open connection & bootstrap
    with open_database(smoke_db) as conn:
        bootstrap_schema_migrations(conn)
        assert get_current_schema_version(conn) == 0

        # 3. Apply migration 0001
        apply_migrations(conn, get_builtin_migrations())
        assert get_current_schema_version(conn) == 1

        # 4. Commit transaction in storage_metadata
        with transaction(conn) as cur:
            cur.execute(
                "INSERT INTO storage_metadata (key, value, created_at, updated_at) "
                "VALUES ('smoke_test', 'passed', '2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z');"
            )

        # 5. Rollback transaction in storage_metadata
        try:
            with transaction(conn) as cur:
                cur.execute(
                    "INSERT INTO storage_metadata (key, value, created_at, updated_at) "
                    "VALUES ('fail_test', 'failed', '2026-09-20T00:00:00Z', "
                    "'2026-09-20T00:00:00Z');"
                )
                raise RuntimeError("Abort rollback probe")
        except RuntimeError:
            pass

        # Verify smoke_test committed and fail_test rolled back
        row_ok = conn.execute(
            "SELECT value FROM storage_metadata WHERE key = 'smoke_test';"
        ).fetchone()
        row_fail = conn.execute(
            "SELECT value FROM storage_metadata WHERE key = 'fail_test';"
        ).fetchone()
        assert row_ok["value"] == "passed"
        assert row_fail is None

    # 6. Check health on closed database
    report = check_database_health(smoke_db)
    assert report.state == DatabaseHealthState.HEALTHY
    assert report.integrity_check_passed is True

    # 7. Clean up files
    for ext in ("", "-wal", "-shm"):
        f = Path(str(smoke_db) + ext)
        if f.exists():
            f.unlink()
    assert not smoke_db.exists()
