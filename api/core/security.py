import hashlib
import hmac


def verify_meta_signature(app_secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    """
    Verifica el header `X-Hub-Signature-256` de un webhook de Meta: HMAC-SHA256
    sobre el body crudo, con el App Secret como llave. Formato del header:
    "sha256=<hex digest>".
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


def internal_auth_header(internal_api_token: str) -> dict[str, str]:
    """Header saliente que este bot manda al llamar a la API del sistema de
    tickets. INTERNAL_API_TOKEN es solo saliente — este repo no valida
    llamadas entrantes con él (ver CLAUDE.md, decisión #1)."""
    return {"Authorization": f"Bearer {internal_api_token}"}
