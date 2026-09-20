"""AEGIS Deterministic Development Environment Validation Test Suite.

Prompt: 005
Scope: Validates that Python 3.12, isolated virtual environment, core developer tooling,
canonical repository structure, and frozen architecture specifications are in strict compliance.
Zero network access is required or permitted during execution of this suite.
"""

import os
import sys
from pathlib import Path

# Core validation dependencies imported to verify installation
import jsonschema  # noqa: F401
import pydantic  # noqa: F401
import yaml  # noqa: F401


def get_canonical_root() -> Path:
    """Resolve and return the canonical repository root path."""
    # File is in tests/, canonical root is its parent directory
    current_file = Path(__file__).resolve()
    repo_root = current_file.parent.parent
    return repo_root


def test_01_python_version_is_3_12():
    """Assert Python major is 3 and minor is 12."""
    assert sys.version_info.major == 3, f"Expected Python 3, got: {sys.version_info.major}"
    assert sys.version_info.minor == 12, f"Expected Python 3.12, got: {sys.version_info.minor}"


def test_02_running_in_isolated_virtual_environment():
    """Assert test is running in the designated isolated virtual environment."""
    # sys.prefix differs from sys.base_prefix inside a virtualenv
    assert sys.prefix != sys.base_prefix, "Execution is not running inside a virtual environment"
    # Verify the environment path contains aegis
    venv_name = os.path.basename(os.path.normpath(sys.prefix))
    assert venv_name == "venv", f"Expected venv folder name, got {venv_name}"
    parent_dir = os.path.basename(os.path.dirname(os.path.normpath(sys.prefix)))
    assert ".aegis" in parent_dir or "aegis" in parent_dir.lower(), (
        f"Expected .aegis venv container, got: {parent_dir}"
    )


def test_03_canonical_repository_root_detected():
    """Assert canonical repository root matches canonical aeigs project path."""
    root = get_canonical_root()
    assert root.exists(), f"Repository root does not exist: {root}"
    # Check folder name is aeigs
    assert root.name.lower() == "aeigs", f"Expected folder name 'aeigs', got: {root.name}"
    # Verify key architectural directories exist
    assert (root / "docs").is_dir()
    assert (root / "schemas").is_dir()
    assert (root / "src").is_dir()
    assert (root / "tests").is_dir()


def test_04_foundation_specification_exists():
    """Assert frozen technical foundation specification exists and is non-empty."""
    spec = (
        get_canonical_root()
        / "docs"
        / "specifications"
        / "AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md"
    )
    assert spec.is_file(), f"Specification not found at: {spec}"
    content = spec.read_text(encoding="utf-8")
    assert "1.0.0-FROZEN" in content, "Specification version 1.0.0-FROZEN missing"
    assert "Trust Core" in content, "Trust Core architecture missing from specification"


def test_05_foundation_manifest_exists():
    """Assert foundation manifest exists with correct canonical root."""
    manifest = get_canonical_root() / "docs" / "specifications" / "AEGIS_FOUNDATION_MANIFEST.md"
    assert manifest.is_file(), f"Manifest not found at: {manifest}"
    content = manifest.read_text(encoding="utf-8")
    assert "C:\\Users\\digvi\\OneDrive\\Desktop\\project\\aeigs" in content


def test_06_database_schema_architecture_exists():
    """Assert database and schema architecture specification exists."""
    db_spec = (
        get_canonical_root() / "docs" / "specifications" / "AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md"
    )
    assert db_spec.is_file(), f"Database schema architecture not found at: {db_spec}"
    content = db_spec.read_text(encoding="utf-8")
    assert "SQLite" in content
    assert "memory_facts" in content


def test_07_data_classification_matrix_exists():
    """Assert data classification matrix exists covering all 5 classes."""
    matrix = (
        get_canonical_root() / "docs" / "specifications" / "AEGIS_DATA_CLASSIFICATION_MATRIX.md"
    )
    assert matrix.is_file(), f"Data classification matrix not found at: {matrix}"
    content = matrix.read_text(encoding="utf-8")
    for cls in [
        "USER_CONTENT",
        "OPERATIONAL_STATE",
        "DERIVED_REBUILDABLE",
        "SECRET_EXCLUDED",
        "TRANSIENT_EXCLUDED",
    ]:
        assert cls in content, f"Data classification {cls} missing from matrix"


def test_08_data_relationship_architecture_exists():
    """Assert data relationships architecture document exists."""
    rel = get_canonical_root() / "docs" / "architecture" / "AEGIS_DATA_RELATIONSHIPS.md"
    assert rel.is_file(), f"Data relationships document not found at: {rel}"
    content = rel.read_text(encoding="utf-8")
    assert "Cascade Deletion" in content or "cascade" in content.lower()


def test_09_schema_architecture_manifest_exists():
    """Assert schema architecture manifest exists."""
    manifest = get_canonical_root() / "schemas" / "SCHEMA_ARCHITECTURE_MANIFEST.md"
    assert manifest.is_file(), f"Schema manifest not found at: {manifest}"
    content = manifest.read_text(encoding="utf-8")
    assert "1.0.0-FROZEN-SCHEMA-SPEC" in content


def test_10_git_repository_exists():
    """Assert valid local Git repository metadata exists."""
    git_dir = get_canonical_root() / ".git"
    assert git_dir.is_dir(), f".git directory not found at: {git_dir}"
    assert (get_canonical_root() / ".gitignore").is_file(), ".gitignore missing"


def test_11_no_model_weights_exist():
    """Assert no AI model weight files (*.gguf, *.bin, *.onnx, *.safetensors) exist."""
    root = get_canonical_root()
    forbidden_extensions = {".gguf", ".bin", ".onnx", ".safetensors", ".pt", ".pth"}
    found_weights = [
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in forbidden_extensions and ".git" not in p.parts
    ]
    assert len(found_weights) == 0, f"Unexpected model weights found: {found_weights}"


def test_12_no_sqlite_database_exists():
    """Assert no SQLite runtime database files (*.sqlite, *.db) exist."""
    root = get_canonical_root()
    forbidden_extensions = {".sqlite", ".sqlite3", ".db", ".wal", ".shm"}
    ignored_dirs = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
    found_dbs = [
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in forbidden_extensions
        and not any(part in ignored_dirs for part in p.parts)
    ]
    assert len(found_dbs) == 0, f"Unexpected SQLite database files found: {found_dbs}"


def test_13_no_unexpected_application_source():
    """Assert only configuration foundation exists in src/, and zero runtime modules exist."""
    src_dir = get_canonical_root() / "src"
    assert src_dir.is_dir(), "src directory missing"
    # In Prompt 010, src/config/, src/hardware/, and src/benchmark/ are authorized
    # (plus standard Python __pycache__).
    allowed_subdirs = {"config", "hardware", "benchmark", "__pycache__"}
    for child in src_dir.iterdir():
        if child.is_dir():
            assert child.name in allowed_subdirs, (
                f"Unexpected application package found in src/: {child.name}"
            )

    forbidden_runtime_packages = {
        "trust_core",
        "manager",
        "skills",
        "storage",
        "memory",
        "scheduler",
        "database",
        "channels",
    }
    for forbidden in forbidden_runtime_packages:
        assert not (src_dir / forbidden).exists(), f"Runtime package {forbidden} must not exist yet"
