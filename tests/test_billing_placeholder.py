from __future__ import annotations

import pytest

from app.main import billing_top_up_placeholder


@pytest.mark.asyncio
async def test_billing_top_up_remains_a_non_payment_contact_affordance() -> None:
    response = await billing_top_up_placeholder(None)  # type: ignore[arg-type]

    assert response.label == "Add credit"
    assert response.action == "contact_us"
    assert response.enabled is False
