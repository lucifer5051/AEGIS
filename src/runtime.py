"""SAGE Central Application Composition Root & Lifecycle Manager.

Canonical Reference:
    PROMPT 073: SAGE Application Runtime Integration & Communication Security Hardening

Invariants:
- Single authoritative application composition root.
- Deterministic 12-step initialization sequence.
- All dependent subsystems receive shared, single-instance services.
- Clean lifecycle management: graceful shutdown, no leaked background threads or orphaned processes.
- 100% offline-capable, local-first operation.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from src.communication.adapters.telegram import TelegramChannelAdapter
from src.communication.adapters.whatsapp import WhatsAppChannelAdapter
from src.communication.domain import ChannelPermissionLevel, ChannelPlatform, IdentityMapping
from src.communication.friday import FridayConversationHandler
from src.communication.gateway import CommunicationGateway
from src.communication.outbound import OutboundPolicyEngine
from src.communication.security import SecurityPolicyEnforcer
from src.config.manager import SageConfigurationManager
from src.config.models import TelegramConfig, WhatsAppConfig
from src.config.policy_models import ConfigurationLayer, NetworkMode
from src.desktop.action_adapter import DesktopActionAdapter
from src.desktop.broker_integration import (
    register_desktop_action_tools,
    register_desktop_tools,
)
from src.desktop.client import DesktopObserverClient
from src.desktop.config import WindowsMCPConfig
from src.desktop.mcp_client import WindowsMCPClient
from src.desktop.policy import WindowsDesktopToolPolicy
from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.memory.engine import UnifiedMemoryEngine
from src.models.adapters.llamacpp import LlamaCppProviderAdapter
from src.models.credentials import ModelCredentialStore
from src.models.domain import ModelCapabilities, ModelDescriptor, ProviderType
from src.models.gateway import ModelGateway
from src.models.provider_adapter import GatewayReasoningProvider
from src.models.registry import ModelRegistry
from src.models.router import ModelRouter
from src.reasoning.provider import LocalReasoningProvider
from src.scheduler.job_queue import JobQueue
from src.scheduler.scheduler import WorkflowScheduler
from src.scheduler.store import SchedulerStore
from src.scheduler.worker_manager import WorkerManager
from src.skills.desktop_automation import register_desktop_automation_skill
from src.skills.first_party import register_first_party_skills
from src.skills.registry import SkillRegistry
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.approval import ApprovalStore
from src.trust.authority import TrustAuthority
from src.trust.policy import AutonomyTier as TrustCoreAutonomyTier

logger = logging.getLogger(__name__)


def _default_runtime_db_path() -> Path:
    return Path.home() / ".aegis" / "runtime" / "sage.db"


def _default_migrations_dir() -> Path:
    return Path(__file__).parent.parent / "migrations"


class SageApplication:
    """Central SAGE application runtime composing all foundational subsystems."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        conn: Optional[sqlite3.Connection] = None,
        migrations_dir: Optional[Path] = None,
        config_manager: Optional[SageConfigurationManager] = None,
        model_gateway: Optional[ModelGateway] = None,
        start_workers: bool = False,
        num_workers: int = 1,
        admin_users: Optional[Set[str]] = None,
        windows_mcp_config: Optional[WindowsMCPConfig] = None,
        desktop_client: Optional[Any] = None,
    ) -> None:
        self._owns_connection = conn is None
        self._start_workers_on_boot = start_workers
        self._num_workers = num_workers
        self._running: bool = False

        # =====================================================================
        # 1. LOAD CONFIGURATION
        # =====================================================================
        # Pre-initialize connection if provided or temporary for config
        resolved_migrations = migrations_dir or _default_migrations_dir()
        if conn is not None:
            self._conn = conn
        else:
            resolved_db = db_path or _default_runtime_db_path()
            self._conn = initialize_runtime_database(resolved_db, migrations_dir=resolved_migrations)

        self._config_mgr = config_manager or SageConfigurationManager(conn=self._conn)
        self._effective_config = self._config_mgr.get_effective_config()
        sec_pol = self._effective_config.security_policy

        # =====================================================================
        # 2. INITIALIZE TRUSTCORE & SECURITY
        # =====================================================================
        autonomy_tier = getattr(self._effective_config.autonomy_profile, "tier", "STRICT_MANUAL")
        tier_str = str(autonomy_tier).upper()
        if "FULL" in tier_str or "3" in tier_str:
            trustcore_tier = TrustCoreAutonomyTier.FULL_BOUNDED
        elif "SUPERVISED" in tier_str or "2" in tier_str:
            trustcore_tier = TrustCoreAutonomyTier.SUPERVISED
        elif "SAFE" in tier_str or "1" in tier_str:
            trustcore_tier = TrustCoreAutonomyTier.SAFE_BOUNDED
        else:
            trustcore_tier = TrustCoreAutonomyTier.STRICT_MANUAL

        self._trust_authority = build_trust_authority(self._conn, autonomy_tier=trustcore_tier)
        self._approval_store = ApprovalStore(self._conn)

        # =====================================================================
        # 3. INITIALIZE SECRET-MANAGEMENT
        # =====================================================================
        self._credential_store = ModelCredentialStore(self._conn)

        # =====================================================================
        # 4. INITIALIZE UNIFIED MEMORY ENGINE
        # =====================================================================
        self._memory_engine = UnifiedMemoryEngine(self._conn)

        # =====================================================================
        # 5. INITIALIZE MODEL GATEWAY & REGISTRY
        # =====================================================================
        if model_gateway is not None:
            self._model_gateway = model_gateway
        else:
            registry = ModelRegistry()
            router = ModelRouter(registry=registry)
            self._model_gateway = ModelGateway(
                registry=registry,
                router=router,
                credential_store=self._credential_store,
                config_manager=self._config_mgr,
                trust_authority=self._trust_authority,
            )
            # Register local adapter if available
            try:
                local_raw = LocalReasoningProvider()
                local_adapter = LlamaCppProviderAdapter(local_raw)
                registry.register_provider(local_adapter)
            except Exception as exc:
                logger.info("Local reasoning model not initialized: %s", exc)

            # Ensure an authoritative local offline fallback exists if no weights are resident
            if not registry.list_models():
                from src.models.adapters.offline import LocalOfflineChatProvider
                offline_adapter = LocalOfflineChatProvider()
                registry.register_provider(offline_adapter)
                logger.info("Registered LocalOfflineChatProvider as default baseline model.")

            # Register standard aliases to healthy discovered models
            models = registry.list_models()
            if models:
                first_model_id = models[0].model_id
                for alias in ("fast", "balanced", "reasoning", "coding", "local"):
                    target = registry.resolve_alias(alias)
                    if not target or not registry.get_model(target):
                        registry.set_alias(alias, first_model_id)

        # =====================================================================
        # 6. INITIALIZE REASONING & SUPERVISOR
        # =====================================================================
        self._reasoning_provider = GatewayReasoningProvider(gateway=self._model_gateway)
        self._state_store = StateStore(self._conn)

        self._tool_broker = ToolBroker(self._trust_authority)
        register_builtin_tools(self._tool_broker)

        self._skill_registry = SkillRegistry()
        register_first_party_skills(self._skill_registry, self._tool_broker)

        # =====================================================================
        # 6B. WINDOWS DESKTOP AUTOMATION & AWARENESS (RESTRICTED MCP & OBSERVER)
        # =====================================================================
        self._desktop_client = desktop_client
        self._desktop_policy: Optional[WindowsDesktopToolPolicy] = None
        if self._desktop_client is None and windows_mcp_config is not None and windows_mcp_config.enabled:
            self._desktop_policy = WindowsDesktopToolPolicy(windows_mcp_config)
            self._desktop_client = WindowsMCPClient(windows_mcp_config, policy=self._desktop_policy)

        if self._desktop_client is not None:
            config = getattr(self._desktop_client, "_config", None) or windows_mcp_config or WindowsMCPConfig(enabled=True)
            policy = self._desktop_policy or WindowsDesktopToolPolicy(config)
            self._desktop_policy = policy
            if isinstance(self._desktop_client, DesktopObserverClient):
                adapter = DesktopActionAdapter(client=self._desktop_client, policy=policy)
                register_desktop_action_tools(self._tool_broker, adapter, policy)
            else:
                register_desktop_tools(self._tool_broker, self._desktop_client, policy)
            register_desktop_automation_skill(self._skill_registry, self._tool_broker)

        self._supervisor = Supervisor(
            state_store=self._state_store,
            trust_authority=self._trust_authority,
            tool_broker=self._tool_broker,
            skill_registry=self._skill_registry,
            reasoning_provider=self._reasoning_provider,
            model_gateway=self._model_gateway,
            unified_memory=self._memory_engine,
        )

        # =====================================================================
        # 7. INITIALIZE SCHEDULER & WORKER MANAGER
        # =====================================================================
        self._job_queue = JobQueue(self._conn)
        self._worker_manager = WorkerManager(
            job_queue=self._job_queue,
            supervisor=self._supervisor,
            trust_authority=self._trust_authority,
            config_manager=self._config_mgr,
            memory_engine=self._memory_engine,
            model_gateway=self._model_gateway,
            max_workers=self._num_workers,
        )
        self._scheduler_store = SchedulerStore(self._conn)
        self._scheduler = WorkflowScheduler(
            scheduler_store=self._scheduler_store,
            job_queue=self._job_queue,
            worker_manager=self._worker_manager,
        )

        # =====================================================================
        # 7.5. INITIALIZE CENTRAL BRAIN & MULTI-AGENT REGISTRY
        # =====================================================================
        from src.brain.central_brain import CentralBrain
        from src.brain.registry import AgentRegistry

        self._agent_registry = AgentRegistry(db_conn=self._conn)
        self._central_brain = CentralBrain(
            agent_registry=self._agent_registry,
            model_gateway=self._model_gateway,
            supervisor=self._supervisor,
            memory_engine=self._memory_engine,
            trust_authority=self._trust_authority,
        )

        # =====================================================================
        # 8. INITIALIZE COMMUNICATION GATEWAY & FRIDAY
        # =====================================================================
        admins = set(admin_users) if admin_users else {"admin"}
        self._security_enforcer = SecurityPolicyEnforcer(admin_users=admins)

        self._friday_handler = FridayConversationHandler(
            model_gateway=self._model_gateway,
            memory_engine=self._memory_engine,
            personality_config=self._effective_config.personality_config,
            supervisor=self._supervisor,
            approval_store=self._approval_store,
            central_brain=self._central_brain,
        )

        self._outbound_policy = OutboundPolicyEngine(
            proactive_enabled=self._effective_config.personality_config.proactive_suggestions
        )

        self._comm_gateway = CommunicationGateway(
            security_enforcer=self._security_enforcer,
            friday_handler=self._friday_handler,
            outbound_policy=self._outbound_policy,
            supervisor=self._supervisor,
            approval_store=self._approval_store,
            worker_manager=self._worker_manager,
            job_queue=self._job_queue,
        )

        # =====================================================================
        # 9. REGISTER DEFAULT CHANNELS
        # =====================================================================
        # Telegram adapter
        try:
            tg_adapter = TelegramChannelAdapter()
            self._comm_gateway.register_adapter(tg_adapter)
        except Exception as exc:
            logger.debug("Telegram channel adapter not pre-initialized: %s", exc)

        # WhatsApp adapter
        try:
            wa_adapter = WhatsAppChannelAdapter()
            self._comm_gateway.register_adapter(wa_adapter)
        except Exception as exc:
            logger.debug("WhatsApp channel adapter not pre-initialized: %s", exc)

    # =========================================================================
    # PROPERTIES FOR INJECTED SUBSYSTEMS
    # =========================================================================

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def config_manager(self) -> SageConfigurationManager:
        return self._config_mgr

    @property
    def trust_authority(self) -> TrustAuthority:
        return self._trust_authority

    @property
    def approval_store(self) -> ApprovalStore:
        return self._approval_store

    @property
    def credential_store(self) -> ModelCredentialStore:
        return self._credential_store

    @property
    def memory_engine(self) -> UnifiedMemoryEngine:
        return self._memory_engine

    @property
    def model_gateway(self) -> ModelGateway:
        return self._model_gateway

    @property
    def supervisor(self) -> Supervisor:
        return self._supervisor

    @property
    def job_queue(self) -> JobQueue:
        return self._job_queue

    @property
    def worker_manager(self) -> WorkerManager:
        return self._worker_manager

    @property
    def scheduler(self) -> WorkflowScheduler:
        return self._scheduler

    @property
    def communication_gateway(self) -> CommunicationGateway:
        return self._comm_gateway

    @property
    def security_enforcer(self) -> SecurityPolicyEnforcer:
        return self._security_enforcer

    @property
    def friday_handler(self) -> FridayConversationHandler:
        return self._friday_handler

    @property
    def desktop_client(self) -> Optional[Any]:
        return self._desktop_client

    @property
    def agent_registry(self) -> Any:
        return self._agent_registry

    @property
    def central_brain(self) -> Any:
        return self._central_brain

    # =========================================================================
    # LIFECYCLE MANAGEMENT
    # =========================================================================

    def start(self) -> None:
        """Start the SAGE application runtime services deterministically."""
        if self._running:
            logger.warning("SAGE Application is already running.")
            return

        logger.info("Starting SAGE Application Runtime...")

        # 1. Start communication adapters (fail-safe: disabled channels don't block boot)
        try:
            self._comm_gateway.initialize_all()
        except Exception as exc:
            logger.warning("Some communication channels failed to initialize: %s", exc)

        # 2. Start desktop MCP client if configured
        if self._desktop_client is not None and not self._desktop_client.is_running:
            try:
                self._desktop_client.start()
            except Exception as exc:
                logger.warning("Windows MCP client failed to start: %s", exc)

        # 3. Start background worker threads if configured
        if self._start_workers_on_boot:
            for i in range(self._num_workers):
                try:
                    wid = f"runtime-worker-{i+1}"
                    self._worker_manager.start_worker(worker_id=wid)
                    logger.info("Started background worker: %s", wid)
                except Exception as exc:
                    logger.error("Failed starting background worker %d: %s", i + 1, exc)

        self._running = True
        logger.info("SAGE Application Runtime started successfully.")

    def stop(self, timeout_seconds: float = 5.0) -> None:
        """Gracefully terminate background workers, channels, and close connections."""
        if not self._running and not self._owns_connection:
            return

        logger.info("Stopping SAGE Application Runtime...")

        # 1. Stop background workers
        try:
            self._worker_manager.stop_all(timeout_seconds=timeout_seconds)
        except Exception as exc:
            logger.warning("Error stopping background workers: %s", exc)

        # 2. Shutdown communication adapters
        try:
            self._comm_gateway.shutdown_all()
        except Exception as exc:
            logger.warning("Error shutting down communication channels: %s", exc)

        # 3. Stop Windows MCP client
        if self._desktop_client is not None:
            try:
                self._desktop_client.stop()
            except Exception as exc:
                logger.warning("Error stopping Windows MCP client: %s", exc)

        # 4. Close database connection if owned
        if self._owns_connection and self._conn is not None:
            try:
                self._conn.close()
            except Exception as exc:
                logger.warning("Error closing database connection: %s", exc)

        self._running = False
        logger.info("SAGE Application Runtime stopped.")

    def __enter__(self) -> "SageApplication":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()
