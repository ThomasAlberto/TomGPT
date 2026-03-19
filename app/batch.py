"""Batch API processing for Anthropic and OpenAI (50% cheaper, async)."""

import io
import json
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BATCH_PATH = Path(__file__).parent / "batches.json"

_anthropic_client = None
_openai_client = None

_OPENAI_REASONING_MODELS = {"o3", "o3-mini", "o4-mini"}

_PRO_CRITIQUE_PROMPT = (
    "You are a critical reviewer. Analyse the response for: "
    "1) Accuracy — factual errors or unsupported claims, "
    "2) Completeness — important missing aspects, "
    "3) Clarity — confusing or poorly explained parts, "
    "4) Relevance — parts that don't address the question. "
    "Be specific and actionable. Format as a concise bullet list."
)

_PRO_REFINE_INSTRUCTION = (
    "A reviewer identified these issues with your response:\n\n"
    "{critique}\n\n"
    "Please provide an improved response addressing these issues. "
    "Do not mention the review or that this is a revision."
)


def init(anthropic_client, openai_client):
    global _anthropic_client, _openai_client
    _anthropic_client = anthropic_client
    _openai_client = openai_client


def _load_jobs() -> dict:
    try:
        return json.loads(BATCH_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"jobs": {}}


def _save_jobs(data: dict):
    with tempfile.NamedTemporaryFile("w", dir=BATCH_PATH.parent, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2)
        temp_path = f.name
    shutil.move(temp_path, BATCH_PATH)


# ── Submission ────────────────────────────────────────────────────────────


def submit_job(config: dict) -> dict:
    """Submit a batch job. config must include: job_id, conversation_id, mode,
    provider, model, system, api_messages, user_message, thinking_budget,
    critique_model."""
    job = {
        "id": config["job_id"],
        "conversation_id": config["conversation_id"],
        "mode": config["mode"],
        "provider": config["provider"],
        "model": config["model"],
        "system": config["system"],
        "api_messages": config["api_messages"],
        "user_message": config["user_message"],
        "thinking_budget": config.get("thinking_budget", 8000),
        "critique_model": config.get("critique_model"),
        "status": "processing",
        "current_step": 0,
        "total_steps": 3 if config["mode"] == "pro" else 1,
        "batch_id": None,
        "initial_response": None,
        "critique_response": None,
        "result": None,
        "thinking": None,
        "thinking_signature": None,
        "result_applied": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "error": None,
    }

    job["batch_id"] = _submit_step(job, step=0)

    data = _load_jobs()
    data["jobs"][job["id"]] = job
    _save_jobs(data)
    logger.info("Batch job %s submitted (mode=%s, model=%s)", job["id"][:8], job["mode"], job["model"])
    return job


def _submit_step(job: dict, step: int) -> str:
    provider = job["provider"]
    model = job["model"]
    system = job["system"]
    messages = job["api_messages"]
    thinking_enabled = job["mode"] == "thinking" and step == 0
    max_tokens = 16000 if thinking_enabled else 8192

    if job["mode"] == "pro" and step == 1:
        # Critique: cheap model, fresh context
        model = job["critique_model"]
        system = _PRO_CRITIQUE_PROMPT
        messages = [{"role": "user",
                     "content": f"Question: {job['user_message']}\n\nResponse to review:\n{job['initial_response']}"}]
    elif job["mode"] == "pro" and step == 2:
        # Refine: main model, extend original messages
        messages = messages + [
            {"role": "assistant", "content": job["initial_response"]},
            {"role": "user", "content": _PRO_REFINE_INSTRUCTION.format(critique=job["critique_response"])},
        ]

    if provider == "anthropic":
        return _submit_anthropic(model, system, messages, max_tokens, thinking_enabled, job["thinking_budget"])
    return _submit_openai(model, system, messages, max_tokens)


