"""Contract checks for the labelled customer-memory screen shipped with the SPA shell."""

from pathlib import Path

SHELL = (Path(__file__).resolve().parents[1] / "infra/spa-shell/index.html").read_text(
    encoding="utf-8"
)


def test_memory_view_separates_authoritative_and_fuzzy_customer_memory() -> None:
    assert "CRM memory" in SHELL
    assert "Deterministic CRM record" in SHELL
    assert "Conversation recall" in SHELL
    assert "AI-assisted recall; verify before use." in SHELL
    assert "/api/voice/contacts/${encodeURIComponent(contactId)}/memory-recall" in SHELL


def test_memory_view_explicitly_handles_fuzzy_provider_unavailability() -> None:
    assert 'result.hindsight_status === "unavailable"' in SHELL
    assert "AI-assisted recall is unavailable. CRM records are still available above." in SHELL
    assert "Source date:" in SHELL
    assert "Source call:" in SHELL
