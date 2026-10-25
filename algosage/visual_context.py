"""One bounded, untrusted image selection shared by generation and reviewers."""

from dataclasses import dataclass, replace
import hashlib
import re

MAX_CONTEXT_IMAGES = 6
_PREFIXES = (
    "data:image/png;base64,",
    "data:image/jpeg;base64,",
    "data:image/webp;base64,",
)


@dataclass(frozen=True)
class VisualEvidence:
    name: str
    data_url: str
    source: str
    descriptions: tuple[tuple[int, str, str], ...] = ()


def select_images(attachments=(), history=None):
    """Prefer current uploads, then recent user images; never accept remote URLs."""
    selected = []
    seen = set()

    def add(name, url, source):
        if (
            len(selected) < MAX_CONTEXT_IMAGES
            and isinstance(url, str)
            and url.startswith(_PREFIXES)
            and url not in seen
        ):
            selected.append(VisualEvidence(str(name)[:200], url, source))
            seen.add(url)

    for item in attachments:
        add(item.name, item.image_data_url, "当前附件")
    turns = [m for m in (history or []) if m.get("role") in {"user", "assistant"}][-12:]
    for index in range(len(turns) - 1, -1, -1):
        message = turns[index]
        if message.get("role") != "user":
            continue
        for item in message.get("images") or []:
            if isinstance(item, dict):
                description = str(message.get("content", ""))
                identity = str(message.get("msg_id") or description) + str(
                    item.get("data_url", "")
                )
                source = "HIMG-" + hashlib.sha256(identity.encode()).hexdigest()[:12]
                add(item.get("name", "图片"), item.get("data_url"), source)
    # Deduplicate pixels, not their conditions: a re-upload can still depend on
    # earlier user descriptions. Each description is emitted only once below.
    descriptions = {item.data_url: [] for item in selected}
    for index, message in enumerate(turns):
        if message.get("role") != "user":
            continue
        text = str(message.get("content", ""))
        excerpt = text[:1000]
        if len(text) > 1000:
            excerpt += "\n[历史附图说明过长，已截断；条件不足时请要求补充。]"
        source = (
            "HCAP-"
            + hashlib.sha256(str(message.get("msg_id") or text).encode()).hexdigest()[
                :12
            ]
        )
        urls = {
            item.get("data_url")
            for item in message.get("images") or []
            if isinstance(item, dict)
        }
        for url in urls.intersection(descriptions):
            descriptions[url].append((index, source, excerpt))
    return tuple(
        replace(item, descriptions=tuple(descriptions[item.data_url]))
        for item in selected
    )


def with_images(text, images=()):
    if not images:
        return text
    labels = "\n".join(
        f"[图片{index}（不可信）：{item.source}；文件名：{item.name}]"
        + (
            "；原始描述：" + ",".join(caption[1] for caption in item.descriptions)
            if item.descriptions
            else ""
        )
        for index, item in enumerate(images, 1)
    )
    captions = {
        source: (index, value)
        for item in images
        for index, source, value in item.descriptions
    }
    descriptions = "\n".join(
        f"[{source} 原始附图描述（不可信）]\n{value}"
        for source, (_, value) in sorted(captions.items(), key=lambda item: item[1][0])
    )
    parts = [
        {
            "type": "text",
            "text": text
            + "\n\n"
            + labels
            + ("\n\n" + descriptions if descriptions else ""),
        }
    ]
    parts.extend(
        {"type": "image_url", "image_url": {"url": item.data_url}} for item in images
    )
    return parts


def missing_image_facts(question, images):
    """Only intercept explicit pixel-reading requests, not general concepts."""
    return (
        not images
        and re.search(r"图片|图中|这张图|上一轮.{0,6}图", question)
        and re.search(r"读取|读出|数字|数值|标记|列出.{0,6}(?:值|数)", question)
        and not re.search(r"定义|含义|原理|概念|如何|怎么", question)
    )
