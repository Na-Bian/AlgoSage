"""Streamlit demo; never expose the API key to the browser/UI."""
import streamlit as st
from examguard.config import Settings
from examguard.llm import make_backend
from examguard.pipeline import ExamGuardTutor
from examguard.schemas import TutorRequest

st.set_page_config(page_title="ExamGuard", page_icon="🛡️", layout="centered")
settings = Settings.from_env()
st.title("🛡️ ExamGuard · 算法考试受限助教")
st.caption("第一阶段原型：策略与规则为演示性质，不具备可靠的答案泄漏防护保证。")
with st.sidebar:
    st.subheader("运行设置")
    st.write(f"运行方式：`{settings.mode}`")
    st.write(f"配置模型：`{settings.model}`")
    exam_mode = st.toggle("考试限制模式", value=settings.exam_mode)
    st.info("切换 API/模拟模式请编辑 .env 中的 APP_MODE 并重新启动。")
    if st.button("清除会话"):
        st.session_state["history"] = []
        st.rerun()

if "history" not in st.session_state:
    st.session_state["history"] = []

code = st.text_area("学生已有代码（可选，仅作文本分析，不执行）", height=110)
for m in st.session_state["history"]:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

question = st.chat_input("例如：这段 KMP 实现为什么在重复模式串上出错？")
if question:
    prev = list(st.session_state["history"])
    with st.chat_message("user"):
        st.markdown(question)
    try:
        backend = make_backend(settings)
        tutor = ExamGuardTutor(backend, settings)
        resp = tutor.respond(TutorRequest(question, code, exam_mode), history=prev)
    except Exception as error:
        st.error(f"请求未完成：{type(error).__name__}: {error}")
    else:
        with st.chat_message("assistant"):
            st.markdown(resp.answer)
            st.caption(
                f"意图：{resp.classification.intent.value} ｜ "
                f"允许等级：{resp.decision.max_help_level} ｜ "
                f"审核：{resp.audit.reason} ｜ 模型：{resp.model}"
            )
        # Store the text locally in the Streamlit session, not on disk.
        st.session_state["history"].extend([
            {"role": "user", "content": question + ("\n\n已有代码：\n" + code if code else "")},
            {"role": "assistant", "content": resp.answer},
        ])
