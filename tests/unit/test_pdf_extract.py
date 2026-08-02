from __future__ import annotations

import pytest

from agentkit.core.ocr import OcrResult
from agentkit.core.pdf_extract import PdfBlock, PdfExtractResult, extract_pdf


class _FakeOcr:
    name = "fake"
    model = "fake-model"
    enabled = True

    def analyze(self, image_bytes: bytes, *, mime_type: str, hint: str = "") -> OcrResult:
        del image_bytes, mime_type, hint
        return OcrResult(status="completed", text="OCR_RECOGNIZED_TEXT", provider="fake")


class _DisabledOcr:
    name = "disabled"
    model = ""
    enabled = False

    def analyze(self, image_bytes: bytes, *, mime_type: str, hint: str = "") -> OcrResult:
        raise AssertionError("disabled OCR must not be called")


def _make_pdf(path, pages: list[str]) -> None:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    try:
        for text in pages:
            page = doc.new_page()
            page.insert_text((72, 72), text)
        doc.save(path)
    finally:
        doc.close()


def test_extract_pdf_text_only(tmp_path):
    path = tmp_path / "sample.pdf"
    _make_pdf(path, ["Page one content", "Page two content"])

    result = extract_pdf(str(path))

    assert isinstance(result, PdfExtractResult)
    assert result.text == "Page one content\n\nPage two content"
    assert [block.page for block in result.blocks] == [1, 2]
    assert all(block.kind == "page_text" for block in result.blocks)


def test_extract_pdf_start_page_and_max_pages(tmp_path):
    path = tmp_path / "pages.pdf"
    _make_pdf(path, ["p1", "p2", "p3", "p4"])

    result = extract_pdf(str(path), start_page=1, max_pages=2)

    assert [block.page for block in result.blocks] == [2, 3]


def test_extract_pdf_ocr_sparse_pages(tmp_path):
    path = tmp_path / "scan.pdf"
    _make_pdf(path, ["short"])

    result = extract_pdf(str(path), ocr_provider=_FakeOcr(), min_page_text_chars=10)

    assert any(block.kind == "page_ocr" for block in result.blocks)
    assert "OCR_RECOGNIZED_TEXT" in result.text


def test_extract_pdf_disabled_ocr_is_never_called(tmp_path):
    path = tmp_path / "scan.pdf"
    _make_pdf(path, ["short"])

    result = extract_pdf(str(path), ocr_provider=_DisabledOcr(), min_page_text_chars=10)

    assert all(block.kind == "page_text" for block in result.blocks)


def test_extract_pdf_no_text_raises(tmp_path):
    path = tmp_path / "empty.pdf"
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    try:
        doc.new_page()
        doc.save(path)
    finally:
        doc.close()

    with pytest.raises(ValueError):
        extract_pdf(str(path))


def test_extract_pdf_images_recorded(tmp_path):
    path = tmp_path / "img.pdf"
    _make_pdf(path, ["has image below"])

    result = extract_pdf(str(path), extract_images=True)

    image_blocks = [b for b in result.blocks if b.kind == "image"]
    assert isinstance(result.blocks[0], PdfBlock)
    # 该 PDF 无内嵌图片时不应报错；若解析出图片则元数据应完整
    for block in image_blocks:
        assert "width" in block.metadata
        assert "height" in block.metadata
