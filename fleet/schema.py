"""VDA 5050 JSON Schema validation with the standard library only.

Messages are checked against the official VDA 5050 JSON schemas kept unchanged
in ``fleet/schemas/vda5050-<version>/`` (provenance and licence in SOURCE.md
there). Only the part of JSON Schema draft 2020-12 those schemas need is
implemented. Each schema is compiled once into a tree of closures, so checking
a message is a walk over the message, not over the schema.

Keywords outside the supported set make compilation fail with
:class:`SchemaError`, so an unsupported rule can never pass silently. Keywords
that carry no validation rule (``title``, ``format``, ...) are ignored; like
the default of the reference ``jsonschema`` package, ``format`` is an
annotation, not an assertion.

>>> validate({"type": "object", "required": ["a"],
...           "properties": {"a": {"type": "integer", "minimum": 0}}}, {"a": -1})
['$.a: -1 is less than the minimum 0']
>>> validate({"type": "integer"}, True)
['$: true is not of type integer']
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import unquote

# (instance, path, errors) -> None; a path is a linked tuple (parent, key) built
# only by reference, and turned into text only when a problem is reported
Check = Callable[[Any, Any, list], None]

SCHEMA_ROOT = Path(__file__).resolve().parent / "schemas"
TOPICS = ("connection", "factsheet", "instantActions", "order", "state", "visualization")

VALIDATION_KEYWORDS = frozenset({
    "type", "enum", "const", "properties", "required", "additionalProperties", "items",
    "minItems", "maxItems", "minLength", "maxLength", "pattern",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "$ref", "anyOf", "oneOf", "allOf",
})
ANNOTATION_KEYWORDS = frozenset({
    "$schema", "$id", "$comment", "title", "description", "format", "examples", "default",
    "definitions", "$defs",   # containers: members are compiled, but they add no rule themselves
    "subtopic",               # VDA 5050: MQTT subtopic of the message
    "unit",                   # VDA 5050 factsheet: physical unit of a value
})
KNOWN_KEYWORDS = VALIDATION_KEYWORDS | ANNOTATION_KEYWORDS
_OBJECT_KEYWORDS = frozenset({"properties", "required", "additionalProperties"})   # compiled as one check
# draft 2020-12 keywords this module does not implement: never mistaken for misplaced property names
_UNSUPPORTED_DRAFT_KEYWORDS = frozenset({
    "$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveRef", "$recursiveAnchor", "$vocabulary",
    "not", "if", "then", "else", "dependentSchemas", "prefixItems", "contains", "patternProperties",
    "propertyNames", "unevaluatedItems", "unevaluatedProperties", "multipleOf", "uniqueItems",
    "maxContains", "minContains", "maxProperties", "minProperties", "dependentRequired",
    "contentEncoding", "contentMediaType", "contentSchema", "deprecated", "readOnly", "writeOnly",
})
DIALECT = "https://json-schema.org/draft/2020-12/schema"

_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda x: isinstance(x, dict),
    "array": lambda x: isinstance(x, list),
    "string": lambda x: isinstance(x, str),
    "boolean": lambda x: isinstance(x, bool),
    "null": lambda x: x is None,
    # bool is a subclass of int in Python but not a number in JSON
    "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
    # draft 2020-12: any number with a zero fractional part, so 3.0 is an integer
    "integer": lambda x: (isinstance(x, int) and not isinstance(x, bool))
    or (isinstance(x, float) and x.is_integer()),
}

# Structural defects in the published files, repaired when they are loaded. Each entry is
# a JSON pointer, applied in order, to a schema object whose property definitions were
# written next to its keywords instead of under "properties".
UPSTREAM_REPAIRS: dict[tuple[str, str], tuple[str, ...]] = {
    ("2.0.0", "factsheet"): ("", "/properties/protocolLimits/properties/maxArrayLens"),
}


class SchemaError(ValueError):
    """The schema uses something this validator does not implement, or is malformed."""


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def json_equal(a: Any, b: Any) -> bool:
    """Equality in the JSON data model: ``true`` is not ``1``, ``1`` is ``1.0``.

    >>> json_equal(1, 1.0), json_equal(True, 1), json_equal([{"a": 0}], [{"a": False}])
    (True, False, False)
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


