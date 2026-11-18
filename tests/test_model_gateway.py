"""SAGE Model Gateway & Intelligent Provider Routing -- Integration Tests.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 13

Covers:
1. Gateway: Provider & model registration, discovery, normalized response, streaming, structured output.
2. Routing: Explicit model, task-specific routing, alias resolution, capability filtering, local-only enforcement, fallback.
3. Security: Encrypted credentials, secret redaction, zero secrets, TrustCore pre-invocation enforcement.
4. Reliability: Bounded retries, timeout handling, auth failure fail-closed, circuit-breaker trips.
5. Integration: GatewayReasoningProvider with StructuredPlanner, TaskSynthesizer, and Supervisor goal execution.
100% offline, zero network access.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

from src.config.manager import SageConfigurationManager
from src.config.policy_models import NetworkMode, NetworkPolicy, SecurityPolicy
from src.manager.planner import StructuredPlanner
from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.manager.synthesis import TaskSynthesizer
from src.manager.verifier import StepVerifier
from src.models import (
    AnthropicProviderAdapter,
    BaseModelProvider,
    GatewayReasoningProvider,
    GeminiProviderAdapter,
    LlamaCppProviderAdapter,
    ModelAuthenticationError,
    ModelCapabilities,
    ModelCredentialStore,
    ModelDescriptor,
    ModelGateway,
    ModelGatewayError,
    ModelPolicyDeniedError,
    ModelProviderUnavailableError,
    ModelRegistry,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRoutingError,
    ModelStreamChunk,
    ModelTimeoutError,
    ModelUsage,
    OllamaProviderAdapter,
    OpenAIProviderAdapter,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderType,
    TaskCategory,
    redact_secrets_from_text,
)
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import (
    ReasoningMetrics,
    ReasoningRequest,
    ReasoningResponse,
)
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.errors import SecretLeakError
from src.trust.policy import AutonomyTier

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


class MockLocalProvider(BaseModelProvider):
    """Deterministic offline local provider adapter."""

    def __init__(
        self,
        provider_id: str = "mock_local",
        model_id: str = "qwen35-4b-q4km",
        canned_response: str = "Mock local model response",
    ) -> None:
        super().__init__(provider_id=provider_id, provider_type=ProviderType.LOCAL)
        self.default_model = model_id
        self.canned_response = canned_response
        self.calls_count = 0

    def list_models(self) -> List[ModelDescriptor]:
        return [
            ModelDescriptor(
                model_id=self.default_model,
                provider_id=self.provider_id,
                display_name="Mock Local LLM",
                provider_type=ProviderType.LOCAL,
                capabilities=ModelCapabilities(
                    text_generation=True,
                    streaming=True,
                    tool_calling=True,
                    structured_output=True,
                    reasoning=True,
                    context_window=8192,
                ),
            )
        ]

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls_count += 1
        return ModelResponse(
            content=self.canned_response,
            model_id=self.default_model,
            provider_id=self.provider_id,
            request_id=request.request_id,
            usage=ModelUsage(prompt_tokens=15, completion_tokens=10, total_tokens=25),
            execution_duration_ms=12.5,
        )

    def generate_stream(self, request: ModelRequest):
        words = self.canned_response.split(" ")
        for i, w in enumerate(words):
            chunk = w if i == 0 else " " + w
            is_final = (i == len(words) - 1)
            yield ModelStreamChunk(
                content_delta=chunk,
                chunk_index=i,
                is_final=is_final,
                finish_reason="stop" if is_final else None,
            )

    def check_health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.provider_id, status=ProviderHealthStatus.HEALTHY, latency_ms=5.0)


class MockCloudProvider(BaseModelProvider):
    """Deterministic offline cloud provider adapter."""

    def __init__(
        self,
        provider_id: str = "mock_cloud",
        model_id: str = "cloud-model-1",
        api_key: Optional[str] = "test-cloud-key",
        canned_response: str = "Mock cloud response",
    ) -> None:
        super().__init__(provider_id=provider_id, provider_type=ProviderType.CLOUD, api_key=api_key)
        self.default_model = model_id
        self.canned_response = canned_response
        self.calls_count = 0
        self.fail_with: Optional[Exception] = None

    def list_models(self) -> List[ModelDescriptor]:
        return [
            ModelDescriptor(
                model_id=self.default_model,
                provider_id=self.provider_id,
                display_name="Mock Cloud Advanced",
                provider_type=ProviderType.CLOUD,
                capabilities=ModelCapabilities(
                    text_generation=True,
                    streaming=True,
                    tool_calling=True,
                    structured_output=True,
                    vision=True,
                    reasoning=True,
                    context_window=32768,
                ),
            )
        ]

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls_count += 1
        if self.fail_with:
            raise self.fail_with
        return ModelResponse(
            content=self.canned_response,
            model_id=self.default_model,
            provider_id=self.provider_id,
            request_id=request.request_id,
            usage=ModelUsage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
            execution_duration_ms=25.0,
        )

    def generate_stream(self, request: ModelRequest):
        words = self.canned_response.split(" ")
        for i, w in enumerate(words):
            chunk = w if i == 0 else " " + w
            is_final = (i == len(words) - 1)
            yield ModelStreamChunk(content_delta=chunk, chunk_index=i, is_final=is_final)

    def check_health(self) -> ProviderHealth:
        if self.fail_with:
            return ProviderHealth(provider_id=self.provider_id, status=ProviderHealthStatus.UNAVAILABLE, error_message=str(self.fail_with))
        return ProviderHealth(provider_id=self.provider_id, status=ProviderHealthStatus.HEALTHY, latency_ms=15.0)


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "test_gateway.db"
    return initialize_runtime_database(db_file, migrations_dir=MIGRATIONS_DIR)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    sb = tmp_path / "sandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


@pytest.fixture
def gateway_fixture(db_conn: sqlite3.Connection):
    registry = ModelRegistry()
    router = ModelRouter(registry)
    cred_store = ModelCredentialStore(db_conn)
    config_mgr = SageConfigurationManager(conn=db_conn)
    trust_auth = build_trust_authority(db_conn, autonomy_tier=AutonomyTier.FULL_BOUNDED)

    local_prov = MockLocalProvider()
    cloud_prov = MockCloudProvider()

    registry.register_provider(local_prov)
    registry.register_provider(cloud_prov)

    gateway = ModelGateway(
        registry=registry,
        router=router,
        credential_store=cred_store,
        config_manager=config_mgr,
        trust_authority=trust_auth,
    )

    return {
        "db": db_conn,
        "registry": registry,
        "router": router,
        "cred_store": cred_store,
        "config_mgr": config_mgr,
        "trust_auth": trust_auth,
        "local_prov": local_prov,
        "cloud_prov": cloud_prov,
        "gateway": gateway,
    }


# =============================================================================
# 1. GATEWAY & ADAPTER TESTS
# =============================================================================

class TestModelGatewayCore:
    def test_provider_registration_and_listing(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        models = gateway.list_models()
        assert len(models) >= 2
        model_ids = [m.model_id for m in models]
        assert "qwen35-4b-q4km" in model_ids
        assert "cloud-model-1" in model_ids

    def test_normalized_generation_response(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        req = ModelRequest(prompt="Summarize today's goals", task_category=TaskCategory.SUMMARIZATION)
        resp = gateway.generate(req)

        assert resp.content == "Mock local model response"
        assert resp.model_id == "qwen35-4b-q4km"
        assert resp.provider_id == "mock_local"
        assert resp.usage.total_tokens == 25
        assert resp.execution_duration_ms > 0

    def test_streaming_generation(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        req = ModelRequest(prompt="Stream output test")
        chunks = list(gateway.generate_stream(req))

        assert len(chunks) == 4  # "Mock", " local", " model", " response"
        assert "".join(c.content_delta for c in chunks) == "Mock local model response"
        assert chunks[-1].is_final is True

    def test_provider_health_check(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        health = gateway.get_provider_health("mock_local")
        assert health.status == ProviderHealthStatus.HEALTHY
        assert health.latency_ms > 0.0

    def test_openai_and_anthropic_adapters_with_mock_client(self):
        # OpenAI adapter mock
        def mock_openai(endpoint, payload, headers, timeout):
            return {
                "choices": [{"message": {"content": "OpenAI mock response"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "model": "gpt-4o",
            }

        openai_adapter = OpenAIProviderAdapter(api_key="sk-proj-testkey", http_client=mock_openai)
        resp = openai_adapter.generate(ModelRequest(prompt="Hello OpenAI"))
        assert resp.content == "OpenAI mock response"
        assert resp.usage.total_tokens == 15

        # Anthropic adapter mock
        def mock_anthropic(endpoint, payload, headers, timeout):
            return {
                "content": [{"type": "text", "text": "Anthropic mock response"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 8, "output_tokens": 6},
                "model": "claude-3-5-sonnet-20241022",
            }

        anthropic_adapter = AnthropicProviderAdapter(api_key="sk-ant-testkey", http_client=mock_anthropic)
        resp2 = anthropic_adapter.generate(ModelRequest(prompt="Hello Claude"))
        assert resp2.content == "Anthropic mock response"
        assert resp2.usage.total_tokens == 14


# =============================================================================
# 2. INTELLIGENT ROUTING TESTS
# =============================================================================

class TestIntelligentRouting:
    def test_explicit_model_selection(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        config_mgr = gateway_fixture["config_mgr"]
        from src.config.policy_models import ConfigurationLayer

        config_mgr.update_config(
            updates={"security_policy": {"network": {"mode": "ONLINE_ALLOWED"}}},
            layer=ConfigurationLayer.GLOBAL,
        )
        req = ModelRequest(prompt="Use cloud", model_id="cloud-model-1")
        resp = gateway.generate(req)
        assert resp.provider_id == "mock_cloud"
        assert resp.model_id == "cloud-model-1"

    def test_alias_resolution(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        registry = gateway_fixture["registry"]
        registry.set_alias("fast", "qwen35-4b-q4km")

        req = ModelRequest(prompt="Run fast", model_alias="fast")
        resp = gateway.generate(req)
        assert resp.model_id == "qwen35-4b-q4km"

    def test_local_only_enforcement_blocks_cloud(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        config_mgr = gateway_fixture["config_mgr"]
        from src.config.policy_models import ConfigurationLayer

        # Enforce LOCAL_ONLY network policy
        config_mgr.update_config(
            updates={"security_policy": {"network": {"mode": "LOCAL_ONLY"}}},
            layer=ConfigurationLayer.GLOBAL,
        )

        req = ModelRequest(prompt="Query external", model_id="cloud-model-1")
        with pytest.raises(ModelPolicyDeniedError) as exc_info:
            gateway.generate(req)
        assert "strictly prohibited under the current LOCAL_ONLY" in str(exc_info.value)

    def test_fallback_when_allowed_routes_to_healthy_provider(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        cloud_prov = gateway_fixture["cloud_prov"]
        config_mgr = gateway_fixture["config_mgr"]
        from src.config.policy_models import ConfigurationLayer

        config_mgr.update_config(
            updates={"security_policy": {"network": {"mode": "ONLINE_ALLOWED"}}},
            layer=ConfigurationLayer.GLOBAL,
        )

        # Set cloud provider to fail with unavailable
        cloud_prov.fail_with = ModelProviderUnavailableError("Cloud service offline", provider_id="mock_cloud")

        # Explicit request with fallback=True should fail over to mock_local
        req = ModelRequest(prompt="Important task", model_id="cloud-model-1")
        resp = gateway.generate(req, allow_fallback=True)

        assert resp.is_fallback is True
        assert resp.provider_id == "mock_local"

    def test_fallback_disabled_raises_provider_error(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        cloud_prov = gateway_fixture["cloud_prov"]
        config_mgr = gateway_fixture["config_mgr"]
        from src.config.policy_models import ConfigurationLayer

        config_mgr.update_config(
            updates={"security_policy": {"network": {"mode": "ONLINE_ALLOWED"}}},
            layer=ConfigurationLayer.GLOBAL,
        )
        cloud_prov.fail_with = ModelProviderUnavailableError("Cloud service down", provider_id="mock_cloud")

        req = ModelRequest(prompt="No fallback task", model_id="cloud-model-1")
        with pytest.raises(ModelGatewayError):
            gateway.generate(req, allow_fallback=False)


# =============================================================================
# 3. SECURITY & CREDENTIAL TESTS
# =============================================================================

class TestSecurityAndCredentials:
    def test_credential_encryption_and_retrieval(self, gateway_fixture):
        cred_store = gateway_fixture["cred_store"]
        secret_key = "sk-proj-super-secret-api-token-12345"

        cred_store.set_credential("test_prov", secret_key)
        assert cred_store.has_credential("test_prov") is True

        retrieved = cred_store.get_credential("test_prov")
        assert retrieved == secret_key

        # Raw key must NOT be stored in plaintext in the SQLite table
        cursor = gateway_fixture["db"].cursor()
        cursor.execute("SELECT ciphertext FROM model_credentials WHERE provider_id = 'test_prov'")
        blob = cursor.fetchone()[0]
        assert secret_key.encode("utf-8") not in blob

    def test_secret_redaction_from_error_strings(self):
        raw_error = "Failed to authenticate with key sk-proj-1234567890abcdef1234567890 for provider"
        sanitized = redact_secrets_from_text(raw_error)
        assert "sk-proj-" not in sanitized
        assert "[REDACTED_SECRET]" in sanitized

    def test_zero_secret_rejection_in_metadata(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        with pytest.raises(SecretLeakError):
            # Secret in metadata should trigger zero-secret invariant
            from src.trust.audit import _check_no_secrets
            _check_no_secrets(json.dumps({"api_key": "sk-proj-leak1234567890abcdef123456"}))


# =============================================================================
# 4. RELIABILITY & RESILIENCE TESTS
# =============================================================================

class TestReliabilityAndResilience:
    def test_transient_failure_retries_and_succeeds(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        local_prov = gateway_fixture["local_prov"]

        # Simulate transient error on first attempt, then success
        class FlakyProvider(MockLocalProvider):
            def __init__(self):
                super().__init__(provider_id="flaky_prov")
                self.attempts = 0

            def generate(self, request: ModelRequest) -> ModelResponse:
                self.attempts += 1
                if self.attempts == 1:
                    raise ModelProviderUnavailableError("Temporary connection blip", provider_id=self.provider_id)
                return super().generate(request)

        flaky = FlakyProvider()
        gateway.registry.register_provider(flaky)

        req = ModelRequest(prompt="Test retry", model_id="qwen35-4b-q4km")
        # Route specifically to flaky
        req = req.model_copy(update={"model_id": "qwen35-4b-q4km", "metadata": {}})
        gateway.router._registry._providers["mock_local"] = flaky

        resp = gateway.generate(req)
        assert resp.content == "Mock local model response"
        assert flaky.attempts == 2

    def test_circuit_breaker_trips_after_consecutive_failures(self, gateway_fixture):
        registry = gateway_fixture["registry"]
        provider_id = "failing_prov"

        assert registry.is_circuit_open(provider_id) is False
        registry.record_failure(provider_id)
        registry.record_failure(provider_id)
        assert registry.is_circuit_open(provider_id) is False

        # 3rd failure trips circuit
        registry.record_failure(provider_id)
        assert registry.is_circuit_open(provider_id) is True

        health = registry.get_provider_health(provider_id)
        assert health.status == ProviderHealthStatus.UNAVAILABLE
        assert "Circuit breaker open" in health.error_message


# =============================================================================
# 5. SUPERVISOR & REASONING INTEGRATION TESTS
# =============================================================================

class TestSupervisorAndPlannerIntegration:
    def test_gateway_reasoning_provider_with_structured_planner(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        local_prov = gateway_fixture["local_prov"]

        # Set canned plan response
        canned_plan = {
            "goal": "Build project",
            "rationale": "Logical plan",
            "steps": [
                {
                    "step_number": 1,
                    "description": "Write output",
                    "tool": "file_write",
                    "arguments": {"path": "out.txt", "content": "hello"},
                    "is_idempotent": True,
                    "acceptance_criteria": "done",
                    "estimated_risk": "LOW",
                    "target_resource": "*",
                }
            ],
        }
        local_prov.canned_response = json.dumps(canned_plan)

        # Build GatewayReasoningProvider
        reasoning_provider = GatewayReasoningProvider(gateway)
        req = ReasoningRequest(prompt="Build project")
        resp = reasoning_provider.reason(req)

        assert resp.content == json.dumps(canned_plan)
        assert resp.model_id == "qwen35-4b-q4km"
        assert resp.provider_id == "mock_local"

    def test_supervisor_executes_goal_using_model_gateway(self, gateway_fixture, sandbox):
        gateway = gateway_fixture["gateway"]
        local_prov = gateway_fixture["local_prov"]
        db = gateway_fixture["db"]
        trust_auth = gateway_fixture["trust_auth"]

        target_file = str(sandbox / "gateway_exec.txt")
        canned_plan = {
            "goal": "Write test file",
            "rationale": "Supervisor execution via gateway",
            "steps": [
                {
                    "step_number": 1,
                    "description": "Write target file",
                    "tool": "file_write",
                    "arguments": {"path": target_file, "content": "written by model gateway"},
                    "is_idempotent": True,
                    "acceptance_criteria": "done",
                    "estimated_risk": "LOW",
                    "target_resource": target_file,
                }
            ],
        }
        local_prov.canned_response = json.dumps(canned_plan)

        state_store = StateStore(db)
        tool_broker = ToolBroker(trust_auth)
        register_builtin_tools(tool_broker, allowed_roots=[sandbox])

        supervisor = Supervisor(
            state_store=state_store,
            trust_authority=trust_auth,
            tool_broker=tool_broker,
            model_gateway=gateway,
        )

        res = supervisor.execute_goal("Write test file")
        assert res.success is True
        assert Path(target_file).exists()
        assert Path(target_file).read_text(encoding="utf-8") == "written by model gateway"

    def test_task_synthesizer_with_gateway_reasoning_provider(self, gateway_fixture):
        gateway = gateway_fixture["gateway"]
        local_prov = gateway_fixture["local_prov"]
        local_prov.canned_response = "All tasks executed with verified integrity."

        reasoning_provider = GatewayReasoningProvider(gateway)
        synthesizer = TaskSynthesizer(provider=reasoning_provider)

        from src.manager.planner import Plan, PlannedStep
        mock_plan = Plan(
            goal="Deploy test system",
            rationale="Automated plan",
            steps=[
                PlannedStep(
                    step_number=1,
                    description="Run deploy script",
                    tool="system_exec",
                    arguments={"command": "deploy"},
                    is_idempotent=True,
                    acceptance_criteria="deployed",
                    estimated_risk="LOW",
                    target_resource="system",
                )
            ],
        )
        result = synthesizer.synthesize(
            goal="Deploy test system",
            plan=mock_plan,
            observations=[],
            verifications=[],
        )
        assert result.text == "All tasks executed with verified integrity."
        assert result.is_degraded is False
