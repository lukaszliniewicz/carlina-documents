---
name: Document Parsing
description: Read PDFs and office documents through the document processor before summarizing or transforming them.
---

Use `parse_document` when the user asks about a PDF, office file, uploaded document, or document path that needs reliable extraction before reasoning.

Treat the parser output as source material, not as a final answer. Preserve page, section, heading, table, figure, and footnote context when it matters. If the user wants conversion or export instead of analysis, hand off to the Document Library workflow rather than inventing a file result in chat.
