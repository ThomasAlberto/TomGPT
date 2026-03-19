import base64
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import anthropic
import fitz
import openai
from dotenv import load_dotenv

from . import batch, rag
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

anthropic_client = anthropic.Anthropic(api_key=_require_env("ANTHROPIC_API_KEY"))
openai_client = openai.OpenAI(api_key=_require_env("OPENAI_API_KEY"))
rag.init(openai_client)
batch.init(anthropic_client, openai_client)

MEMORY_PATH = Path(__file__).parent / "memory.json"
STATIC_DIR = Path(__file__).parent
UPLOADS_DIR = Path(__file__).parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml", ".py", ".js", ".ts", ".html", ".css"}


def load_memory() -> dict:
    try:
        data = json.loads(MEMORY_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"conversations": {}}
    data.setdefault("folders", {})
    data.setdefault("prompt_templates", {})
    return data


def save_memory(data: dict):
    with tempfile.NamedTemporaryFile("w", dir=MEMORY_PATH.parent, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2)
        temp_path = f.name
    shutil.move(temp_path, MEMORY_PATH)


# ── Pydantic models ────────────────────────────────────────────────────────

class NewConversation(BaseModel):
    title: str = "New Conversation"
    system_prompt: str = "You are a helpful assistant."
    folder_id: str | None = None


class FileRef(BaseModel):
    file_id: str
    filename: str


class ChatMessage(BaseModel):
    conversation_id: str
    message: str
    model: str = "claude-sonnet-4-6"
    files: list[FileRef] = []
    mode: str = "standard"
    thinking_budget: int = 8000


class ModeUpdate(BaseModel):
    mode: str = "standard"
    thinking_budget: int = 8000


class SystemPromptUpdate(BaseModel):
    system_prompt: str


class ModelUpdate(BaseModel):
    model: str


class NewFolder(BaseModel):
    name: str = "New Folder"
    parent_id: str | None = None


class PromptTemplate(BaseModel):
    name: str
    content: str


class FolderRename(BaseModel):
    name: str


class FolderMove(BaseModel):
    folder_id: str | None = None


AVAILABLE_MODELS = {
    # Anthropic
    "claude-opus-4-6":           {"name": "Claude Opus 4.6",   "provider": "anthropic", "maker": "Anthropic", "input_price": 5.00,  "output_price": 25.00, "thinking": True},
    "claude-sonnet-4-6":         {"name": "Claude Sonnet 4.6", "provider": "anthropic", "maker": "Anthropic", "input_price": 3.00,  "output_price": 15.00, "thinking": True},
    "claude-haiku-4-5-20251001": {"name": "Claude Haiku 4.5",  "provider": "anthropic", "maker": "Anthropic", "input_price": 1.00,  "output_price": 5.00},
    # "claude-opus-4-20250514":    {"name": "Claude Opus 4",     "provider": "anthropic", "maker": "Anthropic", "input_price": 15.00, "output_price": 75.00},
    "claude-sonnet-4-20250514":  {"name": "Claude Sonnet 4",   "provider": "anthropic", "maker": "Anthropic", "input_price": 3.00,  "output_price": 15.00},
    "claude-haiku-3-5-20241022": {"name": "Claude 3.5 Haiku",  "provider": "anthropic", "maker": "Anthropic", "input_price": 0.80,  "output_price": 4.00},
    # OpenAI
    "gpt-5.4":                   {"name": "GPT-5.4",           "provider": "openai",    "maker": "OpenAI",    "input_price": 2.50,  "output_price": 15.00},
    "gpt-5.3":                   {"name": "GPT-5.3",           "provider": "openai",    "maker": "OpenAI",    "input_price": 1.75,  "output_price": 14.00},
    "gpt-5.2":                   {"name": "GPT-5.2",           "provider": "openai",    "maker": "OpenAI",    "input_price": 1.75,  "output_price": 14.00},
    "gpt-5":                     {"name": "GPT-5",             "provider": "openai",    "maker": "OpenAI",    "input_price": 1.25,  "output_price": 10.00},
    "gpt-4.1":                   {"name": "GPT-4.1",           "provider": "openai",    "maker": "OpenAI",    "input_price": 2.00,  "output_price": 8.00},
    "gpt-4.1-mini":              {"name": "GPT-4.1 Mini",      "provider": "openai",    "maker": "OpenAI",    "input_price": 0.40,  "output_price": 1.60},
    "gpt-4.1-nano":              {"name": "GPT-4.1 Nano",      "provider": "openai",    "maker": "OpenAI",    "input_price": 0.10,  "output_price": 0.40},
    "gpt-4o":                    {"name": "GPT-4o",            "provider": "openai",    "maker": "OpenAI",    "input_price": 2.50,  "output_price": 10.00},
    "gpt-4o-mini":               {"name": "GPT-4o Mini",       "provider": "openai",    "maker": "OpenAI",    "input_price": 0.15,  "output_price": 0.60},
    "o3":                        {"name": "o3",                "provider": "openai",    "maker": "OpenAI",    "input_price": 2.00,  "output_price": 8.00},
    "o4-mini":                   {"name": "o4-mini",           "provider": "openai",    "maker": "OpenAI",    "input_price": 0.55,  "output_price": 2.20},
    "o3-mini":                   {"name": "o3-mini",           "provider": "openai",    "maker": "OpenAI",    "input_price": 1.10,  "output_price": 4.40},
}


# ── Serve frontend ─────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/index.css")
async def serve_css():
    return FileResponse(STATIC_DIR / "index.css")


@app.get("/index.js")
async def serve_js():
    return FileResponse(STATIC_DIR / "index.js")


@app.get("/models")
async def list_models():
    return [{"id": mid, **info} for mid, info in AVAILABLE_MODELS.items()]


# ── Conversation CRUD ──────────────────────────────────────────────────────

