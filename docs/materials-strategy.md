# Materials → strategy (offline fallback)

`POST /api/materials/extract` accepts an authenticated `multipart/form-data`
upload with a `file` part. For the CLI/demo path it also accepts the raw file
body with `X-Material-Filename` (or `X-Filename`). The Phase 1 limit is 25 MB.

Supported inputs are PDF, PPTX, DOCX, XLSX, PNG/JPEG/WebP/GIF/BMP/TIFF, TXT,
Markdown, and CSV. Office containers are parsed with the Python standard
library. No network call or model call is made.

The response has six source-backed work products:

- `selling_points`, ranked by phone utility rather than document order;
- `proof_points`;
- `objection_material`;
- `risky_claims`, each requiring approval;
- `missing`, explicitly labelled as inference;
- `suggested_brief`, assembled from verbatim claims and explicitly labelled as
  inference. Its `source_for` map ties each opener/proof/objection/ask field to
  the exact source (or `null` when that field is an explicitly labelled
  absence).

Every extracted claim has a `source` object containing the uploaded filename,
an extension-appropriate locator (`page`, `slide`, `sheet/cells`, or
`page/line`), an excerpt, and a stable `source://...#...` link. Source links
are local evidence references, not public URLs. `slides_useless_on_phone`
explains which navigation/background slides should be left out of the first
20 seconds.

`processing.state` is honest:

- `completed` means selectable text was extracted;
- `partial` means the upload was accepted but no usable text was found;
- `needs_ocr` means an image was accepted without inventing visual claims.

See [`evidence/materials-strategy-sample.json`](../evidence/materials-strategy-sample.json)
for a source-linked sample response.
