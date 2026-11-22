"""Validate browser POST origins while keeping command-line requests usable."""
import os
from urllib.parse import urlsplit


DEFAULT_PORTS = {"http": 80, "https": 443}
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def parse_origin(value):
    if any(char < "!" or char > "~" or char in "\\,%" for char in value):
        raise ValueError("Адрес должен содержать только допустимые символы.")
    parts = urlsplit(value)
    if (parts.scheme not in DEFAULT_PORTS or not parts.hostname or parts.username is not None
            or value != f"{parts.scheme}://{parts.netloc}" or parts.port == 0):
        raise ValueError("Нужен адрес HTTP или HTTPS без пути.")
    return parts.scheme, parts.hostname, parts.port or DEFAULT_PORTS[parts.scheme]


def configured_origin():
    value = os.environ.get("METEO_PUBLIC_ORIGIN", "")
    return parse_origin(value) if value else None


def allow_browser_write(headers, *, local_port, request_scheme, public_origin):
    sites = headers.get_all("Sec-Fetch-Site", [])
    if sites and sites != ["same-origin"]:
        return False
    origins = headers.get_all("Origin", [])
    if not origins:
        return True
    hosts = headers.get_all("Host", [])
    if len(origins) != 1 or len(hosts) != 1:
        return False
    try:
        origin = parse_origin(origins[0])
        scheme, hostname, port = origin
        if parse_origin(f"{scheme}://{hosts[0]}") != origin:
            return False
    except ValueError:
        return False
    return origin == public_origin or (scheme == request_scheme and hostname in LOOPBACK and port == local_port)
