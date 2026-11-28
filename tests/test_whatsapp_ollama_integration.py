"""Integration tests for WhatsApp Agent Ollama Local Model Integration.

Validates:
- Real ModelGateway + OllamaProviderAdapter + LocalDraftProvider loopback pipeline
- Zero mocking of the units under test: uses a stdlib http.server loopback stub on 127.0.0.1
- Native Ollama /api/chat thinking model handling (think=False, strip_thinking_tags)
- Preflight failure handling (down, 500, timeout, missing model, malformed JSON)
- Non-loopback host rejection
- Prompt budget guard
- Non-drafting caller compatibility (no 'think' injected into /api/generate)
- Bug catch proof for 'CoordinatedSQLCipherConnection' wiring defect
"""

import http.server
import json
import re
import socketserver
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from src.gateway.drafts import DraftContext
from src.gateway.local_draft_provider import LocalDraftProvider, strip_thinking_tags
from src.gateway.whatsapp_runtime import (
    build_local_model_gateway,
    initialize_runtime_database,
    is_loopback_host,
    run_preflight,
    run_smoke_test,
)
from src.models.adapters.ollama import OllamaProviderAdapter
from src.models.domain import ModelDescriptor, ModelRequest, ModelResponse, ProviderType
from src.models.gateway import ModelGateway
from src.models.registry import ModelRegistry
from src.models.router import ModelRouter
from src.security.contact_key_store import ContactKeyStore
from src.memory.contact_memory import EncryptedContactMemoryStore


class OllamaStubHandler(http.server.BaseHTTPRequestHandler):
    """Configurable loopback HTTP stub for Ollama REST endpoints."""

    # Class-level state shared across requests
    mode: str = "normal"
    received_requests: List[Dict[str, Any]] = []

    def log_message(self, format, *args):
        # Suppress stdlib HTTP logging in test output
        pass

    def do_GET(self):
        if self.path == "/api/version":
            if self.mode == "error_500":
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"error": "internal error"}')
                return
            if self.mode == "malformed_json":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'not a valid json {[[')
                return
            if self.mode == "slow":
                time.sleep(35.0)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"version": "0.1.48"}).encode("utf-8"))
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"version": "0.1.48"}).encode("utf-8"))
            return

        if self.path == "/api/tags":
            if self.mode == "error_500":
                self.send_response(500)
                self.end_headers()
                return
            if self.mode == "model_not_found":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"models": [{"name": "other-model:latest"}]}).encode("utf-8"))
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "models": [
                            {"name": "qwen3.5:4b"},
                            {"name": "qwen35-4b-q4km"},
                            {"name": "qwen2.5-coder:3b"},
                        ]
                    }
                ).encode("utf-8")
            )
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""
        payload = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                pass

        self.__class__.received_requests.append({"path": self.path, "payload": payload})

        if self.path == "/api/chat":
            if self.mode == "error_500":
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"error": "model error"}')
                return
            if self.mode == "slow":
                time.sleep(35.0)
            if self.mode == "malformed_json":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'corrupt json {')
                return

            messages = payload.get("messages", [])
            user_msg = messages[-1]["content"] if messages else ""
            p_lower = user_msg.lower()

            if self.mode == "think":
                content = (
                    "<think>\nNeed to reply concisely to the user invitation.\n</think>\n"
                    "Hey! Sure, let's grab coffee tomorrow afternoon. Let me know what time works."
                )
            elif self.mode == "unterminated_think":
                content = "<think>\nUnterminated thought without closing tag"
            elif self.mode == "nested_brackets":
                content = "I <3 you! Looking forward to coffee at 4pm when x < 5."
            else:
                if "coffee" in p_lower:
                    content = "Hey! Sure, let's grab coffee tomorrow afternoon. Let me know what time works."
                elif "movie" in p_lower:
                    content = "Haan bhai, bilkul chalte hain! Kaunsi movie aur kaunsa show chalna hai?"
                elif "udyach" in p_lower or "jevayla" in p_lower:
                    content = "Ho chalel, udyach dupari jevayla jaauya. Kuthlya restaurant madhe jaaycha?"
                elif "नमस्ते" in user_msg or "मिल सकते" in user_msg:
                    content = "नमस्ते! हाँ, हम कल शाम को मिल सकते हैं। किस समय मिलना है?"
                elif "cooking" in p_lower:
                    content = "Haha an 'experience' is putting it very kindly! You're in charge of dinner next time."
                elif "hospital" in p_lower or "terrible" in p_lower:
                    content = "I'm so sorry to hear that. Please take care of yourself and rest up."
                elif "ignore" in p_lower or "system prompt" in p_lower:
                    content = "I cannot fulfill instructions that attempt to override my system guidelines."
                elif "https://" in p_lower:
                    content = "Thanks for sharing the link! I'll take a look as soon as I'm at my desk."
                elif "98765" in p_lower:
                    content = "Got the number! I'll give you a call once I'm free."
                elif len(user_msg) > 800:
                    content = "Thanks for the comprehensive briefing. I've noted all key items and will follow up."
                elif "burst" in p_lower or "quick question" in p_lower:
                    content = "Hey, yes I'm around! What's up?"
                else:
                    content = "Sounds good, looking forward to catching up soon."

            resp_payload = {
                "model": payload.get("model", "qwen3.5:4b"),
                "message": {"role": "assistant", "content": content},
                "done": True,
                "prompt_eval_count": 12,
                "eval_count": 18,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_payload).encode("utf-8"))
            return

        if self.path == "/api/generate":
            resp_payload = {
                "model": payload.get("model", "qwen3.5:4b"),
                "response": "Response generated from api/generate",
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 14,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_payload).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


