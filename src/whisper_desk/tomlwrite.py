"""Writing TOML without a dependency: the stdlib reads it, it does not write it.

The configuration is a hand-commented file: a value is changed in place, on
its own line, and everything around it — comments, blank lines, alignment —
is left as the user or the example wrote it. Only what this editor cannot
parse (a multi-line value) makes it fall back to a clean rewrite.
"""

from __future__ import annotations

import re
import tomllib
from typing import Any

SECTION = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$")
KEY = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$")


class CannotEdit(ValueError):
    """The line holds a value this editor does not know how to replace."""


def format_value(value: Any) -> str:
    """A Python scalar as a TOML value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        return text if any(char in text for char in ".en") else text + ".0"
    if isinstance(value, str):
        escaped = []
        for char in value:
            if char == "\\":
                escaped.append("\\\\")
            elif char == '"':
                escaped.append('\\"')
            elif char == "\n":
                escaped.append("\\n")
            elif char == "\t":
                escaped.append("\\t")
            elif ord(char) < 0x20 or ord(char) == 0x7F:
                escaped.append(f"\\u{ord(char):04x}")
            else:
                escaped.append(char)
        return '"' + "".join(escaped) + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(item) for item in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def _value_end(rest: str) -> int:
    """Where the value at the start of `rest` ends, before its comment."""
    if rest.startswith(('"""', "'''", "[", "{")):
        raise CannotEdit(rest)
    if rest.startswith('"'):
        index = 1
        while index < len(rest):
            if rest[index] == "\\":
                index += 2
                continue
            if rest[index] == '"':
                return index + 1
            index += 1
        raise CannotEdit(rest)
    if rest.startswith("'"):
        end = rest.find("'", 1)
        if end < 0:
            raise CannotEdit(rest)
        return end + 1
    match = re.match(r"[^\s#]*", rest)
    return match.end() if match else 0


def _replace(line: str, value: str) -> str:
    """The line with its value swapped, the comment kept at its column."""
    match = KEY.match(line)
    assert match is not None
    indent, key, separator, rest = match.groups()
    end = _value_end(rest)
    tail = rest[end:]
    comment = tail.lstrip()
    head = f"{indent}{key}{separator}{value}"
    if not comment:
        return head
    column = len(line) - len(comment)
    padding = max(column - len(head), 1 if comment.startswith("#") else 0)
    return head + " " * padding + comment


def update(text: str, changes: dict[str, dict[str, Any]]) -> str:
    """Sets section.key = value in a TOML document, keeping what surrounds it."""
    lines = text.splitlines()
    pending = {section: dict(values) for section, values in changes.items() if values}
    section = ""
    # Where each section's last key sits: a missing key goes right after it.
    last_key: dict[str, int] = {}
    headers: dict[str, int] = {}

    for index, line in enumerate(lines):
        header = SECTION.match(line)
        if header:
            section = header.group(1)
            headers.setdefault(section, index)
            continue
        match = KEY.match(line)
        if not match or line.lstrip().startswith("#"):
            continue
        last_key[section] = index
        key = match.group(2)
        if key in pending.get(section, {}):
            lines[index] = _replace(line, format_value(pending[section].pop(key)))

    # Keys the file does not mention yet, inserted from the bottom up so the
    # recorded positions stay valid.
    insertions = []
    for name, values in pending.items():
        if not values or name not in headers:
            continue
        where = last_key.get(name, headers[name]) + 1
        insertions.append((where, [f"{key} = {format_value(value)}" for key, value in values.items()]))
    for where, new_lines in sorted(insertions, reverse=True):
        lines[where:where] = new_lines

    for name, values in pending.items():
        if values and name not in headers:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[{name}]")
            lines.extend(f"{key} = {format_value(value)}" for key, value in values.items())
    return "\n".join(lines) + "\n"


def dumps(document: dict[str, Any]) -> str:
    """A clean document, without comments: the fallback when editing is impossible."""
    blocks = [
        "\n".join(
            f"{key} = {format_value(value)}"
            for key, value in document.items() if not isinstance(value, dict)
        )
    ]
    for section, values in document.items():
        if isinstance(values, dict):
            body = "\n".join(f"{key} = {format_value(value)}" for key, value in values.items())
            blocks.append(f"[{section}]\n{body}")
    return "\n\n".join(block for block in blocks if block) + "\n"


def apply(text: str, changes: dict[str, dict[str, Any]]) -> str:
    """update() when it can be trusted, a clean rewrite otherwise.

    The edited text is read back: if it does not parse, or does not say
    what was asked, the whole document is rewritten from its values.
    """
    try:
        edited = update(text, changes)
        parsed = tomllib.loads(edited)
        if all(parsed.get(section, {}).get(key) == value
               for section, values in changes.items() for key, value in values.items()):
            return edited
    except (CannotEdit, tomllib.TOMLDecodeError):
        pass
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        document = {}
    for section, values in changes.items():
        document.setdefault(section, {}).update(values)
    return dumps(document)
