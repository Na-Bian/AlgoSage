"""Validate and prepare chat attachments without executing their contents.

Images are normalised for multimodal Chat Completions requests. Documents are
extracted to text on the server and marked as untrusted data by the prompt
builder. Chainlit persists uploads in a temporary session directory, so this
module reads each file during the turn and keeps only a bounded text copy or a
normalised image data URL in backend history.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import mimetypes
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from .schemas import AttachmentPayload

MAX_FILES = 6
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_EXTRACTED_CHARS = 30_000
MAX_IMAGE_DIMENSION = 1600
MAX_IMAGE_BYTES = 2_500_000
MAX_NAME_CHARS = 200

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
}
SUPPORTED_MIMES = {
    "application/pdf",
    "application/json",
    "application/x-ipynb+json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/octet-stream",
}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".ipynb",
    ".html",
    ".htm",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".log",
    ".tex",
    ".py",
    ".pyw",
    ".java",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".swift",
    ".kt",
    ".kts",
    ".scala",
    ".rb",
    ".php",
    ".cs",
    ".sql",
    ".sh",
    ".bash",
    ".zsh",
    ".r",
    ".m",
}
_TEXT_MIME_PREFIXES = ("text/",)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class AttachmentError(ValueError):
    """A safe, user-facing attachment validation error."""


@dataclass(frozen=True)
class PreparedAttachments:
    files: tuple[AttachmentPayload, ...]

    @property
    def text(self) -> str:
        blocks = [
            f"[附件 {index}：{item.name}]\n{item.text}"
            for index, item in enumerate(self.files, start=1)
            if item.text
        ]
        return "\n\n".join(blocks)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.files)

    @property
    def has_images(self) -> bool:
        return any(item.image_data_url for item in self.files)

    @property
    def has_documents(self) -> bool:
        return any(item.text for item in self.files)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        elif tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data)


def _display_name(value: str, path: Path) -> str:
    name = Path(value or path.name).name.replace("\x00", "").strip()
    return (name or "未命名附件")[:MAX_NAME_CHARS]


def _normalise_mime(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AttachmentError("文本文件编码无法识别，请转换为 UTF-8 后重试")


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def _truncate(text: str, budget: int) -> str:
    text = _clean_text(text)
    if len(text) <= budget:
        return text
    return text[: max(0, budget - 80)].rstrip() + "\n\n[内容过长，已按服务端上限截断]"


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:  # pragma: no cover - dependency guard
        raise AttachmentError("服务端缺少 PDF 解析组件，请联系管理员") from error
    try:
        reader = PdfReader(str(path))
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"[第 {index} 页]\n{text}")
        if not pages:
            raise AttachmentError(
                "该 PDF 未提取到可读文本；若是扫描件，请上传清晰的页面图片"
            )
        return "\n\n".join(pages)
    except AttachmentError:
        raise
    except Exception as error:
        raise AttachmentError(
            "PDF 解析失败；文件可能损坏、加密或格式不受支持"
        ) from error


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as error:  # pragma: no cover - dependency guard
        raise AttachmentError("服务端缺少 Word 解析组件，请联系管理员") from error
    try:
        document = Document(str(path))
        parts = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    except Exception as error:
        raise AttachmentError("Word 文档解析失败，请确认文件未损坏") from error


def _extract_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError as error:  # pragma: no cover - dependency guard
        raise AttachmentError("服务端缺少 PowerPoint 解析组件，请联系管理员") from error
    try:
        presentation = Presentation(str(path))
        slides = []
        for index, slide in enumerate(presentation.slides, start=1):
            parts = []
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    text = getattr(shape, "text", "")
                    if text.strip():
                        parts.append(text)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        if any(cells):
                            parts.append(" | ".join(cells))
            if parts:
                slides.append(f"[第 {index} 页幻灯片]\n" + "\n".join(parts))
        return "\n\n".join(slides)
    except Exception as error:
        raise AttachmentError("PowerPoint 文件解析失败，请确认文件未损坏") from error


def _extract_xlsx(path: Path) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError as error:  # pragma: no cover - dependency guard
        raise AttachmentError("服务端缺少 Excel 解析组件，请联系管理员") from error
    try:
        workbook = load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for sheet in workbook.worksheets:
            rows = []
            for row_index, row in enumerate(sheet.iter_rows(values_only=True)):
                if row_index >= 200:
                    rows.append("[该工作表超过 200 行，已截断]")
                    break
                values = ["" if value is None else str(value) for value in row[:50]]
                if any(value.strip() for value in values):
                    rows.append(" | ".join(values).rstrip(" |"))
            if rows:
                parts.append(f"[工作表：{sheet.title}]\n" + "\n".join(rows))
        workbook.close()
        return "\n\n".join(parts)
    except Exception as error:
        raise AttachmentError("Excel 文件解析失败，请确认文件未损坏") from error


def _extract_notebook(text: str) -> str:
    try:
        notebook = json.loads(text)
    except json.JSONDecodeError as error:
        raise AttachmentError("Jupyter Notebook JSON 无效") from error
    cells = notebook.get("cells")
    if not isinstance(cells, list):
        raise AttachmentError("Jupyter Notebook 缺少 cells 列表")
    parts = []
    for index, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            continue
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(str(item) for item in source)
        if str(source).strip():
            kind = str(cell.get("cell_type", "cell"))
            parts.append(f"[单元 {index}：{kind}]\n{source}")
    return "\n\n".join(parts)


def _extract_html(text: str) -> str:
    parser = _HTMLText()
    parser.feed(text)
    return "\n".join(part for part in parser.parts if part.strip())


def _extract_text_file(path: Path, suffix: str) -> str:
    raw = path.read_bytes()
    text = _decode_text(raw)
    if suffix == ".ipynb":
        return _extract_notebook(text)
    if suffix in {".html", ".htm"}:
        return _extract_html(text)
    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        rows = csv.reader(io.StringIO(text), delimiter=delimiter)
        return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
    return text


def _extract_document(path: Path, suffix: str) -> str:
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".pptx":
        return _extract_pptx(path)
    if suffix == ".xlsx":
        return _extract_xlsx(path)
    if suffix in TEXT_EXTENSIONS:
        return _extract_text_file(path, suffix)
    raise AttachmentError(
        "暂不支持该文档格式；请使用 PDF、DOCX、PPTX、XLSX、文本或常见源代码文件"
    )


def _image_data_url(path: Path) -> tuple[str, str]:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as error:  # pragma: no cover - dependency guard
        raise AttachmentError("服务端缺少图片处理组件，请联系管理员") from error
    Image.MAX_IMAGE_PIXELS = 40_000_000
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
            has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
            if has_alpha:
                rgba = image.convert("RGBA")
                buffer = io.BytesIO()
                rgba.save(buffer, format="PNG", optimize=True)
                encoded = buffer.getvalue()
                mime = "image/png"
                if len(encoded) > MAX_IMAGE_BYTES:
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                else:
                    return mime, base64.b64encode(encoded).decode("ascii")
            rgb = image.convert("RGB")
            for max_dimension, quality in (
                (MAX_IMAGE_DIMENSION, 86),
                (1280, 82),
                (1024, 78),
                (800, 75),
            ):
                candidate = rgb.copy()
                candidate.thumbnail((max_dimension, max_dimension))
                buffer = io.BytesIO()
                candidate.save(
                    buffer,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                )
                encoded = buffer.getvalue()
                if len(encoded) <= MAX_IMAGE_BYTES:
                    return "image/jpeg", base64.b64encode(encoded).decode("ascii")
            raise AttachmentError("图片压缩后仍过大，请裁剪或降低分辨率后重试")
    except AttachmentError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise AttachmentError(
            "图片无法读取，请上传有效的 PNG、JPEG 或 WebP 文件"
        ) from error
    except Exception as error:
        raise AttachmentError("图片处理失败；可能是图片过大或已损坏") from error


def _validated_suffix(name: str, mime: str) -> str:
    suffix = Path(name).suffix.lower()
    if mime in IMAGE_MIMES and suffix not in IMAGE_EXTENSIONS:
        guessed = mimetypes.guess_extension(mime)
        suffix = (guessed or suffix).lower()
    if suffix in IMAGE_EXTENSIONS:
        return suffix
    if suffix in TEXT_EXTENSIONS or suffix in {".pdf", ".docx", ".pptx", ".xlsx"}:
        return suffix
    raise AttachmentError(
        f"文件 {name!r} 的格式不受支持；可上传图片、PDF、DOCX、PPTX、XLSX、文本或代码文件"
    )


def prepare_attachment_elements(elements: Iterable[object]) -> PreparedAttachments:
    """Convert Chainlit file/image elements into bounded model payloads."""
    elements = list(elements or [])
    if len(elements) > MAX_FILES:
        raise AttachmentError(f"一次最多上传 {MAX_FILES} 个附件")
    total = 0
    prepared: list[AttachmentPayload] = []
    remaining_text = MAX_EXTRACTED_CHARS
    for element in elements:
        raw_path = getattr(element, "path", None)
        if not raw_path:
            raise AttachmentError("附件缺少服务端文件路径，请重新上传")
        path = Path(str(raw_path))
        if not path.is_file():
            raise AttachmentError("附件已失效，请重新上传")
        size = path.stat().st_size
        if size <= 0:
            raise AttachmentError("附件为空，请重新上传")
        if size > MAX_FILE_BYTES:
            raise AttachmentError(
                f"单个附件不能超过 {MAX_FILE_BYTES // (1024 * 1024)} MB"
            )
        total += size
        if total > MAX_TOTAL_BYTES:
            raise AttachmentError(
                f"附件总大小不能超过 {MAX_TOTAL_BYTES // (1024 * 1024)} MB"
            )

        name = _display_name(getattr(element, "name", ""), path)
        mime = _normalise_mime(getattr(element, "mime", None))
        suffix = _validated_suffix(name, mime)
        if mime and (
            mime not in SUPPORTED_MIMES
            and mime not in IMAGE_MIMES
            and not mime.startswith(_TEXT_MIME_PREFIXES)
        ):
            raise AttachmentError(f"附件 {name!r} 的 MIME 类型不受支持")

        if suffix in IMAGE_EXTENSIONS:
            image_mime, encoded = _image_data_url(path)
            prepared.append(
                AttachmentPayload(
                    name=name,
                    mime=image_mime,
                    kind="image",
                    image_data_url=f"data:{image_mime};base64,{encoded}",
                    size_bytes=size,
                )
            )
            continue

        if suffix in {".docx", ".pptx", ".xlsx"} and not zipfile.is_zipfile(path):
            raise AttachmentError(f"附件 {name!r} 不是有效的 Office Open XML 文件")
        text = _truncate(_extract_document(path, suffix), remaining_text)
        if not text:
            raise AttachmentError(f"附件 {name!r} 未提取到可读文本")
        remaining_text = max(0, remaining_text - len(text))
        prepared.append(
            AttachmentPayload(
                name=name,
                mime=mime
                or mimetypes.guess_type(name)[0]
                or "application/octet-stream",
                kind="document",
                text=text,
                size_bytes=size,
            )
        )
        if remaining_text <= 0:
            break
    return PreparedAttachments(tuple(prepared))
