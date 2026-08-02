"""通用 PDF 提取：文本页 + 表格 + 图片 + 可选 OCR，输出结构化块（blocks）。

设计目的：把 PDF 读取抽象成可复用、带来源/页码元数据的管线，供
- 知识图谱抽取（``agentkit.core.knowledge.ingest`` 文本模式）
- RAG 入库（按块溯源、表格/图片/OCR 各自成块）等场景共用。

OCR 复用 ``agentkit.core.ocr.OcrProvider`` 契约（如 ``ocr_media`` / ``ollama_ocr``），
不在此模块内实现具体 OCR 供应商。

用法::

    result = extract_pdf("a.pdf", extract_tables=True, ocr_provider=provider)
    for block in result.blocks:
        print(block.page, block.kind, block.text[:40])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 文本提取优先 PyMuPDF（约比 pypdf 快 10 倍、中文提取更好），未安装时回退 pypdf


@dataclass(frozen=True)
class PdfBlock:
    """PDF 里的一个内容块。``kind``: page_text / table / image / page_ocr / image_ocr。"""

    text: str
    kind: str = "text"
    page: int | None = None
    source: str = "pdf"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PdfExtractResult:
    """一次 PDF 提取的结果。``text`` 是所有有文本的块按页拼接（供简单管线直接用）。"""

    text: str
    blocks: list[PdfBlock]
    warnings: list[str] = field(default_factory=list)


_KIND_RANK = {"page_text": 0, "table": 1, "page_ocr": 2, "image": 3, "image_ocr": 4}


def _kind_rank(kind: str) -> int:
    return _KIND_RANK.get(kind, 5)


def _page_range(total: int, start_page: int, max_pages: int) -> tuple[int, int]:
    begin = max(0, int(start_page))
    end = begin + int(max_pages) if max_pages else total
    return begin, min(end, total)


def extract_pdf(
    path: str | Path,
    *,
    start_page: int = 0,
    max_pages: int = 0,
    ocr_provider: Any | None = None,
    extract_tables: bool = False,
    extract_images: bool = False,
    min_page_text_chars: int = 40,
) -> PdfExtractResult:
    """提取 PDF 为结构化块。

    参数
    ----
    start_page / max_pages : 起始页（0-based）与最多读取页数（0 = 全部），用于跳过前言等。
    ocr_provider : 实现了 ``OcrProvider`` 契约的对象；启用且页面文本过少时对该页做 OCR。
    extract_tables : 用 PyMuPDF 检测表格并输出 ``table`` 块（仅 pymupdf 路径）。
    extract_images : 记录页面图片为 ``image`` 块（含尺寸/xref 元数据，不占文本）。
    min_page_text_chars : 低于该字符数的页面视为"稀疏"，交给 OCR。
    """
    path = Path(path)
    try:
        import fitz  # PyMuPDF
    except ImportError:  # pragma: no cover - 依赖 pymupdf
        fitz = None

    if fitz is not None:
        return _extract_with_fitz(
            path,
            fitz,
            start_page=start_page,
            max_pages=max_pages,
            ocr_provider=ocr_provider,
            extract_tables=extract_tables,
            extract_images=extract_images,
            min_page_text_chars=min_page_text_chars,
        )
    if extract_tables or extract_images or ocr_provider is not None:
        raise RuntimeError("表格/图片/OCR 提取需要 PyMuPDF；请安装: pip install 'agentkit[kg]'")
    return _extract_with_pypdf(path, start_page=start_page, max_pages=max_pages)


def _extract_with_fitz(
    path: Path,
    fitz: Any,
    *,
    start_page: int,
    max_pages: int,
    ocr_provider: Any | None,
    extract_tables: bool,
    extract_images: bool,
    min_page_text_chars: int,
) -> PdfExtractResult:
    warnings: list[str] = []
    blocks: list[PdfBlock] = []
    doc = fitz.open(path)
    try:
        begin, end = _page_range(doc.page_count, start_page, max_pages)
        for index in range(begin, end):
            page = doc[index]
            page_no = index + 1
            clean = (page.get_text("text") or "").strip()
            if clean:
                blocks.append(
                    PdfBlock(text=clean, kind="page_text", page=page_no, source="pdf_text")
                )
            if extract_tables:
                blocks.extend(_extract_tables(page, page_no))
            if extract_images:
                blocks.extend(_extract_images(page, page_no))
            if (
                ocr_provider is not None
                and getattr(ocr_provider, "enabled", False)
                and len(clean) < min_page_text_chars
            ):
                ocr_text = _ocr_page(ocr_provider, fitz, page, page_no, path, warnings)
                if ocr_text:
                    blocks.append(
                        PdfBlock(text=ocr_text, kind="page_ocr", page=page_no, source="ocr")
                    )
    finally:
        doc.close()

    if not blocks:
        raise ValueError(f"PDF 未提取到任何文本: {path}")
    blocks.sort(key=lambda block: (block.page or 0, _kind_rank(block.kind)))
    text = "\n\n".join(block.text for block in blocks if block.text.strip())
    return PdfExtractResult(text=text, blocks=blocks, warnings=warnings)


def _extract_tables(page: Any, page_no: int) -> list[PdfBlock]:
    out: list[PdfBlock] = []
    try:
        finder = page.find_tables()
    except Exception:  # noqa: BLE001 - 表格解析失败不影响其他内容
        return out
    for table in finder.tables:
        rows: list[str] = []
        for row in table.extract():
            cells = [str(cell).strip() if cell is not None else "" for cell in row]
            line = " | ".join(cell for cell in cells if cell)
            if line:
                rows.append(line)
        text = "\n".join(rows).strip()
        if text:
            out.append(
                PdfBlock(
                    text=text,
                    kind="table",
                    page=page_no,
                    source="pdf_table",
                    metadata={"rows": len(rows)},
                )
            )
    return out


def _extract_images(page: Any, page_no: int) -> list[PdfBlock]:
    out: list[PdfBlock] = []
    for info in page.get_images(full=True):
        xref = info[0]
        width, height = info[2], info[3]
        out.append(
            PdfBlock(
                text="",
                kind="image",
                page=page_no,
                source="pdf_image",
                metadata={"xref": xref, "width": width, "height": height},
            )
        )
    return out


def _ocr_page(
    provider: Any,
    fitz: Any,
    page: Any,
    page_no: int,
    path: Path,
    warnings: list[str],
) -> str:
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        result = provider.analyze(
            pix.tobytes("png"),
            mime_type="image/png",
            hint=f"{path.name} page {page_no}",
        )
        return result.text.strip() if result.status == "completed" else ""
    except Exception as exc:  # noqa: BLE001 - 单页 OCR 失败不影响整体
        warnings.append(f"OCR failed on page {page_no}: {exc}")
        return ""


def _extract_with_pypdf(
    path: Path,
    *,
    start_page: int,
    max_pages: int,
) -> PdfExtractResult:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    begin, end = _page_range(len(reader.pages), start_page, max_pages)
    blocks: list[PdfBlock] = []
    for index in range(begin, end):
        clean = (reader.pages[index].extract_text() or "").strip()
        if clean:
            blocks.append(PdfBlock(text=clean, kind="page_text", page=index + 1, source="pdf_text"))
    if not blocks:
        raise ValueError(f"PDF 未提取到任何文本: {path}")
    text = "\n\n".join(block.text for block in blocks)
    return PdfExtractResult(text=text, blocks=blocks)


__all__ = [
    "PdfBlock",
    "PdfExtractResult",
    "extract_pdf",
]
