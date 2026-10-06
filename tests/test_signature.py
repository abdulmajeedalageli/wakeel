from app.security import verify_signature
from tests.conftest import TEST_APP_SECRET, sign

BODY = b'{"object":"whatsapp_business_account","entry":[]}'


def test_valid_signature_is_accepted():
    assert verify_signature(BODY, sign(BODY), TEST_APP_SECRET)


def test_signature_made_with_wrong_secret_is_rejected():
    assert not verify_signature(BODY, sign(BODY, secret="someone-else"), TEST_APP_SECRET)


def test_tampered_body_is_rejected():
    header = sign(BODY)
    assert not verify_signature(BODY + b" ", header, TEST_APP_SECRET)


def test_missing_header_is_rejected():
    assert not verify_signature(BODY, None, TEST_APP_SECRET)
    assert not verify_signature(BODY, "", TEST_APP_SECRET)


def test_header_without_sha256_prefix_is_rejected():
    digest_only = sign(BODY).removeprefix("sha256=")
    assert not verify_signature(BODY, digest_only, TEST_APP_SECRET)


def test_garbage_header_is_rejected():
    assert not verify_signature(BODY, "sha256=not-hex-ÿ", TEST_APP_SECRET)
