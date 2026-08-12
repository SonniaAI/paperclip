from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.schemas import RegisterRequest


def v2_registration(**overrides: object) -> RegisterRequest:
    payload: dict[str, object] = {
        "account_type": "individual",
        "full_name": "Jordan Example",
        "email": "jordan@example.test",
        "password": "correct-horse-battery-staple",
        "country": "Singapore",
        "phone": "+65 8123 4567",
        "date_of_birth": "1990-05-18",
        "role": "owner_founder",
        "industry": "solar_renewable_energy",
        "terms_version": "2026-08-12",
        "marketing_consent": False,
    }
    payload.update(overrides)
    return RegisterRequest.model_validate(payload)


def test_register_request_requires_and_normalises_v2_profile_fields() -> None:
    registration = v2_registration()

    assert registration.phone == "+6581234567"
    assert registration.date_of_birth == date(1990, 5, 18)
    assert registration.gender is None
    assert registration.role == "owner_founder"
    assert registration.industry == "solar_renewable_energy"
    assert registration.country == "Singapore"
    assert registration.terms_version == "2026-08-12"
    assert registration.marketing_consent is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phone", "81234567"),
        ("role", "Founder"),
        ("industry", "Solar energy"),
        ("country", " "),
        (
            "date_of_birth",
            date.today().replace(year=date.today().year - 18) + timedelta(days=1),
        ),
    ],
)
def test_register_request_rejects_invalid_v2_profile_fields(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        v2_registration(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "phone",
        "date_of_birth",
        "role",
        "industry",
        "country",
        "terms_version",
        "marketing_consent",
    ],
)
def test_register_request_requires_every_v2_profile_field(field: str) -> None:
    payload = v2_registration().model_dump()
    payload.pop(field)

    with pytest.raises(ValidationError):
        RegisterRequest.model_validate(payload)


def test_gender_is_optional_and_rejects_unknown_values() -> None:
    assert v2_registration().gender is None
    assert v2_registration(gender="prefer_not_to_say").gender == "prefer_not_to_say"
    with pytest.raises(ValidationError):
        v2_registration(gender="not-listed")


def test_date_of_birth_accepts_the_exact_eighteenth_birthday() -> None:
    today = date.today()
    assert v2_registration(
        date_of_birth=today.replace(year=today.year - 18)
    ).date_of_birth


def test_individual_country_is_retained() -> None:
    assert v2_registration(country=" Singapore ").country == "Singapore"


def test_business_requires_company_name_and_size() -> None:
    with pytest.raises(ValidationError):
        v2_registration(account_type="business", company_name="Sonnia Solar")
    registration = v2_registration(
        account_type="business", company_name="Sonnia Solar", company_size="2_10"
    )
    assert registration.company_name == "Sonnia Solar"
    assert registration.company_size == "2_10"


def test_registration_metadata_is_validated() -> None:
    registration = v2_registration(
        company_website=" https://sonnia.ai/register ",
        referral_source="word_of_mouth",
        marketing_consent=True,
    )
    assert registration.company_website == "https://sonnia.ai/register"
    assert registration.referral_source == "word_of_mouth"
    assert registration.marketing_consent is True
    with pytest.raises(ValidationError):
        v2_registration(company_website="javascript:alert(1)")
