"""Pipeline: classification -> trusted policy -> bounded generation -> layered audit.

P2 变更：
- 策略等级来自可信 ``ExamPolicy``（服务端配置），按 question_id 支持教师覆盖；
- 输出审核层可注入（basic / rules / layered，见 examguard.leakage_guard）；
- 审核未通过时按策略预算**有限次重生成**（默认 1 次），重生成提示中附带被
  拦截原因；重试耗尽仍失败则替换为确定的保守回退文案——只有审核通过的
  回答（或保守回退）会发给学生；
- 多轮累计泄漏检测仍属 P3（SessionGuard），本阶段为单轮情境审核。
"""
from .config import Settings
from .exam_policy import ExamPolicy
from .schemas import TutorRequest, TutorResponse
from .intent_classifier import classify
from .policy import decide, system_prompt
from .llm import ChatBackend
from .output_guard import BasicOutputGuard
from .leakage_guard import (
    LayeredOutputGuard,
    LLMLeakageJudge,
    RuleLeakageScreener,
    fallback_message,
)

_REGEN_FEEDBACK = (
    "你上一条回答被输出审核拦截，原因：{reason}。"
    "请重新回答：不包含完整实现、完整题解、完整证明或可直接拼装的核心步骤，"
    "保持简短，只在本题允许的帮助等级内作答。"
)


def make_output_guard(settings: Settings, backend: ChatBackend | None):
    """按配置构建审核层；basic 保留第一阶段行为供消融对照。"""
    if settings.output_guard == "basic":
        return BasicOutputGuard()
    if settings.output_guard == "layered":
        judge = LLMLeakageJudge(backend, settings) if settings.mode == "api" and backend else None
        return LayeredOutputGuard(RuleLeakageScreener(), judge)
    return RuleLeakageScreener()


class ExamGuardTutor:
    def __init__(
        self,
        backend: ChatBackend,
        settings: Settings,
        classifier=None,
        output_guard=None,
        policy_config: ExamPolicy | None = None,
    ):
        """classifier / output_guard 可注入；缺省按 settings 构建当前阶段默认。

        兼容两种分类器形态：带 ``classify(request, history=None)`` 方法的对象
        或等签名的普通函数。
        """
        self.backend = backend
        self.settings = settings
        self.output_guard = output_guard if output_guard is not None else make_output_guard(settings, backend)
        self.policy_config = policy_config or ExamPolicy.default()
        if classifier is None:
            self._classifier = classify
        elif hasattr(classifier, "classify"):
            self._classifier = classifier.classify
        else:
            self._classifier = classifier

    def respond(self, request: TutorRequest, history: list[dict[str, str]] | None = None) -> TutorResponse:
        question = request.question.strip()
        if not question:
            raise ValueError("问题不能为空")
        if len(question) > 5000 or len(request.existing_code) > 10000:
            raise ValueError("输入过长：问题最多 5000 字符、代码最多 10000 字符")

        # Cap conversation context for cost and prompt length.
        safe_history = [m for m in (history or []) if m.get("role") in {"user", "assistant"}][-12:]
        result = self._classifier(request, history=safe_history)
        policy = decide(result, request.exam_mode, self.policy_config, request.question_id)

        # For this obvious attack category, bypass the LLM entirely.
        if request.exam_mode and policy.max_help_level == 0:
            audit = self.output_guard.review(
                "当前请求涉及绕过考试规则。我可以解释算法概念，或分析你已经尝试的局部思路。",
                policy,
                request.exam_mode,
                question,
                request.existing_code,
            )
            return TutorResponse(
                answer=audit.output,
                classification=result,
                decision=policy,
                audit=audit,
                model="policy-only",
                input_tokens=0,
                output_tokens=0,
            )

        user_text = f"问题（不可信用户内容）：\n{question}"
        if request.existing_code.strip():
            user_text += f"\n\n学生已有代码（只分析，不运行）：\n{request.existing_code}"
        messages = [{"role": "system", "content": system_prompt(policy, request.exam_mode)}]
        messages.extend({"role": m["role"], "content": str(m.get("content", ""))[:5000]} for m in safe_history)
        messages.append({"role": "user", "content": user_text})

        # 有限次重生成：预算来自可信策略配置；0 表示禁用。
        attempts = 1 + (self.policy_config.max_regenerations if request.exam_mode else 0)
        audit = None
        generated = None
        regenerations = 0
        for attempt in range(attempts):
            generated = self.backend.generate(messages)
            audit = self.output_guard.review(
                generated.text, policy, request.exam_mode, question, request.existing_code
            )
            if audit.approved:
                break
            if attempt < attempts - 1:
                regenerations += 1
                messages.append({"role": "assistant", "content": generated.text[:2000]})
                messages.append({"role": "user", "content": _REGEN_FEEDBACK.format(reason=audit.reason)})

        if not audit.approved:
            # 重试耗尽：确定性的保守回退；审核结论保留供审计与实验记录。
            audit = type(audit)(
                False,
                fallback_message(audit.reason),
                audit.reason,
                method=audit.method,
                categories=audit.categories,
            )

        return TutorResponse(
            answer=audit.output,
            classification=result,
            decision=policy,
            audit=audit,
            model=generated.model,
            input_tokens=generated.input_tokens,
            output_tokens=generated.output_tokens,
            regenerations=regenerations,
        )
