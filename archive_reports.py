"""Redact passwords from exported operation text."""

import re
from collections.abc import Iterable


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    values = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    return re.sub("|".join(re.escape(secret) for secret in values), "[已隐藏]", text) if values else text
