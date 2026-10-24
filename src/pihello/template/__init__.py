"""pihello's template engine.

Templates are plain text with style tags (``[bold steelblue]``), expressions
(``{queries.total|number}``) and comments (``{# ... #}``); see
``docs/templates.md`` for the language.

>>> from pihello.template import ColorDepth, RenderOptions, Template
>>> template = Template.parse("Blocked [bold]{queries.blocked|number}[] queries")
>>> sorted(template.paths())
['queries.blocked']
>>> template.render({"queries": {"blocked": 48213}}, options=RenderOptions(color=ColorDepth.NONE))
'Blocked 48,213 queries'
"""

from __future__ import annotations

from ..errors import TemplateError
from .color import ColorDepth
from .engine import RenderOptions, Template, escape, render, sanitize
from .layout import strip_ansi, visible_width

__all__ = [
    "ColorDepth",
    "RenderOptions",
    "Template",
    "TemplateError",
    "escape",
    "render",
    "sanitize",
    "strip_ansi",
    "visible_width",
]
