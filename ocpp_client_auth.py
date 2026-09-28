"""Shared HTTP Basic auth header construction for OCPP-J charge point clients (s6.2.2).

A charge point authenticates itself to the Central System with its identity as the username
and its authorization key as the password (see main.py's authorize() for the server side).
Extracted here, rather than left inline in simulate_charge_point.py, so both that script and
the test suite in tests/ build the same header the same way instead of each re-implementing
the same few lines -- and so the tests can import it without importing simulate_charge_point.py
itself, which is a standalone script that runs on import.
"""

import base64


def basic_auth_header(identity, key, raw_password=False):
    """Build the Authorization header for the handshake (OCPP-J 1.6 s6.2.2).

    The key is sent as its 40-character hexadecimal form by default. The example in the spec
    sends the raw 20 bytes instead, which raw_password=True does.
    """
    password = bytes.fromhex(key) if raw_password else key.encode("ascii")
    token = base64.b64encode(identity.encode("utf-8") + b":" + password).decode("ascii")
    return f"Basic {token}"
