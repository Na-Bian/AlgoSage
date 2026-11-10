"""常见厂商的 OpenAI 兼容接入预设。

用户在「添加模型提供商」弹窗选择预设后只需填 API Key；Base URL 与默认
模型名按各厂商官方文档预填。预设只影响表单初值，注册链路（探测、能力
推断、加密落盘）与手动填写完全一致；模型名可改，列表也可用「获取模型
列表」重新拉取。调整预设请核对厂商官方文档——端点路径偶尔变动。
"""

PROVIDER_PRESETS = [
    {
        "id": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen3.5-9B",
        "models": [
            "Qwen/Qwen3.5-9B",
            "Qwen/Qwen3.5-4B",
            "deepseek-ai/DeepSeek-V3.2",
            "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",
        ],
        "hint": "聚合平台，Key 可在 siliconflow.cn 控制台创建",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "hint": "Key 在 platform.deepseek.com 创建",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.3-flash",
        "models": ["glm-5.3-flash", "glm-5.3", "glm-5.3-air"],
        "hint": "按量付费端点；Coding Plan 请改用 api/coding/paas/v4",
    },
    {
        "id": "moonshot",
        "label": "Kimi (Moonshot)",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2.5",
        "models": ["kimi-k2.5", "kimi-k2.5-turbo"],
        "hint": "Key 在 platform.moonshot.cn / platform.kimi.com 创建",
    },
    {
        "id": "dashscope",
        "label": "通义千问（阿里云百炼）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "models": ["qwen-plus", "qwen-max", "qwen3.5-9b"],
        "hint": "兼容模式端点；Key 在 bailian.console.aliyun.com 创建",
    },
    {
        "id": "volcengine",
        "label": "火山方舟（豆包）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seed-2-0-pro",
        "models": ["doubao-seed-2-0-pro", "doubao-seed-2-0-lite"],
        "hint": "需在方舟控制台开通模型并使用接入点 ID 或模型名",
    },
    {
        "id": "xiaomimimo",
        "label": "小米 MiMo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-2.6",
        "models": ["mimo-2.6"],
        "hint": "Key 在 platform.xiaomimimo.com 创建",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.2",
        "models": ["gpt-5.2", "gpt-5.2-mini"],
        "hint": "Key 在 platform.openai.com 创建",
    },
]


def preset_by_id(preset_id):
    for preset in PROVIDER_PRESETS:
        if preset["id"] == preset_id:
            return preset
    return None


def public_presets():
    """给前端的选择器数据：id/label/base_url/model/models/hint。"""
    return [
        {
            "id": p["id"],
            "label": p["label"],
            "base_url": p["base_url"],
            "model": p["model"],
            "models": list(p["models"]),
            "hint": p["hint"],
        }
        for p in PROVIDER_PRESETS
    ]
