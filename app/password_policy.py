"""Password policy for manager.sonnia.ai (Registration & Password spec §4).

Enforces composition rules, the top-10,000 common-password list with
leetspeak substitution normalisation, the ``sonnia`` brand string, the
user's own name / email local part / company name, sequences, repeats and
keyboard walks, and a Have I Been Pwned range check via k-anonymity (only
the first five characters of the SHA-1 prefix ever leave the server).

Every check returns a plain-language, affirmative-friendly violation message
the caller can surface verbatim.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

MIN_LENGTH = 10
MAX_LENGTH = 128
HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"
HIBP_TIMEOUT_SECONDS = 5.0

_COMMON_PASSWORDS: frozenset[str] | None = None

# leetspeak → plain letter substitutions used before comparing against the
# common list and the brand/name checks (p@ssw0rd → password, etc.).
_LEET = str.maketrans(
    {
        "@": "a",
        "4": "a",
        "8": "b",
        "3": "e",
        "6": "g",
        "9": "g",
        "1": "i",
        "!": "i",
        "0": "o",
        "$": "s",
        "5": "s",
        "7": "t",
        "+": "t",
        "2": "z",
    }
)

_SEQUENCES = (
    "abcdefghijklmnopqrstuvwxyz",
    "zyxwvutsrqponmlkjihgfedcba",
    "0123456789",
    "9876543210",
    "qwertyuiop",
    "poiuytrewq",
    "asdfghjkl",
    "lkjhgfdsa",
    "zxcvbnm",
    "mnbvcxz",
    "qwerty",
    "asdf",
    "zxcv",
)

_KEYBOARD_WALK = re.compile(
    r"(qwerty|asdf|zxcv|qaz|wsx|edc|rfv|tgb|yhn|ujm|ik,|ol\.|p;/|1qaz|2wsx|3edc|4rfv|5tgb|6yhn|7ujm|8ik,|9ol\.|0p;/)",
    re.IGNORECASE,
)

_REPEAT = re.compile(r"^(.)\1{2,}$")
_SEQUENCE_RUN = re.compile(
    r"("
    + "|".join(re.escape(seq[i : i + 4]) for seq in _SEQUENCES for i in range(len(seq) - 3))
    + r")",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PasswordCheckContext:
    """Personalised context used for the §4.2 'user's own' rejections."""

    display_name: str = ""
    email: str = ""
    company_name: str = ""


def _load_common_passwords() -> frozenset[str]:
    global _COMMON_PASSWORDS
    if _COMMON_PASSWORDS is None:
        path = Path(__file__).with_name("data") / "top_10000_passwords.txt"
        try:
            _COMMON_PASSWORDS = frozenset(
                line.strip().lower()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except OSError:
            logger.exception(
                "Could not load the top-10k password list; common-list checks disabled"
            )
            _COMMON_PASSWORDS = frozenset()
    return _COMMON_PASSWORDS


def _normalised(value: str) -> str:
    """Lower-case and leetspeak-normalise a candidate password."""
    return value.lower().translate(_LEET)


def _personal_fragments(context: PasswordCheckContext) -> set[str]:
    fragments: set[str] = set()
    if context.display_name:
        fragments.update(
            part.lower()
            for part in re.findall(r"[A-Za-z]+", context.display_name)
            if len(part) >= 3
        )
    if context.email:
        local = context.email.split("@", 1)[0].lower()
        fragments.add(local)
        fragments.update(part for part in re.split(r"[^a-z0-9]+", local) if len(part) >= 3)
    if context.company_name:
        fragments.update(
            part.lower()
            for part in re.findall(r"[A-Za-z]+", context.company_name)
            if len(part) >= 3
        )
    return fragments


def composition_violations(password: str) -> list[str]:
    """§4.1 composition rules, in affirmative plain language."""
    violations: list[str] = []
    if len(password) < MIN_LENGTH:
        violations.append(f"Use at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        violations.append(f"Use no more than {MAX_LENGTH} characters.")
    if not re.search(r"[A-Z]", password):
        violations.append("Include an uppercase letter.")
    if not re.search(r"[a-z]", password):
        violations.append("Include a lowercase letter.")
    if not re.search(r"\d", password):
        violations.append("Include a number.")
    if not re.search(r"[^A-Za-z0-9]", password):
        violations.append("Include a special character.")
    return violations


def common_password_violations(password: str) -> list[str]:
    """§4.2 common-password and substitution rejections."""
    normalised = _normalised(password)
    if normalised in _load_common_passwords():
        return ["Choose a password that is not among the most common passwords."]
    # Also reject when the password embeds a common entry as its core
    # (e.g. a long passphrase that is just "password" with decorations).
    for common in (
        "password",
        "letmein",
        "admin",
        "qwerty",
        "welcome",
        "monkey",
        "dragon",
        "master",
    ):
        if common in normalised:
            return ["Choose a password that is not based on a common password."]
    if "sonnia" in normalised:
        return ["Choose a password that does not contain the Sonnia brand name."]
    return []


def personal_violations(password: str, context: PasswordCheckContext) -> list[str]:
    """§4.2 rejections for the user's own name, email local part, company name."""
    normalised = _normalised(password)
    for fragment in _personal_fragments(context):
        if fragment and fragment in normalised:
            return ["Choose a password that does not contain your name, email or company name."]
    return []


def pattern_violations(password: str) -> list[str]:
    """§4.2 sequences, repeats and keyboard walks."""
    lowered = password.lower()
    if _REPEAT.match(password):
        return ["Choose a password without repeated characters."]
    if _KEYBOARD_WALK.search(lowered):
        return ["Choose a password without keyboard patterns such as qwerty."]
    if _SEQUENCE_RUN.search(lowered):
        return ["Choose a password without sequences such as abc123."]
    return []


async def hibp_violations(password: str) -> list[str]:
    """Have I Been Pwned range check via k-anonymity (spec §4.2).

    Only the first five characters of the SHA-1 digest are sent; the full
    password never leaves the server. Fails open on network error so a
    provider outage cannot lock out every registration, but the outage is
    logged for monitoring.
    """
    digest = sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    try:
        async with httpx.AsyncClient(timeout=HIBP_TIMEOUT_SECONDS) as client:
            response = await client.get(HIBP_RANGE_URL.format(prefix=prefix))
            response.raise_for_status()
    except (httpx.HTTPError, OSError):
        logger.warning("HIBP range check unavailable; continuing without it")
        return []
    for line in response.text.splitlines():
        candidate, _, count = line.partition(":")
        if candidate.strip().upper() == suffix:
            return ["Choose a password that has not appeared in a known data breach."]
    return []


async def password_violations(
    password: str,
    context: PasswordCheckContext | None = None,
    *,
    check_hibp: bool = True,
) -> list[str]:
    """All §4.1/§4.2 violations for a candidate password, in display order."""
    violations: list[str] = []
    violations.extend(composition_violations(password))
    violations.extend(common_password_violations(password))
    violations.extend(pattern_violations(password))
    if context is not None:
        violations.extend(personal_violations(password, context))
    if check_hibp:
        violations.extend(await hibp_violations(password))
    return violations
