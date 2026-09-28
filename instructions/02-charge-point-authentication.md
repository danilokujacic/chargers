# 02 — Charge point authentication (HTTP Basic)

Authenticate the charge point to the Central System with HTTP Basic auth on the WebSocket
handshake, per **OCPP-J 1.6 s6.2.2**.

> **Status: already implemented** in `main.py` and `models.py`. Treat this file as the
> specification to verify against, and as the reference if it has to be rebuilt or extended.
> Do not rewrite working code to match the wording here; check behaviour, not shape.

## What authenticates what

This is the **machine** authenticating, not the driver. It proves the thing connecting is a
charger the operator knows. Authorizing a *person* to draw energy is a separate mechanism
(`Authorize.req` with an idTag) covered by `04-normal-charge-flow.md`. Do not conflate them.

## The credentials

From s6.2.2, verbatim:

> The username is equal to the charge point identity, which is the identifying string of the
> charge point as it uses it in the OCPP-J connection URL. The password is a 20-byte key that is
> stored on the charge point.

So:

| | Value |
|---|---|
| Username | the charge point identity, e.g. `CP001` — the same string as the URL path segment |
| Password | the charge point's **20-byte** authorization key |
| Key length on the wire | **40 hexadecimal characters** when set with `ChangeConfiguration` |
| Where the key lives on the device | installed at manufacture/installation, or pushed over OCPP |
| Where it lives centrally | **hashed, with a unique salt** — never in plaintext |

The key is set over OCPP with `ChangeConfiguration` on configuration key `AuthorizationKey`,
whose value is "a 40-character hexadecimal representation of the 20-byte authorization key".

## The password encoding trap

The spec's own worked example is:

```
charge point identity  AL1000
authorization key      0001020304050607FFFFFFFFFFFFFFFFFFFFFFFF
Authorization: Basic   QUwxMDAwOgABAgMEBQYH////////////////
```

Base64-decoding that yields `b"AL1000:\x00\x01\x02\x03\x04\x05\x06\x07\xff..."` — the password is
the **raw 20 bytes**, not the hex string, and those bytes are not valid UTF-8.

Consequences, both mandatory:

1. **Accept both encodings.** A 40-character ASCII hex password and a 20-raw-byte password
   describe the same key. Normalise to uppercase hex before verifying. Most real chargers send
   hex; the spec's example sends raw bytes.
2. **Parse the header yourself.** Do not use `websockets.asyncio.server.basic_auth()`: it does
   `base64.b64decode(...).decode()` guarded only by `except binascii.Error`, so a raw-byte
   password raises an uncaught `UnicodeDecodeError`. Decode only the username as text; keep the
   password as `bytes`.

## Required behaviour

Reject at the handshake, before any OCPP frame is exchanged, using
`websockets.serve(..., process_request=...)`.

| Condition | Response |
|---|---|
| No `Authorization` header | `401` + `WWW-Authenticate: Basic realm="ocpp"` |
| Scheme is not `Basic` | `401` + `WWW-Authenticate` |
| Credentials are not valid base64 | `401` + `WWW-Authenticate` |
| No `:` separating identity and key | `401` + `WWW-Authenticate` |
| Password is neither 20 bytes nor 40 hex chars | `401` + `WWW-Authenticate` |
| Identity unknown, or key does not match the stored hash | `401` + `WWW-Authenticate` |
| **Basic username ≠ URL path identity** | `403` |
| Valid | accept (`return None`) |

The username/path check is not in the spec but is required here: without it a charger holding
valid credentials could connect on another charger's URL and file transactions against it.

Build the `WWW-Authenticate` value with `websockets.headers.build_www_authenticate_basic`.

## Identity handling

Per OCPP-J 1.6 s3.1.1 the identity is one path segment, percent-encoded as necessary, and the
spec's example `RDAM 123` contains a space. Therefore:

- The server MUST percent-decode the path before comparing (`urllib.parse.unquote`).
- A client MUST percent-encode it (`quote(identity, safe="")`).
- Reject an identity containing `/` or control characters. Do not reject spaces.

## Storage

s6.2.2:

> On the Central System side, it is RECOMMENDED to store the authorization key hashed, with a
> unique salt, using a cryptographic hash algorithm designed for secure storage of passwords.

Implemented as salted `hashlib.scrypt` (N=16384, r=8, p=1, 16-byte salt), compared with
`hmac.compare_digest`. **A stored key is therefore unrecoverable**: losing it means rotating to a
new one, not reading the old one back.

Also s6.2.2, for whoever implements the charger side: "The charge point should not give back the
authorization key in response to a GetConfiguration request."

## Registration status does not gate the connection

A charger whose `registration_status` is `Pending` MUST still be allowed to connect — that is how
it gets onboarded. Authentication decides whether the socket opens; registration status is
answered in `BootNotification.conf`. See `03-commissioning-flow.md`.

## TLS

s6.2.2 is explicit that this mechanism is meant for TLS-encrypted connections, and that on an
unencrypted one "anyone who can see the network traffic between Charge Point and Central System
can see the charge point credentials, and can thus impersonate the Charge Point". The POC serves
plain `ws://`. Do not present it as secure. If asked to add TLS, note the extra constraint from
s6.2.1: "The TLS certificate SHALL be an RSA certificate with a size no greater than 2048 bytes."

## Acceptance criteria

1. All eight rows of the response table above, exercised against a running server.
2. A 40-hex-character password and the equivalent 20-raw-byte password both authenticate the
   same charger.
3. The spec's example key `0001020304050607FFFFFFFFFFFFFFFFFFFFFFFF` is accepted as a key value.
4. An identity containing a space authenticates over a percent-encoded URL.
5. The stored document contains no plaintext key: assert the issued key does not appear anywhere
   in the raw MongoDB document.
6. Two registrations of the same identity are refused by a unique index, not by a race-prone
   pre-check.
7. Rotating a key invalidates the previous one and validates the new one.

## When you are done

Write the completion brief specified in [README.md](README.md#report-when-you-finish): what
is done, the use case, why it is built this way, and how you verified it. Written for someone
fluent in Python but new to EV charging, so define the charging terms you use.

For this task, the use case is stopping someone who is not one of our chargers from connecting and
filing fake transactions; explain how it differs from authorizing a driver.
