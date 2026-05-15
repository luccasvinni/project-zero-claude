"""
log_ai.py — AI token usage logger
==================================
Logs every AI API call (Anthropic, OpenAI, Google Gemini) to [dbo].[openai]
on the atimo_platform SQL Server database.

REQUIRED DB COLUMN (run once if not yet present):
    ALTER TABLE [dbo].[openai]
        ADD openai_provider NVARCHAR(32) NULL;

Usage:
    from log_ai import log_ai_tokens

    response = client.messages.create(...)
    log_ai_tokens(response, task="reader_extract", atimo_team=True)
"""

from __future__ import annotations

import json
from typing import Any


# ── Provider detection ───────────────────────────────────────────────────────

def _detect_provider(model: str) -> str:
    """Derive provider name from model string."""
    m = (model or "").lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3") or m.startswith("text-davinci"):
        return "openai"
    if m.startswith("gemini"):
        return "google"
    return "unknown"


# ── Response meta extraction ─────────────────────────────────────────────────

def _extract_meta(response: Any, model_hint: str = "") -> dict:
    """
    Extract (model, response_id, input_tokens, output_tokens) from any AI
    response object or dict.

    Supported formats:
      • Anthropic SDK  — anthropic.types.Message
      • Google Gemini  — google.genai.types.GenerateContentResponse
      • OpenAI SDK     — openai.types.chat.ChatCompletion
      • Plain dict     — already-parsed JSON body
    """
    # ── Anthropic SDK Message ────────────────────────────────────────────────
    if hasattr(response, "usage") and hasattr(response, "model") and hasattr(
        getattr(response, "usage", None), "input_tokens"
    ):
        return {
            "model":         response.model,
            "response_id":   getattr(response, "id", None),
            "input_tokens":  response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

    # ── Google Gemini GenerateContentResponse ────────────────────────────────
    if hasattr(response, "usage_metadata"):
        um = response.usage_metadata
        return {
            "model":         model_hint or getattr(response, "model_version", "gemini"),
            "response_id":   None,
            "input_tokens":  getattr(um, "prompt_token_count",     0) or 0,
            "output_tokens": getattr(um, "candidates_token_count", 0) or 0,
        }

    # ── OpenAI SDK ChatCompletion ────────────────────────────────────────────
    if hasattr(response, "usage") and hasattr(response, "model") and hasattr(
        getattr(response, "usage", None), "prompt_tokens"
    ):
        return {
            "model":         response.model,
            "response_id":   getattr(response, "id", None),
            "input_tokens":  response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
        }

    # ── Plain dict (already-parsed JSON) ────────────────────────────────────
    if isinstance(response, dict):
        usage = response.get("usage", {})
        return {
            "model":         response.get("model", model_hint),
            "response_id":   response.get("id"),
            "input_tokens":  int(usage.get("input_tokens",  usage.get("prompt_tokens",     0)) or 0),
            "output_tokens": int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0),
        }

    # ── Fallback ─────────────────────────────────────────────────────────────
    return {
        "model":         model_hint,
        "response_id":   None,
        "input_tokens":  0,
        "output_tokens": 0,
    }


# ── Main public function ─────────────────────────────────────────────────────

def log_ai_tokens(
    response:    Any,
    task:        str,
    atimo_team:  bool,
    project_id:  int | None = None,
    customer_id: int | None = None,
    model_hint:  str = "",
) -> dict:
    """
    Log an AI API call to [dbo].[openai].

    Args:
        response:    The raw response object from Anthropic, Gemini, or OpenAI.
        task:        Short label for the operation, e.g. "reader_extract".
        atimo_team:  True when the request originates from the Atimo internal team.
        project_id:  Platform project ID (optional; None when called outside CMS).
        customer_id: Platform customer ID (optional).
        model_hint:  Model name to fall back to if it cannot be read from `response`
                     (required for Gemini calls where the model isn't on the response).

    Returns:
        dict with keys: provider, model, task, usage {input_tokens, output_tokens}
    """
    meta     = _extract_meta(response, model_hint)
    provider = _detect_provider(meta["model"])

    result = {
        "provider": provider,
        "model":    meta["model"],
        "task":     task,
        "usage": {
            "input_tokens":  meta["input_tokens"],
            "output_tokens": meta["output_tokens"],
        },
    }

    try:
        from db import get_conn  # lazy import so tests can mock it
        conn = get_conn()
        cur  = conn.cursor()

        sql = """
            INSERT INTO [dbo].[openai] (
                openai_projectid,
                openai_customerid,
                openai_responseid,
                openai_model,
                openai_provider,
                openai_input_tokens,
                openai_output_tokens,
                openai_task,
                openai_atimoteam,
                openai_timestamp
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, GETUTCDATE())
        """

        cur.execute(sql, (
            project_id,
            customer_id,
            meta["response_id"],
            meta["model"],
            provider,
            meta["input_tokens"],
            meta["output_tokens"],
            task,
            1 if atimo_team else 0,
        ))
        conn.commit()
        cur.close()
        conn.close()

    except Exception as e:
        # Never crash the agent because of a logging failure
        print(f"[log_ai_tokens] DB error ({task}): {e}")

    return result