_IDENT = re.compile(r"[A-Za-z_$][\w$]*\Z")


def format_path(path: Any) -> str:
    """``(((None, 'nodes'), 0), 'nodeId')`` -> ``$.nodes[0].nodeId``."""
    parts: list[str] = []
    while path is not None:
        path, key = path
        if isinstance(key, int):
            parts.append(f"[{key}]")
        elif isinstance(key, str) and _IDENT.match(key):
            parts.append(f".{key}")
        else:
            parts.append(f"[{json.dumps(str(key), ensure_ascii=False)}]")
    return "$" + "".join(reversed(parts))


def _pointer(where: str, *tokens: str) -> str:
    """Extend a JSON pointer, escaping ``~`` and ``/`` inside tokens."""
    return where + "".join("/" + t.replace("~", "~0").replace("/", "~1") for t in tokens)


def _short(value: Any, limit: int = 60) -> str:
    text = json.dumps(value, ensure_ascii=False, default=repr)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _accept(inst: Any, path: Any, errors: list) -> None:
    return None


class _Compiler:
    def __init__(self, root: Any) -> None:
        self.root = root
        self._refs: dict[str, Check] = {}     # by decoded JSON pointer
        self._depth = 0                       # instance levels entered by the schema being compiled
        self._open: dict[str, int] = {}       # references being compiled -> depth where they started

    def _child(self, schema: Any, where: str) -> Check:
        """Compile a schema that applies one level down in the instance (a property or an item)."""
        self._depth += 1
        try:
            return self.compile(schema, where)
        finally:
            self._depth -= 1

    def compile(self, schema: Any, where: str) -> Check:
        if schema is True:
            return _accept
        if schema is False:
            return lambda inst, path, errors: errors.append(f"{format_path(path)}: no value is allowed here")
        if not isinstance(schema, dict):
            raise SchemaError(f"#{where}: a schema must be an object or a boolean")
        unknown = sorted(set(schema) - KNOWN_KEYWORDS)
        if unknown:
            raise SchemaError(f"#{where}: unsupported or misplaced keyword(s) {unknown}")
        if "$schema" in schema and schema["$schema"] != DIALECT:
            # other drafts differ (e.g. draft-07 ignores keywords next to $ref), so they are not guessed at
            raise SchemaError(f"#{where}/$schema: only {DIALECT} is supported")
        if "$id" in schema and where:
            raise SchemaError(f"#{where}/$id: embedded schema resources are not supported")
        for container in ("definitions", "$defs", "properties"):
            if not isinstance(schema.get(container, {}), dict):
                raise SchemaError(f"#{where}/{container} must be an object")
        for container in ("definitions", "$defs"):
            for name in schema.get(container, {}):
                # compiled for strictness even when nothing refers to them
                self._at(_pointer(where, container, name))
        checks = [c for c in (self._compile_keyword(k, schema, where) for k in schema if k not in _OBJECT_KEYWORDS)
                  if c is not None]
        if _OBJECT_KEYWORDS & schema.keys():
            checks.append(self._object(schema, where))
        if not checks:
            return _accept
        if len(checks) == 1:
            return checks[0]
        checks_t = tuple(checks)

        def check_all(inst: Any, path: Any, errors: list) -> None:
            for c in checks_t:
                c(inst, path, errors)
        return check_all

    def _ref(self, ref: str) -> Check:
        if not ref.startswith("#"):
            raise SchemaError(f"$ref {ref!r}: only references inside the same document are supported")
        pointer = unquote(ref[1:])            # the fragment is URI-encoded, the pointer is not
        if pointer and not pointer.startswith("/"):
            raise SchemaError(f"$ref {ref!r}: anchors are not supported")
        return self._at(pointer)

    def _at(self, pointer: str) -> Check:
        """Compiled schema at a JSON pointer of the root document."""
        if pointer in self._open and self._open[pointer] == self._depth:
            # would recurse forever at validation time without consuming any of the instance
            raise SchemaError(f"$ref cycle through #{pointer} does not descend into the instance")
        if pointer in self._refs:
            return self._refs[pointer]
        target: list[Check] = []

        def check_ref(inst: Any, path: Any, errors: list) -> None:
            target[0](inst, path, errors)
        self._refs[pointer] = check_ref       # registered first so recursive schemas terminate
        self._open[pointer] = self._depth
        try:
            target.append(self.compile(self._resolve(pointer), pointer))
        finally:
            del self._open[pointer]
        return check_ref

    def _resolve(self, pointer: str) -> Any:
        node = self.root
        for token in pointer.split("/")[1:]:
            token = token.replace("~1", "/").replace("~0", "~")
            try:
                node = node[int(token)] if isinstance(node, list) else node[token]
            except (KeyError, IndexError, ValueError, TypeError):
                raise SchemaError(f"$ref #{pointer} does not resolve") from None
        return node

    def _compile_keyword(self, key: str, s: dict, where: str) -> Check | None:
        v = s[key]
        if key in ANNOTATION_KEYWORDS:
            return None
        if key == "type":
            return self._type(v, where)
        if key == "items":
            return self._items(v, where)
        if key == "enum":
            return self._enum(v, where)
        if key == "const":
            return self._const(v)
        if key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            return self._bound(key, v, where)
        if key in ("minItems", "maxItems", "minLength", "maxLength"):
            return self._length(key, v, where)
        if key == "pattern":
            return self._pattern(v, where)
        if key == "$ref":
            if not isinstance(v, str):
                raise SchemaError(f"#{where}/$ref must be a string")
            return self._ref(v)
        if key in ("anyOf", "oneOf", "allOf"):
            return self._combinator(key, v, where)
        raise SchemaError(f"#{where}: keyword {key!r} has no implementation")

    # ------------------------------------------------------------------ keywords
    def _type(self, v: Any, where: str) -> Check:
        names = [v] if isinstance(v, str) else v
        if not isinstance(names, list) or not names or any(n not in _TYPES for n in names):
            raise SchemaError(f"#{where}/type: unknown type {v!r}")
        tests = tuple(_TYPES[n] for n in names)
        label = " or ".join(names)

        def check_type(inst: Any, path: Any, errors: list) -> None:
            for t in tests:
                if t(inst):
                    return
            errors.append(f"{format_path(path)}: {_short(inst)} is not of type {label}")
        return check_type

    def _object(self, s: dict, where: str) -> Check:
        props = {name: self._child(sub, _pointer(where, "properties", name))
                 for name, sub in s.get("properties", {}).items()}
        required = s.get("required", [])
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            raise SchemaError(f"#{where}/required must list strings")
        required = tuple(required)
        extra = s.get("additionalProperties", True)
        extra_check = None if extra is True else self._child(extra, _pointer(where, "additionalProperties"))
        forbid_extra = extra is False

        def check_object(inst: Any, path: Any, errors: list) -> None:
            if not isinstance(inst, dict):
                return
            for name in required:
                if name not in inst:
                    errors.append(f"{format_path(path)}: missing required property {name!r}")
            for name, value in inst.items():
                sub = props.get(name)
                if sub is not None:
                    sub(value, (path, name), errors)
                elif forbid_extra:
                    errors.append(f"{format_path(path)}: additional property {name!r} is not allowed")
                elif extra_check is not None:
                    extra_check(value, (path, name), errors)
        return check_object

    def _items(self, v: Any, where: str) -> Check:
        if isinstance(v, list):
            raise SchemaError(f"#{where}/items: the array form is not draft 2020-12 (prefixItems unsupported)")
        sub = self._child(v, f"{where}/items")

        def check_items(inst: Any, path: Any, errors: list) -> None:
            if isinstance(inst, list):
                for i, item in enumerate(inst):
                    sub(item, (path, i), errors)
        return check_items

    def _enum(self, v: Any, where: str) -> Check:
        if not isinstance(v, list) or not v:
            raise SchemaError(f"#{where}/enum must be a non-empty array")
        allowed = tuple(v)
        text = ", ".join(_short(a, 30) for a in allowed)
        if all(isinstance(a, str) for a in allowed):
            fast = frozenset(allowed)

            def check_str_enum(inst: Any, path: Any, errors: list) -> None:
                if not (isinstance(inst, str) and inst in fast):
                    errors.append(f"{format_path(path)}: {_short(inst)} is not one of [{text}]")
            return check_str_enum

        def check_enum(inst: Any, path: Any, errors: list) -> None:
            if not any(json_equal(inst, a) for a in allowed):
                errors.append(f"{format_path(path)}: {_short(inst)} is not one of [{text}]")
        return check_enum

    def _const(self, v: Any) -> Check:
        def check_const(inst: Any, path: Any, errors: list) -> None:
            if not json_equal(inst, v):
                errors.append(f"{format_path(path)}: {_short(inst)} is not {_short(v)}")
        return check_const

    def _bound(self, key: str, v: Any, where: str) -> Check:
        if not _is_number(v):
            raise SchemaError(f"#{where}/{key} must be a number")
        if key == "minimum":
            bad, word = (lambda x: x < v), "less than the minimum"
        elif key == "maximum":
            bad, word = (lambda x: x > v), "greater than the maximum"
        elif key == "exclusiveMinimum":
            bad, word = (lambda x: x <= v), "not greater than the exclusive minimum"
        else:
            bad, word = (lambda x: x >= v), "not less than the exclusive maximum"

        def check_bound(inst: Any, path: Any, errors: list) -> None:
            if _is_number(inst) and bad(inst):
                errors.append(f"{format_path(path)}: {inst!r} is {word} {v!r}")
        return check_bound

    def _length(self, key: str, v: Any, where: str) -> Check:
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise SchemaError(f"#{where}/{key} must be a non-negative integer")
        kind = list if key.endswith("Items") else str
        noun = "items" if kind is list else "characters"
        if key.startswith("min"):
            bad, word = (lambda n: n < v), "fewer"
        else:
            bad, word = (lambda n: n > v), "more"

        def check_length(inst: Any, path: Any, errors: list) -> None:
            if isinstance(inst, kind) and bad(len(inst)):
                errors.append(f"{format_path(path)}: has {len(inst)} {noun}, {word} than {key} {v}")
        return check_length

    def _pattern(self, v: Any, where: str) -> Check:
        # Python re, not ECMA-262: equal for plain patterns, but "$" also matches before a final
        # newline and "\d" matches any Unicode digit (the reference jsonschema package does the same)
        try:
            rx = re.compile(v)
        except (re.error, TypeError) as exc:
            raise SchemaError(f"#{where}/pattern: {exc}") from None

        def check_pattern(inst: Any, path: Any, errors: list) -> None:
            if isinstance(inst, str) and rx.search(inst) is None:
                errors.append(f"{format_path(path)}: {_short(inst)} does not match {v!r}")
        return check_pattern

    def _combinator(self, key: str, v: Any, where: str) -> Check:
        if not isinstance(v, list) or not v:
            raise SchemaError(f"#{where}/{key} must be a non-empty array")
        subs = tuple(self.compile(sub, f"{where}/{key}/{i}") for i, sub in enumerate(v))
        if key == "allOf":
            def check_all_of(inst: Any, path: Any, errors: list) -> None:
                for sub in subs:
                    sub(inst, path, errors)
            return check_all_of
        if key == "anyOf":
            def check_any_of(inst: Any, path: Any, errors: list) -> None:
                first: list = []
                for sub in subs:
                    found: list = []
                    sub(inst, path, found)
                    if not found:
                        return
                    first = first or found
                errors.append(f"{format_path(path)}: matches none of the {len(subs)} anyOf schemas "
                              f"(first: {first[0]})")
            return check_any_of

        def check_one_of(inst: Any, path: Any, errors: list) -> None:
            matched = 0
            for sub in subs:
                found: list = []
                sub(inst, path, found)
                matched += not found
            if matched != 1:
                errors.append(f"{format_path(path)}: matches {matched} of the {len(subs)} oneOf schemas, "
                              f"exactly one required")
        return check_one_of


