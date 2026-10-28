_tools_def = [
    {
        "type": "function",
        "function": {
            "name": "parse_document",
            "description": "Converts a document (like a PDF, TXT, EPUB, DOCX, DOC, MSG, or HTML file) into Markdown text. Use this when you need to read the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "The absolute path or web path (e.g., /uploads/...) to the document."
                    },
                    "engine": {
                        "type": "string",
                        "description": "Optional. The specific extraction engine to use (e.g., 'llm_vision', 'marker', 'pymupdf'). Defaults to 'auto' to let the system pick the best available."
                    },
                    "force_ocr": {
                        "type": "boolean",
                        "description": "Optional. Prefer OCR-heavy extraction engines for scanned PDFs."
                    },
                    "no_ocr": {
                        "type": "boolean",
                        "description": "Optional. Prefer text-native extraction engines when the PDF already contains selectable text."
                    },
                    "page_range": {
                        "type": "string",
                        "description": "Optional. A comma-separated list of page numbers or ranges to extract (e.g., '1-5, 8, 11-13'). Extracts the full document if omitted."
                    }
                },
                "required": ["filepath"]
            }
        }
    }
]
