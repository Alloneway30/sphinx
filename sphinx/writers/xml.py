"""Docutils-native XML and pseudo-XML writers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from docutils.writers import docutils_xml

if TYPE_CHECKING:
    from typing import Any

    from sphinx.builders import Builder


class XMLWriter(docutils_xml.Writer):  # type: ignore[misc]
    output: str

    def __init__(self, builder: Builder) -> None:
        super().__init__()
        self.builder = builder
        self._config = builder.config

    def translate(self, *args: Any, **kwargs: Any) -> None:
        self.document.settings.newlines = self.document.settings.indents = (
            self._config.xml_pretty
        )
        self.document.settings.xml_declaration = True
        self.document.settings.doctype_declaration = True

        # copied from docutils.writers.docutils_xml.Writer.translate()
        # so that we can override the translator class
        self.visitor = visitor = self.builder.create_translator(self.document)
        self.document.walkabout(visitor)
        self.output = ''.join(visitor.output)  # type: ignore[attr-defined]


class PseudoXMLWriter(docutils_xml.Writer):  # type: ignore[misc]
    supported = ('pprint', 'pformat', 'pseudoxml')
    """Formats this writer supports."""

    config_section = 'pseudoxml writer'
    config_section_dependencies = ('writers',)

    output: str
    """Final translated form of `document`."""

    def __init__(self, builder: Builder) -> None:
        super().__init__()
        self.builder = builder

    def translate(self) -> None:
        self.output = self.document.pformat()

    def supports(self, format: str) -> bool:
        """All format-specific elements are supported."""
        return True
