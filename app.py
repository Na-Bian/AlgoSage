"""Streamlit fallback; all turns and safety state belong to TutorService."""

from uuid import uuid4

import streamlit as st
from examguard.config import Settings
from examguard.service import TutorService


@st.cache_resource
def get_service() -> TutorService:
    return TutorService(Settings.from_env())


st.set_page_config(page_title="ExamGuard", page_icon="🛡️", layout="centered")
service = get_service()
settings = service.settings
if "examguard_session_id" not in st.session_state:
    st.session_state["examguard_session_id"] = uuid4().hex
session_id = st.session_state["examguard_session_id"]
context = service.new_context(session_id)

st.title("🛡️ ExamGuard · 算法考试受限助教")
st.caption("第一阶段原型：策略与规则为演示性质，不具备可靠的答案泄漏防护保证。")
with st.sidebar:
    st.subheader("运行设置")
    st.write(f"运行方式：`{settings.mode}`")
    st.write(f"配置模型：`{settings.model}`")
    st.write(f"考试限制模式：`{settings.exam_mode}`（服务端配置）")
    st.info("切换 API/模拟模式请编辑 .env 中的 APP_MODE 并重新启动。")
    st.caption("匿名新会话没有可信的跨聊天身份，无法保证跨聊天累计风险防护。")
    if st.button("清除聊天历史"):
        service.drop_context(session_id)
        st.rerun()

code = st.text_area("学生已有代码（可选，仅作文本分析，不执行）", height=110)
for message in context.history:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

question = st.chat_input("例如：这段 KMP 实现为什么在重复模式串上出错？")
if question:
    with st.chat_message("user"):
        st.markdown(question)
    try:
        response = service.chat(context, question, code)
    except Exception as error:
        st.error(f"请求未完成：{type(error).__name__}")
    else:
        with st.chat_message("assistant"):
            st.markdown(response.answer)
            st.caption(
                f"意图：{response.classification.intent.value} ｜ "
                f"允许等级：{response.decision.max_help_level} ｜ "
                f"审核：{response.audit.reason} ｜ 模型：{response.model}"
            )
