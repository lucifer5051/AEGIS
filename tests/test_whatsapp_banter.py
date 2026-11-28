"""Tests for WhatsApp Banter Style, Guardrails, Sensitive Topic Demotion, and Card Formatting.

Canonical Reference:
    Prompt 6 Phase 3 (3.1 - 3.4)
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.gateway.drafts import DraftContext, DraftManager, format_approval_card
from src.gateway.local_draft_provider import (
    ContentGuards,
    DraftGenerationError,
    LocalDraftProvider,
    contains_banned_terms,
    is_protected_topic,
    load_banned_terms,
)
from src.security.contact_key_store import ContactKeyStore
from src.storage.database import initialize_runtime_database

CANONICAL_PN = "919876543210@s.whatsapp.net"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_whatsapp_banter.db"
    conn = initialize_runtime_database(db_file, migrations_dir=MIGRATIONS_DIR)
    return conn


@pytest.fixture
def key_store(temp_db):
    return ContactKeyStore(temp_db)


# ==============================================================================
# 3.1 BANTER PROMPT & NORMAL PROMPT SEPARATION
# ==============================================================================

def test_3_1_banter_prompt_includes_banter_rules():
    """Banter mode prompt contains playful banter rules and strict guardrails."""
    captured_sys = []

    def mock_llm(p, s):
        captured_sys.append(s)
        return "Nice try, but you still owe me pizza."

    provider = LocalDraftProvider(custom_llm_fn=mock_llm)
    ctx = DraftContext(
        contact_jid=CANONICAL_PN,
        label="Bob",
        style_mode="banter",
        inbound_text="I won our gaming match yesterday!",
        recent_messages=[],
    )
    provider.draft(ctx)

    assert len(captured_sys) == 1
    sys_prompt = captured_sys[0]
    assert "BANTER STYLE RULES" in sys_prompt
    assert "friendly teasing" in sys_prompt
    assert "at most TWO short sentences" in sys_prompt
    assert "NO mean-spirited attacks" in sys_prompt
    assert "NO references to sensitive topics" in sys_prompt


def test_3_1_normal_prompt_does_not_include_banter_rules():
    """Normal mode prompt does not include banter rules."""
    captured_sys = []

    def mock_llm(p, s):
        captured_sys.append(s)
        return "Congratulations on your win."

    provider = LocalDraftProvider(custom_llm_fn=mock_llm)
    ctx = DraftContext(
        contact_jid=CANONICAL_PN,
        label="Bob",
        style_mode="normal",
        inbound_text="I won our gaming match yesterday!",
        recent_messages=[],
    )
    provider.draft(ctx)

    assert len(captured_sys) == 1
    sys_prompt = captured_sys[0]
    assert "BANTER STYLE RULES" not in sys_prompt
    assert "friendly teasing" not in sys_prompt


# ==============================================================================
# 3.2 SENSITIVE / PROTECTED TOPIC AUTOMATIC DEMOTION
# ==============================================================================

@pytest.mark.parametrize(
    "sensitive_text,expected_category",
    [
        ("My father was admitted to the hospital ICU today.", "hospital"),
        ("I received a diagnosis of cancer from the doctor.", "doctor"),
        ("My cousin died in a car accident yesterday, RIP.", "died"),
        ("I had to declare bankruptcy and lost my job.", "bankruptcy"),
        ("The funeral service is scheduled for tomorrow morning.", "funeral"),
        ("What do you think about the prime minister and election corruption?", "election"),
        ("Did you hear about the communal religious riots near the temple?", "temple"),
    ],
)
def test_3_2_protected_topics_detection(sensitive_text, expected_category):
    """Protected topics detector catches sensitive medical, financial, family, political, and religious topics."""
    is_prot, topic = is_protected_topic(sensitive_text)
    assert is_prot is True
    assert topic is not None


def test_3_2_inbound_sensitive_topic_demotes_banter_to_normal():
    """When inbound message touches a protected topic, style_mode is demoted to normal."""
    captured_sys = []

    def mock_llm(p, s):
        captured_sys.append(s)
        return "I am so sorry to hear that. Please take care."

    provider = LocalDraftProvider(
        custom_llm_fn=mock_llm,
        content_guards=ContentGuards(banned_terms=True, protected_topics=True),
    )
    ctx = DraftContext(
        contact_jid=CANONICAL_PN,
        label="Bob",
        style_mode="banter",  # Contact prefers banter
        inbound_text="Hey, my grandmother passed away this morning.",  # Sensitive topic
        recent_messages=[],
    )
    res = provider.draft(ctx)

    assert len(captured_sys) == 1
    sys_prompt = captured_sys[0]
    # Invariant: Demoted to normal prompt despite contact settings
    assert "BANTER STYLE RULES" not in sys_prompt
    assert "I am so sorry to hear that" in res


# ==============================================================================
# 3.2 BANNED PROFANITY & REGENERATION IN NORMAL MODE
# ==============================================================================

@pytest.mark.parametrize(
    "profane_text,expected_term",
    [
        ("You are such an idiot sometimes.", "idiot"),
        ("What a stupid idea that was.", "stupid"),
        ("Stop being a bhenchod and reply.", "bhenchod"),
        ("Tu motha chutiya ahes re.", "chutiya"),
        ("Kahi pan faltu bolu nako.", "faltu"),
        ("Tu lavdya sarkha vagto.", "lavdya"),
    ],
)
def test_3_2_multilingual_banned_terms(profane_text, expected_term):
    """Contains banned terms catches English, Hindi, and Marathi profanity."""
    has_banned, term = contains_banned_terms(profane_text)
    assert has_banned is True
    assert term == expected_term


def test_3_2_draft_with_banned_term_regenerates_in_normal_mode():
    """Draft with banned profanity is rejected and regenerated in normal mode."""
    attempts = [0]
    prompts_seen = []

    def mock_llm(p, s):
        attempts[0] += 1
        prompts_seen.append(s)
        if attempts[0] == 1:
            return "Haha you idiot, you totally failed."
        return "Haha nice try, better luck next time."

    provider = LocalDraftProvider(
        custom_llm_fn=mock_llm,
        content_guards=ContentGuards(banned_terms=True, protected_topics=True),
    )
    ctx = DraftContext(
        contact_jid=CANONICAL_PN,
        label="Bob",
        style_mode="banter",
        inbound_text="Did you see my score?",
        recent_messages=[],
    )
    res = provider.draft(ctx)

    # Invariant: First attempt caught for banned term 'idiot', second attempt succeeded in normal mode
    assert attempts[0] == 2
    assert "BANTER STYLE RULES" in prompts_seen[0]
    assert "BANTER STYLE RULES" not in prompts_seen[1]
    assert res == "Haha nice try, better luck next time."


# ==============================================================================
# 3.3 CARD FORMATTING FOR BANTER
# ==============================================================================

def test_3_3_card_formatting_contains_banter_tag():
    """Card prominently displays [BANTER] tag when in banter mode."""
    card = format_approval_card(
        draft_id="bntr",
        label="Bob",
        contact_jid=CANONICAL_PN,
        style_mode="banter",
        their_text="Where are you?",
        draft_text="Lost in your boring stories.",
        is_placeholder=False,
    )
    assert "[AEGIS] Draft #BNTR [BANTER]" in card
    assert "mode: banter" in card
    assert "#approve BNTR | #edit BNTR <text> | #reject BNTR" in card


def test_3_3_card_formatting_normal_mode_no_banter_tag():
    """Card does not contain [BANTER] tag when in normal mode."""
    card = format_approval_card(
        draft_id="norm",
        label="Bob",
        contact_jid=CANONICAL_PN,
        style_mode="normal",
        their_text="Where are you?",
        draft_text="On my way.",
        is_placeholder=False,
    )
    assert "[AEGIS] Draft #NORM\n" in card
    assert "[BANTER]" not in card


# ==============================================================================
# 3.1 & 3.4 TWO-SENTENCE CONSTRAINT
# ==============================================================================

def test_3_4_banter_enforces_two_sentence_limit():
    """Banter mode truncates output to at most two short sentences."""
    three_sentence_banter = (
        "Nice try on that score! You still owe me pizza though. Don't even think about running away."
    )
    provider = LocalDraftProvider(
        custom_llm_fn=lambda p, s: three_sentence_banter
    )
    ctx = DraftContext(
        contact_jid=CANONICAL_PN,
        label="Bob",
        style_mode="banter",
        inbound_text="I beat your score!",
        recent_messages=[],
    )
    res = provider.draft(ctx)

    # Invariant: Exactly the first two sentences kept
    assert res == "Nice try on that score! You still owe me pizza though."
    assert "Don't even think about running away." not in res
