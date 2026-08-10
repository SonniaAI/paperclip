from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient

from app import main
from app.materials import MaterialUpload, extract_material, parse_upload_body


def _pptx(slides: list[list[str]]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for number, paragraphs in enumerate(slides, start=1):
            text = "".join(f"<a:p><a:r><a:t>{value}</a:t></a:r></a:p>" for value in paragraphs)
            archive.writestr(
                f"ppt/slides/slide{number}.xml",
                "<p:sld xmlns:p='p' xmlns:a='a'><p:cSld><p:spTree>"
                f"{text}</p:spTree></p:cSld></p:sld>",
            )
    return output.getvalue()


def _xlsx() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            "<workbook xmlns='main' xmlns:r='rel'><sheets><sheet name='Proof' "
            "r:id='rId1'/></sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            "<Relationships xmlns='rel'><Relationship Id='rId1' "
            "Target='worksheets/sheet1.xml'/></Relationships>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            "<worksheet xmlns='main'><sheetData><row r='2'><c r='A2' "
            "t='inlineStr'><is><t>Customer result</t></is></c><c r='B2'><v>42</v>"
            "</c></row></sheetData></worksheet>",
        )
    return output.getvalue()


def _pdf() -> bytes:
    return b"""%PDF-1.4
1 0 obj
<< /Type /Page /Contents 2 0 R >>
endobj
2 0 obj
<< /Length 36 >>
stream
BT (Solar teams save 30% time.) Tj ET
endstream
endobj
%%EOF
"""


def test_pptx_is_ranked_by_phone_value_and_keeps_slide_links() -> None:
    result = extract_material(
        MaterialUpload(
            "pitch-deck.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            _pptx(
                [
                    ["Agenda"],
                    ["Our team", "Founded in 2020."],
                    [
                        "Solar teams reduce quote time by 30%.",
                        "Customers recover 12 hours per week.",
                    ],
                    [
                        "Security and integration questions are answered in the "
                        "implementation guide."
                    ],
                    ["Appendix: technical glossary"],
                ]
            ),
        ),
    )

    assert result["processing"]["state"] == "completed"
    assert result["processing"]["mode"] == "offline-fallback"
    assert result["selling_points"][0]["claim"] == "Customers recover 12 hours per week."
    assert result["selling_points"][0]["source"]["locator"] == "slide 3"
    assert result["selling_points"][0]["rank"] == 1
    assert {item["slide"] for item in result["slides_useless_on_phone"]} == {
        "slide 1",
        "slide 2",
        "slide 5",
    }

    for field in ("selling_points", "proof_points", "objection_material", "risky_claims"):
        for item in result[field]:
            assert item["inference"] is False
            assert item["source"]["document"] == "pitch-deck.pptx"
            assert item["source"]["link"].startswith("source://pitch-deck.pptx#")


def test_risky_claims_are_verbatim_and_flagged_for_approval() -> None:
    result = extract_material(
        MaterialUpload(
            "claims.md",
            "text/markdown",
            b"We are the only platform that always guarantees zero risk.\nBook a demo next week.",
        )
    )

    assert result["risky_claims"][0]["claim"] == (
        "We are the only platform that always guarantees zero risk."
    )
    assert result["risky_claims"][0]["requires_approval"] is True
    assert result["risky_claims"][0]["source"]["locator"] == "page 1, line 1"
    assert result["suggested_brief"]["twenty_second_call"]["ask"] == "Book a demo next week."
    assert all(
        link["link"].startswith("source://") for link in result["suggested_brief"]["source_links"]
    )


def test_multipart_upload_and_spreadsheet_locators_are_supported() -> None:
    material = _pptx([["A customer saves 2 hours per day."]])
    boundary = "----sonnia-test"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="proof.pptx"\r\n'
            "Content-Type: application/vnd.openxmlformats-officedocument.presentationml."
            "presentation\r\n\r\n"
        ).encode()
        + material
        + f"\r\n--{boundary}--\r\n".encode()
    )
    upload = parse_upload_body(body, f"multipart/form-data; boundary={boundary}")
    assert upload.filename == "proof.pptx"
    assert upload.content == material

    spreadsheet = extract_material(
        MaterialUpload(
            "proof.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            _xlsx(),
        )
    )
    assert spreadsheet["processing"]["state"] == "completed"
    assert spreadsheet["proof_points"][0]["source"]["kind"] == "sheet"
    assert "cells A2, B2" in spreadsheet["proof_points"][0]["source"]["locator"]


def test_pdf_page_locator_and_image_state_are_honest() -> None:
    pdf = extract_material(MaterialUpload("one-page.pdf", "application/pdf", _pdf()))
    assert pdf["proof_points"][0]["source"]["locator"] == "page 1"
    assert pdf["proof_points"][0]["source"]["link"].endswith("#page%201")

    image = extract_material(MaterialUpload("slide.png", "image/png", b"not-ocr"))
    assert image["processing"]["state"] == "needs_ocr"
    assert image["selling_points"] == []
    assert "OCR" in image["processing"]["notes"][0]


async def _test_context():
    yield None


def test_authenticated_upload_route_returns_extraction_result() -> None:
    main.app.dependency_overrides[main.authenticated_context] = _test_context
    try:
        with TestClient(main.app) as client:
            response = client.post(
                "/api/materials/extract",
                files={
                    "file": (
                        "route-proof.pptx",
                        _pptx([["Customers save 2 hours per day."]]),
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    )
                },
            )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["processing"]["mode"] == "offline-fallback"
        assert payload["selling_points"][0]["source"]["locator"] == "slide 1"
    finally:
        main.app.dependency_overrides.pop(main.authenticated_context, None)