@app.get("/conversations")
async def list_conversations():
    memory = load_memory()
    return [
        {
            "id": cid,
            "title": conv.get("title", "New Conversation"),
            "created_at": conv.get("created_at", ""),
            "folder_id": conv.get("folder_id"),
        }
        for cid, conv in memory["conversations"].items()
    ]


@app.post("/conversations")
async def create_conversation(body: NewConversation):
    memory = load_memory()
    cid = str(uuid.uuid4())
    folder_id = body.folder_id
    if folder_id and folder_id not in memory.get("folders", {}):
        folder_id = None
    memory["conversations"][cid] = {
        "title": body.title,
        "system_prompt": body.system_prompt,
        "model": "claude-sonnet-4-6",
        "folder_id": folder_id,
        "mode": "standard",
        "thinking_budget": 8000,
        "messages": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_memory(memory)
    return {"id": cid, "title": body.title}


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    memory = load_memory()
    conv = memory["conversations"].get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {
        "id": conversation_id,
        "title": conv.get("title", "New Conversation"),
        "system_prompt": conv.get("system_prompt", ""),
        "model": conv.get("model", "claude-sonnet-4-6"),
        "folder_id": conv.get("folder_id"),
        "mode": conv.get("mode") or ("thinking" if conv.get("thinking_enabled") else "standard"),
        "thinking_budget": conv.get("thinking_budget", 8000),
        "messages": conv.get("messages", []),
        "created_at": conv.get("created_at", ""),
    }


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    memory = load_memory()
    memory["conversations"].pop(conversation_id, None)
    save_memory(memory)
    return {"ok": True}


class TitleUpdate(BaseModel):
    title: str


@app.patch("/conversations/{conversation_id}/title")
async def update_title(conversation_id: str, body: TitleUpdate):
    memory = load_memory()
    conv = memory["conversations"].get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv["title"] = body.title.strip() or "New Conversation"
    save_memory(memory)
    return {"ok": True, "title": conv["title"]}


@app.patch("/conversations/{conversation_id}/system-prompt")
async def update_system_prompt(conversation_id: str, body: SystemPromptUpdate):
    memory = load_memory()
    conv = memory["conversations"].get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv["system_prompt"] = body.system_prompt
    save_memory(memory)
    return {"ok": True}


@app.patch("/conversations/{conversation_id}/model")
async def update_model(conversation_id: str, body: ModelUpdate):
    memory = load_memory()
    conv = memory["conversations"].get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv["model"] = body.model
    save_memory(memory)
    return {"ok": True}


@app.patch("/conversations/{conversation_id}/mode")
async def update_mode(conversation_id: str, body: ModeUpdate):
    memory = load_memory()
    conv = memory["conversations"].get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv["mode"] = body.mode
    conv["thinking_budget"] = body.thinking_budget
    save_memory(memory)
    return {"ok": True}


@app.patch("/conversations/{conversation_id}/folder")
async def move_conversation_to_folder(conversation_id: str, body: FolderMove):
    memory = load_memory()
    conv = memory["conversations"].get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv["folder_id"] = body.folder_id
    save_memory(memory)
    return {"ok": True}


# ── Prompt Template CRUD ───────────────────────────────────────────────────

@app.get("/prompt-templates")
async def list_prompt_templates():
    memory = load_memory()
    return [{"id": tid, **t} for tid, t in memory["prompt_templates"].items()]


@app.post("/prompt-templates")
async def create_prompt_template(body: PromptTemplate):
    memory = load_memory()
    tid = str(uuid.uuid4())
    memory["prompt_templates"][tid] = {"name": body.name, "content": body.content}
    save_memory(memory)
    return {"id": tid, "name": body.name, "content": body.content}


@app.put("/prompt-templates/{template_id}")
async def update_prompt_template(template_id: str, body: PromptTemplate):
    memory = load_memory()
    if template_id not in memory["prompt_templates"]:
        raise HTTPException(status_code=404, detail="Template not found")
    memory["prompt_templates"][template_id] = {"name": body.name, "content": body.content}
    save_memory(memory)
    return {"id": template_id, "name": body.name, "content": body.content}


@app.delete("/prompt-templates/{template_id}")
async def delete_prompt_template(template_id: str):
    memory = load_memory()
    memory["prompt_templates"].pop(template_id, None)
    save_memory(memory)
    return {"ok": True}


# ── Folder CRUD ───────────────────────────────────────────────────────────

@app.get("/folders")
async def list_folders():
    memory = load_memory()
    return [
        {"id": fid, "name": f["name"], "parent_id": f.get("parent_id"), "created_at": f.get("created_at", "")}
        for fid, f in memory["folders"].items()
    ]


@app.post("/folders")
async def create_folder(body: NewFolder):
    memory = load_memory()
    fid = str(uuid.uuid4())
    memory["folders"][fid] = {
        "name": body.name,
        "parent_id": body.parent_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_memory(memory)
    return {"id": fid, "name": body.name, "parent_id": body.parent_id}


@app.patch("/folders/{folder_id}")
async def rename_folder(folder_id: str, body: FolderRename):
    memory = load_memory()
    folder = memory["folders"].get(folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    folder["name"] = body.name
    save_memory(memory)
    return {"ok": True}


@app.delete("/folders/{folder_id}")
async def delete_folder(folder_id: str):
    memory = load_memory()
    folder = memory["folders"].get(folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    parent_id = folder.get("parent_id")

    for conv in memory["conversations"].values():
        if conv.get("folder_id") == folder_id:
            conv["folder_id"] = None

    for f in memory["folders"].values():
        if f.get("parent_id") == folder_id:
            f["parent_id"] = parent_id

    del memory["folders"][folder_id]
    save_memory(memory)
    rag.delete_collection(folder_id)
    return {"ok": True}


# ── Knowledge base ─────────────────────────────────────────────────────────

def get_folder_chain(folder_id: str, memory: dict) -> list[str]:
    """Walk parent_id links up the tree, return [folder_id, parent_id, ...]."""
    chain = []
    visited = set()
    current = folder_id
    while current and current not in visited:
        visited.add(current)
        folder = memory["folders"].get(current)
        if not folder:
            break
        chain.append(current)
        current = folder.get("parent_id")
    return chain


@app.post("/folders/{folder_id}/kb/upload")
async def upload_kb_document(folder_id: str, file: UploadFile):
    memory = load_memory()
    if folder_id not in memory["folders"]:
        raise HTTPException(status_code=404, detail="Folder not found")

    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")

    ext = Path(file.filename).suffix.lower()
    if ext == ".pdf":
        import tempfile as _tf
        with _tf.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        text = _extract_pdf_text(tmp_path)
        tmp_path.unlink()
    elif ext in TEXT_EXTENSIONS:
        text = content.decode(errors="replace")
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

    chunks = rag.index_document(folder_id, file.filename, text)
    logger.info("KB upload: %s -> folder %s (%d chunks)", file.filename, folder_id[:8], chunks)
    return {"filename": file.filename, "chunks": chunks}


@app.get("/folders/{folder_id}/kb")
async def list_kb_documents(folder_id: str):
    memory = load_memory()
    if folder_id not in memory["folders"]:
        raise HTTPException(status_code=404, detail="Folder not found")
    return rag.list_documents(folder_id)


@app.delete("/folders/{folder_id}/kb/{filename:path}")
async def delete_kb_document(folder_id: str, filename: str):
    memory = load_memory()
    if folder_id not in memory["folders"]:
        raise HTTPException(status_code=404, detail="Folder not found")
    rag.remove_document(folder_id, filename)
    return {"ok": True}


@app.get("/folders/{folder_id}/kb/chain")
async def get_kb_chain(folder_id: str):
    memory = load_memory()
    if folder_id not in memory["folders"]:
        raise HTTPException(status_code=404, detail="Folder not found")
    chain = get_folder_chain(folder_id, memory)
    result = []
    for fid in chain:
        folder = memory["folders"].get(fid)
        if not folder:
            continue
        doc_count = rag.collection_doc_count(fid)
        result.append({"id": fid, "name": folder["name"], "doc_count": doc_count})
    return result


# ── File upload ────────────────────────────────────────────────────────────

MAX_UPLOAD_SIZE = 50 * 1024 * 1024 # 50MB


@app.post("/upload")
async def upload_file(file: UploadFile):
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")

    ext = Path(file.filename).suffix.lower()
    file_id = str(uuid.uuid4())
    save_name = file_id + ext
    save_path = UPLOADS_DIR / save_name
    save_path.write_bytes(content)
    logger.info("Upload: %s (%d bytes) -> %s", file.filename, len(content), save_name)

    return {
        "file_id": file_id,
        "filename": file.filename,
        "stored_name": save_name,
        "content_type": file.content_type or "",
    }


_COMMON_EXTENSIONS = [".pdf", ".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css", ".xml", ".yaml", ".yml", ".log"]


def _find_upload(file_id: str) -> Path | None:
    for ext in _COMMON_EXTENSIONS:
        path = UPLOADS_DIR / f"{file_id}{ext}"
        if path.exists():
            return path
    for p in UPLOADS_DIR.iterdir():
        if p.stem == file_id:
            return p
    return None


def _extract_pdf_text(file_path: Path) -> str:
    try:
        doc = fitz.open(file_path)
        pages = [page.get_text() for page in doc]
        doc.close()
        return "\n\n".join(pages)
    except Exception as e:
        logger.warning("Failed to extract PDF text from %s: %s", file_path.name, e)
        return f"[Error reading PDF: {e}]"


def build_anthropic_blocks(message: str, file_refs: list[dict]) -> list[dict]:
    """Build Anthropic content blocks from text + attached files."""
    blocks = []

    for ref in file_refs:
        file_path = _find_upload(ref["file_id"])
        if not file_path:
            continue

        ext = file_path.suffix.lower()
        if ext == ".pdf":
            pdf_b64 = base64.standard_b64encode(file_path.read_bytes()).decode()
            blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf_b64,
                },
            })
        elif ext in TEXT_EXTENSIONS:
            text = file_path.read_text(errors="replace")
            blocks.append({
                "type": "text",
                "text": f"[File: {ref['filename']}]\n\n{text}",
            })

    blocks.append({"type": "text", "text": message})
    return blocks


def build_openai_content(message: str, file_refs: list[dict]) -> str:
    """Build a single text string with file contents inlined for OpenAI."""
    parts = []

    for ref in file_refs:
        file_path = _find_upload(ref["file_id"])
        if not file_path:
            continue

        ext = file_path.suffix.lower()
        if ext == ".pdf":
            text = _extract_pdf_text(file_path)
            parts.append(f"[File: {ref['filename']}]\n\n{text}")
        elif ext in TEXT_EXTENSIONS:
            text = file_path.read_text(errors="replace")
            parts.append(f"[File: {ref['filename']}]\n\n{text}")

    parts.append(message)
    return "\n\n".join(parts)


# ── Chat ───────────────────────────────────────────────────────────────────

TITLE_PROMPT = "Give a concise 3-6 word title for a conversation that starts with this message. Reply with ONLY the title, no quotes or punctuation:\n\n"


def generate_title(first_user_message: str, provider: str = "anthropic") -> str:
    if provider == "openai":
        resp = openai_client.chat.completions.create(
            model="gpt-4.1-nano",
            max_tokens=30,
            timeout=30.0,
            messages=[{"role": "user", "content": TITLE_PROMPT + first_user_message}],
        )
        return resp.choices[0].message.content.strip()
    resp = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=30,
        timeout=30.0,
        messages=[{"role": "user", "content": TITLE_PROMPT + first_user_message}],
    )
    return resp.content[0].text.strip()


