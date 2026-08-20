FALLBACK_TITLE_LENGTH = 60


def fallback_title(raw_text: str) -> str:
    """Título determinista: los primeros ~60 caracteres del mensaje crudo.
    No depende del LLM para existir."""
    text = raw_text.strip()
    if len(text) <= FALLBACK_TITLE_LENGTH:
        return text
    return text[:FALLBACK_TITLE_LENGTH].rstrip() + "…"
