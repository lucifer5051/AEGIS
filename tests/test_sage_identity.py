"""SAGE Project Identity Validation Test Suite.

Prompt: 020
Scope: Validates that the official project identity has migrated from AEGIS to SAGE,
ensuring canonical naming in active documentation, package metadata, aliases,
and verifying that legitimate historical/compatibility references are preserved.
"""

import tomllib
from pathlib import Path

from src.config import AegisConfig, SageConfig
from src.storage import get_default_aegis_home, get_default_sage_home


def get_canonical_root() -> Path:
    """Resolve and return the canonical repository root path."""
    return Path(__file__).resolve().parent.parent


def test_01_pyproject_identity_is_sage():
    """pyproject.toml project name and description must reflect SAGE."""
    pyproject_path = get_canonical_root() / "pyproject.toml"
    assert pyproject_path.is_file()
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    assert data["project"]["name"] == "sage"
    assert "SAGE" in data["project"]["description"]
    assert "System for Autonomous Guidance & Execution" in data["project"]["description"]


def test_02_readme_canonical_identity():
    """README.md must prominently establish SAGE as the canonical project."""
    readme_path = get_canonical_root() / "README.md"
    assert readme_path.is_file()
    content = readme_path.read_text(encoding="utf-8")
    assert "# SAGE" in content
    assert "System for Autonomous Guidance & Execution" in content
    # Canonical definition
    assert (
        "SAGE is a personal autonomous AI system that understands user goals, "
        "plans and executes tasks, manages context and memory, coordinates skills "
        "and tools, and operates within deterministic security and permission boundaries."
    ) in content


def test_03_config_model_and_storage_aliases():
    """SageConfig and get_default_sage_home must exist and equal their legacy counterparts."""
    assert SageConfig is AegisConfig
    assert get_default_sage_home is get_default_aegis_home
    # Path resolution returns .aegis for legacy compatibility
    home = get_default_sage_home()
    assert home.name == ".aegis"


def test_04_historical_references_preserved():
    """Historical migration files and frozen specifications must retain their exact filenames."""
    root = get_canonical_root()

    # Migration files 0001 through 0005 must exist under original names
    assert (root / "migrations" / "0001_initial_storage.sql").is_file()
    assert (root / "migrations" / "0002_ekm_schema.sql").is_file()
    assert (root / "migrations" / "0003_conversations_schema.sql").is_file()
    assert (root / "migrations" / "0004_domain_schema_consistency.sql").is_file()
    assert (root / "migrations" / "0005_trust_core_schema.sql").is_file()

    # Frozen specs must exist under historical names
    tech_spec = root / "docs" / "specifications" / "AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md"
    db_spec = root / "docs" / "specifications" / "AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md"
    assert tech_spec.is_file()
    assert db_spec.is_file()


def test_05_activation_scripts_use_sage():
    """Environment activation scripts must announce SAGE."""
    root = get_canonical_root()
    bat_content = (root / "scripts" / "activate_env.bat").read_text(encoding="utf-8")
    ps1_content = (root / "scripts" / "activate_env.ps1").read_text(encoding="utf-8")

    assert "SAGE" in bat_content
    assert "SAGE" in ps1_content