def _build_system_prompt(model: str, user_system_prompt: str) -> str:
    info = AVAILABLE_MODELS[model]
    identity = (
        f"You are {info['name']}, made by {info['maker']}. "
        f"Your model ID is {model}. This is accurate and authoritative — "
        "do not contradict or speculate about your model version, "
        "even if it is absent from your training data."
    )
    if user_system_prompt:
        return f"{identity} {user_system_prompt}"
    return f"{identity} You are a helpful assistant."


PRO_CRITIQUE_PROMPT = (
    "You are a critical reviewer. Analyse the response for: "
    "1) Accuracy — factual errors or unsupported claims, "
    "2) Completeness — important missing aspects, "
    "3) Clarity — confusing or poorly explained parts, "
    "4) Relevance — parts that don't address the question. "
    "Be specific and actionable. Format as a concise bullet list."
)

PRO_REFINE_INSTRUCTION = (
    "A reviewer identified these issues with your response:\n\n"
    "{critique}\n\n"
    "Please provide an improved response addressing these issues. "
    "Do not mention the review or that this is a revision."
)


def _pro_stream(model: str, system: str, messages: list[dict],
                provider: str, user_message: str):
    """Pro mode: generate → critique → refine (final step streamed)."""

    yield {"type": "pro_status", "message": "Generating initial response\u2026"}

    # Step 1 — initial response (non-streaming)
    if provider == "anthropic":
        initial, _, _ = _call_anthropic(model, system, messages)
    else:
        initial = _call_openai(model, system, messages)

    yield {"type": "pro_stage", "stage": "initial", "content": initial}
    yield {"type": "pro_status", "message": "Reviewing response\u2026"}

    # Step 2 — critique with a cheap model (non-streaming)
    critique_model = "claude-haiku-4-5-20251001" if provider == "anthropic" else "gpt-4.1-mini"
    critique_msgs = [
        {"role": "user",
         "content": f"Question: {user_message}\n\nResponse to review:\n{initial}"},
    ]
    if AVAILABLE_MODELS[critique_model]["provider"] == "anthropic":
        critique, _, _ = _call_anthropic(critique_model, PRO_CRITIQUE_PROMPT, critique_msgs)
    else:
        critique = _call_openai(critique_model, PRO_CRITIQUE_PROMPT, critique_msgs)

    yield {"type": "pro_stage", "stage": "critique", "content": critique}
    yield {"type": "pro_status", "message": "Refining response\u2026"}

    # Step 3 — refine with streaming (full conversation context)
    refine_msgs = messages + [
        {"role": "assistant", "content": initial},
        {"role": "user", "content": PRO_REFINE_INSTRUCTION.format(critique=critique)},
    ]
    if provider == "anthropic":
        yield from _stream_anthropic(model, system, refine_msgs)
    else:
        for text in _stream_openai(model, system, refine_msgs):
            yield text


