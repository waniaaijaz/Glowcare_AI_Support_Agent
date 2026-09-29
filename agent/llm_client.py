"""LLM client for Gemini, OpenAI or a local Ollama model (set LLM_PROVIDER in .env).

Retries rate limits and temporary outages, and keeps a running count of tokens
and estimated cost. Check the connection with:

    python agent/llm_client.py
"""

import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

# Some home networks have a broken IPv6 route to Google: the TLS handshake then hangs and dies with
# SSLEOFError while IPv4 works fine. Resolving to IPv4 only avoids that. Set FORCE_IPV4=off to undo.
if os.getenv("FORCE_IPV4", "on").strip().lower() != "off":
    import socket
    import urllib3.util.connection
    urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()

# Rewording a reply, or a question for search, into another language (support_agent.py,
# responder.py): seconds per attempt, and one retry after a timeout or dropped connection.
TRANSLATE_TIMEOUT = 20
TRANSLATE_RETRIES = 1

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b").strip()

MODEL_NAME = {"openai": OPENAI_MODEL, "gemini": GEMINI_MODEL}.get(LLM_PROVIDER, OLLAMA_MODEL)

# Set MATCH_CUSTOMER_LANGUAGE=off in .env to skip the extra translation calls.
MATCH_CUSTOMER_LANGUAGE = os.getenv("MATCH_CUSTOMER_LANGUAGE", "on").strip().lower() != "off"

# The documents are in English, so a question in another language is restated in English before the search.
# Leave this on even when MATCH_CUSTOMER_LANGUAGE is off, or Roman Urdu questions often find nothing.
TRANSLATE_FOR_SEARCH = os.getenv("TRANSLATE_FOR_SEARCH", "on").strip().lower() != "off"

class LLMUnavailableError(RuntimeError):
    """Raised when the LLM service can't be reached, or stays overloaded after retrying.
    """
    pass


# Statuses worth retrying, and how many seconds to wait before each attempt.
# Kept short: a customer is waiting for the reply.
TRANSIENT_STATUS = {500, 502, 503, 504}
RATE_LIMIT_WAITS = [10, 20]
OVERLOAD_WAITS = [2, 4, 8]

MAX_ANSWER_TOKENS = 300


