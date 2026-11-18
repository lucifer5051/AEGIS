"""Models for SAGE Central Brain, Independent Agents, Sessions and Delegation.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & Independent Multi-Agent System
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class BrainIntent(str, Enum):
    """Classified intent for an incoming user or channel message."""
    CONVERSATIONAL = "conversational"
    ACTIONABLE_TASK = "actionable_task"
    MULTI_AGENT_DELEGATION = "multi_agent_delegation"
    STATUS_QUERY = "status_query"
    TASK_CANCEL = "task_cancel"
    ESCALATION = "escalation"


class IntentClassification(BaseModel):
    """Result of Central Brain intent and routing classification."""
    intent: BrainIntent
    target_agent_id: Optional[str] = None
    confidence: float = 1.0
    requires_reasoning_model: bool = False
    extracted_goal: Optional[str] = None
    rationale: str = ""


class AgentStatus(str, Enum):
    """Live availability state of an independent agent."""
    ONLINE = "online"
    BUSY = "busy"
    IDLE = "idle"
    OFFLINE = "offline"


class AgentProfile(BaseModel):
    """Specification of an independent specialized agent."""
    agent_id: str
    name: str
    description: str
    persona: str
    system_instructions: str
    model_alias: str = "balanced"
    skills: List[str] = Field(default_factory=list)
    tool_permissions: List[str] = Field(default_factory=list)
    memory_namespace: str
    execution_mode: str = "BALANCED"  # FAST, BALANCED, DEEP, CUSTOM
    is_system: bool = False
    status: AgentStatus = AgentStatus.ONLINE
    avatar_color: str = "#8B45F5"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class DelegationStatus(str, Enum):
    """Lifecycle state of an agent-to-agent delegation assignment."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class DelegationRecord(BaseModel):
    """Controlled record of work delegated between agents."""
    delegation_id: str = Field(default_factory=lambda: str(uuid4()))
    correlation_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    source_agent_id: str
    destination_agent_id: str
    assignment: str
    shared_context_refs: List[str] = Field(default_factory=list)
    expected_output: str = ""
    status: DelegationStatus = DelegationStatus.PENDING
    result: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    completed_at: Optional[str] = None


class AgentSession(BaseModel):
    """Independent conversation session scoped strictly to one agent."""
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    agent_id: str
    title: str = "New Conversation"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_activity: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = "active"
    active_task_id: Optional[str] = None
    pending_approval_id: Optional[str] = None
    message_count: int = 0
    preview: str = ""


class SessionMessage(BaseModel):
    """Single message within an agent's independent session history."""
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    agent_id: str
    role: str  # "user", "agent", "system", "tool"
    content: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    task_id: Optional[str] = None
    delegation_id: Optional[str] = None
    model_name: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
