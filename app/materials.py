"""Offline materials-to-strategy extraction.

The Phase 1 fallback deliberately has no model or network dependency.  It
extracts text from common office containers, keeps the original document
locator beside every claim, and labels any conclusion that is not verbatim
source text as an inference.  This is intentionally a small, auditable seam
that can later be replaced by a richer extractor without changing the API
shape.
"""

from __future__ import annotations

import hashlib
import re
import struct
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from email import policy
from email.parser import BytesParser
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

MAX_MATERIAL_BYTES = 25 * 1024 * 1024
SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".pptx",
    ".docx",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tiff",
    ".txt",
    ".md",
    ".csv",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}


class MaterialUploadError(ValueError):
    """Raised when an upload cannot be parsed safely."""


class UnsupportedMaterialError(MaterialUploadError):
    """Raised for a file type outside the Phase 1 extraction contract."""


@dataclass(frozen=True)
class MaterialUpload:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class SourceReference:
    """A stable, human-readable pointer into the uploaded source document."""

    document: str
    kind: str
    locator: str
    excerpt: str = ""

    @property
    def link(self) -> str:
        return f"source://{quote(self.document, safe='')}#{quote(self.locator, safe='')}"

    def as_dict(self) -> dict[str, str]:
        return {
            "document": self.document,
            "kind": self.kind,
            "locator": self.locator,
            "link": self.link,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class SourceUnit:
    text: str
    source: SourceReference


@dataclass(frozen=True)
class Candidate:
    text: str
    source: SourceReference
    order: int
    score: int


def _normalise_filename(filename: str) -> str:
    """Drop client-side path components while retaining the displayed name."""

    name = filename.replace("\\", "/")
    name = PurePosixPath(name).name.strip()
    if not name or name in {".", ".."}:
        raise MaterialUploadError("a filename is required")
    return name[:240]


def parse_upload_body(
    body: bytes,
    content_type: str | None,
    filename_header: str | None = None,
) -> MaterialUpload:
    """Parse a multipart upload without requiring a multipart runtime plugin.

    The app accepts normal browser ``multipart/form-data`` uploads and a
    simpler ``application/octet-stream`` form for the offline CLI/demo.  The
    latter uses ``X-Material-Filename`` (or the historical ``X-Filename``)
    because a raw body has no filename metadata.
    """

    if len(body) > MAX_MATERIAL_BYTES:
        raise MaterialUploadError("material is larger than the 25 MB Phase 1 limit")
    if not body:
        raise MaterialUploadError("material is empty")

    header = (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    if header == "multipart/form-data":
        if not content_type or "boundary=" not in content_type.lower():
            raise MaterialUploadError("multipart upload boundary is missing")
        envelope = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        message = BytesParser(policy=policy.default).parsebytes(envelope)
        for part in message.walk():
            if part.get_content_disposition() != "form-data" or not part.get_filename():
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                raise MaterialUploadError("uploaded material could not be decoded")
            return MaterialUpload(
                filename=_normalise_filename(part.get_filename()),
                content_type=(part.get_content_type() or "application/octet-stream").lower(),
                content=payload,
            )
        raise MaterialUploadError("multipart upload must contain a file field")

    filename = filename_header or ""
    if not filename:
        raise MaterialUploadError(
            "a raw upload needs X-Material-Filename (or use multipart/form-data)"
        )
    return MaterialUpload(
        filename=_normalise_filename(filename),
        content_type=header,
        content=body,
    )


def _xml_root(archive: ZipFile, name: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(archive.read(name))
    except (KeyError, ElementTree.ParseError) as exc:
        raise MaterialUploadError(f"material contains invalid {name}") from exc


def _tag(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _source(
    filename: str,
    kind: str,
    locator: str,
    excerpt: str = "",
) -> SourceReference:
    return SourceReference(
        document=filename,
        kind=kind,
        locator=locator,
        excerpt=_clean_text(excerpt),
    )


def _with_excerpt(source: SourceReference, excerpt: str) -> SourceReference:
    return replace(source, excerpt=_clean_text(excerpt))


def _pptx_units(filename: str, content: bytes) -> list[SourceUnit]:
    try:
        archive = ZipFile(__import__("io").BytesIO(content))
    except BadZipFile as exc:
        raise MaterialUploadError("PPTX archive is invalid") from exc

    slide_names = sorted(
        (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
        key=lambda name: int(re.search(r"(\d+)", name).group(1)),  # type: ignore[union-attr]
    )
    units: list[SourceUnit] = []
    for slide_number, name in enumerate(slide_names, start=1):
        root = _xml_root(archive, name)
        paragraphs: list[str] = []
        for paragraph in root.iter():
            if _tag(paragraph) != "p":
                continue
            parts = [node.text or "" for node in paragraph.iter() if _tag(node) == "t"]
            value = _clean_text("".join(parts))
            if value:
                paragraphs.append(value)
        text = "\n".join(paragraphs)
        if text:
            units.append(
                SourceUnit(
                    text=text,
                    source=_source(filename, "slide", f"slide {slide_number}", text),
                )
            )
    if not slide_names:
        raise MaterialUploadError("PPTX contains no presentation slides")
    return units


def _docx_units(filename: str, content: bytes) -> tuple[list[SourceUnit], list[str]]:
    try:
        archive = ZipFile(__import__("io").BytesIO(content))
    except BadZipFile as exc:
        raise MaterialUploadError("DOCX archive is invalid") from exc
    root = _xml_root(archive, "word/document.xml")
    units: list[SourceUnit] = []
    page = 1
    paragraph_number = 0
    for paragraph in root.iter():
        if _tag(paragraph) != "p":
            continue
        paragraph_number += 1
        parts: list[str] = []
        has_page_break = False
        for node in paragraph.iter():
            if _tag(node) == "t":
                parts.append(node.text or "")
            elif _tag(node) == "lastRenderedPageBreak" or (
                _tag(node) == "br"
                and node.attrib.get(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type"
                )
                == "page"
            ):
                has_page_break = True
        text = _clean_text("".join(parts))
        if text:
            units.append(
                SourceUnit(
                    text=text,
                    source=_source(
                        filename,
                        "page",
                        f"page {page}, paragraph {paragraph_number}",
                        text,
                    ),
                )
            )
        if has_page_break:
            page += 1
    return units, [
        "DOCX page numbers use explicit Word page-break markers; without one, content is "
        "reported on page 1."
    ]


def _xlsx_shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml_root(archive, "xl/sharedStrings.xml")
    values: list[str] = []
    for item in root.iter():
        if _tag(item) != "si":
            continue
        values.append(
            _clean_text("".join(node.text or "" for node in item.iter() if _tag(node) == "t"))
        )
    return values


def _xlsx_sheet_names(archive: ZipFile) -> dict[str, str]:
    if "xl/workbook.xml" not in archive.namelist():
        return {}
    workbook = _xml_root(archive, "xl/workbook.xml")
    relationships: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in archive.namelist():
        rels = _xml_root(archive, "xl/_rels/workbook.xml.rels")
        for relationship in rels.iter():
            if _tag(relationship) != "Relationship":
                continue
            target = relationship.attrib.get("Target", "")
            if target.startswith("/"):
                target = target[1:]
            elif not target.startswith("xl/"):
                target = f"xl/{target}"
            relationships[relationship.attrib.get("Id", "")] = target
    sheets: dict[str, str] = {}
    for sheet in workbook.iter():
        if _tag(sheet) != "sheet":
            continue
        name = sheet.attrib.get("name", "Sheet")
        relationship_id = next(
            (value for key, value in sheet.attrib.items() if key.rsplit("}", 1)[-1] == "id"),
            "",
        )
        sheets[name] = relationships.get(relationship_id, "")
    return sheets


def _xlsx_units(filename: str, content: bytes) -> list[SourceUnit]:
    try:
        archive = ZipFile(__import__("io").BytesIO(content))
    except BadZipFile as exc:
        raise MaterialUploadError("XLSX archive is invalid") from exc
    shared = _xlsx_shared_strings(archive)
    sheets = _xlsx_sheet_names(archive)
    if not sheets:
        sheets = {
            name.rsplit("/", 1)[-1].removesuffix(".xml"): name
            for name in archive.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        }
    units: list[SourceUnit] = []
    for sheet_name, sheet_path in sheets.items():
        if not sheet_path or sheet_path not in archive.namelist():
            continue
        root = _xml_root(archive, sheet_path)
        for row in root.iter():
            if _tag(row) != "row":
                continue
            cells: list[str] = []
            addresses: list[str] = []
            for cell in row:
                if _tag(cell) != "c":
                    continue
                address = cell.attrib.get("r", "cell")
                cell_type = cell.attrib.get("t")
                value = ""
                if cell_type == "inlineStr":
                    value = "".join(node.text or "" for node in cell.iter() if _tag(node) == "t")
                else:
                    raw = next((node.text or "" for node in cell if _tag(node) == "v"), "")
                    if cell_type == "s" and raw.isdigit() and int(raw) < len(shared):
                        value = shared[int(raw)]
                    elif cell_type == "b":
                        value = "true" if raw == "1" else "false"
                    else:
                        value = raw
                value = _clean_text(value)
                if value:
                    addresses.append(address)
                    cells.append(f"{address}: {value}")
            if cells:
                row_number = row.attrib.get("r", "?")
                locator = f'sheet "{sheet_name}", row {row_number}, cells {", ".join(addresses)}'
                text = " | ".join(cells)
                units.append(
                    SourceUnit(text=text, source=_source(filename, "sheet", locator, text))
                )
    return units


def _pdf_literal_strings(data: bytes) -> list[str]:
    """Read simple PDF text operands from a content stream."""

    values: list[str] = []
    index = 0
    while index < len(data):
        if data[index] == ord("("):
            index += 1
            depth = 1
            value = bytearray()
            while index < len(data) and depth:
                byte = data[index]
                index += 1
                if byte == ord("\\") and index < len(data):
                    escaped = data[index]
                    index += 1
                    escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
                    if escaped in escapes:
                        value.append(escapes[escaped])
                    elif escaped in {ord("("), ord(")"), ord("\\")}:
                        value.append(escaped)
                    elif 48 <= escaped <= 55:
                        octal = bytes([escaped])
                        for _ in range(2):
                            if index < len(data) and 48 <= data[index] <= 55:
                                octal += bytes([data[index]])
                                index += 1
                            else:
                                break
                        value.append(int(octal, 8))
                    else:
                        value.append(escaped)
                elif byte == ord("("):
                    depth += 1
                    value.append(byte)
                elif byte == ord(")"):
                    depth -= 1
                    if depth:
                        value.append(byte)
                else:
                    value.append(byte)
            if value:
                values.append(value.decode("utf-8", errors="replace"))
            continue
        if data[index] == ord("<") and data[index : index + 2] != b"<<":
            end = data.find(b">", index + 1)
            if end != -1:
                raw = re.sub(rb"\s+", b"", data[index + 1 : end])
                if raw and re.fullmatch(rb"[0-9a-fA-F]+", raw):
                    if len(raw) % 2:
                        raw += b"0"
                    values.append(bytes.fromhex(raw.decode()).decode("utf-16-be", errors="replace"))
                    index = end + 1
                    continue
        index += 1
    return values


def _pdf_stream(body: bytes) -> bytes:
    match = re.search(rb"\bstream\r?\n(.*?)\r?\nendstream", body, re.DOTALL)
    if not match:
        return b""
    payload = match.group(1)
    if re.search(rb"/FlateDecode\b", body[: match.start()]):
        try:
            return zlib.decompress(payload)
        except zlib.error:
            return payload
    return payload


def _pdf_units(filename: str, content: bytes) -> tuple[list[SourceUnit], list[str]]:
    objects: dict[int, bytes] = {}
    for match in re.finditer(rb"(?m)^(\d+)\s+\d+\s+obj\s*(.*?)\s*endobj", content, re.DOTALL):
        objects[int(match.group(1))] = match.group(2)
    page_objects = [
        (number, body)
        for number, body in objects.items()
        if re.search(rb"/Type\s*/Page\b", body) and not re.search(rb"/Type\s*/Pages\b", body)
    ]
    page_objects.sort(key=lambda item: item[0])
    units: list[SourceUnit] = []
    for page_number, (_, page_body) in enumerate(page_objects, start=1):
        refs: list[int] = []
        contents = re.search(rb"/Contents\s+(\[.*?\]|\d+\s+\d+\s+R)", page_body, re.DOTALL)
        if contents:
            refs = [int(value) for value in re.findall(rb"(\d+)\s+\d+\s+R", contents.group(1))]
        streams = [_pdf_stream(objects[ref]) for ref in refs if ref in objects]
        text = _clean_text(" ".join(" ".join(_pdf_literal_strings(stream)) for stream in streams))
        if text:
            units.append(
                SourceUnit(
                    text=text,
                    source=_source(filename, "page", f"page {page_number}", text),
                )
            )
    if units:
        return units, []

    # Some minimal/generated PDFs omit page dictionaries.  Preserve extracted
    # text rather than pretending the file is empty, but make the limitation
    # visible so a reviewer can swap in a PDF-native parser when exact pages
    # matter.
    streams = [_pdf_stream(body) for body in objects.values()]
    text = _clean_text(" ".join(" ".join(_pdf_literal_strings(stream)) for stream in streams))
    if text:
        return [SourceUnit(text=text, source=_source(filename, "page", "page 1", text))], [
            "PDF page dictionaries were not available to the offline parser; extracted text "
            "is conservatively linked to page 1."
        ]
    return [], ["No selectable PDF text was found; an OCR/PDF-native parser is required."]


def _plain_text_units(filename: str, content: bytes) -> list[SourceUnit]:
    text = content.decode("utf-8-sig", errors="replace")
    units: list[SourceUnit] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        value = _clean_text(line)
        if value:
            units.append(
                SourceUnit(
                    text=value,
                    source=_source(filename, "page", f"page 1, line {line_number}", value),
                )
            )
    return units


def _image_dimensions(content: bytes, extension: str) -> str | None:
    if extension == ".png" and content[:8] == b"\x89PNG\r\n\x1a\n" and len(content) >= 24:
        width, height = struct.unpack(">II", content[16:24])
        return f"{width}x{height}px"
    if extension in {".jpg", ".jpeg"} and content[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(content):
            if content[index] != 0xFF:
                index += 1
                continue
            marker = content[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(content):
                break
            length = struct.unpack(">H", content[index : index + 2])[0]
            if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(
                range(0xC9, 0xCC)
            ) | set(range(0xCD, 0xD0)) and index + 7 <= len(content):
                height, width = struct.unpack(">HH", content[index + 3 : index + 7])
                return f"{width}x{height}px"
            index += max(length, 2)
    return None


def _extract_units(
    filename: str, content: bytes, extension: str
) -> tuple[list[SourceUnit], list[str], str]:
    if extension == ".pptx":
        return _pptx_units(filename, content), [], "completed"
    if extension == ".docx":
        units, notes = _docx_units(filename, content)
        return units, notes, "completed" if units else "partial"
    if extension == ".xlsx":
        units = _xlsx_units(filename, content)
        return units, [], "completed" if units else "partial"
    if extension == ".pdf":
        units, notes = _pdf_units(filename, content)
        return units, notes, "completed" if units else "partial"
    if extension in IMAGE_EXTENSIONS:
        dimensions = _image_dimensions(content, extension)
        note = "OCR is not configured in the offline fallback; no visual claims were inferred."
        if dimensions:
            note = f"{dimensions}; {note}"
        return [], [note], "needs_ocr"
    return _plain_text_units(filename, content), [], "completed"


_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“'\[])|\n+")
_LEADING_BULLET_RE = re.compile(r"^\s*(?:[-*•‣]|\d+[.)])\s*")


def _candidate_texts(unit_text: str) -> Iterable[str]:
    parts = _SPLIT_RE.split(unit_text)
    for part in parts:
        value = _LEADING_BULLET_RE.sub("", _clean_text(part))
        if 8 <= len(value) <= 500:
            yield value


def _score(text: str) -> int:
    value = text.lower()
    score = 0
    for term in (
        "reduce",
        "save",
        "faster",
        "increase",
        "improve",
        "automate",
        "grow",
        "prevent",
        "eliminate",
        "deliver",
        "help",
        "enable",
        "connect",
        "simplify",
    ):
        if term in value:
            score += 3
    for term in ("customer", "sales", "revenue", "cost", "time", "lead", "team", "energy"):
        if term in value:
            score += 2
    if re.search(
        r"(?:\d+(?:\.\d+)?\s*%|[$€£]\s*\d|\b\d+(?:\.\d+)?x\b|\b\d+\s+(?:days?|hours?|minutes?|customers?)\b)",
        value,
    ):
        score += 5
    if any(term in value for term in ("unique", "unlike", "integrat", "built for", "different")):
        score += 3
    if len(value.split()) <= 28:
        score += 1
    if any(
        term in value
        for term in ("agenda", "contents", "appendix", "thank you", "contact us", "our team")
    ):
        score -= 8
    if any(term in value for term in ("roadmap", "vision", "about us")):
        score -= 5
    return score


def _has_proof(text: str) -> bool:
    value = text.lower()
    return bool(
        re.search(
            r"(?:\d+(?:\.\d+)?\s*%|[$€£]\s*\d|\b\d+(?:\.\d+)?x\b|\b\d+\s+(?:days?|hours?|minutes?|customers?)\b)",
            value,
        )
        or any(
            term in value
            for term in ("case study", "certified", "award", "customer result", "pilot")
        )
    )


def _has_objection(text: str) -> bool:
    value = text.lower()
    return any(
        term in value
        for term in (
            "question",
            "concern",
            "risk",
            "however",
            "but ",
            "cost",
            "price",
            "security",
            "integration",
            "implementation",
            "objection",
            "challenge",
        )
    )


def _risk_reason(text: str) -> str | None:
    value = text.lower()
    absolute = [
        term
        for term in (
            "always",
            "never",
            "only",
            "guarantee",
            "guaranteed",
            "best",
            "#1",
            "zero risk",
            "100%",
        )
        if term in value
    ]
    if absolute:
        return (
            f"Absolute or superiority wording ({', '.join(absolute)}) needs approval "
            "and substantiation."
        )
    if re.search(r"(?:\d+(?:\.\d+)?\s*%|[$€£]\s*\d|\b\d+(?:\.\d+)?x\b)", value):
        return (
            "Quantified outcome; confirm the denominator, timeframe, and supporting "
            "evidence before use."
        )
    if any(term in value for term in ("save", "reduce", "increase", "improve", "eliminate")):
        return "Outcome claim; confirm scope and evidence before presenting it as a promise."
    return None


def _candidates(units: list[SourceUnit]) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[str] = set()
    order = 0
    for unit in units:
        for text in _candidate_texts(unit.text):
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                Candidate(
                    text=text,
                    source=_with_excerpt(unit.source, text),
                    order=order,
                    score=_score(text),
                )
            )
            order += 1
    return candidates


def _claim(candidate: Candidate) -> dict[str, Any]:
    return {
        "claim": candidate.text,
        "source": candidate.source.as_dict(),
        "inference": False,
    }


def _inference(
    item: str, reason: str, evidence: list[SourceReference] | None = None
) -> dict[str, Any]:
    return {
        "item": item,
        "reason": reason,
        "inference": True,
        "evidence_sources": [source.as_dict() for source in evidence or []],
    }


def _slide_exclusions(
    filename: str, units: list[SourceUnit], candidates: list[Candidate]
) -> list[dict[str, Any]]:
    slides = [unit for unit in units if unit.source.kind == "slide"]
    exclusions: list[dict[str, Any]] = []
    for unit in slides:
        value = unit.text.lower()
        obvious = any(
            term in value
            for term in (
                "agenda",
                "contents",
                "appendix",
                "our team",
                "about us",
                "thank you",
                "contact us",
            )
        )
        slide_candidates = [
            candidate for candidate in candidates if candidate.source.locator == unit.source.locator
        ]
        if obvious or (
            slide_candidates and max(candidate.score for candidate in slide_candidates) < 0
        ):
            if "agenda" in value or "contents" in value:
                reason = (
                    "Navigation only; it gives no source-backed customer outcome for a cold call."
                )
            elif "appendix" in value:
                reason = (
                    "Reference material; keep it for follow-up rather than the first 20 seconds."
                )
            elif "team" in value or "about us" in value:
                reason = (
                    "Company background without a customer outcome; it is low-value as an opener."
                )
            else:
                reason = "No customer outcome or proof point was found for the phone opener."
            exclusions.append(
                {
                    "slide": unit.source.locator,
                    "reason": reason,
                    "source": unit.source.as_dict(),
                    "inference": True,
                }
            )
    return exclusions


def _suggested_brief(
    selling_points: list[Candidate],
    proof_points: list[Candidate],
    objection_points: list[Candidate],
    all_candidates: list[Candidate],
) -> dict[str, Any]:
    top = selling_points[0] if selling_points else None
    proof = proof_points[0] if proof_points else None
    objection = objection_points[0] if objection_points else None
    cta = next(
        (
            candidate
            for candidate in all_candidates
            if any(
                term in candidate.text.lower()
                for term in (
                    "book a",
                    "schedule",
                    "start a",
                    "try ",
                    "demo",
                    "call us",
                    "next step",
                )
            )
        ),
        None,
    )
    sources = [candidate.source for candidate in (top, proof, objection, cta) if candidate]
    return {
        "inference": True,
        "note": (
            "This brief is assembled from verbatim source claims; confirm the ask "
            "and approval flags before calling."
        ),
        "twenty_second_call": {
            "opener": top.text if top else "No source-backed opener was found; do not invent one.",
            "proof": proof.text
            if proof
            else "No quantified proof point was found in the material.",
            "objection": objection.text
            if objection
            else "No objection handling was stated in the material.",
            "ask": cta.text
            if cta
            else "The material does not state a call-to-action; confirm the ask before use.",
        },
        "source_for": {
            "opener": top.source.as_dict() if top else None,
            "proof": proof.source.as_dict() if proof else None,
            "objection": objection.source.as_dict() if objection else None,
            "ask": cta.source.as_dict() if cta else None,
        },
        "source_links": [source.as_dict() for source in sources],
    }


def extract_material(upload: MaterialUpload) -> dict[str, Any]:
    """Return an auditable extraction result for one uploaded material."""

    filename = _normalise_filename(upload.filename)
    extension = PurePosixPath(filename.lower()).suffix
    if extension not in SUPPORTED_EXTENSIONS:
        raise UnsupportedMaterialError(
            "supported material types are PDF, PPTX, DOCX, XLSX, images, TXT, MD, and CSV"
        )
    if len(upload.content) > MAX_MATERIAL_BYTES:
        raise MaterialUploadError("material is larger than the 25 MB Phase 1 limit")
    units, notes, state = _extract_units(filename, upload.content, extension)
    candidates = _candidates(units)
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            _risk_reason(candidate.text) is not None,
            -candidate.score,
            candidate.order,
        ),
    )
    selling_candidates = ranked[:5]
    proof_candidates = sorted(
        (candidate for candidate in candidates if _has_proof(candidate.text)),
        key=lambda candidate: (-candidate.score, candidate.order),
    )[:5]
    objection_candidates = sorted(
        (candidate for candidate in candidates if _has_objection(candidate.text)),
        key=lambda candidate: (-candidate.score, candidate.order),
    )[:5]
    risky_candidates = [candidate for candidate in candidates if _risk_reason(candidate.text)][:8]

    missing: list[dict[str, Any]] = []
    if not any(
        any(
            term in candidate.text.lower()
            for term in ("customer", "sales", "buyer", "team", "business")
        )
        for candidate in candidates
    ):
        missing.append(
            _inference(
                "target audience", "The material does not identify who the cold call is for."
            )
        )
    if not proof_candidates:
        missing.append(
            _inference(
                "proof point",
                "No quantified result, customer result, certification, or case study was found.",
            )
        )
    if not objection_candidates:
        missing.append(
            _inference(
                "objection handling",
                "No cost, security, implementation, integration, or explicit concern "
                "response was found.",
            )
        )
    if not any(
        any(
            term in candidate.text.lower()
            for term in ("book a", "schedule", "start a", "try ", "demo", "call us", "next step")
        )
        for candidate in candidates
    ):
        missing.append(
            _inference(
                "call-to-action", "The material does not state what the prospect should do next."
            )
        )

    result: dict[str, Any] = {
        "processing": {
            "state": state,
            "mode": "offline-fallback",
            "message": (
                "Processed locally with deterministic extraction; no external model "
                "or connector was used."
            ),
            "claims_are_verbatim": True,
            "notes": notes,
        },
        "material": {
            "filename": filename,
            "content_type": upload.content_type,
            "extension": extension,
            "bytes": len(upload.content),
            "sha256": hashlib.sha256(upload.content).hexdigest(),
        },
        "selling_points": [
            {
                "rank": rank,
                "claim": candidate.text,
                "source": candidate.source.as_dict(),
                "phone_reason": (
                    "Ranked for customer outcome, specificity, and evidence value "
                    "rather than source order."
                ),
                "inference": False,
            }
            for rank, candidate in enumerate(selling_candidates, start=1)
        ],
        "proof_points": [_claim(candidate) for candidate in proof_candidates],
        "objection_material": [_claim(candidate) for candidate in objection_candidates],
        "risky_claims": [
            {
                "claim": candidate.text,
                "risk": _risk_reason(candidate.text),
                "requires_approval": True,
                "source": candidate.source.as_dict(),
                "inference": False,
            }
            for candidate in risky_candidates
        ],
        "slides_useless_on_phone": _slide_exclusions(filename, units, candidates),
        "missing": missing,
        "suggested_brief": _suggested_brief(
            selling_candidates,
            proof_candidates,
            objection_candidates,
            candidates,
        ),
    }
    if extension not in IMAGE_EXTENSIONS and not candidates and state == "partial":
        result["processing"]["message"] = (
            "Upload accepted, but no selectable text was found; no claims were invented."
        )
    return result
