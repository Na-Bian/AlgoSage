"""Isolated, loopback-only browser acceptance target; never deploy this journal.

Use run_app.py tools/browser_acceptance_app.py with EG_ACCEPTANCE_DIR and
EG_ACCEPTANCE_VARIANT. Browser UI/callbacks are the ordinary student application.
Only the service assembly changes for the documented A/B/C/D controls. Evidence
contains synthetic test prompts and unapproved drafts for local developer review.
"""
from dataclasses import asdict
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import threading
import time

from examguard.config import Settings
from examguard.local_identity import ensure_local_secret
from examguard.schemas import TutorRequest
from examguard.service import TutorService
from harness.variants import build_tutor, normalize_variant, VARIANTS

root = Path(os.environ["EG_ACCEPTANCE_DIR"]).resolve()
variant = normalize_variant(os.environ.get("EG_ACCEPTANCE_VARIANT", "D"))
root.mkdir(parents=True, exist_ok=True)
ensure_local_secret()
_journal_thread = ContextVar("acceptance_thread", default="")


class JournalService(TutorService):
    def __init__(self):
        super().__init__(Settings.from_env(), allow_policy_override=False)
        # Preserve only safe provider error fields, never headers/requests/keys.
        original = self.backend
        service = self
        class RecordingBackend:
            def __getattr__(self, name):
                method = getattr(original, name)
                if not name.startswith("generate"):
                    return method
                def call(*args, **kwargs):
                    messages = args[0] if args else kwargs.get("messages", [])
                    images = [part["image_url"]["url"] for m in messages
                        if isinstance(m.get("content"), list) for part in m["content"]
                        if part.get("type") == "image_url"]
                    system = str(messages[0].get("content", "")) if messages else ""
                    stage = ("classification" if "输入意图分类器" in system else
                        "judge" if "输出审核员" in system else
                        "quality" if "独立知识点" in system else "generation")
                    with service.journal_lock:
                        with (root / (variant + "-visual-calls.jsonl")).open("a", encoding="utf-8") as f:
                            f.write(json.dumps({"thread_id": _journal_thread.get(), "stage": stage,
                                "image_sha256": [hashlib.sha256(url.encode()).hexdigest() for url in images]}) + "\n")
                    try:
                        return method(*args, **kwargs)
                    except Exception as error:
                        body = getattr(error, "body", None)
                        if isinstance(body, dict):
                            body = body.get("error", body)
                        safe = {k: body[k] for k in ("code", "type", "param", "message") if isinstance(body, dict) and k in body}
                        raw = json.dumps({"at": datetime.now(timezone.utc).isoformat(), "method": name,
                            "status_code": getattr(error, "status_code", None),
                            "error_type": type(error).__name__, "body": safe}, ensure_ascii=False)
                        if service.settings.api_key:
                            raw = raw.replace(service.settings.api_key, "[REDACTED]")
                        with (root / (variant + "-provider-errors.jsonl")).open("a", encoding="utf-8") as f:
                            f.write(raw + "\n")
                        raise
                return call
        self.backend = RecordingBackend()
        self.acceptance_tutor = build_tutor(variant, self.backend, self.settings)
        self.journal_lock = threading.Lock()

    def _respond(self, context, question, existing_code, question_id="", attachments=()):
        start = time.monotonic()
        token = _journal_thread.set(context.session_id)
        row = {
            "at": datetime.now(timezone.utc).isoformat(), "variant": variant,
            "thread_id": context.session_id, "question": question,
            "existing_code": existing_code,
            "attachments": [{"name": a.name, "mime": a.mime, "kind": a.kind} for a in attachments],
        }
        try:
            response = self.acceptance_tutor.respond(
                TutorRequest(question, existing_code, context.exam_mode, question_id, tuple(attachments)),
                history=context.history,
                session_guard=(context.session_guard if VARIANTS[variant].use_session_guard else None),
            )
            row["response"] = asdict(response)
            return response
        except Exception as error:
            # Exception messages may contain provider request details; record type only.
            row["error_type"] = type(error).__name__
            raise
        finally:
            _journal_thread.reset(token)
            row["seconds"] = round(time.monotonic() - start, 3)
            with self.journal_lock:
                with (root / (variant + "-journal.jsonl")).open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()


import chainlit_app as student_app

student_app._service = JournalService()
