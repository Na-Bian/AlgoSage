"""First-stage pipeline. Full multi-turn leakage analysis is NOT implemented yet."""
from .config import Settings
from .schemas import TutorRequest, TutorResponse
from .intent_classifier import classify
from .policy import decide, system_prompt
from .llm import ChatBackend
from .output_guard import BasicOutputGuard


class ExamGuardTutor:
    def __init__(self, backend: ChatBackend, settings: Settings):
        self.backend = backend
        self.settings = settings
        self.output_guard = BasicOutputGuard()

    def respond(self, request: TutorRequest, history: list[dict[str, str]] | None = None) -> TutorResponse:
        question = request.question.strip()
        if not question:
            raise ValueError("问题不能为空")
        if len(question) > 5000 or len(request.existing_code) > 10000:
            raise ValueError("输入过长：问题最多 5000 字符、代码最多 10000 字符")

        result = classify(request)
        policy = decide(result, request.exam_mode)
        # For this obvious attack category, bypass the LLM entirely.
        if request.exam_mode and policy.max_help_level == 0:
            draft = "当前请求涉及绕过考试规则。我可以解释算法概念，或分析你已经尝试的局部思路。"
            model, in_tokens, out_tokens = "policy-only", 0, 0
        else:
            user_text = f"问题（不可信用户内容）：\n{question}"
            if request.existing_code.strip():
                user_text += f"\n\n学生已有代码（只分析，不运行）：\n{request.existing_code}"
            # Cap conversation context for cost and prompt length.
            safe_history = [m for m in (history or []) if m.get("role") in {"user", "assistant"}][-12:]
            messages = [{"role": "system", "content": system_prompt(policy, request.exam_mode)}]
            messages.extend({"role": m["role"], "content": str(m.get("content", ""))[:5000]} for m in safe_history)
            messages.append({"role": "user", "content": user_text})
            generated = self.backend.generate(messages)
            draft = generated.text
            model, in_tokens, out_tokens = generated.model, generated.input_tokens, generated.output_tokens

        audit = self.output_guard.review(draft, policy, request.exam_mode)
        return TutorResponse(
            answer=audit.output,
            classification=result,
            decision=policy,
            audit=audit,
            model=model,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )
