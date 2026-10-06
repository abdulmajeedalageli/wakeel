"""Webhook signature verification.

Meta signs every webhook POST with our App Secret and sends the result in the
`X-Hub-Signature-256` header as "sha256=<hex digest>". If the signature doesn't
match, the request didn't come from Meta (or was changed on the way), so we reject it.
"""

import hashlib
import hmac

SIGNATURE_PREFIX = "sha256="


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Return True only if `signature_header` is a valid signature of `raw_body`.

    Important: `raw_body` must be the exact bytes Meta sent. Re-encoding the
    parsed JSON would change the bytes and break the check.
    """
    if not signature_header or not signature_header.startswith(SIGNATURE_PREFIX):
        return False
    received = signature_header[len(SIGNATURE_PREFIX):]
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    # compare_digest takes the same time whether or not the values match,
    # so an attacker can't guess the signature one character at a time.
    return hmac.compare_digest(expected.encode(), received.encode())