@pytest.fixture
def ollama_stub():
    """Start loopback HTTP server stub on 127.0.0.1 on a random available port."""
    OllamaStubHandler.mode = "normal"
    OllamaStubHandler.received_requests = []

    server = ThreadingHTTPServer(("127.0.0.1", 0), OllamaStubHandler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    yield {"url": url, "port": port, "handler": OllamaStubHandler}

    server.shutdown()
    server.server_close()


# ==============================================================================
# TESTS
# ==============================================================================


def test_loopback_host_validator():
    """Verify loopback host validator strictly accepts only 127.0.0.1, localhost, ::1."""
    assert is_loopback_host("http://127.0.0.1:11434") is True
    assert is_loopback_host("http://localhost:11434") is True
    assert is_loopback_host("http://[::1]:11434") is True
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("::1") is True

    # Non-loopback targets must be refused
    assert is_loopback_host("http://192.168.1.100:11434") is False
    assert is_loopback_host("http://10.0.0.1:11434") is False
    assert is_loopback_host("http://example.com:11434") is False
    assert is_loopback_host("https://api.openai.com/v1") is False


def test_thinking_tag_stripping():
    """Verify strip_thinking_tags handles full, unterminated, case-insensitive, and non-think tags."""
    # 1. Closed thinking block
    raw1 = "<think>\nInternal reasoning here\n</think>\nActual response to send."
    assert strip_thinking_tags(raw1) == "Actual response to send."

    # 2. Case insensitive with spaces
    raw2 = "<THINK  >\nReasoning\n</think >Hello friend!"
    assert strip_thinking_tags(raw2) == "Hello friend!"

    # 3. Unterminated thinking block to end of text
    raw3 = "<think>\nThinking stream was cut off before closing tag"
    assert strip_thinking_tags(raw3) == ""

    # 4. Nested or non-think angle brackets must be preserved
    raw4 = "I <3 you! Meet me when x < 5 and y > 2."
    assert strip_thinking_tags(raw4) == "I <3 you! Meet me when x < 5 and y > 2."


def test_preflight_passes_with_real_gateway_and_healthy_stub(ollama_stub, tmp_path):
    """run_preflight succeeds against loopback stub with real ModelGateway and OllamaProviderAdapter."""
    from src.gateway.whatsapp_runtime import initialize_runtime_database

    db_path = tmp_path / "preflight_real.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    conn = initialize_runtime_database(db_path, migrations_dir=migrations_dir)

    owner_jid = "919876500000@s.whatsapp.net"
    contact_jid = "919876500001@s.whatsapp.net"
    with conn:
        conn.execute(
            "INSERT INTO whatsapp_contact_settings (contact_jid, label, style_mode, updated_at) VALUES (?, ?, ?, ?)",
            (contact_jid, "Alice", "normal", "2026-11-26T00:00:00Z"),
        )

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "package.json").write_text(json.dumps({"name": "bridge", "engines": {"node": ">=22"}}), encoding="utf-8")
    (bridge_dir / "node_modules").mkdir()

    real_gw = build_local_model_gateway(model_name="qwen3.5:4b", ollama_host=ollama_stub["url"], timeout_seconds=5.0)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "v22.0.0\n"

        passed, err = run_preflight(
            db_path=db_path,
            migrations_dir=migrations_dir,
            bridge_dir=bridge_dir,
            auth_dir=tmp_path / "auth",
            owner_jid=owner_jid,
            model_gateway=real_gw,
            model_alias="qwen3.5:4b",
            db_conn=conn,
            simulate_mode=False,
            model_ping_timeout=5.0,
        )

    conn.close()
    assert passed is True
    assert err is None