def _call_chat_api(provider, url, api_key, model, prompt, system_prompt, timeout, patient=True):
    """Call an OpenAI-compatible chat completions endpoint (used for both OpenAI and Gemini).
    """
    key_name = f"{provider.upper()}_API_KEY"
    if not api_key:
        raise RuntimeError(
            f"LLM_PROVIDER is '{provider}' but {key_name} is empty. Add your key "
            f"to the .env file ({key_name}=...), or change LLM_PROVIDER."
        )
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {"model": model, "messages": messages,
               "max_completion_tokens": MAX_ANSWER_TOKENS}
    # Gemini's reasoning models need extra output room; low effort keeps replies fast and cheap.
    if provider == "gemini":
        payload["max_completion_tokens"] = 2000
        payload["reasoning_effort"] = "low"
        if model.startswith("gemini-2"):
            payload["temperature"] = 0
    elif model.startswith(("gpt-5", "o1", "o3", "o4")):
        payload["max_completion_tokens"] = 2000
    else:
        payload["temperature"] = 0
        payload["seed"] = 42

    attempt = 0
    while True:
        try:
            response = requests.post(
                url, json=payload, timeout=timeout,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            status, error = response.status_code, None
        except requests.exceptions.RequestException as e:
            response, status, error = None, None, e
        waits = (RATE_LIMIT_WAITS if status == 429 else OVERLOAD_WAITS) if patient else []
        transient = error is not None or status in TRANSIENT_STATUS or status == 429
        if not transient or attempt >= len(waits):
            break
        what = ("rate limit hit" if status == 429 else
                f"busy (error {status})" if status else "not reachable")
        print(f"[LLM] {provider} {what}, waiting {waits[attempt]}s and retrying "
              f"({attempt + 1}/{len(waits)})...")
        time.sleep(waits[attempt])
        attempt += 1

    if error is not None:
        raise LLMUnavailableError(f"Could not reach the {provider} API after {attempt + 1} tries. "
                                  f"Check your internet connection. Original error: {error}") from error

    if response.status_code != 200:
        try:
            body = response.json()
            if isinstance(body, list):
                body = body[0]
            detail = body.get("error", {}).get("message", response.text)
        except (ValueError, AttributeError, IndexError):
            detail = response.text
        key_page = {"openai": "platform.openai.com/api-keys",
                    "gemini": "aistudio.google.com/apikey"}[provider]
        hints = {
            400: f"Check {key_name} and the model name in .env.",
            401: f"Your {key_name} is wrong or was revoked. Copy it again from {key_page}.",
            403: f"Your {key_name} isn't allowed to use this API. Copy it again from {key_page}.",
            404: f"The model '{model}' doesn't exist or your account can't use it. "
                 f"Check {provider.upper()}_MODEL in .env.",
            429: "Rate limit still hit after retrying, or your quota/credit is used up.",
            503: "The provider is overloaded right now. This is on their side and usually "
                 "passes within minutes.",
        }
        error_class = LLMUnavailableError if response.status_code in TRANSIENT_STATUS else RuntimeError
        raise error_class(f"{provider} returned error {response.status_code}"
                          + (f" after {attempt + 1} tries" if attempt else "") + ". "
                          f"{hints.get(response.status_code, '')} Details: {detail}")

    data = response.json()
    answer = data["choices"][0]["message"].get("content") or ""
    usage = data.get("usage") or {}
    _record_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    return answer.strip()


def _call_ollama(prompt: str, system_prompt: str, timeout: int) -> str:
    """Call a local Ollama server."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "system": system_prompt,
        "options": {"temperature": 0, "seed": 42},
        "stream": False,
    }
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        response.raise_for_status()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        raise LLMUnavailableError(
            "Could not reach Ollama at http://localhost:11434 (or it timed out). "
            "Is Ollama running? Start it with `ollama serve` "
            "(or it may already be running as a background service — "
            "check with `ollama list`)."
        ) from e
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"Ollama returned an error. Is the model '{OLLAMA_MODEL}' pulled? "
            f"Run `ollama pull {OLLAMA_MODEL}` if not. Original error: {e}"
        ) from e
    return response.json()["response"].strip()


# USD per million tokens (input, output). Only used for the cost estimate.
PRICES_PER_MILLION = {
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
}
usage_totals = {"input_tokens": 0, "output_tokens": 0, "calls": 0}


def _record_usage(input_tokens: int, output_tokens: int):
    """Add one call's token counts to the running totals."""
    usage_totals["input_tokens"] += input_tokens
    usage_totals["output_tokens"] += output_tokens
    usage_totals["calls"] += 1


def estimated_cost_usd():
    """Estimated USD cost of the calls so far, or None when the model's price is unknown.
    """
    if LLM_PROVIDER not in ("openai", "gemini") or MODEL_NAME not in PRICES_PER_MILLION:
        return None
    price_in, price_out = PRICES_PER_MILLION[MODEL_NAME]
    return (usage_totals["input_tokens"] * price_in
            + usage_totals["output_tokens"] * price_out) / 1_000_000


def call_llm(prompt: str, system_prompt: str = "", timeout: int = 60, patient: bool = True) -> str:
    """Send one prompt to the configured provider and return the reply text.

    patient=False makes a single attempt with no waiting between retries. Use it for optional calls
    (such as rewording a reply) whose caller has a fallback, so a busy provider never delays a customer.
    """
    if LLM_PROVIDER == "openai":
        return _call_chat_api("openai", OPENAI_URL, OPENAI_API_KEY, OPENAI_MODEL,
                              prompt, system_prompt, timeout, patient)
    if LLM_PROVIDER == "gemini":
        return _call_chat_api("gemini", GEMINI_URL, GEMINI_API_KEY, GEMINI_MODEL,
                              prompt, system_prompt, timeout, patient)
    if LLM_PROVIDER == "ollama":
        return _call_ollama(prompt, system_prompt, timeout)
    raise RuntimeError(f"Unknown LLM_PROVIDER '{LLM_PROVIDER}' in .env. "
                       "Use 'gemini', 'openai' or 'ollama'.")


# Old name, kept as an alias.
call_ollama = call_llm


if __name__ == "__main__":
    print(f"Provider: {LLM_PROVIDER} | model: {MODEL_NAME}")
    answer = call_llm(
        prompt="Say 'LLM connection working' and nothing else.",
        system_prompt="You are a test assistant. Follow instructions exactly.",
    )
    print(f"Response: {answer}")
    if LLM_PROVIDER in ("openai", "gemini"):
        cost = estimated_cost_usd()
        print(f"Tokens used: {usage_totals} | est. cost: "
              + (f"${cost:.6f}" if cost is not None else "unknown for this model"))
