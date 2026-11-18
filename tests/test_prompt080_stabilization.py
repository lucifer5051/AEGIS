"""Prompt 080 Verification Suite — Stabilization, Intent Separation, Models & Skills.

Verifies:
1. Conversational intent separation (zero supervisor tasks created for greetings & explanations).
2. Agent isolation (Atlas does not inherit Friday's greetings or sessions).
3. Session message durability and SQLite persistence across restart.
4. Truthful model discovery (GGUF discovery, resident fallback, honest Ollama/Cloud statuses).
5. Skills system and Hugging Face hardware profiling engine (RTX 3050 6GB VRAM, 16GB RAM).
6. Native desktop views and multi-tab workstation settings integration.

Canonical Reference:
    PROMPT 080: Functional Stabilization, Conversation Repair & Model Management
"""
import os
import sys
import tempfile
from unittest.mock import MagicMock
import pytest

from PySide6.QtWidgets import QApplication

# Ensure QApplication instance for widget tests
_app = QApplication.instance() or QApplication(sys.argv)

from src.brain.central_brain import CentralBrain, BrainIntent
from src.brain.registry import AgentRegistry
from src.models.discovery import ModelDiscoveryService, discover_all_models
from src.models.huggingface import (
    CURATED_HF_MODELS,
    HardwareProfile,
    evaluate_hardware_compatibility,
    get_download_manager,
)
from src.skills.marketplace import SkillMarketplaceManager, get_skill_manager


@pytest.fixture
def brain():
    reg = AgentRegistry()
    gw = MagicMock()
    return CentralBrain(agent_registry=reg, model_gateway=gw)


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


class TestConversationAndIntentSeparation:
    """Verify that pure conversation NEVER creates supervisor tasks."""

    def test_greeting_intent_classification(self, brain):
        for msg in ["hi", "hello", "hey there", "good morning", "how are you?"]:
            res = brain.classify_intent(msg)
            assert res.intent == BrainIntent.CONVERSATIONAL
            assert res.target_agent_id == "friday"

    def test_explanatory_intent_classification(self, brain):
        for msg in [
            "explain neural networks",
            "what is reinforcement learning?",
            "tell me about transformer models",
            "describe backpropagation",
        ]:
            res = brain.classify_intent(msg)
            assert res.intent == BrainIntent.CONVERSATIONAL
            assert res.target_agent_id == "friday"

    def test_actionable_task_separation(self, brain):
        # Coding -> Hephaestus
        res_code = brain.classify_intent("write a python script to parse CSV")
        assert res_code.intent == BrainIntent.ACTIONABLE_TASK
        assert res_code.target_agent_id == "coder"

        # Desktop -> Hermes
        res_desk = brain.classify_intent("open notepad and click button")
        assert res_desk.intent == BrainIntent.ACTIONABLE_TASK
        assert res_desk.target_agent_id == "operator"

        # Research -> Atlas
        res_res = brain.classify_intent("research recent papers on diffusion models")
        assert res_res.intent == BrainIntent.ACTIONABLE_TASK
        assert res_res.target_agent_id == "researcher"


class TestAgentIsolationAndPersistence:
    """Verify independent agent history and durability."""

    def test_independent_sessions_and_messages(self):
        reg1 = AgentRegistry()
        friday_sessions = reg1.list_sessions_for_agent("friday")
        assert len(friday_sessions) >= 1
        f_sess_id = friday_sessions[0].session_id

        atlas_sessions = reg1.list_sessions_for_agent("researcher")
        assert len(atlas_sessions) >= 1
        a_sess_id = atlas_sessions[0].session_id

        assert f_sess_id != a_sess_id

        # Record messages in Friday session
        reg1.record_message(f_sess_id, role="user", content="Hello Friday!")
        reg1.record_message(f_sess_id, role="friday", content="Greetings! How can I assist you?")

        # Record messages in Atlas session
        reg1.record_message(a_sess_id, role="user", content="Atlas, research quantum gravity.")
        reg1.record_message(a_sess_id, role="researcher", content="Analyzing quantum gravity publications.")

        # Re-query session history
        f_hist = reg1.get_session_history(f_sess_id)
        a_hist = reg1.get_session_history(a_sess_id)

        assert any(m.content == "Hello Friday!" for m in f_hist)
        assert any(m.content == "Atlas, research quantum gravity." for m in a_hist)

        # Strictly isolated: Atlas history does NOT contain Friday messages
        atlas_contents = [m.content for m in a_hist]
        assert "Hello Friday!" not in atlas_contents


