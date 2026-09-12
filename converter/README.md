# PDF → Image Converter

This service is the first stage of the Konkour OCR pipeline.

It watches the same input tree used by the Python OCR pipeline. When it finds a PDF that has been present for at least the configured age, it:

1. Rasterises every PDF page to JPEG with `pdftoppm`.
2. Compresses the generated JPEGs with `jpegoptim`.
3. Moves the original PDF into that folder's `done/` directory after successful conversion.
4. Moves the PDF into `failed/` if rasterisation fails.

The generated JPEG files remain in the original `question` or `answer` directory, so the existing Python scanner can pick them up automatically.

## Folder flow

```text
OCR-quest/                 (project root)
├── converter/
│   ├── Dockerfile
│   ├── watch.sh
│   └── README.md
├── docker-compose.converter.yml
├── main.py
├── pipeline/
├── ui/
└── konkour-ocr/           (the watched data tree = INPUT_ROOT)
    └── {subject}/{grade}/{topic}/{question|answer}/
```

Input example (relative to `INPUT_ROOT`):

```text
{subject}/{grade}/{topic}/question/
├── meta.json
└── exam.pdf
```

The grade level is optional; `{subject}/{topic}/question/` is accepted as well. A
folder that matches neither shape is skipped with a warning instead of aborting the run.

After conversion:

```text
{subject}/{grade}/{topic}/question/
├── meta.json
├── exam-1.jpg
├── exam-2.jpg
└── done/
    └── exam.pdf
```

The Python OCR pipeline already accepts `.jpg`, `.jpeg`, `.png`, `.webp`, and `.pdf`, so no scanner change is required.

## Start the converter

From the project root:

```bat
docker compose -f docker-compose.converter.yml up -d --build
```

Watch its logs:

```bat
docker compose -f docker-compose.converter.yml logs -f converter
```

Stop it:

```bat
docker compose -f docker-compose.converter.yml down
```

## Configuration

The defaults reproduce the original converter behaviour:

- `CONVERTER_DPI=170`
- `CONVERTER_RASTER_QUALITY=78`
- `CONVERTER_JPEG_QUALITY=70`
- `CONVERTER_MIN_AGE_MINUTES=1`
- `CONVERTER_SCAN_INTERVAL_SECONDS=15`
- `INPUT_ROOT` is shared with the Python pipeline and defaults to `./konkour-ocr` in `docker-compose.converter.yml`.

These can be added to the project's `.env` file if you want to tune them.

## Important interaction with the OCR pipeline

The converter and OCR runner intentionally use the same folder tree. The converter only looks for PDFs, while the Python scanner processes the resulting image files. The OCR scanner skips `done/` and `failed/`, so converted source PDFs will not be processed again.

Keep `MIN_AGE_SECONDS` in the Python `.env` at its existing value (60 seconds by default). This gives the converter time to finish writing JPEGs before the OCR scanner sees them.
