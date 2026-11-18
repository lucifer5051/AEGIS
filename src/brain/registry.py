"""AgentRegistry — Manages independent specialized agents, profiles, and persistent sessions.

Canonical Reference:
    PROMPT 080: Functional Stabilization, Conversation Repair & Model Manager
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.brain.models import AgentProfile, AgentSession, AgentStatus, SessionMessage

logger = logging.getLogger(__name__)


def get_default_system_agents() -> List[AgentProfile]:
    """Default first-party specialized agents seeded at system initialization."""
    return [
        AgentProfile(
            agent_id="friday",
            name="Friday",
            description="Default conversational assistant, executive advisor and primary user companion.",
            persona="Poised, highly intelligent, capable executive assistant who orchestrates complex tasks with calm confidence.",
            system_instructions=(
                "You are Friday, the primary conversational partner for SAGE. "
                "Engage helpfully with the user, answer questions, maintain natural dialogue, "
                "and coordinate with the Central Brain when specialized execution is requested."
            ),
            model_alias="balanced",
            skills=["workspace.inspect", "research.diagnose"],
            tool_permissions=["file_read", "file_list", "research_query"],
            memory_namespace="agent:friday",
            execution_mode="BALANCED",
            is_system=True,
            status=AgentStatus.ONLINE,
            avatar_color="#8B45F5",  # Primary Kiro Purple
        ),
        AgentProfile(
            agent_id="researcher",
            name="Atlas",
            description="Deep Research, evidence synthesis, and codebase diagnosis specialist.",
            persona="Rigorous, analytical investigator who breaks down technical problems with empirical precision.",
            system_instructions=(
                "You are Atlas, SAGE's specialized Deep Research and diagnostic agent. "
                "Focus on systematic analysis, codebase inspection, verification, and structured technical reporting."
            ),
            model_alias="reasoning",
            skills=["research.diagnose", "workspace.inspect"],
            tool_permissions=["file_read", "file_list", "file_search", "research_query"],
            memory_namespace="agent:researcher",
            execution_mode="DEEP",
            is_system=True,
            status=AgentStatus.ONLINE,
            avatar_color="#00D2FF",  # Cyan Accent
        ),
        AgentProfile(
            agent_id="coder",
            name="Hephaestus",
            description="Code engineering, test synthesis, and software verification specialist.",
            persona="Pragmatic senior software architect focused on clean minimal implementations and flawless test passes.",
            system_instructions=(
                "You are Hephaestus, SAGE's dedicated Code & Engineering specialist. "
                "Implement precise changes, respect architectural invariants, run unit tests, and write reliable code."
            ),
            model_alias="coding",
            skills=["dev.verify", "workspace.inspect"],
            tool_permissions=["file_read", "file_write", "file_list", "python_eval", "dev_runner"],
            memory_namespace="agent:coder",
            execution_mode="BALANCED",
            is_system=True,
            status=AgentStatus.ONLINE,
            avatar_color="#10B981",  # Emerald Green
        ),
        AgentProfile(
            agent_id="operator",
            name="Hermes",
            description="Desktop Awareness, UI automation, and system control operator.",
            persona="Accurate, safety-conscious systems operator managing UI controls and environment commands.",
            system_instructions=(
                "You are Hermes, SAGE's Desktop and Systems Operator. "
                "Interact with desktop applications, inspect system status, and ensure operations execute safely under TrustCore."
            ),
            model_alias="fast",
            skills=["desktop.automation", "workspace.inspect"],
            tool_permissions=["desktop_action", "file_list", "file_read"],
            memory_namespace="agent:operator",
            execution_mode="FAST",
            is_system=True,
            status=AgentStatus.ONLINE,
            avatar_color="#F59E0B",  # Amber Warning Accent
        ),
    ]


class AgentRegistry:
    """Thread-safe catalog of independent agents and their isolated conversation sessions with SQLite persistence."""

    def __init__(self, db_conn: Optional[sqlite3.Connection] = None) -> None:
        self._lock = threading.RLock()
        self._conn = db_conn
        self._agents: Dict[str, AgentProfile] = {}
        self._sessions: Dict[str, AgentSession] = {}
        self._session_messages: Dict[str, List[SessionMessage]] = {}

        # Seed default system agents in memory
        for default_agent in get_default_system_agents():
            self._agents[default_agent.agent_id] = default_agent

        # Initialize SQLite tables and load persistent sessions/messages
        if self._conn:
            self._init_db()
            self._load_from_db()

        # Ensure default session exists for each agent
        for agent_id in ("friday", "researcher", "coder", "operator"):
            self.get_or_create_default_session(agent_id)

    def _init_db(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_profiles (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    persona TEXT NOT NULL,
                    system_instructions TEXT NOT NULL,
                    model_alias TEXT NOT NULL,
                    skills TEXT NOT NULL,
                    tool_permissions TEXT NOT NULL,
                    memory_namespace TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    is_system INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    avatar_color TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    session_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_activity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    active_task_id TEXT,
                    pending_approval_id TEXT,
                    message_count INTEGER NOT NULL,
                    preview TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS session_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    task_id TEXT,
                    delegation_id TEXT,
                    tool_calls TEXT NOT NULL,
                    model_name TEXT
                )
            """)
            try:
                cur.execute("ALTER TABLE session_messages ADD COLUMN model_name TEXT")
            except Exception:
                pass
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_session_messages_session 
                ON session_messages(session_id, timestamp)
            """)
            self._conn.commit()

    def _load_from_db(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            # Load custom agent profiles
            cur.execute("SELECT agent_id, name, description, persona, system_instructions, model_alias, skills, tool_permissions, memory_namespace, execution_mode, is_system, status, avatar_color, created_at FROM agent_profiles")
            for r in cur.fetchall():
                try:
                    profile = AgentProfile(
                        agent_id=r[0],
                        name=r[1],
                        description=r[2],
                        persona=r[3],
                        system_instructions=r[4],
                        model_alias=r[5],
                        skills=json.loads(r[6]),
                        tool_permissions=json.loads(r[7]),
                        memory_namespace=r[8],
                        execution_mode=r[9],
                        is_system=bool(r[10]),
                        status=AgentStatus(r[11]),
                        avatar_color=r[12],
                        created_at=r[13],
                    )
                    self._agents[profile.agent_id] = profile
                except Exception as exc:
                    logger.debug("Error loading agent profile %s: %s", r[0], exc)

            # Load active sessions
            cur.execute("SELECT session_id, agent_id, title, created_at, last_activity, status, active_task_id, pending_approval_id, message_count, preview FROM agent_sessions")
            for r in cur.fetchall():
                try:
                    sess = AgentSession(
                        session_id=r[0],
                        agent_id=r[1],
                        title=r[2],
                        created_at=r[3],
                        last_activity=r[4],
                        status=r[5],
                        active_task_id=r[6],
                        pending_approval_id=r[7],
                        message_count=r[8],
                        preview=r[9],
                    )
                    self._sessions[sess.session_id] = sess
                    self._session_messages[sess.session_id] = []
                except Exception as exc:
                    logger.debug("Error loading session %s: %s", r[0], exc)

            # Load historical session messages
            cur.execute("SELECT message_id, session_id, agent_id, role, content, timestamp, task_id, delegation_id, tool_calls, model_name FROM session_messages ORDER BY timestamp ASC")
            for r in cur.fetchall():
                try:
                    msg = SessionMessage(
                        message_id=r[0],
                        session_id=r[1],
                        agent_id=r[2],
                        role=r[3],
                        content=r[4],
                        timestamp=r[5],
                        task_id=r[6],
                        delegation_id=r[7],
                        tool_calls=json.loads(r[8]) if r[8] else [],
                        model_name=r[9] if len(r) > 9 else None,
                    )
                    if msg.session_id not in self._session_messages:
                        self._session_messages[msg.session_id] = []
                    self._session_messages[msg.session_id].append(msg)
                except Exception as exc:
                    logger.debug("Error loading message %s: %s", r[0], exc)

    # -------------------------------------------------------------------------
    # Agent Profile Operations
    # -------------------------------------------------------------------------

    def register_agent(self, profile: AgentProfile) -> AgentProfile:
        """Register or update an independent agent profile."""
        with self._lock:
            self._agents[profile.agent_id] = profile
            if self._conn:
                cur = self._conn.cursor()
                cur.execute("""
                    INSERT OR REPLACE INTO agent_profiles
                    (agent_id, name, description, persona, system_instructions, model_alias, skills, tool_permissions, memory_namespace, execution_mode, is_system, status, avatar_color, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    profile.agent_id, profile.name, profile.description, profile.persona,
                    profile.system_instructions, profile.model_alias, json.dumps(profile.skills),
                    json.dumps(profile.tool_permissions), profile.memory_namespace,
                    profile.execution_mode, 1 if profile.is_system else 0,
                    profile.status.value, profile.avatar_color, profile.created_at
                ))
                self._conn.commit()
            logger.info("Registered agent: %s (%s)", profile.name, profile.agent_id)
            return profile

    def get_agent(self, agent_id: str) -> Optional[AgentProfile]:
        """Retrieve an agent profile by unique ID."""
        with self._lock:
            return self._agents.get(agent_id)

    def list_agents(self) -> List[AgentProfile]:
        """List all registered agents."""
        with self._lock:
            return list(self._agents.values())

    def update_agent_status(self, agent_id: str, status: AgentStatus) -> None:
        """Update live availability of an agent."""
        with self._lock:
            if agent_id in self._agents:
                self._agents[agent_id] = self._agents[agent_id].model_copy(
                    update={"status": status}
                )
                if self._conn:
                    cur = self._conn.cursor()
                    cur.execute("UPDATE agent_profiles SET status = ? WHERE agent_id = ?", (status.value, agent_id))
                    self._conn.commit()

    # -------------------------------------------------------------------------
    # Session & Conversation Operations (Strict Agent Isolation)
    # -------------------------------------------------------------------------

    def create_session(self, agent_id: str, title: str = "New Conversation") -> AgentSession:
        """Create a new independent session strictly scoped to one agent."""
        with self._lock:
            if agent_id not in self._agents:
                raise ValueError(f"Agent '{agent_id}' does not exist in registry.")

            session = AgentSession(
                session_id=str(uuid4()),
                agent_id=agent_id,
                title=title,
            )
            self._sessions[session.session_id] = session
            self._session_messages[session.session_id] = []

            if self._conn:
                cur = self._conn.cursor()
                cur.execute("""
                    INSERT INTO agent_sessions
                    (session_id, agent_id, title, created_at, last_activity, status, active_task_id, pending_approval_id, message_count, preview)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    session.session_id, session.agent_id, session.title,
                    session.created_at, session.last_activity, session.status,
                    session.active_task_id, session.pending_approval_id,
                    session.message_count, session.preview
                ))
                self._conn.commit()

            return session

    def get_or_create_default_session(self, agent_id: str) -> AgentSession:
        """Return the most recent active session for an agent, or create one."""
        with self._lock:
            existing = self.list_sessions_for_agent(agent_id)
            if existing:
                return existing[0]
            agent = self.get_agent(agent_id)
            title = f"{agent.name if agent else 'Agent'} Session"
            return self.create_session(agent_id, title=title)

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        """Retrieve a session by its unique ID."""
        with self._lock:
            return self._sessions.get(session_id)

    def list_sessions_for_agent(self, agent_id: str) -> List[AgentSession]:
        """List sessions belonging strictly to the specified agent."""
        with self._lock:
            sessions = [
                s for s in self._sessions.values()
                if s.agent_id == agent_id and s.status == "active"
            ]
            sessions.sort(key=lambda s: s.last_activity, reverse=True)
            return sessions

    def list_all_sessions(self) -> List[AgentSession]:
        """List all active sessions across all agents."""
        with self._lock:
            sessions = [s for s in self._sessions.values() if s.status == "active"]
            sessions.sort(key=lambda s: s.last_activity, reverse=True)
            return sessions

    def record_message(
        self,
        session_id: str,
        role: str,
        content: str,
        task_id: Optional[str] = None,
        delegation_id: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        model_name: Optional[str] = None,
    ) -> SessionMessage:
        """Record an isolated conversation message in a session with persistence."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise ValueError(f"Session '{session_id}' not found.")

            msg = SessionMessage(
                session_id=session_id,
                agent_id=session.agent_id,
                role=role,
                content=content,
                task_id=task_id,
                delegation_id=delegation_id,
                model_name=model_name,
                tool_calls=tool_calls or [],
            )

            if session_id not in self._session_messages:
                self._session_messages[session_id] = []
            self._session_messages[session_id].append(msg)

            # Update session metadata
            now_iso = datetime.now(timezone.utc).isoformat()
            preview = content[:100].replace("\n", " ")
            updated_session = session.model_copy(
                update={
                    "last_activity": now_iso,
                    "message_count": session.message_count + 1,
                    "preview": preview,
                    "active_task_id": task_id or session.active_task_id,
                }
            )
            self._sessions[session_id] = updated_session

            # Persist message and updated session
            if self._conn:
                cur = self._conn.cursor()
                cur.execute("""
                    INSERT INTO session_messages
                    (message_id, session_id, agent_id, role, content, timestamp, task_id, delegation_id, tool_calls, model_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    msg.message_id, msg.session_id, msg.agent_id, msg.role,
                    msg.content, msg.timestamp, msg.task_id, msg.delegation_id,
                    json.dumps(msg.tool_calls), msg.model_name
                ))
                cur.execute("""
                    UPDATE agent_sessions
                    SET last_activity = ?, message_count = ?, preview = ?, active_task_id = ?
                    WHERE session_id = ?
                """, (now_iso, updated_session.message_count, preview, updated_session.active_task_id, session_id))
                self._conn.commit()

            return msg

    def get_session_history(self, session_id: str, limit: int = 100) -> List[SessionMessage]:
        """Retrieve isolated message history for a given session."""
        with self._lock:
            msgs = self._session_messages.get(session_id, [])
            return msgs[-limit:]
