KEY_PREFIX = "bot:"


def buffer_key(phone: str) -> str:
    return f"{KEY_PREFIX}buffer:{phone}"


def buffer_lock_key(phone: str) -> str:
    return f"{KEY_PREFIX}locks:buffer:{phone}"


def buffer_marker_key(phone: str) -> str:
    return f"{KEY_PREFIX}markers:buffer:{phone}"


def session_key(phone: str) -> str:
    return f"{KEY_PREFIX}session:{phone}"


def session_lock_key(phone: str, step: str) -> str:
    return f"{KEY_PREFIX}locks:session:{phone}:{step}"


def phone_from_buffer_key(key: str) -> str:
    return key.removeprefix(f"{KEY_PREFIX}buffer:")


def phone_from_session_key(key: str) -> str:
    return key.removeprefix(f"{KEY_PREFIX}session:")
