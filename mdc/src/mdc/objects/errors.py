"""Image/document storage errors."""

from __future__ import annotations


class ImageNotFoundError(Exception):
    def __init__(self, image_id: str):
        super().__init__(f"No image found with image_id={image_id!r}")
        self.image_id = image_id


class DocumentNotFoundError(Exception):
    def __init__(self, document_id: str):
        super().__init__(f"No document found with document_id={document_id!r}")
        self.document_id = document_id
