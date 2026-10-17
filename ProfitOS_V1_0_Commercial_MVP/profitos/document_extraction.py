"""Shared primitives for document extraction fallbacks."""

class PdfTextUnavailable(ValueError):
    """The PDF is readable but has no usable native text layer."""


def is_pdf_text_unavailable(exc):
    """True only for the explicit no-native-text condition."""
    return isinstance(exc, PdfTextUnavailable)
