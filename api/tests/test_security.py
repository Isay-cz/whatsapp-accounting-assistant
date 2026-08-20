import hashlib
import hmac

from core.security import internal_auth_header, verify_meta_signature

APP_SECRET = "test-app-secret"


def _sign(body: bytes) -> str:
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_passes():
    body = b'{"object": "whatsapp_business_account"}'
    assert verify_meta_signature(APP_SECRET, body, _sign(body)) is True


def test_tampered_body_fails():
    body = b'{"object": "whatsapp_business_account"}'
    signature = _sign(body)
    tampered = body + b"x"
    assert verify_meta_signature(APP_SECRET, tampered, signature) is False


def test_missing_header_fails():
    assert verify_meta_signature(APP_SECRET, b"{}", None) is False


def test_malformed_header_fails():
    assert verify_meta_signature(APP_SECRET, b"{}", "not-a-valid-header") is False


def test_internal_auth_header_shape():
    assert internal_auth_header("abc123") == {"Authorization": "Bearer abc123"}
