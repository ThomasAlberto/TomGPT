import base64
import json
import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import anthropic
import fitz
import openai
from dotenv import load_dotenv

from . import rag
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
    "claude-opus-4-6":           {"name": "Claude Opus 4.6",   "provider": "anthropic", "maker": "Anthropic", "input_price": 5.00,  "output_price": 25.00},
    "claude-sonnet-4-6":         {"name": "Claude Sonnet 4.6", "provider": "anthropic", "maker": "Anthropic", "input_price": 3.00,  "output_price": 15.00},
    "claude-haiku-4-5-20251001": {"name": "Claude Haiku 4.5",  "provider": "anthropic", "maker": "Anthropic", "input_price": 1.00,  "output_price": 5.00},
    "claude-opus-4-20250514":    {"name": "Claude Opus 4",     "provider": "anthropic", "maker": "Anthropic", "input_price": 15.00, "output_price": 75.00},
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
        "messages": conv.get("messages", []),
        "created_at": conv.get("created_at", ""),
    }


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    memory = load_memory()
    memory["conversations"].pop(conversation_id, None)
    save_memory(memory)
    return {"ok": True}


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
        model="claude-sonnet-4-6",
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


def _call_anthropic(model: str, system: str, messages: list[dict]) -> str:
    api_messages = []
    for m in messages:
        if m["role"] == "user" and m.get("files"):
            api_messages.append({
                "role": "user",
                "content": build_anthropic_blocks(m["content"], m["files"]),
            })
        else:
            api_messages.append({"role": m["role"], "content": m["content"]})

    response = anthropic_client.messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=api_messages,
        timeout=120.0,
    )
    return response.content[0].text


OPENAI_REASONING_MODELS = {"o3", "o3-mini", "o4-mini"}


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
        kwargs["max_completion_tokens"] = 4096
    else:
        kwargs["max_tokens"] = 4096

    response = openai_client.chat.completions.create(**kwargs)
    return response.choices[0].message.content


def _stream_anthropic(model: str, system: str, messages: list[dict]):
    api_messages = []
    for m in messages:
        if m["role"] == "user" and m.get("files"):
            api_messages.append({
                "role": "user",
                "content": build_anthropic_blocks(m["content"], m["files"]),
            })
        else:
            api_messages.append({"role": m["role"], "content": m["content"]})

    with anthropic_client.messages.stream(
        model=model,
        max_tokens=4096,
        system=system,
        messages=api_messages,
    ) as stream:
        for text in stream.text_stream:
            yield text


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
        kwargs["max_completion_tokens"] = 4096
    else:
        kwargs["max_tokens"] = 4096

    response = openai_client.chat.completions.create(**kwargs)
    for chunk in response:
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content


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

    folder_id = conv.get("folder_id")
    if folder_id:
        chain = get_folder_chain(folder_id, memory)
        try:
            rag_results = rag.search_folder_chain(chain, body.message, top_k=5)
        except Exception as e:
            logger.warning("RAG search failed: %s", e)
            rag_results = []
        if rag_results:
            chunks = []
            for r in rag_results:
                chunks.append(f'<document source="{r["source"]}" relevance="{r["score"]:.2f}">\n{r["content"]}\n</document>')
            rag_block = "<knowledge_base>\n" + "\n".join(chunks) + "\n</knowledge_base>"
            full_system += (
                "\n\nYou have access to a knowledge base. Use it to inform your answers when relevant. "
                "Cite sources when you use information from the knowledge base.\n\n" + rag_block
            )
            logger.info("RAG: injected %d chunks from %d folders", len(rag_results), len(chain))

    logger.info("Chat: model=%s, provider=%s, conv=%s, files=%d", model, provider, body.conversation_id[:8], len(file_refs))

    if provider == "anthropic":
        reply = _call_anthropic(model, full_system, conv["messages"])
    else:
        reply = _call_openai(model, full_system, conv["messages"])

    conv["messages"].append({"role": "assistant", "content": reply})

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

    folder_id = conv.get("folder_id")
    if folder_id:
        chain = get_folder_chain(folder_id, memory)
        try:
            rag_results = rag.search_folder_chain(chain, body.message, top_k=5)
        except Exception as e:
            logger.warning("RAG search failed: %s", e)
            rag_results = []
        if rag_results:
            chunks = []
            for r in rag_results:
                chunks.append(f'<document source="{r["source"]}" relevance="{r["score"]:.2f}">\n{r["content"]}\n</document>')
            rag_block = "<knowledge_base>\n" + "\n".join(chunks) + "\n</knowledge_base>"
            full_system += (
                "\n\nYou have access to a knowledge base. Use it to inform your answers when relevant. "
                "Cite sources when you use information from the knowledge base.\n\n" + rag_block
            )
            logger.info("RAG: injected %d chunks from %d folders", len(rag_results), len(chain))

    logger.info("Chat stream: model=%s, provider=%s, conv=%s, files=%d", model, provider, body.conversation_id[:8], len(file_refs))

    def event_generator():
        full_response = []
        try:
            if provider == "anthropic":
                stream = _stream_anthropic(model, full_system, conv["messages"])
            else:
                stream = _stream_openai(model, full_system, conv["messages"])

            for chunk in stream:
                full_response.append(chunk)
                yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"

            reply = "".join(full_response)

            memory2 = load_memory()
            conv2 = memory2["conversations"].get(body.conversation_id)
            if conv2:
                conv2["messages"].append({"role": "assistant", "content": reply})
                if len(conv2["messages"]) == 2 and conv2.get("title") == "New Conversation":
                    try:
                        conv2["title"] = generate_title(body.message, provider)
                    except Exception as e:
                        logger.warning("Title generation failed: %s", e)
                        conv2["title"] = body.message[:40]
                save_memory(memory2)
                yield f"data: {json.dumps({'type': 'done', 'title': conv2['title']})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'done', 'title': 'New Conversation'})}\n\n"

        except Exception as e:
            logger.error("Stream error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
