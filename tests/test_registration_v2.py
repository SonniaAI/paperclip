from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas import RegisterRequest


def v2_registration(**overrides: object) -> RegisterRequest:
    payload: dict[str, object] = {
        "account_type": "individual",
        "full_name": "Jordan Example",
        "email": "jordan@example.test",
        "password": "correct-horse-battery-staple",
        "phone": "+65 8123 4567",
        "date_of_birth": "1990-05-18",
        "gender": "prefer_not_to_say",
        "role": "Founder",
        "industry": "Solar energy",
    }
    payload.update(overrides)
    return RegisterRequest.model_validate(payload)


def test_register_request_requires_and_normalises_v2_profile_fields() -> None:
    registration = v2_registration()

    assert registration.phone == "+6581234567"
    assert registration.date_of_birth == date(1990, 5, 18)
    assert registration.gender == "prefer_not_to_say"
    assert registration.role == "Founder"
    assert registration.industry == "Solar energy"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phone", "81234567"),
        ("gender", "   "),
        ("role", "   "),
        ("industry", "   "),
        ("date_of_birth", date.today()),
    ],
)
def test_register_request_rejects_invalid_v2_profile_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        v2_registration(**{field: value})


@pytest.mark.parametrize(
    "field",
    ["phone", "date_of_birth", "gender", "role", "industry"],
)
def test_register_request_requires_every_required_v2_profile_field(field: str) -> None:
    payload = v2_registration().model_dump()
    payload.pop(field)

    with pytest.raises(ValidationError):
        RegisterRequest.model_validate(payload)


def test_v2_profile_text_is_trimmed_before_persistence() -> None:
    registration = v2_registration(
        gender=" woman ", role=" Consultant ", industry=" Professional services "
    )

    assert registration.gender == "woman"
    assert registration.role == "Consultant"
    assert registration.industry == "Professional services"


def test_company_accounts_retain_existing_company_and_country_requirements() -> None:
    with pytest.raises(ValidationError):
        v2_registration(account_type="business")

    registration = v2_registration(
        account_type="business",
        company_name="Sonnia Solar",
        country="Singapore",
    )
    assert registration.company_name == "Sonnia Solar"
    assert registration.country == "Singapore"
