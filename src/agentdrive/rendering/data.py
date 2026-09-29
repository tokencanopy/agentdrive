"""JSON, JSON Lines, YAML and XML → a readable document, or rows for the table.

Two shapes, chosen from the data rather than the file:

* **Table** — an array of flat objects (every value a scalar), or a JSON Lines
  file of them. That is the shape an agent's export or API dump almost always
  has, and rows-and-columns is how a reader wants it; `render.py` hands the
  rows to the same `_data_table` a csv and a parquet use.
* **Tree** — everything else. Each object and array is a native `<details>`
  disclosure, so every field expands and collapses on its own with no script
  and no policy change; the first two levels open, deeper ones start closed
  behind a summary that says what is inside ("3 fields", "120 items").

Everything is bounded, and every bound is stated. A 2 MiB document can be
one array of a million numbers or one string, and either would be a DOM that
locks the reader's tab: arrays show their first `MAX_ITEMS`, strings their
first `MAX_STRING_CHARS`, nesting stops at `MAX_DEPTH`, and the whole tree
stops at `MAX_NODES`. When any of those cuts, the caption says so.

Fidelity is kept where it is cheap and matters: numbers render as the lexeme
the author wrote (`1.0` stays `1.0`, `1e3` stays `1e3`), keys keep their
order, and duplicate keys are all shown — `json.loads` would silently keep
the last one, and a reader debugging a payload needs to see the collision.

This module renders JSON WE parsed into markup WE wrote. Every string that
reaches the output passes through `html.escape`; nothing an author wrote is
ever emitted as markup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape

import yaml
from defusedxml import ElementTree as SafeElementTree

MAX_NODES = 5_000
MAX_DEPTH = 16
MAX_ITEMS = 500
MAX_STRING_CHARS = 500
MAX_RECORDS = 500
# Levels that start expanded. Two is enough to see the shape of a payload
# without unrolling a large one; a reader opens the rest.
OPEN_DEPTH = 2


class _Number(str):
    """A number as the author wrote it — the lexeme, not a float."""


class _Object(list):
    """An object as an ordered list of `(key, value)` pairs, duplicates kept."""


def _parse_one(text: str) -> object:
    return json.loads(
        text,
        object_pairs_hook=_Object,
        parse_float=_Number,
        parse_int=_Number,
        parse_constant=_Number,
    )


@dataclass(frozen=True)
class Rows:
    """A table the caller draws: header, capped body, true extents."""

    head: list[str]
    body: list[list[str]]
    total_rows: int
    total_columns: int


@dataclass(frozen=True)
class Tree:
    html: str
    truncated: list[str]


def parse(text: str, *, lines: bool) -> object | list[object] | None:
    """The document's value, or the list of a JSON Lines file's records.
    None when it is not valid JSON, so the caller can fall back to source."""
    try:
        if not lines:
            return _parse_one(text)
        records: list[object] = []
        for raw in text.splitlines():
            if raw.strip():
                records.append(_parse_one(raw))
        return records
    except (ValueError, RecursionError):
        return None


def _is_scalar(value: object) -> bool:
    return not isinstance(value, (_Object, list, dict))


def _pairs(value: object) -> list[tuple[str, object]]:
    """A mapping as ordered pairs, whichever parser produced it: JSON keeps
    duplicates in `_Object`; YAML's mapping is a dict whose keys may not be
    strings, so they are shown as the loader read them."""
    if isinstance(value, _Object):
        return list(value)
    return [(k if isinstance(k, str) else str(k), v) for k, v in value.items()]


def _is_mapping(value: object) -> bool:
    return isinstance(value, (_Object, dict))


def as_rows(value: object) -> Rows | None:
    """Rows when `value` is a non-empty array of flat objects, else None."""
    if not isinstance(value, list) or isinstance(value, _Object) or not value:
        return None
    if not all(
        _is_mapping(item) and all(_is_scalar(v) for _, v in _pairs(item)) for item in value
    ):
        return None
    columns: list[str] = []
    seen: set[str] = set()
    for item in value:
        for key, _ in _pairs(item):
            if key not in seen:
                seen.add(key)
                columns.append(key)
    body: list[list[str]] = []
    for item in value[:MAX_RECORDS]:
        cells = dict(_pairs(item))  # last duplicate wins in a cell; the tree shows both
        body.append([_cell(cells.get(column)) if column in cells else "" for column in columns])
    return Rows(head=columns, body=body, total_rows=len(value), total_columns=len(columns))


def _cell(value: object) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    text = str(value)
    if len(text) > MAX_STRING_CHARS:
        return text[:MAX_STRING_CHARS] + "…"
    return text


class _Budget:
    def __init__(self) -> None:
        self.nodes = 0
        self.truncated: list[str] = []

    def spend(self) -> bool:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            self._note(f"the first {MAX_NODES:,} values")
            return False
        return True

    def _note(self, what: str) -> None:
        if what not in self.truncated:
            self.truncated.append(what)


def tree(value: object) -> Tree:
    """The collapsible document for any JSON value."""
    budget = _Budget()
    html = _node(value, budget, depth=0, key=None)
    return Tree(html=f'<div class="json-tree">{html}</div>', truncated=budget.truncated)


def _scalar_html(value: object) -> str:
    if value is None:
        return '<span class="json-null">null</span>'
    if value is True:
        return '<span class="json-bool">true</span>'
    if value is False:
        return '<span class="json-bool">false</span>'
    if isinstance(value, _Number):
        return f'<span class="json-number">{escape(value)}</span>'
    text = str(value)
    cut = len(text) > MAX_STRING_CHARS
    shown = text[:MAX_STRING_CHARS] + "…" if cut else text
    extra = ' data-cut="1"' if cut else ""
    literal = escape(json.dumps(shown, ensure_ascii=False))
    return f'<span class="json-string"{extra}>{literal}</span>'


def _label(key: str | None) -> str:
    if key is None:
        return ""
    return (
        f'<span class="json-key">{escape(json.dumps(key, ensure_ascii=False))}</span>'
        '<span class="json-punct">: </span>'
    )


def _node(value: object, budget: _Budget, *, depth: int, key: str | None) -> str:
    if not budget.spend():
        return ""
    if _is_scalar(value):
        is_text = isinstance(value, str) and not isinstance(value, _Number)
        if is_text and len(value) > MAX_STRING_CHARS:
            budget._note(f"the first {MAX_STRING_CHARS} characters of long strings")
        return f'<div class="json-leaf">{_label(key)}{_scalar_html(value)}</div>'

    is_object = _is_mapping(value)
    open_b, close_b = ("{", "}") if is_object else ("[", "]")
    count = len(value)
    noun = ("field" if is_object else "item") + ("" if count == 1 else "s")
    if count == 0:
        return (
            f'<div class="json-leaf">{_label(key)}'
            f'<span class="json-brace">{open_b}{close_b}</span></div>'
        )
    if depth >= MAX_DEPTH:
        budget._note(f"{MAX_DEPTH} levels of nesting")
        return (
            f'<div class="json-leaf">{_label(key)}<span class="json-brace">{open_b}</span>'
            f'<span class="json-count">{count} {noun}, not expanded</span>'
            f'<span class="json-brace">{close_b}</span></div>'
        )

    children: list[str] = []
    items = _pairs(value) if is_object else list(value)
    shown = items[:MAX_ITEMS]
    for entry in shown:
        child_key, child = entry if is_object else (None, entry)
        rendered = _node(child, budget, depth=depth + 1, key=child_key)
        if not rendered:
            break
        children.append(rendered)
    if len(items) > len(shown):
        budget._note(f"the first {MAX_ITEMS} items of long arrays")
        children.append(
            f'<div class="json-more">… {len(items) - len(shown):,} more {noun}</div>'
        )
    opened = " open" if depth < OPEN_DEPTH else ""
    return (
        f'<details class="json-node"{opened}>'
        f"<summary>{_label(key)}<span class=\"json-brace\">{open_b}</span>"
        f'<span class="json-count">{count} {noun}</span></summary>'
        f'<div class="json-children">{"".join(children)}</div>'
        f'<span class="json-brace json-close">{close_b}</span>'
        "</details>"
    )


# ─── YAML ────────────────────────────────────────────────────────────────────


class _LexemeLoader(yaml.SafeLoader):
    """SafeLoader — no arbitrary tags, no object construction — that keeps
    numbers as the text the author wrote (`1.0`, `0x1F`, `1e3`) rather than
    the float PyYAML would make of them, so the tree shows the file, not a
    normalisation of it. Everything else resolves as YAML 1.1 would."""


def _lexeme(loader, node):  # noqa: ANN001 - PyYAML constructor signature
    return _Number(node.value)


for _tag in ("tag:yaml.org,2002:int", "tag:yaml.org,2002:float"):
    _LexemeLoader.add_constructor(_tag, _lexeme)
# Timestamps as written too: a date is a fact about the document, and the
# reader wants the string, not a `datetime` repr.
_LexemeLoader.add_constructor("tag:yaml.org,2002:timestamp", lambda loader, node: node.value)


def parse_yaml(text: str) -> object | None:
    """The document's value — a list of documents for a multi-document
    stream — or None when it is not YAML, so the caller keeps the source."""
    try:
        documents = list(yaml.load_all(text, Loader=_LexemeLoader))  # noqa: S506 - SafeLoader subclass
    except (yaml.YAMLError, RecursionError, ValueError):
        return None
    if not documents:
        return None
    return documents[0] if len(documents) == 1 else documents


# ─── XML ─────────────────────────────────────────────────────────────────────


def xml_tree(text: str) -> Tree | None:
    """An XML document as the same foldable tree: each element a node whose
    summary is its start tag with attributes, its text and children inside,
    a leaf when it holds only text. Parsed by defusedxml, so entity
    expansion bombs, external entities and DTD retrieval are refused rather
    than expanded — those are the ways an XML file attacks the parser that
    reads it, and this parser reads what an agent uploaded.

    Comments and processing instructions are not shown: they are not the
    document's data. Namespaced names show as the local name with the
    namespace on hover, because ElementTree does not keep prefixes.
    """
    try:
        root = SafeElementTree.fromstring(text.encode("utf-8"))
    except Exception:  # noqa: BLE001 - defusedxml's refusals and any parse error alike
        return None
    if root is None:
        return None
    budget = _Budget()
    html = _xml_node(root, budget, depth=0)
    return Tree(html=f'<div class="json-tree xml-tree">{html}</div>', truncated=budget.truncated)


def _xml_name(tag: str) -> tuple[str, str | None]:
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return local, namespace
    return tag, None


def _xml_start(element, self_closing: bool = False) -> str:  # noqa: ANN001
    local, namespace = _xml_name(element.tag)
    title = f' title="{escape(namespace)}"' if namespace else ""
    attrs = "".join(
        f' <span class="xml-attr">{escape(_xml_name(k)[0])}</span>'
        '<span class="json-punct">=</span>'
        f'<span class="json-string">{escape(json.dumps(v, ensure_ascii=False))}</span>'
        for k, v in element.attrib.items()
    )
    end = "/&gt;" if self_closing else "&gt;"
    return (
        f'<span class="xml-tag"{title}>&lt;{escape(local)}</span>{attrs}'
        f'<span class="xml-tag">{end}</span>'
    )


def _xml_end(element) -> str:  # noqa: ANN001
    local, _ = _xml_name(element.tag)
    return f'<span class="xml-tag">&lt;/{escape(local)}&gt;</span>'


def _xml_text(text: str, budget: _Budget) -> str:
    text = text.strip()
    if not text:
        return ""
    if len(text) > MAX_STRING_CHARS:
        budget._note(f"the first {MAX_STRING_CHARS} characters of long text")
        text = text[:MAX_STRING_CHARS] + "…"
    return f'<span class="xml-text">{escape(text)}</span>'


def _xml_node(element, budget: _Budget, *, depth: int) -> str:  # noqa: ANN001
    if not budget.spend():
        return ""
    children = list(element)
    text = _xml_text(element.text or "", budget)
    if not children:
        if not text:
            return f'<div class="json-leaf">{_xml_start(element, self_closing=True)}</div>'
        return f'<div class="json-leaf">{_xml_start(element)}{text}{_xml_end(element)}</div>'
    count = len(children)
    noun = "element" if count == 1 else "elements"
    if depth >= MAX_DEPTH:
        budget._note(f"{MAX_DEPTH} levels of nesting")
        return (
            f'<div class="json-leaf">{_xml_start(element)}'
            f'<span class="json-count">{count} {noun}, not expanded</span>{_xml_end(element)}</div>'
        )
    parts: list[str] = []
    if text:
        parts.append(f'<div class="json-leaf">{text}</div>')
    for child in children[:MAX_ITEMS]:
        rendered = _xml_node(child, budget, depth=depth + 1)
        if not rendered:
            break
        parts.append(rendered)
        tail = _xml_text(child.tail or "", budget)
        if tail:
            parts.append(f'<div class="json-leaf">{tail}</div>')
    if count > MAX_ITEMS:
        budget._note(f"the first {MAX_ITEMS} children of large elements")
        parts.append(f'<div class="json-more">… {count - MAX_ITEMS:,} more {noun}</div>')
    opened = " open" if depth < OPEN_DEPTH else ""
    return (
        f'<details class="json-node"{opened}>'
        f"<summary>{_xml_start(element)}"
        f'<span class="json-count">{count} {noun}</span></summary>'
        f'<div class="json-children">{"".join(parts)}</div>'
        f'<span class="json-close">{_xml_end(element)}</span>'
        "</details>"
    )
