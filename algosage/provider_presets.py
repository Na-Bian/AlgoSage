"""常见厂商的 OpenAI 兼容接入预设。

用户在「添加模型提供商」弹窗点「预设」芯片后只需填 API Key——Base URL
按各厂商官方文档预填；厂商会随时发布新模型，预设不锁定模型名，模型一律
由「获取模型列表」实时拉取后由用户选择。预设只影响表单初值，注册链路
（探测、能力推断、加密落盘）与手动填写完全一致。调整预设请核对厂商官方
文档——端点路径偶尔变动。
"""

# 排序原则：国内课程最常用优先，聚合平台（多模型）靠前。
PROVIDER_PRESETS = [
    {
        "id": "siliconflow",
        "label": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "hint": "聚合平台（Qwen/DeepSeek/Llama 等 200+ 模型），Key 在 siliconflow.cn 创建",
    },
    {
        "id": "dashscope",
        "label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "hint": "阿里云百炼兼容模式端点，Key 在 bailian.console.aliyun.com 创建",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "hint": "Key 在 platform.deepseek.com 创建",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "hint": "按量付费端点；Coding Plan 请改用 api/coding/paas/v4",
    },
    {
        "id": "moonshot",
        "label": "Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "hint": "Key 在 platform.moonshot.cn（国际站 moonshot.ai）创建",
    },
    {
        "id": "volcengine",
        "label": "火山方舟",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "hint": "豆包系列；需在方舟控制台开通模型（接入点 ID 或模型名均可）",
    },
    {
        "id": "xiaomimimo",
        "label": "小米 MiMo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "hint": "Key 在 platform.xiaomimimo.com 创建",
    },
    {
        "id": "minimax",
        "label": "MiniMax",
        "base_url": "https://api.minimaxi.com/v1",
        "hint": "国内站端点；Key 在 platform.minimaxi.com 创建（与国际站不通用）",
    },
    {
        "id": "hunyuan",
        "label": "腾讯混元",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "hint": "Key 在腾讯云混元控制台创建",
    },
    {
        "id": "qianfan",
        "label": "百度千帆",
        "base_url": "https://qianfan.baidubce.com/v2",
        "hint": "V2 API（Bearer 认证）；Key 在千帆控制台创建",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "hint": "Key 在 platform.openai.com 创建",
    },
]


def preset_by_id(preset_id):
    for preset in PROVIDER_PRESETS:
        if preset["id"] == preset_id:
            return preset
    return None


def public_presets():
    """给前端的选择器数据：id/label/base_url/hint。"""
    return [
        {
            "id": p["id"],
            "label": p["label"],
            "base_url": p["base_url"],
            "hint": p["hint"],
        }
        for p in PROVIDER_PRESETS
    ]