def compile_schema(schema: Any) -> Callable[[Any], list[str]]:
    """Compile ``schema`` once; the returned function lists the problems of one instance.

    Raises :class:`SchemaError` for keywords this module does not implement.
    """
    check = _Compiler(schema)._at("")        # the root is a reference target like any other

    def run(instance: Any) -> list[str]:
        errors: list[str] = []
        check(instance, None, errors)
        return errors
    return run


# compiled schemas keyed by id(); the schema object is kept alive with its entry so the id
# cannot be reused
_compiled: dict[int, tuple[Any, Callable[[Any], list[str]]]] = {}


def validate(schema: Any, instance: Any) -> list[str]:
    """Problems of ``instance`` against ``schema``, each prefixed with its path; empty when valid.

    The compiled form is cached per schema object, so a schema must not be changed after it
    has been used here: pass a new object, or use :func:`compile_schema` for a changed schema.
    """
    entry = _compiled.get(id(schema))
    if entry is None or entry[0] is not schema:
        if len(_compiled) >= 64:
            _compiled.clear()
        entry = (schema, compile_schema(schema))
        _compiled[id(schema)] = entry
    return entry[1](instance)


def _relocate_properties(node: dict, where: str) -> None:
    misplaced = {k: v for k, v in node.items() if k not in KNOWN_KEYWORDS}
    if not misplaced or not all(isinstance(v, dict) for v in misplaced.values()) \
            or misplaced.keys() & _UNSUPPORTED_DRAFT_KEYWORDS:
        raise SchemaError(f"#{where}: expected only misplaced property definitions to repair, got {sorted(misplaced)}")
    for name in misplaced:
        del node[name]
    node.setdefault("properties", {}).update(misplaced)


