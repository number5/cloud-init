# Copyright (C) 2012 Canonical Ltd.
# Copyright (C) 2012 Hewlett-Packard Development Company, L.P.
# Copyright (C) 2012 Yahoo! Inc.
# Copyright (C) 2016 Amazon.com, Inc. or its affiliates.
#
# Author: Scott Moser <scott.moser@canonical.com>
# Author: Juerg Haefliger <juerg.haefliger@hp.com>
# Author: Joshua Harlow <harlowja@yahoo-inc.com>
# Author: Andrew Jorgensen <ajorgens@amazon.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

# noqa: E402

import collections
import logging
import re
import sys
from typing import Any

from jinja2 import TemplateSyntaxError

from cloudinit import performance
from cloudinit import type_utils as tu
from cloudinit import util
from cloudinit.atomic_helper import write_file

# After bionic EOL, mypy==1.0.0 will be able to type-analyse dynamic
# base types, substitute this by:
# JUndefined: typing.Type
JUndefined: Any
try:
    from jinja2 import DebugUndefined as _DebugUndefined
    from jinja2 import Template as JTemplate

    JINJA_AVAILABLE = True
    JUndefined = _DebugUndefined
except (ImportError, AttributeError):
    JINJA_AVAILABLE = False
    JUndefined = object

LOG = logging.getLogger(__name__)
MISSING_JINJA_PREFIX = "CI_MISSING_JINJA_VAR/"


class JinjaSyntaxParsingException(TemplateSyntaxError):
    def __init__(
        self,
        error: TemplateSyntaxError,
    ) -> None:
        super().__init__(
            error.message or "unknown syntax error",
            error.lineno,
            error.name,
            error.filename,
        )
        self.source = error.source

    def __str__(self):
        """Avoid jinja2.TemplateSyntaxError multi-line __str__ format."""
        return self.format_error_message(
            syntax_error=self.message,
            line_number=self.lineno,
            line_content=self.source.splitlines()[self.lineno - 2].strip(),
        )

    @staticmethod
    def format_error_message(
        syntax_error: str,
        line_number: str,
        line_content: str = "",
    ) -> str:
        """Avoid jinja2.TemplateSyntaxError multi-line __str__ format."""
        line_content = f": {line_content}" if line_content else ""
        return JinjaSyntaxParsingException.message_template.format(
            syntax_error=syntax_error,
            line_number=line_number,
            line_content=line_content,
        )

    message_template = (
        "Unable to parse Jinja template due to syntax error: "
        "{syntax_error} on line {line_number}{line_content}"
    )


# Mypy, and the PEP 484 ecosystem in general, does not support creating
# classes with dynamic base types: https://stackoverflow.com/a/59636248
class UndefinedJinjaVariable(JUndefined):
    """Class used to represent any undefined jinja template variable."""

    def __str__(self):
        return "%s%s" % (MISSING_JINJA_PREFIX, self._undefined_name)

    def __sub__(self, other):
        other = str(other).replace(MISSING_JINJA_PREFIX, "")
        raise TypeError(
            'Undefined jinja variable: "{this}-{other}". Jinja tried'
            ' subtraction. Perhaps you meant "{this}_{other}"?'.format(
                this=self._undefined_name, other=other
            )
        )


@performance.timed("Rendering basic template")
def basic_render(content, params):
    """This does simple replacement of bash variable like templates.

    It identifies patterns like ${a} or $a and can also identify patterns like
    ${a.b} or $a.b which will look for a key 'b' in the dictionary rooted
    by key 'a'.
    """

    def replacer(match):
        # Only 1 of the 2 groups will actually have a valid entry.
        name = match.group(1)
        if name is None:
            name = match.group(2)
        if name is None:
            raise RuntimeError("Match encountered but no valid group present")
        path = collections.deque(name.split("."))
        selected_params = params
        while len(path) > 1:
            key = path.popleft()
            if not isinstance(selected_params, dict):
                raise TypeError(
                    "Can not traverse into"
                    " non-dictionary '%s' of type %s while"
                    " looking for subkey '%s'"
                    % (selected_params, tu.obj_name(selected_params), key)
                )
            selected_params = selected_params[key]
        key = path.popleft()
        if not isinstance(selected_params, dict):
            raise TypeError(
                "Can not extract key '%s' from non-dictionary '%s' of type %s"
                % (key, selected_params, tu.obj_name(selected_params))
            )
        return str(selected_params[key])

    return re.sub(
        r"\$\{([A-Za-z0-9_.]+)\}|\$([A-Za-z0-9_.]+)", replacer, content
    )


def detect_template(text):
    def jinja_render(content, params):
        # keep_trailing_newline is in jinja2 2.7+, not 2.6
        add = "\n" if content.endswith("\n") else ""
        try:
            with performance.Timed("Rendering jinja2 template"):
                return (
                    JTemplate(
                        content,
                        undefined=UndefinedJinjaVariable,
                        trim_blocks=True,
                        extensions=["jinja2.ext.do"],
                    ).render(**params)
                    + add
                )
        except TemplateSyntaxError as template_syntax_error:
            template_syntax_error.lineno += 1
            raise JinjaSyntaxParsingException(
                error=template_syntax_error,
            ) from template_syntax_error
        except Exception as unknown_error:
            raise unknown_error from unknown_error

    if text.find("\n") != -1:
        ident, rest = text.split("\n", 1)  # remove the first line
    else:
        ident = text
        rest = ""
    type_match = re.match(r"##\s*template:(.*)", ident, re.I)
    if not type_match:
        return ("basic", basic_render, text)
    else:
        template_type = type_match.group(1).lower().strip()
        if template_type not in ("jinja", "basic"):
            raise ValueError(
                "Unknown template rendering type '%s' requested"
                % template_type
            )
        if template_type == "jinja" and not JINJA_AVAILABLE:
            LOG.warning(
                "Jinja not available as the selected renderer for"
                " desired template, reverting to the basic renderer."
            )
            return ("basic", basic_render, rest)
        elif template_type == "jinja" and JINJA_AVAILABLE:
            return ("jinja", jinja_render, rest)
        # Only thing left over is the basic renderer (it is always available).
        return ("basic", basic_render, rest)


def render_from_file(fn, params):
    if not params:
        params = {}
    template_type, renderer, content = detect_template(util.load_text_file(fn))
    LOG.debug("Rendering content of '%s' using renderer %s", fn, template_type)
    return renderer(content, params)


def render_to_file(fn, outfn, params, mode=0o644):
    contents = render_from_file(fn, params)
    util.write_file(outfn, contents, mode=mode)


def render_string(content, params):
    """Render string"""
    if not params:
        params = {}
    _template_type, renderer, content = detect_template(content)
    return renderer(content, params)


def render_template(variant, template, output, is_yaml, prefix=None):
    contents = util.load_text_file(template)
    tpl_params = {"variant": variant, "prefix": prefix}
    contents = (render_string(contents, tpl_params)).rstrip() + "\n"
    if is_yaml:
        out = util.load_yaml(contents, default=True)
        if not out:
            raise RuntimeError(
                "Cannot render template file %s - invalid yaml." % template
            )
    if output == "-":
        sys.stdout.write(contents)
    else:
        write_file(output, contents, omode="w")
