import json

import httpx
import pytest

from services.llm.deepseek import DeepSeekExtractor
from services.llm.fallback import fallback_title

BASE_URL = "https://api.deepseek.test"


def _extractor() -> DeepSeekExtractor:
    return DeepSeekExtractor(
        api_key="test-key", model="deepseek-v4-flash", base_url=BASE_URL, timeout_seconds=5.0
    )


async def test_extract_title_success(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE_URL}/chat/completions",
        json={
            "choices": [
                {"message": {"content": json.dumps({"title": "Cliente pide CFDI de enero"})}}
            ]
        },
    )
    result = await _extractor().extract_title(["necesito mi cfdi de enero por favor"])
    assert result.source == "llm"
    assert result.title == "Cliente pide CFDI de enero"
    assert result.error is None


async def test_extract_title_falls_back_on_http_error(httpx_mock):
    httpx_mock.add_response(url=f"{BASE_URL}/chat/completions", status_code=500)
    messages = ["mensaje corto de cliente"]
    result = await _extractor().extract_title(messages)
    assert result.source == "fallback"
    assert result.title == fallback_title("\n".join(messages))
    assert result.error is not None


async def test_extract_title_falls_back_on_malformed_json(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE_URL}/chat/completions",
        json={"choices": [{"message": {"content": "esto no es json"}}]},
    )
    messages = ["otro mensaje de cliente"]
    result = await _extractor().extract_title(messages)
    assert result.source == "fallback"
    assert result.title == fallback_title("\n".join(messages))


async def test_extract_title_falls_back_on_timeout(httpx_mock):
    httpx_mock.add_exception(httpx.TimeoutException("timed out"))
    messages = ["mensaje lento de cliente"]
    result = await _extractor().extract_title(messages)
    assert result.source == "fallback"
    assert result.title == fallback_title("\n".join(messages))


def test_fallback_title_truncates_long_text():
    long_text = "a" * 100
    title = fallback_title(long_text)
    assert title.endswith("…")
    assert len(title) == 61  # 60 chars + ellipsis


def test_fallback_title_keeps_short_text_as_is():
    assert fallback_title("mensaje corto") == "mensaje corto"