def _submit_anthropic(model, system, messages, max_tokens, thinking_enabled, thinking_budget) -> str:
    params = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
    if thinking_enabled:
        params["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

    batch = _anthropic_client.messages.batches.create(requests=[{"custom_id": "req-0", "params": params}])
    logger.info("Anthropic batch submitted: %s", batch.id)
    return batch.id


def _submit_openai(model, system, messages, max_tokens) -> str:
    body = {"model": model, "messages": [{"role": "system", "content": system}] + messages}
    if model in _OPENAI_REASONING_MODELS:
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens

    jsonl = json.dumps({"custom_id": "req-0", "method": "POST",
                         "url": "/v1/chat/completions", "body": body}).encode() + b"\n"
    file_obj = io.BytesIO(jsonl)
    file_obj.name = "batch_input.jsonl"

    batch_file = _openai_client.files.create(file=file_obj, purpose="batch")
    batch = _openai_client.batches.create(
        input_file_id=batch_file.id, endpoint="/v1/chat/completions", completion_window="24h")
    logger.info("OpenAI batch submitted: %s", batch.id)
    return batch.id


# ── Polling & advancement ─────────────────────────────────────────────────


def check_and_advance(job_id: str) -> dict:
    """Poll batch status. Auto-advances pro mode steps. Returns job dict."""
    data = _load_jobs()
    job = data["jobs"].get(job_id)
    if not job:
        return {"status": "not_found", "error": "Job not found"}
    if job["status"] in ("completed", "failed"):
        return job

    try:
        if job["provider"] == "anthropic":
            done, text, thinking, sig, err = _poll_anthropic(job["batch_id"])
        else:
            done, text, thinking, sig, err = _poll_openai(job["batch_id"])
    except Exception as e:
        logger.error("Batch poll failed for %s: %s", job["batch_id"], e)
        return job

    if not done:
        return job

    if err:
        job["status"] = "failed"
        job["error"] = err
        _save_jobs(data)
        return job

    step = job["current_step"]
    mode = job["mode"]

    if mode == "pro":
        if step == 0:
            job["initial_response"] = text
            job["current_step"] = 1
            try:
                job["batch_id"] = _submit_step(job, step=1)
                logger.info("Pro batch: advanced to critique (job %s)", job["id"][:8])
            except Exception as e:
                job["status"] = "failed"
                job["error"] = str(e)
        elif step == 1:
            job["critique_response"] = text
            job["current_step"] = 2
            try:
                job["batch_id"] = _submit_step(job, step=2)
                logger.info("Pro batch: advanced to refine (job %s)", job["id"][:8])
            except Exception as e:
                job["status"] = "failed"
                job["error"] = str(e)
        elif step == 2:
            job["result"] = text
            job["status"] = "completed"
            job["completed_at"] = datetime.now(timezone.utc).isoformat()
    else:
        job["result"] = text
        if thinking:
            job["thinking"] = thinking
            job["thinking_signature"] = sig
        job["status"] = "completed"
        job["completed_at"] = datetime.now(timezone.utc).isoformat()

    _save_jobs(data)
    return job


def _poll_anthropic(batch_id: str) -> tuple:
    """Returns (done, text, thinking, thinking_sig, error)."""
    b = _anthropic_client.messages.batches.retrieve(batch_id)
    if b.processing_status != "ended":
        return False, None, None, None, None

    results = list(_anthropic_client.messages.batches.results(batch_id))
    if not results:
        return True, None, None, None, "No results returned"
    r = results[0]
    if r.result.type != "succeeded":
        return True, None, None, None, f"Batch error: {r.result.error}"

    text, thinking, sig = "", None, None
    for block in r.result.message.content:
        if block.type == "thinking":
            thinking = block.thinking
            sig = getattr(block, "signature", None)
        elif block.type == "text":
            text = block.text
    return True, text, thinking, sig, None


def _poll_openai(batch_id: str) -> tuple:
    """Returns (done, text, thinking, thinking_sig, error)."""
    b = _openai_client.batches.retrieve(batch_id)
    if b.status in ("validating", "in_progress", "finalizing"):
        return False, None, None, None, None
    if b.status != "completed":
        return True, None, None, None, f"Batch status: {b.status}"
    if not b.output_file_id:
        return True, None, None, None, "No output file"

    content = _openai_client.files.content(b.output_file_id)
    lines = content.text.strip().split("\n")
    if not lines:
        return True, None, None, None, "Empty result file"

    result = json.loads(lines[0])
    if result["response"]["status_code"] != 200:
        return True, None, None, None, f"Request failed: {result['response']['body']}"

    text = result["response"]["body"]["choices"][0]["message"]["content"]
    return True, text, None, None, None


# ── Management ────────────────────────────────────────────────────────────


def cancel_job(job_id: str) -> dict:
    data = _load_jobs()
    job = data["jobs"].get(job_id)
    if not job:
        return {"status": "not_found", "error": "Job not found"}
    if job["status"] in ("completed", "failed"):
        return job

    try:
        if job["provider"] == "anthropic":
            _anthropic_client.messages.batches.cancel(job["batch_id"])
        else:
            _openai_client.batches.cancel(job["batch_id"])
    except Exception as e:
        logger.warning("Failed to cancel batch %s: %s", job["batch_id"], e)

    job["status"] = "failed"
    job["error"] = "Cancelled by user"
    _save_jobs(data)
    return job


def list_jobs() -> list[dict]:
    data = _load_jobs()
    return list(data["jobs"].values())


def mark_applied(job_id: str):
    """Mark a job's result as applied to the conversation."""
    data = _load_jobs()
    job = data["jobs"].get(job_id)
    if job:
        job["result_applied"] = True
        _save_jobs(data)