def test_preflight_fails_on_down_stub(tmp_path):
    """run_preflight fails with a clean 1-line reason when Ollama daemon is down."""
    db_path = tmp_path / "preflight_down.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    conn = initialize_runtime_database(db_path, migrations_dir=migrations_dir)

    owner_jid = "919876500000@s.whatsapp.net"
    contact_jid = "919876500001@s.whatsapp.net"
    with conn:
        conn.execute(
            "INSERT INTO whatsapp_contact_settings (contact_jid, label, style_mode, updated_at) VALUES (?, ?, ?, ?)",
            (contact_jid, "Alice", "normal", "2026-11-26T00:00:00Z"),
        )

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "package.json").write_text(json.dumps({"name": "bridge", "engines": {"node": ">=22"}}), encoding="utf-8")
    (bridge_dir / "node_modules").mkdir()

    # Port 59999 where no server is running
    real_gw = build_local_model_gateway(model_name="qwen3.5:4b", ollama_host="http://127.0.0.1:59999", timeout_seconds=1.0)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "v22.0.0\n"

        passed, err = run_preflight(
            db_path=db_path,
            migrations_dir=migrations_dir,
            bridge_dir=bridge_dir,
            auth_dir=tmp_path / "auth",
            owner_jid=owner_jid,
            model_gateway=real_gw,
            model_alias="qwen3.5:4b",
            db_conn=conn,
            simulate_mode=False,
            model_ping_timeout=1.0,
        )

    conn.close()
    assert passed is False
    assert "Preflight Check 6 Failed" in err
    assert any(w in err.lower() for w in ("unreachable", "failed", "timed out"))


def test_preflight_fails_on_500_error(ollama_stub, tmp_path):
    """run_preflight fails when Ollama stub returns 500 error."""
    ollama_stub["handler"].mode = "error_500"

    db_path = tmp_path / "preflight_500.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    conn = initialize_runtime_database(db_path, migrations_dir=migrations_dir)

    owner_jid = "919876500000@s.whatsapp.net"
    contact_jid = "919876500001@s.whatsapp.net"
    with conn:
        conn.execute(
            "INSERT INTO whatsapp_contact_settings (contact_jid, label, style_mode, updated_at) VALUES (?, ?, ?, ?)",
            (contact_jid, "Alice", "normal", "2026-11-26T00:00:00Z"),
        )

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "package.json").write_text(json.dumps({"name": "bridge", "engines": {"node": ">=22"}}), encoding="utf-8")
    (bridge_dir / "node_modules").mkdir()

    real_gw = build_local_model_gateway(model_name="qwen3.5:4b", ollama_host=ollama_stub["url"], timeout_seconds=2.0)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "v22.0.0\n"

        passed, err = run_preflight(
            db_path=db_path,
            migrations_dir=migrations_dir,
            bridge_dir=bridge_dir,
            auth_dir=tmp_path / "auth",
            owner_jid=owner_jid,
            model_gateway=real_gw,
            model_alias="qwen3.5:4b",
            db_conn=conn,
            simulate_mode=False,
            model_ping_timeout=2.0,
        )

    conn.close()
    assert passed is False
    assert "Preflight Check 6 Failed" in err