def _schema_file(version: str, name: str) -> Path:
    folder = SCHEMA_ROOT / f"vda5050-{version}"
    for suffix in (".schema", ".json"):      # 2.0.0 publishes the factsheet as factsheet.json
        path = folder / f"{name}{suffix}"
        if path.is_file():
            return path
    raise FileNotFoundError(f"no VDA 5050 {version} schema for {name!r} in {folder}")


def load_vda5050_schemas(version: str = "2.0.0") -> dict[str, dict]:
    """Official schemas by topic name, with the known upstream defects repaired.

    A fresh copy is read on every call; see :func:`validate` before changing one.
    """
    out: dict[str, dict] = {}
    for name in TOPICS:
        schema = json.loads(_schema_file(version, name).read_text(encoding="utf-8"))
        for pointer in UPSTREAM_REPAIRS.get((version, name), ()):
            node = schema
            for token in pointer.split("/")[1:]:
                node = node[token]
            _relocate_properties(node, pointer)
        out[name] = schema
    return out


@lru_cache(maxsize=None)
def _validators(version: str) -> dict[str, Callable[[Any], list[str]]]:
    return {name: compile_schema(s) for name, s in load_vda5050_schemas(version).items()}


def validate_message(topic_name: str, msg: Any, version: str = "2.0.0") -> list[str]:
    """Problems of one VDA 5050 message; ``topic_name`` is the last topic level, e.g. ``state``."""
    validators = _validators(version)
    if topic_name not in validators:
        raise KeyError(f"{topic_name!r} is not a VDA 5050 topic; expected one of {', '.join(TOPICS)}")
    return validators[topic_name](msg)