def _build_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Build Anthropic API messages, preserving thinking blocks in history."""
    api_messages = []
    for m in messages:
        if m["role"] == "user" and m.get("files"):
            api_messages.append({
                "role": "user",
                "content": build_anthropic_blocks(m["content"], m["files"]),
            })
        elif m["role"] == "assistant" and m.get("thinking") and m.get("thinking_signature"):
            api_messages.append({
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": m["thinking"],
                     "signature": m["thinking_signature"]},
                    {"type": "text", "text": m["content"]},
                ],
            })
        else:
            api_messages.append({"role": m["role"], "content": m["content"]})
    return api_messages


def _call_anthropic(model: str, system: str, messages: list[dict],
                    thinking_enabled: bool = False, thinking_budget: int = 8000) -> tuple[str, str | None, str | None]:
    api_messages = _build_anthropic_messages(messages)

    kwargs: dict = dict(
        model=model,
        max_tokens=16000 if thinking_enabled else 8192,
        system=system,
        messages=api_messages,
        timeout=120.0,
    )
    if thinking_enabled:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = anthropic_client.messages.create(**kwargs)
            break
        except anthropic.APIStatusError as e:
            if e.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                wait = 2 ** attempt
                logger.warning("Anthropic API error %d, retrying in %ds (%d/%d)",
                               e.status_code, wait, attempt + 1, _MAX_RETRIES)
                time.sleep(wait)
            else:
                raise

    thinking_text = None
    thinking_signature = None
    reply_text = ""
    for block in response.content:
        if block.type == "thinking":
            thinking_text = block.thinking
            thinking_signature = getattr(block, "signature", None)
        elif block.type == "text":
            reply_text = block.text
    return reply_text, thinking_text, thinking_signature


OPENAI_REASONING_MODELS = {"o3", "o3-mini", "o4-mini"}

_RETRYABLE_STATUSES = {429, 500, 529}
_MAX_RETRIES = 3


def _call_openai(model: str, system: str, messages: list[dict]) -> str:
    api_messages = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user" and m.get("files"):
            api_messages.append({
                "role": "user",
                "content": build_openai_content(m["content"], m["files"]),
            })
        else:
            api_messages.append({"role": m["role"], "content": m["content"]})

    kwargs = {"model": model, "messages": api_messages, "timeout": 120.0}
    if model in OPENAI_REASONING_MODELS:
        kwargs["max_completion_tokens"] = 8192
    else:
        kwargs["max_tokens"] = 8192

    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = openai_client.chat.completions.create(**kwargs)
            return response.choices[0].message.content
        except openai.APIStatusError as e:
            if e.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                wait = 2 ** attempt
                logger.warning("OpenAI API error %d, retrying in %ds (%d/%d)",
                               e.status_code, wait, attempt + 1, _MAX_RETRIES)
                time.sleep(wait)
            else:
                raise


def _stream_anthropic(model: str, system: str, messages: list[dict],
                      thinking_enabled: bool = False, thinking_budget: int = 8000):
    api_messages = _build_anthropic_messages(messages)

    kwargs: dict = dict(
        model=model,
        max_tokens=16000 if thinking_enabled else 8192,
        system=system,
        messages=api_messages,
    )
    if thinking_enabled:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

    with anthropic_client.messages.stream(**kwargs) as stream:
        if not thinking_enabled:
            for text in stream.text_stream:
                yield {"type": "chunk", "content": text}
        else:
            current_block_type = None
            current_signature = None
            for event in stream:
                if event.type == "content_block_start":
                    current_block_type = event.content_block.type
                    if current_block_type == "thinking":
                        yield {"type": "thinking_start"}
                elif event.type == "content_block_delta":
                    if event.delta.type == "thinking_delta":
                        yield {"type": "thinking_chunk", "content": event.delta.thinking}
                    elif event.delta.type == "signature_delta":
                        current_signature = event.delta.signature
                    elif event.delta.type == "text_delta":
                        yield {"type": "chunk", "content": event.delta.text}
                elif event.type == "content_block_stop":
                    if current_block_type == "thinking":
                        yield {"type": "thinking_end", "signature": current_signature}
                        current_signature = None
                    current_block_type = None


def _stream_openai(model: str, system: str, messages: list[dict]):
    api_messages = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user" and m.get("files"):
            api_messages.append({
                "role": "user",
                "content": build_openai_content(m["content"], m["files"]),
            })
        else:
            api_messages.append({"role": m["role"], "content": m["content"]})

    kwargs = {"model": model, "messages": api_messages, "stream": True}
    if model in OPENAI_REASONING_MODELS:
        kwargs["max_completion_tokens"] = 8192
    else:
        kwargs["max_tokens"] = 8192

    response = openai_client.chat.completions.create(**kwargs)
    for chunk in response:
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content


def _inject_rag_context(system_prompt: str, folder_id: str | None, query: str, memory: dict) -> str:
    """Append RAG knowledge base context to the system prompt if available."""
    if not folder_id:
        return system_prompt
    chain = get_folder_chain(folder_id, memory)
    try:
        rag_results = rag.search_folder_chain(chain, query, top_k=5)
    except Exception as e:
        logger.warning("RAG search failed: %s", e)
        return system_prompt
    if not rag_results:
        return system_prompt
    chunks = [
        f'<document source="{r["source"]}" relevance="{r["score"]:.2f}">\n{r["content"]}\n</document>'
        for r in rag_results
    ]
    rag_block = "<knowledge_base>\n" + "\n".join(chunks) + "\n</knowledge_base>"
    logger.info("RAG: injected %d chunks from %d folders", len(rag_results), len(chain))
    return system_prompt + (
        "\n\nYou have access to a knowledge base. Use it to inform your answers when relevant. "
        "Cite sources when you use information from the knowledge base.\n\n" + rag_block
    )


@app.post("/chat")
async def chat(body: ChatMessage):
    memory = load_memory()
    conv = memory["conversations"].get(body.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    model = body.model
    if model not in AVAILABLE_MODELS:
        model = "claude-sonnet-4-6"
    conv["model"] = model

    file_refs = []
    for f in body.files:
        file_path = _find_upload(f.file_id)
        if file_path:
            file_refs.append({"file_id": f.file_id, "filename": f.filename})

    stored_message = {"role": "user", "content": body.message}
    if file_refs:
        stored_message["files"] = file_refs
    conv["messages"].append(stored_message)

    full_system = _build_system_prompt(model, conv.get("system_prompt", ""))
    provider = AVAILABLE_MODELS[model]["provider"]
    full_system = _inject_rag_context(full_system, conv.get("folder_id"), body.message, memory)

    logger.info("Chat: model=%s, provider=%s, conv=%s, files=%d", model, provider, body.conversation_id[:8], len(file_refs))

    mode = body.mode
    if mode == "thinking" and not AVAILABLE_MODELS[model].get("thinking"):
        logger.warning("Mode 'thinking' not supported by %s, falling back to standard", model)
        mode = "standard"
    thinking = None
    thinking_sig = None
    pro_initial = None
    pro_critique = None

    if mode == "pro":
        if provider == "anthropic":
            initial, _, _ = _call_anthropic(model, full_system, conv["messages"])
        else:
            initial = _call_openai(model, full_system, conv["messages"])
        crit_model = "claude-haiku-4-5-20251001" if provider == "anthropic" else "gpt-4.1-mini"
        crit_msgs = [{"role": "user", "content": f"Question: {body.message}\n\nResponse to review:\n{initial}"}]
        if AVAILABLE_MODELS[crit_model]["provider"] == "anthropic":
            critique, _, _ = _call_anthropic(crit_model, PRO_CRITIQUE_PROMPT, crit_msgs)
        else:
            critique = _call_openai(crit_model, PRO_CRITIQUE_PROMPT, crit_msgs)
        refine_msgs = conv["messages"] + [
            {"role": "assistant", "content": initial},
            {"role": "user", "content": PRO_REFINE_INSTRUCTION.format(critique=critique)},
        ]
        if provider == "anthropic":
            reply, _, _ = _call_anthropic(model, full_system, refine_msgs)
        else:
            reply = _call_openai(model, full_system, refine_msgs)
        pro_initial = initial
        pro_critique = critique
    elif mode == "thinking" and provider == "anthropic":
        reply, thinking, thinking_sig = _call_anthropic(model, full_system, conv["messages"],
                                                        thinking_enabled=True, thinking_budget=body.thinking_budget)
    else:
        if provider == "anthropic":
            reply, _, _ = _call_anthropic(model, full_system, conv["messages"])
        else:
            reply = _call_openai(model, full_system, conv["messages"])

    assistant_msg: dict = {"role": "assistant", "content": reply}
    if thinking:
        assistant_msg["thinking"] = thinking
        if thinking_sig:
            assistant_msg["thinking_signature"] = thinking_sig
    if pro_initial:
        assistant_msg["pro_initial"] = pro_initial
        assistant_msg["pro_critique"] = pro_critique
    conv["messages"].append(assistant_msg)

    if len(conv["messages"]) == 2 and conv.get("title") == "New Conversation":
        try:
            conv["title"] = generate_title(body.message, provider)
        except Exception as e:
            logger.warning("Title generation failed: %s", e)
            conv["title"] = body.message[:40]

    save_memory(memory)
    return {"reply": reply, "title": conv["title"]}


@app.post("/chat/stream")
async def chat_stream(body: ChatMessage):
    memory = load_memory()
    conv = memory["conversations"].get(body.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    model = body.model
    if model not in AVAILABLE_MODELS:
        model = "claude-sonnet-4-6"
    conv["model"] = model

    file_refs = []
    for f in body.files:
        file_path = _find_upload(f.file_id)
        if file_path:
            file_refs.append({"file_id": f.file_id, "filename": f.filename})

    stored_message = {"role": "user", "content": body.message}
    if file_refs:
        stored_message["files"] = file_refs
    conv["messages"].append(stored_message)
    save_memory(memory)

    full_system = _build_system_prompt(model, conv.get("system_prompt", ""))
    provider = AVAILABLE_MODELS[model]["provider"]
    full_system = _inject_rag_context(full_system, conv.get("folder_id"), body.message, memory)

    logger.info("Chat stream: model=%s, provider=%s, conv=%s, files=%d", model, provider, body.conversation_id[:8], len(file_refs))

    validated_mode = body.mode
    if validated_mode == "thinking" and not AVAILABLE_MODELS[model].get("thinking"):
        logger.warning("Mode 'thinking' not supported by %s, falling back to standard", model)
        validated_mode = "standard"

    def event_generator():
        full_response = []
        thinking_parts = []
        thinking_signature = None
        pro_initial = ""
        pro_critique = ""
        assistant_saved = False
        try:
            mode = validated_mode
            if mode == "pro":
                stream = _pro_stream(model, full_system, conv["messages"], provider, body.message)
            elif mode == "thinking" and provider == "anthropic":
                stream = _stream_anthropic(model, full_system, conv["messages"],
                                           thinking_enabled=True, thinking_budget=body.thinking_budget)
            elif provider == "anthropic":
                stream = _stream_anthropic(model, full_system, conv["messages"])
            else:
                stream = _stream_openai(model, full_system, conv["messages"])

            for chunk in stream:
                if isinstance(chunk, str):
                    full_response.append(chunk)
                    yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"
                else:
                    if chunk["type"] == "chunk":
                        full_response.append(chunk["content"])
                    elif chunk["type"] == "thinking_chunk":
                        thinking_parts.append(chunk["content"])
                    elif chunk["type"] == "thinking_end":
                        thinking_signature = chunk.get("signature")
                    elif chunk["type"] == "pro_stage":
                        if chunk["stage"] == "initial":
                            pro_initial = chunk["content"]
                        elif chunk["stage"] == "critique":
                            pro_critique = chunk["content"]
                    yield f"data: {json.dumps(chunk)}\n\n"

            reply = "".join(full_response)

            memory2 = load_memory()
            conv2 = memory2["conversations"].get(body.conversation_id)
            if conv2:
                assistant_msg: dict = {"role": "assistant", "content": reply}
                if thinking_parts:
                    assistant_msg["thinking"] = "".join(thinking_parts)
                    if thinking_signature:
                        assistant_msg["thinking_signature"] = thinking_signature
                if pro_initial:
                    assistant_msg["pro_initial"] = pro_initial
                    assistant_msg["pro_critique"] = pro_critique
                conv2["messages"].append(assistant_msg)
                if len(conv2["messages"]) == 2 and conv2.get("title") == "New Conversation":
                    try:
                        conv2["title"] = generate_title(body.message, provider)
                    except Exception as e:
                        logger.warning("Title generation failed: %s", e)
                        conv2["title"] = body.message[:40]
                save_memory(memory2)
                assistant_saved = True
                yield f"data: {json.dumps({'type': 'done', 'title': conv2['title']})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'done', 'title': 'New Conversation'})}\n\n"

        except Exception as e:
            logger.error("Stream error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            if not assistant_saved:
                try:
                    mem = load_memory()
                    c = mem["conversations"].get(body.conversation_id)
                    if c and c["messages"] and c["messages"][-1]["role"] == "user":
                        c["messages"].pop()
                        save_memory(mem)
                        logger.info("Cleaned up orphaned user message for conv %s", body.conversation_id[:8])
                except Exception:
                    pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ── Batch API ─────────────────────────────────────────────────────────────

@app.post("/batch/submit")
async def batch_submit(body: ChatMessage):
    memory = load_memory()
    conv = memory["conversations"].get(body.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    model = body.model
    if model not in AVAILABLE_MODELS:
        model = "claude-sonnet-4-6"
    conv["model"] = model
    provider = AVAILABLE_MODELS[model]["provider"]

    mode = body.mode
    if mode == "thinking" and not AVAILABLE_MODELS[model].get("thinking"):
        mode = "standard"

    file_refs = []
    for f in body.files:
        if _find_upload(f.file_id):
            file_refs.append({"file_id": f.file_id, "filename": f.filename})

    stored_message = {"role": "user", "content": body.message}
    if file_refs:
        stored_message["files"] = file_refs
    conv["messages"].append(stored_message)

    full_system = _build_system_prompt(model, conv.get("system_prompt", ""))
    full_system = _inject_rag_context(full_system, conv.get("folder_id"), body.message, memory)

    # Build provider-specific messages
    if provider == "anthropic":
        api_messages = _build_anthropic_messages(conv["messages"])
    else:
        api_messages = []
        for m in conv["messages"]:
            if m["role"] == "user" and m.get("files"):
                api_messages.append({"role": "user", "content": build_openai_content(m["content"], m["files"])})
            else:
                api_messages.append({"role": m["role"], "content": m["content"]})

    job_id = str(uuid.uuid4())

    # Save pending assistant message
    conv["messages"].append({"role": "assistant", "content": "", "batch_job_id": job_id})
    save_memory(memory)

    critique_model = "claude-haiku-4-5-20251001" if provider == "anthropic" else "gpt-4.1-mini"

    job = batch.submit_job({
        "job_id": job_id,
        "conversation_id": body.conversation_id,
        "mode": mode,
        "provider": provider,
        "model": model,
        "system": full_system,
        "api_messages": api_messages,
        "user_message": body.message,
        "thinking_budget": body.thinking_budget,
        "critique_model": critique_model,
    })

    # Generate title if first message
    if len(conv["messages"]) == 2 and conv.get("title") == "New Conversation":
        try:
            conv["title"] = generate_title(body.message, provider)
            memory2 = load_memory()
            c2 = memory2["conversations"].get(body.conversation_id)
            if c2:
                c2["title"] = conv["title"]
                save_memory(memory2)
        except Exception:
            pass

    return {"job_id": job_id, "status": job["status"], "total_steps": job["total_steps"],
            "title": conv.get("title", "New Conversation")}


@app.get("/batch/jobs/{job_id}")
async def batch_check(job_id: str):
    job = batch.check_and_advance(job_id)

    if job["status"] == "completed" and not job.get("result_applied"):
        # Apply result to conversation
        memory = load_memory()
        conv = memory["conversations"].get(job["conversation_id"])
        if conv:
            # Find and replace the pending assistant message
            for i, m in enumerate(conv["messages"]):
                if m.get("batch_job_id") == job_id:
                    conv["messages"][i] = {"role": "assistant", "content": job["result"]}
                    if job.get("thinking"):
                        conv["messages"][i]["thinking"] = job["thinking"]
                        if job.get("thinking_signature"):
                            conv["messages"][i]["thinking_signature"] = job["thinking_signature"]
                    if job.get("initial_response"):
                        conv["messages"][i]["pro_initial"] = job["initial_response"]
                        conv["messages"][i]["pro_critique"] = job["critique_response"]
                    break
            save_memory(memory)
        batch.mark_applied(job_id)

    return {
        "job_id": job["id"],
        "status": job["status"],
        "current_step": job.get("current_step", 0),
        "total_steps": job.get("total_steps", 1),
        "result": job.get("result"),
        "error": job.get("error"),
    }


@app.delete("/batch/jobs/{job_id}")
async def batch_cancel(job_id: str):
    job = batch.cancel_job(job_id)

    # Remove pending assistant message
    if job.get("conversation_id"):
        memory = load_memory()
        conv = memory["conversations"].get(job["conversation_id"])
        if conv:
            conv["messages"] = [m for m in conv["messages"] if m.get("batch_job_id") != job_id]
            # Also remove the user message that preceded it
            if conv["messages"] and conv["messages"][-1]["role"] == "user":
                conv["messages"].pop()
            save_memory(memory)

    return {"ok": True, "status": job.get("status")}


# ── Audio export ──────────────────────────────────────────────────────────

AUDIO_DIR = Path(__file__).parent / "audio"
AUDIO_PREVIEW_DIR = AUDIO_DIR / "previews"
AUDIO_CONV_DIR = AUDIO_DIR / "outputs" / "conversation"
AUDIO_PODCAST_DIR = AUDIO_DIR / "outputs" / "podcasts"
AUDIO_DIR.mkdir(exist_ok=True)
AUDIO_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_CONV_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_PODCAST_DIR.mkdir(parents=True, exist_ok=True)

TTS_VOICES = {
    "user": "nova",
    "assistant": "onyx",
}


TTS_VOICE_LIST = ["alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"]
TTS_PREVIEW_LINES = {
    "alloy":   "Hey there! I'm Alloy. I've got a balanced, versatile tone — great for everyday conversations.",
    "ash":     "Hi, I'm Ash. My voice is calm and measured. I work well for thoughtful, explanatory content.",
    "coral":   "Hi! I'm Coral. I sound friendly and approachable. People say I'm great for casual chats.",
    "echo":    "Greetings. I'm Echo. My tone is clear and resonant, well suited for professional or formal content.",
    "fable":   "Hello there! I'm Fable. I have a distinctive, characterful voice — good for creative and dramatic readings.",
    "nova":    "Hey! I'm Nova. I'm bright and energetic. I bring a natural, conversational energy to everything I read.",
    "onyx":    "Hello. I'm Onyx. My voice is deep and authoritative. I'm often chosen for serious or technical discussions.",
    "sage":    "Hi, I'm Sage. I have a smooth, thoughtful delivery. I'm well suited for educational and analytical content.",
    "shimmer": "Hello! I'm Shimmer. I sound light and clear, with a gentle quality that works nicely for friendly dialogue.",
}


@app.get("/audio/voices")
async def list_voices():
    return [{"id": v, "name": v.title()} for v in TTS_VOICE_LIST]


@app.get("/audio/preview/{voice}")
async def preview_voice(voice: str):
    if voice not in TTS_VOICE_LIST:
        raise HTTPException(status_code=400, detail=f"Unknown voice: {voice}")

    cache_path = AUDIO_PREVIEW_DIR / f"preview-{voice}.mp3"
    if not cache_path.exists():
        logger.info("Generating TTS preview for voice: %s", voice)
        response = openai_client.audio.speech.create(
            model="tts-1", voice=voice, input=TTS_PREVIEW_LINES[voice], response_format="mp3",
        )
        cache_path.write_bytes(response.content)

    return FileResponse(str(cache_path), media_type="audio/mp3", filename=f"preview-{voice}.mp3")


class AudioExportOptions(BaseModel):
    user_voice: str = "nova"
    assistant_voice: str = "onyx"
    format: str = "mp3"
    speed: float = 1.0


@app.post("/conversations/{conversation_id}/audio")
async def export_audio(conversation_id: str, body: AudioExportOptions):
    from pydub import AudioSegment

    memory = load_memory()
    conv = memory["conversations"].get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = [m for m in conv.get("messages", []) if m.get("content") and not m.get("batch_job_id")]
    if not messages:
        raise HTTPException(status_code=400, detail="No messages to convert")

    if body.format not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="Format must be mp3 or wav")

    voice_map = {"user": body.user_voice, "assistant": body.assistant_voice}
    tts_format = "mp3"  # always generate as mp3, convert to wav if needed

    segments: list[AudioSegment] = []
    pause = AudioSegment.silent(duration=600)  # 600ms pause between messages

    for i, m in enumerate(messages):
        text = m["content"][:4096]  # TTS limit per call
        role = m["role"]
        voice = voice_map.get(role, "onyx")

        logger.info("TTS: message %d/%d, role=%s, voice=%s, %d chars",
                     i + 1, len(messages), role, voice, len(text))

        try:
            response = openai_client.audio.speech.create(
                model="tts-1",
                voice=voice,
                input=text,
                response_format=tts_format,
                speed=body.speed,
            )
            audio_bytes = response.content
            seg = AudioSegment.from_file(io.BytesIO(audio_bytes), format=tts_format)
            segments.append(seg)
            if i < len(messages) - 1:
                segments.append(pause)
        except Exception as e:
            logger.error("TTS failed for message %d: %s", i, e)
            raise HTTPException(status_code=500, detail=f"TTS failed on message {i + 1}: {e}")

    combined = segments[0]
    for seg in segments[1:]:
        combined += seg

    title = conv.get("title", "conversation").strip()
    slug = "".join(c if c.isalnum() or c in " -_" else "" for c in title).strip().replace(" ", "-")[:50] or "conversation"
    filename = f"{slug}.{body.format}"
    out_path = AUDIO_CONV_DIR / filename

    combined.export(str(out_path), format=body.format)
    logger.info("Audio exported: %s (%d messages, %.1fs)", filename, len(messages), combined.duration_seconds)

    return FileResponse(
        str(out_path),
        media_type=f"audio/{body.format}",
        filename=filename,
    )


_PODCAST_LINE_RE = re.compile(r"^(SPEAKER_ONE|SPEAKER_TWO)\s*:\s*(.+)", re.MULTILINE)


def _parse_podcast_script(text: str) -> list[tuple[str, str]]:
    """Extract (speaker, line) pairs from SPEAKER_ONE/SPEAKER_TWO formatted text."""
    return [(m.group(1), m.group(2).strip()) for m in _PODCAST_LINE_RE.finditer(text) if m.group(2).strip()]


class PodcastExportOptions(BaseModel):
    speaker_one_voice: str = "nova"
    speaker_two_voice: str = "onyx"
    format: str = "mp3"
    speed: float = 1.0


@app.post("/conversations/{conversation_id}/audio/podcast")
async def export_podcast(conversation_id: str, body: PodcastExportOptions):
    from pydub import AudioSegment

    memory = load_memory()
    conv = memory["conversations"].get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if body.format not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="Format must be mp3 or wav")

    # Collect all SPEAKER_ONE/SPEAKER_TWO lines from assistant messages
    all_text = "\n".join(m["content"] for m in conv.get("messages", [])
                         if m.get("role") == "assistant" and m.get("content"))
    lines = _parse_podcast_script(all_text)

    if not lines:
        raise HTTPException(status_code=400, detail="No SPEAKER_ONE/SPEAKER_TWO lines found in assistant messages")

    voice_map = {"SPEAKER_ONE": body.speaker_one_voice, "SPEAKER_TWO": body.speaker_two_voice}
    tts_format = "mp3"

    segments: list[AudioSegment] = []
    pause = AudioSegment.silent(duration=400)  # 400ms between lines

    for i, (speaker, text) in enumerate(lines):
        voice = voice_map[speaker]
        text = text[:4096]

        logger.info("Podcast TTS: line %d/%d, %s (%s), %d chars",
                     i + 1, len(lines), speaker, voice, len(text))

        try:
            response = openai_client.audio.speech.create(
                model="tts-1", voice=voice, input=text, response_format=tts_format, speed=body.speed,
            )
            seg = AudioSegment.from_file(io.BytesIO(response.content), format=tts_format)
            segments.append(seg)
            if i < len(lines) - 1:
                segments.append(pause)
        except Exception as e:
            logger.error("Podcast TTS failed on line %d: %s", i, e)
            raise HTTPException(status_code=500, detail=f"TTS failed on line {i + 1}: {e}")

    combined = segments[0]
    for seg in segments[1:]:
        combined += seg

    title = conv.get("title", "podcast").strip()
    slug = "".join(c if c.isalnum() or c in " -_" else "" for c in title).strip().replace(" ", "-")[:50] or "podcast"
    filename = f"{slug}.{body.format}"
    out_path = AUDIO_PODCAST_DIR / filename

    combined.export(str(out_path), format=body.format)
    logger.info("Podcast exported: %s (%d lines, %.1fs)", filename, len(lines), combined.duration_seconds)

    return FileResponse(
        str(out_path),
        media_type=f"audio/{body.format}",
        filename=filename,
    )