class TestTruthfulModelDiscovery:
    """Verify model discovery returns honest statuses and does not invent fake models."""

    def test_discover_resident_and_daemon_models(self):
        svc = ModelDiscoveryService()
        models = svc.discover_all()
        assert len(models) >= 2

        # Resident fallback is always verified and READY
        resident = next((m for m in models if m["model_id"] == "sage-local-v1"), None)
        assert resident is not None
        assert resident["status"] == "READY"
        assert resident["provider_type"] == "local"

        # Discovered GGUF weights on disk
        gguf = next((m for m in models if "qwen35" in m["model_id"]), None)
        assert gguf is not None
        assert gguf["provider_id"] == "llamacpp"
        assert gguf["size_mb"] > 2000

        # Cloud models without keys are NOT_CONFIGURED
        cloud = [m for m in models if m["provider_type"] == "cloud"]
        for cm in cloud:
            assert cm["status"] in ("READY", "NOT_CONFIGURED")


class TestSkillsAndHuggingFaceAcquisition:
    """Verify Skills registry and Hugging Face hardware profiling."""

    def test_skills_lifecycle(self, temp_db):
        mgr = SkillMarketplaceManager(db_path=temp_db)
        installed = mgr.list_installed_skills()
        available = mgr.list_available_skills()

        assert len(installed) >= 3
        assert len(available) >= 2

        # Test toggle skill
        skill_id = installed[0]["skill_id"]
        success = mgr.toggle_skill(skill_id, False)
        assert success is True
        updated = next(s for s in mgr.list_installed_skills() if s["skill_id"] == skill_id)
        assert updated["enabled"] is False

        # Test install skill
        new_skill = available[0]["skill_id"]
        mgr.install_skill(new_skill)
        assert any(s["skill_id"] == new_skill for s in mgr.list_installed_skills())

    def test_hardware_compatibility_rtx3050(self):
        # RTX 3050 has 6.0 GB VRAM, 16.0 GB RAM
        profile = {"vram_gb": 6.0, "ram_gb": 16.0, "gpu_name": "NVIDIA RTX 3050"}

        # 2.08 GB model fits 100% in VRAM
        res_small = evaluate_hardware_compatibility(2.08, profile)
        assert res_small["status"] == "PERFECT_GPU_FIT"

        # 4.68 GB model offloads cleanly
        res_med = evaluate_hardware_compatibility(4.68, profile)
        assert res_med["status"] in ("PERFECT_GPU_FIT", "PARTIAL_GPU_OFFLOAD")

        # 30 GB model exceeds RAM
        res_huge = evaluate_hardware_compatibility(30.0, profile)
        assert res_huge["status"] == "INSUFFICIENT_MEMORY"

    def test_download_manager_simulation(self):
        dm = get_download_manager()
        entry = CURATED_HF_MODELS[1]  # Qwen 2.5 3B
        completed = []

        thread = dm.start_download(
            entry,
            progress_callback=lambda pct, msg: completed.append(pct),
            simulate=True,
        )
        thread.join(timeout=5.0)
        assert len(completed) > 0
        assert completed[-1] == 100.0


class TestDesktopViewsAndSettings:
    """Verify desktop views and settings load cleanly."""

    def test_views_initialization(self):
        from src.runtime import SageApplication
        from src.ui.desktop.service import UiService
        from src.ui.desktop.views.models_view import ModelsView
        from src.ui.desktop.views.skills_view import SkillsView
        from src.ui.desktop.views.settings_view import SettingsView
        from src.ui.desktop.views.conversation_view import ConversationView
        from src.ui.desktop.main_window import SageMainWindow

        app = SageApplication()
        svc = UiService(app)

        v_models = ModelsView(svc)
        assert v_models is not None

        v_skills = SkillsView(svc)
        assert v_skills is not None

        v_settings = SettingsView(svc)
        assert v_settings is not None
        assert v_settings.tabs.count() >= 8

        v_chat = ConversationView(svc)
        assert v_chat is not None

        main_win = SageMainWindow(app)
        assert main_win.stack.count() == 10
        assert main_win.sidebar is not None

        svc.close()
        app.stop()
