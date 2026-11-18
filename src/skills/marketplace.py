"""SAGE Skills Marketplace & Registry.

Manages installed and discoverable modular agent skills, their tool requirements,
model dependencies, and enable/disable states.

Canonical Reference:
    PROMPT 080: Functional Stabilization, Conversation Repair & Model Management
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = r"C:\Users\digvi\.aegis\sage.db"

BUILTIN_SKILLS: List[Dict[str, Any]] = [
    {
        "skill_id": "workspace.inspect",
        "name": "Workspace Inspector",
        "category": "System",
        "version": "1.2.0",
        "description": "Inspects directories, parses source trees, reads files, and locates project symbols safely.",
        "author": "SAGE Core",
        "required_tools": ["read_file", "list_dir", "search_symbols"],
        "primary_model": "qwen2.5-7b-instruct",
        "alternative_models": ["gemma-2-9b-it", "phi-4-mini"],
        "compatible_models": ["qwen2.5-7b-instruct", "gemma-2-9b-it", "phi-4-mini", "sage-local-v1"],
        "recommended_models": ["qwen2.5-7b-instruct", "gemma-2-9b-it", "phi-4-mini"],
        "assigned_model": "Auto",
        "installed": True,
        "enabled": True,
    },
    {
        "skill_id": "dev.verify",
        "name": "Code Verification & Testing",
        "category": "Development",
        "version": "1.1.0",
        "description": "Executes test suites, validates syntax, verifies assertions, and reports regressions.",
        "author": "SAGE Core",
        "required_tools": ["run_command", "read_test_results"],
        "primary_model": "qwen2.5-coder-7b",
        "alternative_models": ["deepseek-coder-v2-lite", "devstral-small-24b"],
        "compatible_models": ["qwen2.5-coder-7b", "deepseek-coder-v2-lite", "devstral-small-24b", "sage-local-v1"],
        "recommended_models": ["qwen2.5-coder-7b", "deepseek-coder-v2-lite", "devstral-small-24b"],
        "assigned_model": "Auto",
        "installed": True,
        "enabled": True,
    },
    {
        "skill_id": "research.diagnose",
        "name": "Diagnostic Reasoner",
        "category": "Research",
        "version": "1.0.0",
        "description": "Performs chain-of-thought analysis, traces error origins, and evaluates root causes.",
        "author": "SAGE Core",
        "required_tools": ["read_logs", "inspect_state"],
        "primary_model": "deepseek-r1-distill-qwen-7b",
        "alternative_models": ["deepseek-r1-distill-qwen-1.5b", "qwen2.5-14b-instruct"],
        "compatible_models": ["deepseek-r1-distill-qwen-7b", "deepseek-r1-distill-qwen-1.5b", "qwen2.5-14b-instruct", "sage-local-v1"],
        "recommended_models": ["deepseek-r1-distill-qwen-7b", "deepseek-r1-distill-qwen-1.5b", "qwen2.5-14b-instruct"],
        "assigned_model": "Auto",
        "installed": True,
        "enabled": True,
    },
    {
        "skill_id": "desktop.automation",
        "name": "Desktop Application Controller",
        "category": "Desktop",
        "version": "1.0.4",
        "description": "Manages desktop windows, opens registered local applications, and captures screenshots.",
        "author": "SAGE Core",
        "required_tools": ["launch_app", "window_control", "screen_capture"],
        "primary_model": "mistral-7b-instruct-v0.3",
        "alternative_models": ["qwen2.5-7b-instruct", "hermes-3-llama-3.1-8b"],
        "compatible_models": ["mistral-7b-instruct-v0.3", "qwen2.5-7b-instruct", "hermes-3-llama-3.1-8b", "sage-local-v1"],
        "recommended_models": ["mistral-7b-instruct-v0.3", "qwen2.5-7b-instruct", "hermes-3-llama-3.1-8b"],
        "assigned_model": "Auto",
        "installed": True,
        "enabled": True,
    },
    {
        "skill_id": "web.researcher",
        "name": "Offline Document Synthesizer",
        "category": "Research",
        "version": "0.9.0",
        "description": "Extracts summaries and cross-references findings across local papers and documentation.",
        "author": "SAGE Community",
        "required_tools": ["extract_pdf", "index_documents"],
        "primary_model": "qwen2.5-vl-7b-instruct",
        "alternative_models": ["gemma-3-4b-it", "qwen2.5-7b-instruct"],
        "compatible_models": ["qwen2.5-vl-7b-instruct", "gemma-3-4b-it", "qwen2.5-7b-instruct", "sage-local-v1"],
        "recommended_models": ["qwen2.5-vl-7b-instruct", "gemma-3-4b-it", "qwen2.5-7b-instruct"],
        "assigned_model": "Auto",
        "installed": False,
        "enabled": False,
    },
    {
        "skill_id": "code.architect",
        "name": "Multi-File Refactoring Assistant",
        "category": "Development",
        "version": "0.8.5",
        "description": "Plans complex code refactoring, structural reorganizations, and API migrations.",
        "author": "SAGE Community",
        "required_tools": ["ast_parse", "diff_apply"],
        "primary_model": "qwen2.5-coder-7b",
        "alternative_models": ["deepseek-coder-v2-lite", "devstral-small-24b"],
        "compatible_models": ["qwen2.5-coder-7b", "deepseek-coder-v2-lite", "devstral-small-24b", "sage-local-v1"],
        "recommended_models": ["qwen2.5-coder-7b", "deepseek-coder-v2-lite", "devstral-small-24b"],
        "assigned_model": "Auto",
        "installed": False,
        "enabled": False,
    },
    {
        "skill_id": "data.tabular_engine",
        "name": "Tabular Data & CSV Analyst",
        "category": "Data",
        "version": "1.0.1",
        "description": "Computes aggregates, parses CSV/JSON data, and formats markdown comparison tables.",
        "author": "SAGE Community",
        "required_tools": ["tabular_calc"],
        "primary_model": "phi-4-mini",
        "alternative_models": ["qwen2.5-7b-instruct", "gemma-2-9b-it"],
        "compatible_models": ["phi-4-mini", "qwen2.5-7b-instruct", "gemma-2-9b-it", "sage-local-v1"],
        "recommended_models": ["phi-4-mini", "qwen2.5-7b-instruct", "gemma-2-9b-it"],
        "assigned_model": "Auto",
        "installed": False,
        "enabled": False,
    },
]


class SkillMarketplaceManager:
    """Manages skill lifecycles, states, and persistence."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS skills_registry (
                        skill_id TEXT PRIMARY KEY,
                        installed INTEGER NOT NULL,
                        enabled INTEGER NOT NULL,
                        assigned_model TEXT
                    )
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning("Could not initialize skills_registry DB table: %s", exc)

    def assign_model_to_skill(self, skill_id: str, model_id: str) -> bool:
        """Assign specific model to skill with persistence."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO skills_registry (skill_id, installed, enabled, assigned_model)
                    VALUES (?, 1, 1, ?)
                    ON CONFLICT(skill_id) DO UPDATE SET assigned_model = excluded.assigned_model
                    """,
                    (skill_id, model_id),
                )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("Failed to assign model to skill %s: %s", skill_id, exc)
            return False

    def list_all_skills(self) -> List[Dict[str, Any]]:
        """List all skills merged with persistence overrides."""
        overrides = {}
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT skill_id, installed, enabled, assigned_model FROM skills_registry")
                for row in cursor.fetchall():
                    overrides[row[0]] = {
                        "installed": bool(row[1]),
                        "enabled": bool(row[2]),
                        "assigned_model": row[3] if len(row) > 3 and row[3] else None,
                    }
        except Exception:
            pass

        results = []
        for s in BUILTIN_SKILLS:
            item = dict(s)
            if s["skill_id"] in overrides:
                item["installed"] = overrides[s["skill_id"]]["installed"]
                item["enabled"] = overrides[s["skill_id"]]["enabled"]
                if overrides[s["skill_id"]].get("assigned_model"):
                    item["assigned_model"] = overrides[s["skill_id"]]["assigned_model"]
            results.append(item)
        return results

    def list_installed_skills(self) -> List[Dict[str, Any]]:
        return [s for s in self.list_all_skills() if s["installed"]]

    def list_available_skills(self) -> List[Dict[str, Any]]:
        return [s for s in self.list_all_skills() if not s["installed"]]

    def toggle_skill(self, skill_id: str, enabled: bool) -> bool:
        """Enable or disable an installed skill."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO skills_registry (skill_id, installed, enabled)
                    VALUES (?, 1, ?)
                    ON CONFLICT(skill_id) DO UPDATE SET enabled = excluded.enabled
                    """,
                    (skill_id, int(enabled)),
                )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("Failed to toggle skill %s: %s", skill_id, exc)
            return False

    def install_skill(self, skill_id: str) -> bool:
        """Mark an available skill as installed and enabled."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO skills_registry (skill_id, installed, enabled)
                    VALUES (?, 1, 1)
                    ON CONFLICT(skill_id) DO UPDATE SET installed = 1, enabled = 1
                    """,
                    (skill_id,),
                )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("Failed to install skill %s: %s", skill_id, exc)
            return False

    def uninstall_skill(self, skill_id: str) -> bool:
        """Uninstall a skill."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO skills_registry (skill_id, installed, enabled)
                    VALUES (?, 0, 0)
                    ON CONFLICT(skill_id) DO UPDATE SET installed = 0, enabled = 0
                    """,
                    (skill_id,),
                )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("Failed to uninstall skill %s: %s", skill_id, exc)
            return False


_global_skill_mgr: Optional[SkillMarketplaceManager] = None


def get_skill_manager() -> SkillMarketplaceManager:
    global _global_skill_mgr
    if _global_skill_mgr is None:
        _global_skill_mgr = SkillMarketplaceManager()
    return _global_skill_mgr


def list_installed_and_available_skills(reg=None) -> Dict[str, Any]:
    mgr = get_skill_manager()
    return {
        "installed": mgr.list_installed_skills(),
        "available": mgr.list_available_skills(),
    }