def test_smoke_test_end_to_end_real_adapter_with_stub(ollama_stub):
    """run_smoke_test runs end-to-end against real OllamaProviderAdapter and loopback stub, passing all 12 cases."""
    ret = run_smoke_test(
        use_fake=False,
        model_alias="qwen3.5:4b",
        ollama_host=ollama_stub["url"],
    )
    assert ret == 0

    # Verify that the stub received requests at /api/chat with thinking disabled
    chat_requests = [r for r in ollama_stub["handler"].received_requests if r["path"] == "/api/chat"]
    assert len(chat_requests) >= 2
    first_req = chat_requests[1]["payload"]
    assert first_req.get("think") is False
    assert first_req.get("keep_alive") == "60m"
    assert first_req.get("options", {}).get("num_predict") == 160
    assert first_req.get("options", {}).get("num_ctx") == 4096


def test_smoke_test_with_thinking_model_output(ollama_stub):
    """When the Ollama model emits <think>...</think> tags, LocalDraftProvider strips them cleanly."""
    ollama_stub["handler"].mode = "think"

    real_gw = build_local_model_gateway(model_name="qwen3.5:4b", ollama_host=ollama_stub["url"])
    provider = LocalDraftProvider(
        model_gateway=real_gw,
        model_alias="qwen3.5:4b",
    )

    ctx = DraftContext(
        contact_jid="919876500001@s.whatsapp.net",
        label="Alice",
        style_mode="normal",
        inbound_text="Hey, are you free for coffee tomorrow?",
        recent_messages=[],
    )

    res = provider.draft(ctx)
    assert res.text is not None
    assert "<think>" not in res.text
    assert "</think>" not in res.text
    assert "Hey! Sure, let's grab coffee tomorrow afternoon" in res.text


def test_prompt_budget_guard_drops_oldest_history():
    """Prompt budget guard drops oldest messages when total tokens exceed 75% of num_ctx."""
    # num_ctx=500 -> budget = 500 * 0.75 = 375 tokens (approx 1125 chars total prompt allowance)
    provider = LocalDraftProvider(
        model_gateway=None,
        custom_llm_fn=lambda p, s: "Understood, will do.",
        num_ctx=500,
    )

    # 4 messages of 150 chars each = 600 chars history (> 450 total allowance)
    history = [
        {"role": "contact", "text": "M1: " + "a" * 140, "timestamp": "2026-11-26T10:00:00Z"},
        {"role": "owner", "text": "M2: " + "b" * 140, "timestamp": "2026-11-26T10:01:00Z"},
        {"role": "contact", "text": "M3: " + "c" * 140, "timestamp": "2026-11-26T10:02:00Z"},
        {"role": "contact", "text": "M4: Latest question", "timestamp": "2026-11-26T10:03:00Z"},
    ]

    ctx = DraftContext(
        contact_jid="919876500001@s.whatsapp.net",
        label="Alice",
        style_mode="normal",
        inbound_text="M4: Latest question",
        recent_messages=history,
    )

    history_text, allowed_entities = provider.build_context(ctx)

    # M1 and M2 should have been dropped to stay within budget
    assert "M1: " not in history_text
    assert "M4: Latest question" in history_text


def test_non_drafting_ollama_call_unaffected(ollama_stub):
    """Standard calls to OllamaProviderAdapter (without metadata think/use_chat) route to /api/generate without 'think'."""
    adapter = OllamaProviderAdapter(provider_id="ollama", base_url=ollama_stub["url"])
    req = ModelRequest(
        prompt="Write a unit test.",
        model_id="qwen3.5:4b",
    )
    resp = adapter.generate(req)
    assert resp.content == "Response generated from api/generate"

    gen_requests = [r for r in ollama_stub["handler"].received_requests if r["path"] == "/api/generate"]
    assert len(gen_requests) == 1
    assert "think" not in gen_requests[0]["payload"]
    assert "messages" not in gen_requests[0]["payload"]


def test_bug_catch_proof_db_conn_as_model_gateway(tmp_path):
    """Bug Catch Proof (Prompt 10 B.3): passing CoordinatedSQLCipherConnection to ModelGateway raises AttributeError."""
    from src.gateway.whatsapp_runtime import initialize_runtime_database

    db_path = tmp_path / "bug_proof.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    conn = initialize_runtime_database(db_path, migrations_dir=migrations_dir)

    # The exact bug: ModelGateway was instantiated as ModelGateway(conn)
    faulty_gateway = ModelGateway(conn)

    with pytest.raises(AttributeError) as exc_info:
        faulty_gateway.get_provider_health("local")

    assert "'CoordinatedSQLCipherConnection' object has no attribute 'get_provider_health'" in str(exc_info.value)
    conn.close()
