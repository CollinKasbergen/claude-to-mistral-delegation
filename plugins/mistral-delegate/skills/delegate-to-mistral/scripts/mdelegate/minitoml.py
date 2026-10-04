"""A small TOML reader for Python versions without tomllib (before 3.11).

It covers what .mistral-delegate.toml and Vibe's config.toml use: tables, arrays
of tables, dotted and quoted keys, strings (basic, literal, multi-line), integers,
floats, booleans, arrays and inline tables. Dates are kept as strings.
"""

from __future__ import annotations

import re


class TOMLDecodeError(ValueError):
    pass


_BARE = re.compile(r"[A-Za-z0-9_-]+")
_NUMBER = re.compile(r"[+-]?(0x[0-9A-Fa-f_]+|0o[0-7_]+|0b[01_]+|inf|nan|[0-9_]+(\.[0-9_]+)?([eE][+-]?[0-9_]+)?)")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?|\d{2}:\d{2}:\d{2}(\.\d+)?")
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\", "e": "\x1b"}


class _Parser:
    def __init__(self, text: str):
        self.s, self.i = text.replace("\r\n", "\n"), 0

    def error(self, msg: str) -> TOMLDecodeError:
        line = self.s.count("\n", 0, self.i) + 1
        return TOMLDecodeError(f"{msg} (line {line})")

    def peek(self, n: int = 1) -> str:
        return self.s[self.i:self.i + n]

    def skip_ws(self, newlines: bool = False) -> None:
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t" or (newlines and c == "\n"):
                self.i += 1
            elif c == "#":
                while self.i < len(self.s) and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def expect_line_end(self) -> None:
        self.skip_ws()
        if self.i < len(self.s) and self.s[self.i] != "\n":
            raise self.error(f"unexpected {self.s[self.i]!r}")

    # --- keys ---
    def key(self) -> list[str]:
        parts = []
        while True:
            self.skip_ws()
            c = self.peek()
            if c == '"':
                parts.append(self.basic_string())
            elif c == "'":
                parts.append(self.literal_string())
            else:
                m = _BARE.match(self.s, self.i)
                if not m:
                    raise self.error("expected a key")
                parts.append(m.group())
                self.i = m.end()
            self.skip_ws()
            if self.peek() != ".":
                return parts
            self.i += 1

    # --- values ---
    def value(self):
        self.skip_ws()
        c = self.peek()
        if self.peek(3) == '"""':
            return self.multiline_basic()
        if self.peek(3) == "'''":
            return self.multiline_literal()
        if c == '"':
            return self.basic_string()
        if c == "'":
            return self.literal_string()
        if c == "[":
            return self.array()
        if c == "{":
            return self.inline_table()
        for word, val in (("true", True), ("false", False)):
            if self.s.startswith(word, self.i):
                self.i += len(word)
                return val
        m = _DATE.match(self.s, self.i)
        if m:
            self.i = m.end()
            return m.group()
        m = _NUMBER.match(self.s, self.i)
        if m:
            self.i = m.end()
            text = m.group().replace("_", "")
            if text.lstrip("+-") in ("inf", "nan"):
                return float(text)
            if text.lstrip("+-")[:2] in ("0x", "0o", "0b"):
                return int(text, 0)
            if "." in text or "e" in text or "E" in text:
                return float(text)
            return int(text)
        raise self.error("expected a value")

    def basic_string(self) -> str:
        self.i += 1
        out = []
        while True:
            if self.i >= len(self.s) or self.s[self.i] == "\n":
                raise self.error("unterminated string")
            c = self.s[self.i]
            if c == '"':
                self.i += 1
                return "".join(out)
            if c == "\\":
                out.append(self.escape())
            else:
                out.append(c)
                self.i += 1

    def escape(self) -> str:
        c = self.s[self.i + 1:self.i + 2]
        if c in _ESCAPES:
            self.i += 2
            return _ESCAPES[c]
        if c in ("u", "U"):
            n = 4 if c == "u" else 8
            code = self.s[self.i + 2:self.i + 2 + n]
            self.i += 2 + n
            try:
                return chr(int(code, 16))
            except ValueError:
                raise self.error("bad unicode escape") from None
        raise self.error(f"bad escape \\{c}")

    def literal_string(self) -> str:
        end = self.s.find("'", self.i + 1)
        if end < 0 or "\n" in self.s[self.i:end]:
            raise self.error("unterminated string")
        text = self.s[self.i + 1:end]
        self.i = end + 1
        return text

    def multiline_basic(self) -> str:
        self.i += 3
        if self.peek() == "\n":
            self.i += 1
        out = []
        while True:
            if self.i >= len(self.s):
                raise self.error("unterminated string")
            if self.s.startswith('"""', self.i):
                extra = 0
                while self.s.startswith('"', self.i + 3 + extra) and extra < 2:
                    extra += 1
                out.append('"' * extra)
                self.i += 3 + extra
                return "".join(out)
            c = self.s[self.i]
            if c == "\\":
                rest = self.s[self.i + 1:]
                stripped = rest.lstrip(" \t")
                if stripped.startswith("\n"):  # line-ending backslash: trim the following whitespace
                    self.i += 1
                    while self.i < len(self.s) and self.s[self.i] in " \t\n":
                        self.i += 1
                    continue
                out.append(self.escape())
            else:
                out.append(c)
                self.i += 1

    def multiline_literal(self) -> str:
        self.i += 3
        if self.peek() == "\n":
            self.i += 1
        end = self.s.find("'''", self.i)
        if end < 0:
            raise self.error("unterminated string")
        extra = 0
        while extra < 2 and self.s.startswith("'", end + 3 + extra):
            extra += 1
        end += extra  # up to two quotes right before the closing ones belong to the string
        text = self.s[self.i:end]
        self.i = end + 3
        return text

    def array(self) -> list:
        self.i += 1
        items = []
        while True:
            self.skip_ws(newlines=True)
            if self.peek() == "]":
                self.i += 1
                return items
            items.append(self.value())
            self.skip_ws(newlines=True)
            if self.peek() == ",":
                self.i += 1
            elif self.peek() != "]":
                raise self.error("expected , or ] in array")

    def inline_table(self) -> dict:
        self.i += 1
        table: dict = {}
        self.skip_ws()
        if self.peek() == "}":
            self.i += 1
            return table
        while True:
            key = self.key()
            self.skip_ws()
            if self.peek() != "=":
                raise self.error("expected =")
            self.i += 1
            _set(table, key, self.value(), self)
            self.skip_ws()
            if self.peek() == ",":
                self.i += 1
            elif self.peek() == "}":
                self.i += 1
                return table
            else:
                raise self.error("expected , or } in inline table")

    def document(self) -> dict:
        root: dict = {}
        current = root
        while True:
            self.skip_ws(newlines=True)
            if self.i >= len(self.s):
                return root
            if self.peek(2) == "[[":
                self.i += 2
                path = self.key()
                if self.peek(2) != "]]":
                    raise self.error("expected ]]")
                self.i += 2
                parent = _table(root, path[:-1], self)
                lst = parent.setdefault(path[-1], [])
                if not isinstance(lst, list):
                    raise self.error(f"{'.'.join(path)} is not an array of tables")
                current = {}
                lst.append(current)
            elif self.peek() == "[":
                self.i += 1
                path = self.key()
                if self.peek() != "]":
                    raise self.error("expected ]")
                self.i += 1
                current = _table(root, path, self)
            else:
                key = self.key()
                self.skip_ws()
                if self.peek() != "=":
                    raise self.error("expected =")
                self.i += 1
                _set(current, key, self.value(), self)
            self.expect_line_end()


def _table(root: dict, path: list[str], parser: _Parser) -> dict:
    node = root
    for part in path:
        node = node.setdefault(part, {})
        if isinstance(node, list):
            node = node[-1]
        if not isinstance(node, dict):
            raise parser.error(f"{'.'.join(path)} is not a table")
    return node


def _set(table: dict, key: list[str], value, parser: _Parser) -> None:
    node = _table(table, key[:-1], parser)
    if key[-1] in node:
        raise parser.error(f"duplicate key {'.'.join(key)}")
    node[key[-1]] = value


def loads(text: str) -> dict:
    return _Parser(text).document()


def load(f) -> dict:
    data = f.read()
    return loads(data.decode("utf-8") if isinstance(data, bytes) else data)
