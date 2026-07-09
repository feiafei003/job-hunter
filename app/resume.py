"""简历文本提取：支持 PDF / DOCX / 纯文本。

依赖 pypdf（PDF）与 python-docx（DOCX）；都是纯 Python 库。
解析失败抛 ValueError，调用方转成友好的 400 提示。
"""

from __future__ import annotations

import io


def _from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # noqa: BLE001
        raise ValueError("服务端缺少 pypdf，无法解析 PDF，请粘贴简历文本") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"PDF 解析失败：{exc}") from exc


def _from_docx(data: bytes) -> str:
    try:
        import docx  # python-docx
    except Exception as exc:  # noqa: BLE001
        raise ValueError("服务端缺少 python-docx，无法解析 Word，请粘贴简历文本") from exc
    try:
        document = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in document.paragraphs)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Word 解析失败：{exc}") from exc


def extract_text(filename: str, data: bytes) -> str:
    """按文件扩展名提取文本。"""
    name = (filename or "").lower()
    if not data:
        raise ValueError("文件为空")
    if name.endswith(".pdf"):
        text = _from_pdf(data)
    elif name.endswith(".docx"):
        text = _from_docx(data)
    elif name.endswith(".doc"):
        raise ValueError("暂不支持旧版 .doc，请另存为 .docx 或 PDF，或直接粘贴文本")
    else:
        # 纯文本/未知：尽力按文本解码
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            raise ValueError("无法识别的文件编码，请粘贴简历文本")
    text = (text or "").strip()
    if not text:
        raise ValueError("未能从文件中提取到文本，请粘贴简历文本")
    return text
