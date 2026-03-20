"""Cloud LLM calls (Gemini, OpenAI, Anthropic) with provider fallback."""

from __future__ import annotations

from typing import Callable

import httpx

from repofix import config as cfg
from repofix.output import display

# ── OpenAI-compatible chat ─────────────────────────────────────────────────────


def _openai_base_url() -> str:
    u = cfg.get_openai_base_url().strip().rstrip("/")
    return u or "https://api.openai.com/v1"


def _generate_openai(prompt: str, *, max_tokens: int) -> str:
    key = cfg.get_openai_api_key()
    if not key:
        raise RuntimeError("OpenAI API key not configured")
    model = cfg.load().openai_model.strip() or "gpt-4o-mini"
    base = _openai_base_url()
    url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a precise assistant. Follow instructions exactly.",
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI: empty choices")
    msg = choices[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    if not content:
        raise RuntimeError("OpenAI: empty content")
    return content


def _generate_anthropic(prompt: str, *, max_tokens: int) -> str:
    key = cfg.get_anthropic_api_key()
    if not key:
        raise RuntimeError("Anthropic API key not configured")
    model = cfg.load().anthropic_model.strip() or "claude-3-5-haiku-20241022"
    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "system": "You are a precise assistant. Follow instructions exactly.",
        "messages": [{"role": "user", "content": prompt}],
    }
    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            url,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
    blocks = data.get("content") or []
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(str(b.get("text", "")))
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("Anthropic: empty content")
    return text


def _generate_gemini(prompt: str, *, max_tokens: int = 2048) -> str:
    _ = max_tokens
    key = cfg.get_gemini_key()
    if not key:
        raise RuntimeError("Gemini API key not configured")
    model = cfg.load().gemini_model.strip() or "gemini-2.0-flash-lite"
    from google import genai  # type: ignore

    client = genai.Client(api_key=key)
    response = client.models.generate_content(model=model, contents=prompt)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini: empty response")
    return text


_GENERATORS: dict[str, Callable[..., str]] = {
    "openai": _generate_openai,
    "anthropic": _generate_anthropic,
    "gemini": _generate_gemini,
}


def _provider_configured(name: str) -> bool:
    if name == "openai":
        return bool(cfg.get_openai_api_key())
    if name == "anthropic":
        return bool(cfg.get_anthropic_api_key())
    if name == "gemini":
        return bool(cfg.get_gemini_key())
    return False


def cloud_provider_try_order() -> list[str]:
    """
    Ordered list of cloud providers to try for this request.

    Honors `ai_cloud_provider` (auto = any key, stable default order) and
    `ai_cloud_fallback` (whether to try additional providers after failure).
    """
    c = cfg.load()
    primary = (c.ai_cloud_provider or "auto").strip().lower()
    all_names = ["gemini", "openai", "anthropic"]

    if primary == "auto":
        ordered = [n for n in all_names if _provider_configured(n)]
        return ordered

    if primary not in all_names:
        primary = "gemini"

    result: list[str] = []
    if _provider_configured(primary):
        result.append(primary)
    if c.ai_cloud_fallback:
        for n in all_names:
            if n != primary and _provider_configured(n):
                result.append(n)
    if not result:
        return [n for n in all_names if _provider_configured(n)]
    return result


def generate_cloud(
    prompt: str,
    *,
    task_label: str = "cloud LLM",
    max_tokens: int = 1024,
) -> str:
    """
    Run the prompt against the first available cloud provider; on error,
    try fallbacks when enabled in config.
    """
    order = cloud_provider_try_order()
    if not order:
        raise RuntimeError(
            "No cloud AI provider configured. Run: repofix config set-key … "
            "or repofix config set-provider"
        )
    last_exc: Exception | None = None
    for name in order:
        gen = _GENERATORS[name]
        try:
            display.muted(f"({task_label}: {name})")
            return gen(prompt, max_tokens=max_tokens)
        except Exception as exc:
            last_exc = exc
            display.warning(f"{name} request failed ({task_label}): {exc}")
    if last_exc:
        raise last_exc
    raise RuntimeError("No cloud provider succeeded")
