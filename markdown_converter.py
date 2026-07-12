import re
import sys
import os

# Add bundled lib to path
_lib_path = os.path.join(os.path.dirname(__file__), "lib")
if _lib_path not in sys.path:
    sys.path.insert(0, _lib_path)

import markdown

# Tags allowed in AI-generated content
ALLOWED_TAGS = {
    "p", "br", "strong", "em", "b", "i", "u",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "blockquote", "pre", "code",
    "hr", "sub", "sup",
    "table", "thead", "tbody", "tr", "th", "td",
}

# Attributes allowed (tag -> set of attrs)
ALLOWED_ATTRS = {
    "td": {"align"},
    "th": {"align"},
}


def _sanitize_html(html):
    """Remove dangerous HTML tags and attributes from AI-generated content."""
    # Remove script tags and their content
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove style tags and their content
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove event handler attributes (onclick, onerror, onload, etc.)
    html = re.sub(r"\s+on\w+\s*=\s*\"[^\"]*\"", "", html, flags=re.IGNORECASE)
    html = re.sub(r"\s+on\w+\s*=\s*'[^']*'", "", html, flags=re.IGNORECASE)
    html = re.sub(r"\s+on\w+\s*=\s*[^\s>]+", "", html, flags=re.IGNORECASE)
    # Remove javascript: URLs
    html = re.sub(r"href\s*=\s*[\"']?\s*javascript:", 'href="', html, flags=re.IGNORECASE)
    html = re.sub(r"src\s*=\s*[\"']?\s*javascript:", 'src="', html, flags=re.IGNORECASE)
    # Remove iframe, object, embed, form, input
    for tag in ("iframe", "object", "embed", "form", "input", "textarea", "select", "button", "link", "meta"):
        html = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(rf"<{tag}[^>]*/?>", "", html, flags=re.IGNORECASE)

    return html


def markdown_to_html(text):
    """Convert markdown to HTML, then sanitize dangerous elements."""
    if not text or not text.strip():
        return ""

    html = markdown.markdown(
        text,
        extensions=["extra", "nl2br", "sane_lists"],
    )

    return _sanitize_html(html)
