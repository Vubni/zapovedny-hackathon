"""Explicit offline HTTP stub for tests that need the April provider payloads."""

from io import BytesIO
from urllib.parse import urlparse

from external_fill import FIXTURES, MODELS


def april_response(url, timeout):
    for source in MODELS.values():
        if urlparse(url).netloc == urlparse(source.endpoint).netloc:
            return BytesIO((FIXTURES / f"{source.id}-krasnoe-april-2024.json").read_bytes())
    raise AssertionError(f"Unexpected external weather endpoint: {url}")
