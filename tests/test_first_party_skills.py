"""Unit and Boundary Tests for First-Party Skills (Prompt 031).

Tests:
- Manifest validation for all 3 first-party skills.
- Tool binding boundary enforcement (SkillToolAccessDeniedError for unlisted tools).
- Handlers execution:
  - workspace.inspect (read-only file listing, text searching, evidence summary)
  - dev.verify (controlled python_eval and dev_runner)
  - research.diagnose (local research first, testable hypotheses formulation)
- Security invariants (read-only enforcement, no unrestricted shell).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.skills import (
    DEV_VERIFY_MANIFEST,
    RESEARCH_DIAGNOSE_MANIFEST,
    WORKSPACE_INSPECT_MANIFEST,
    BoundSkillToolBroker,
    SkillRegistry,
    SkillToolAccessDeniedError,
    dev_verify_handler,
    register_first_party_skills,
    research_diagnose_handler,
    validate_skill_manifest,
    workspace_inspect_handler,
)
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.policy import AutonomyTier


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "test_skills.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    sb = tmp_path / "sandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


@pytest.fixture
def tool_broker(test_db: sqlite3.Connection, sandbox: Path) -> ToolBroker:
    ta = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    tb = ToolBroker(ta)
    register_builtin_tools(tb, allowed_roots=[sandbox])
    return tb


class TestFirstPartyManifests:
    """Validate declarative manifests against SAGE schema and security invariants."""

    def test_workspace_inspect_manifest_valid(self, tool_broker: ToolBroker) -> None:
        known_tools = list(tool_broker.list_tools().keys())
        validate_skill_manifest(WORKSPACE_INSPECT_MANIFEST, known_tools=known_tools)
        assert WORKSPACE_INSPECT_MANIFEST.skill_id == "workspace.inspect"
        assert "file_list" in WORKSPACE_INSPECT_MANIFEST.tool_bindings
        assert "file_write" not in WORKSPACE_INSPECT_MANIFEST.tool_bindings

    def test_dev_verify_manifest_valid(self, tool_broker: ToolBroker) -> None:
        known_tools = list(tool_broker.list_tools().keys())
        validate_skill_manifest(DEV_VERIFY_MANIFEST, known_tools=known_tools)
        assert DEV_VERIFY_MANIFEST.skill_id == "dev.verify"
        assert "dev_runner" in DEV_VERIFY_MANIFEST.tool_bindings
        assert "python_eval" in DEV_VERIFY_MANIFEST.tool_bindings

    def test_research_diagnose_manifest_valid(self, tool_broker: ToolBroker) -> None:
        known_tools = list(tool_broker.list_tools().keys())
        validate_skill_manifest(RESEARCH_DIAGNOSE_MANIFEST, known_tools=known_tools)
        assert RESEARCH_DIAGNOSE_MANIFEST.skill_id == "research.diagnose"
        assert "research_query" in RESEARCH_DIAGNOSE_MANIFEST.tool_bindings


class TestFirstPartyRegistration:
    """Test deterministic registration of first-party skills."""

    def test_register_first_party_skills(self, tool_broker: ToolBroker) -> None:
        registry = SkillRegistry()
        registered = register_first_party_skills(registry, tool_broker)
        assert len(registered) == 3

        skill_ids = {s.manifest.skill_id for s in registered}
        assert skill_ids == {"workspace.inspect", "dev.verify", "research.diagnose"}

        # Search by intent
        inspect_candidates = registry.search_skills(intent="inspect project structure")
        assert any(c.manifest.skill_id == "workspace.inspect" for c in inspect_candidates)

        dev_candidates = registry.search_skills(intent="run test verification")
        assert any(c.manifest.skill_id == "dev.verify" for c in dev_candidates)

        research_candidates = registry.search_skills(intent="diagnose error failure")
        assert any(c.manifest.skill_id == "research.diagnose" for c in research_candidates)


class TestWorkspaceInspectSkill:
    """Test workspace inspection execution and boundary guarantees."""

    def test_inspect_directory_and_search(self, tool_broker: ToolBroker, sandbox: Path) -> None:
        # Create fixture files
        pkg_dir = sandbox / "pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "mod_a.py").write_text("def run(): return 'SUCCESS_VALUE'\n", encoding="utf-8")
        (pkg_dir / "mod_b.py").write_text("from mod_a import run\n", encoding="utf-8")

        bound_broker = BoundSkillToolBroker(
            broker=tool_broker,
            manifest=WORKSPACE_INSPECT_MANIFEST,
            actor="test_user",
        )

        args = {
            "target_dir": str(pkg_dir),
            "search_query": "SUCCESS_VALUE",
        }
        result = workspace_inspect_handler(args, bound_broker)

        assert result["read_only"] is True
        assert result["total_files"] == 2
        assert len(result["matches"]) == 1
        assert "SUCCESS_VALUE" in result["matches"][0]["line_content"]
        assert "Workspace inspection" in result["summary"]

    def test_inspect_boundary_denies_file_write(
        self, tool_broker: ToolBroker, sandbox: Path
    ) -> None:
        bound_broker = BoundSkillToolBroker(
            broker=tool_broker,
            manifest=WORKSPACE_INSPECT_MANIFEST,
            actor="test_user",
        )

        with pytest.raises(SkillToolAccessDeniedError) as exc_info:
            bound_broker.invoke("file_write", {"path": str(sandbox / "bad.txt"), "content": "bad"})

        assert "not authorized to invoke tool 'file_write'" in str(exc_info.value)


class TestDevVerifySkill:
    """Test controlled development verification execution."""

    def test_syntax_evaluation(self, tool_broker: ToolBroker) -> None:
        bound_broker = BoundSkillToolBroker(
            broker=tool_broker,
            manifest=DEV_VERIFY_MANIFEST,
            actor="test_user",
        )

        args = {
            "check_type": "syntax",
            "python_code": "x = 10 + 20\nassert x == 30",
        }
        res = dev_verify_handler(args, bound_broker)
        assert res["success"] is True
        assert res["verification_passed"] is True
        assert res["exit_code"] == 0

    def test_syntax_evaluation_failure(self, tool_broker: ToolBroker) -> None:
        bound_broker = BoundSkillToolBroker(
            broker=tool_broker,
            manifest=DEV_VERIFY_MANIFEST,
            actor="test_user",
        )

        args = {
            "check_type": "syntax",
            "python_code": "assert 1 == 2",
        }
        res = dev_verify_handler(args, bound_broker)
        assert res["success"] is False
        assert res["verification_passed"] is False

    def test_dev_runner_execution(self, tool_broker: ToolBroker, sandbox: Path) -> None:
        bound_broker = BoundSkillToolBroker(
            broker=tool_broker,
            manifest=DEV_VERIFY_MANIFEST,
            actor="test_user",
        )

        args = {
            "check_type": "syntax",
            "target_path": str(sandbox),
        }
        res = dev_verify_handler(args, bound_broker)
        assert "command" in res
        assert "exit_code" in res


class TestResearchDiagnoseSkill:
    """Test technical research and error diagnosis."""

    def test_diagnose_assertion_error(self, tool_broker: ToolBroker) -> None:
        bound_broker = BoundSkillToolBroker(
            broker=tool_broker,
            manifest=RESEARCH_DIAGNOSE_MANIFEST,
            actor="test_user",
        )

        args = {
            "error_message": "AssertionError: Expected 200 OK but received 404 Not Found",
            "context": "API route integration test",
            "allow_online": False,
        }
        res = research_diagnose_handler(args, bound_broker)

        assert res["verified_hypothesis"] is True
        assert len(res["recovery_hypotheses"]) > 0
        hyp_ids = [h["hypothesis_id"] for h in res["recovery_hypotheses"]]
        assert "HYP-01" in hyp_ids
        assert "recommended_action" in res

    def test_diagnose_syntax_error(self, tool_broker: ToolBroker) -> None:
        bound_broker = BoundSkillToolBroker(
            broker=tool_broker,
            manifest=RESEARCH_DIAGNOSE_MANIFEST,
            actor="test_user",
        )

        args = {
            "error_message": "SyntaxError: invalid syntax in line 42",
            "allow_online": False,
        }
        res = research_diagnose_handler(args, bound_broker)
        assert res["recovery_hypotheses"][0]["strategy"] == "CORRECT_SYNTAX"
