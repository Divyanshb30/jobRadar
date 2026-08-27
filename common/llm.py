"""Minimal single-completion LLM helper for free-form text (outreach copy).

The scorer has its own batch-JSON machinery; this is the lightweight counterpart
for one-off text/JSON generation. It reuses the same free keys and endpoints from
``config/scoring.yaml`` and prefers Groq (fast, generous free tier) then falls
back to Gemini. Returns "" on total failure so callers degrade gracefully.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests

from common import config

log = logging.getLogger("jobradar")


def _groq_effort(model: str) -> str | None:
    m = model.lower()
    if "gpt-oss" in m:
        return "low"
    if "qwen3" in m or "deepseek" in m:
        return "none"
    return None


def complete(prompt: str, *, want_json: bool = False,
             temperature: float = 0.4, max_tokens: int = 1200) -> str:
    """Return the model's text for ``prompt`` (Groq first, then Gemini)."""
    sc = config.scoring()["gemini"]
    groq_key = config.env(sc.get("groq_api_key_env", "GROQ_API_KEY"))
    gem_key = config.env(sc.get("api_key_env", "GEMINI_API_KEY"))

    if groq_key:
        text = _try_groq(sc, groq_key, prompt, want_json, temperature, max_tokens)
        if text:
            return text
    if gem_key:
        text = _try_gemini(sc, gem_key, prompt, want_json, temperature)
        if text:
            return text
    log.warning("[llm] no usable provider for completion")
    return ""


def _try_groq(sc: dict, key: str, prompt: str, want_json: bool,
              temperature: float, max_tokens: int) -> str:
    model = "openai/gpt-oss-120b"
    payload: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    effort = _groq_effort(model)
    if effort is not None:
        payload["reasoning_effort"] = effort
    elif want_json:
        payload["response_format"] = {"type": "json_object"}
    endpoint = sc.get("groq_endpoint",
                      "https://api.groq.com/openai/v1/chat/completions")
    for attempt in range(2):
        try:
            r = requests.post(endpoint,
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"},
                              json=payload, timeout=60)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"].get("content", "").strip()
            if r.status_code in (429, 500, 502, 503) and attempt == 0:
                time.sleep(6)      # transient — one short backoff then retry
                continue
            log.warning("[llm] groq HTTP %s: %s", r.status_code, r.text[:150])
            return ""
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            log.warning("[llm] groq failed: %s", exc)
            return ""
    return ""


def _try_gemini(sc: dict, key: str, prompt: str, want_json: bool,
                temperature: float) -> str:
    model = "gemini-flash-latest"
    gen: dict[str, Any] = {"temperature": temperature}
    if want_json:
        gen["responseMimeType"] = "application/json"
    endpoint = sc["endpoint"].format(model=model)
    for attempt in range(2):
        try:
            r = requests.post(endpoint, params={"key": key},
                              json={"contents": [{"parts": [{"text": prompt}]}],
                                    "generationConfig": gen}, timeout=60)
            if r.status_code == 200:
                parts = (r.json().get("candidates", [{}])[0]
                         .get("content", {}).get("parts", []))
                return "".join(p.get("text", "") for p in parts).strip()
            if r.status_code in (429, 500, 503) and attempt == 0:
                time.sleep(6)
                continue
            log.warning("[llm] gemini HTTP %s: %s", r.status_code, r.text[:150])
            return ""
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            log.warning("[llm] gemini failed: %s", exc)
            return ""
    return ""


def complete_json(prompt: str, **kwargs: Any) -> dict[str, Any] | None:
    """``complete`` + tolerant JSON parse. Returns None if nothing parseable."""
    text = complete(prompt, want_json=True, **kwargs)
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None
