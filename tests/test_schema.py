"""Tests for the standard-library JSON Schema validator and the official VDA 5050 2.0.0 schemas.

Valid examples are hand-made minimal messages plus the messages of a short simulation that pass the
schema; invalid examples are mutations of them. Simulation messages that currently fail the schema are
not asserted on here (they are compared with the reference ``jsonschema`` package only).
"""
from __future__ import annotations

import copy
import doctest
from functools import lru_cache
import hashlib
import json
import re
import unittest
from unittest import mock
import warnings

from fleet import schema as schema_mod
from fleet.bus import Bus
from fleet.scenario import get
from fleet.schema import (SCHEMA_ROOT, TOPICS, SchemaError, compile_schema, load_vda5050_schemas, validate,
                          validate_message)
from fleet.sim import Simulation
from fleet.vda5050 import split_topic

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import jsonschema
except ImportError:          # only the cross-check against the reference implementation needs it
    jsonschema = None

SCHEMA_DIR = SCHEMA_ROOT / "vda5050-2.0.0"
SIM_SECONDS = 30.0           # enough for every topic: connection and factsheet go out at start-up

HDR = {"headerId": 0, "timestamp": "2026-10-01T00:00:00.000Z", "version": "2.0.0",
       "manufacturer": "sim", "serialNumber": "AMR-01"}
# the smallest messages the 2.0.0 schemas accept, written from their required lists
MINIMAL: dict[str, dict] = {
    "connection": {**HDR, "connectionState": "ONLINE"},
    "factsheet": {
        **HDR,
        "typeSpecification": {"seriesName": "S", "agvKinematic": "DIFF", "agvClass": "CARRIER", "maxLoadMass": 100,
                              "localizationTypes": ["NATURAL"], "navigationTypes": ["VIRTUAL_LINE_GUIDED"]},
        "physicalParameters": {"speedMin": 0.0, "speedMax": 1.5, "accelerationMax": 0.5, "decelerationMax": 0.8,
                               "heightMax": 0.3, "width": 0.6, "length": 0.8},
        "protocolLimits": {"maxStringLens": {}, "maxArrayLens": {},
                           "timing": {"minOrderInterval": 0.2, "minStateInterval": 0.2}},
        "protocolFeatures": {"optionalParameters": [],
                             "agvActions": [{"actionType": "startPause", "actionScopes": ["INSTANT"]}]},
        "agvGeometry": {},
        "loadSpecification": {},
    },
    # the 2.0.0 schema calls this field actionName; the specification text and the 2.1.0 schema say actionType
    "instantActions": {**HDR, "actions": [{"actionName": "x_fleetSpeedLimit", "actionId": "a-1", "blockingType": "NONE",
                                           "actionParameters": [{"key": "limit", "value": 0.3}]}]},
    "order": {**HDR, "orderId": "o-1", "orderUpdateId": 0, "edges": [],
              "nodes": [{"nodeId": "n1", "sequenceId": 0, "released": True,
                         "actions": [{"actionType": "pick", "actionId": "a-1", "blockingType": "HARD"}]}]},
    "state": {**HDR, "orderId": "", "orderUpdateId": 0, "lastNodeId": "", "lastNodeSequenceId": 0,
              "nodeStates": [], "edgeStates": [], "driving": False, "actionStates": [], "operatingMode": "AUTOMATIC",
              "batteryState": {"batteryCharge": 90.0, "charging": False}, "errors": [],
              "safetyState": {"eStop": "NONE", "fieldViolation": False}},
    "visualization": {**HDR, "agvPosition": {"x": 1.0, "y": 2.0, "theta": 0.0, "mapId": "m",
                                             "positionInitialized": True}},
}

DELETE = object()
# (topic, path to change, new value or DELETE, text expected among the problems)
MUTATIONS: list[tuple[str, tuple, object, str]] = [
    ("connection", ("connectionState",), DELETE, "$: missing required property 'connectionState'"),
    ("connection", ("connectionState",), "BROKEN", '$.connectionState: "BROKEN" is not one of'),
    ("connection", ("headerId",), True, "$.headerId: true is not of type integer"),
    ("factsheet", ("typeSpecification",), DELETE, "$: missing required property 'typeSpecification'"),
    ("factsheet", ("typeSpecification", "agvClass"), "DRONE", '$.typeSpecification.agvClass: "DRONE" is not one of'),
    ("factsheet", ("physicalParameters", "speedMax"), "fast", '$.physicalParameters.speedMax: "fast" is not of type'),
    ("instantActions", ("actions", 0, "actionId"), DELETE, "$.actions[0]: missing required property 'actionId'"),
    ("instantActions", ("actions", 0, "blockingType"), "MEDIUM", '$.actions[0].blockingType: "MEDIUM" is not one of'),
    ("instantActions", ("actions", 0, "actionParameters"), [{"key": "limit", "value": None}],
     "$.actions[0].actionParameters[0].value: null is not of type"),
    ("order", ("orderId",), DELETE, "$: missing required property 'orderId'"),
    ("order", ("nodes", 0, "released"), DELETE, "$.nodes[0]: missing required property 'released'"),
    ("order", ("nodes", 0, "actions"), [{"actionType": "pick", "actionId": "a", "blockingType": "MEDIUM"}],
     '$.nodes[0].actions[0].blockingType: "MEDIUM" is not one of'),
    ("order", ("orderUpdateId",), 1.5, "$.orderUpdateId: 1.5 is not of type integer"),
    ("order", ("nodes", 0, "sequenceId"), -2, "$.nodes[0].sequenceId: -2 is less than the minimum 0"),
    ("state", ("batteryState",), DELETE, "$: missing required property 'batteryState'"),
    ("state", ("safetyState", "eStop"), DELETE, "$.safetyState: missing required property 'eStop'"),
    ("state", ("operatingMode",), "AUTO", '$.operatingMode: "AUTO" is not one of'),
    ("state", ("errors",), [{"errorType": "x", "errorLevel": "ERROR"}],
     '$.errors[0].errorLevel: "ERROR" is not one of'),
    ("state", ("driving",), "yes", '$.driving: "yes" is not of type boolean'),
    ("state", ("batteryState", "batteryCharge"), False, "$.batteryState.batteryCharge: false is not of type number"),
    ("visualization", ("agvPosition", "mapId"), DELETE, "$.agvPosition: missing required property 'mapId'"),
    ("visualization", ("agvPosition", "x"), "1", '$.agvPosition.x: "1" is not of type number'),
    ("visualization", ("agvPosition", "localizationScore"), 1.5,
     "$.agvPosition.localizationScore: 1.5 is greater than the maximum"),
    ("visualization", ("headerId",), "7", '$.headerId: "7" is not of type integer'),
]

# (schema, instance, valid) for every supported keyword and its edge cases
KEYWORD_CASES: list[tuple[object, object, bool]] = [
    ({"type": "integer"}, 3, True),
    ({"type": "integer"}, 3.0, True),                 # draft 2020-12: a zero fractional part is an integer
    ({"type": "integer"}, 3.5, False),
    ({"type": "integer"}, True, False),               # bool is not a number in JSON
    ({"type": "number"}, False, False),
    ({"type": "number"}, 2, True),
    ({"type": ["string", "null"]}, None, True),
    ({"type": ["array", "boolean", "number", "string"]}, None, False),
    ({"type": "object"}, [], False),
    ({"type": "array"}, {}, False),
    ({"enum": ["A", "B"]}, "C", False),
    ({"enum": [1, "x"]}, True, False),
    ({"enum": [1, "x"]}, 1.0, True),
    ({"const": False}, 0, False),
    ({"const": {"a": [1]}}, {"a": [1.0]}, True),
    ({"minimum": 0}, -0.1, False),
    ({"minimum": 0}, 0, True),
    ({"minimum": 0}, "x", True),                      # numeric keywords ignore other types
    ({"maximum": 1}, 1.0001, False),
    ({"exclusiveMinimum": 0}, 0, False),
    ({"exclusiveMaximum": 1}, 0.99, True),
    ({"exclusiveMaximum": 1}, 1, False),
    ({"minItems": 1}, [], False),
    ({"maxItems": 1}, [1, 2], False),
    ({"minItems": 1}, "not an array", True),
    ({"minLength": 2}, "a", False),
    ({"maxLength": 1}, "ab", False),
    ({"minLength": 2}, "한글", True),                  # length counts characters, not bytes
    ({"pattern": "^x_"}, "x_speed", True),
    ({"pattern": "b"}, "abc", True),                  # unanchored, as in ECMA-262
    ({"pattern": "^b"}, "abc", False),
    ({"required": ["a"]}, {}, False),
    ({"required": ["a"]}, [], True),                  # object keywords ignore non-objects
    ({"properties": {"a": {"type": "string"}}, "additionalProperties": False}, {"a": "x", "x_b": 1}, False),
    ({"properties": {"a": {"type": "string"}}, "additionalProperties": False}, {"a": "x"}, True),
    ({"additionalProperties": {"type": "number"}}, {"a": 1, "b": "2"}, False),
    ({"required": ["a"], "additionalProperties": False}, {"a": 1}, False),
    ({"properties": {"a": False}}, {"a": 1}, False),
    ({"properties": {"a": False}}, {"b": 1}, True),
    ({"items": {"type": "integer"}}, [1, "2"], False),
    ({"items": {"type": "integer"}}, [], True),
    ({"$ref": "#/definitions/p", "definitions": {"p": {"type": "integer", "minimum": 1}}}, 0, False),
    ({"$ref": "#/$defs/p", "$defs": {"p": {"type": "string"}}}, "ok", True),
    ({"$ref": "#/$defs/a%2541", "$defs": {"a%41": {"type": "string"}}}, 1, False),   # URI-encoded fragment
    ({"$ref": "#/$defs/a~1b", "$defs": {"a/b": {"type": "integer"}}}, "x", False),     # escaped pointer token
    ({"$ref": "#/$defs/n",
      "$defs": {"n": {"type": "object", "required": ["v"], "properties": {"next": {"$ref": "#/$defs/n"}}}}},
     {"v": 1, "next": {"v": 2, "next": {}}}, False),  # recursive reference
    # draft 2020-12: keywords next to $ref apply as well
    ({"$ref": "#/$defs/p", "$defs": {"p": {"type": "integer"}}, "minimum": 5}, 3, False),
    ({"anyOf": [{"type": "string"}, {"minimum": 5}]}, 3, False),
    ({"anyOf": [{"type": "string"}, {"minimum": 5}]}, 7, True),
    ({"oneOf": [{"type": "integer"}, {"minimum": 0}]}, 5, False),      # both match
    ({"oneOf": [{"type": "integer"}, {"minimum": 0}]}, -1, True),
    ({"oneOf": [{"type": "integer"}, {"minimum": 0}]}, "s", True),     # minimum ignores strings
    ({"allOf": [{"type": "number"}, {"maximum": 3}]}, 4, False),
    (True, {"anything": 1}, True),
    (False, 1, False),
    ({"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://example.com/s", "title": "t",
      "description": "d", "format": "date-time", "examples": [1], "default": 0, "$comment": "c",
      "subtopic": "/state", "unit": "m"}, "not a date", True),      # annotations only
]
# jsonschema 4.10.3 compares enum members containing booleans with Python ==, so [true] equals [1] there;
# JSON Schema equality says it does not. Checked against the specification only.
OWN_ONLY_CASES: list[tuple[object, object, bool]] = [
    ({"enum": [[1]]}, [True], False),
    ({"const": [0]}, [False], False),
]


@lru_cache(maxsize=1)
def sim_messages() -> dict[str, tuple[dict, ...]]:
    """Every VDA 5050 message of a short standard run, as it would go on the wire."""
    by_topic: dict[str, list[dict]] = {name: [] for name in TOPICS}
    publish = Bus.publish

    def spy(bus: Bus, topic: str, payload: dict, now: float) -> bool:
        parts = split_topic(topic)
        if parts is not None:                     # facility/... topics are not VDA 5050
            by_topic.setdefault(parts[2], []).append(json.loads(json.dumps(payload)))
        return publish(bus, topic, payload, now)

    # patched on the class: connection messages are published while Simulation is constructed
    with mock.patch.object(Bus, "publish", spy):
        Simulation(get("standard"), seed=1).run(SIM_SECONDS)
    return {name: tuple(msgs) for name, msgs in by_topic.items()}


@lru_cache(maxsize=1)
def valid_examples() -> dict[str, tuple[dict, ...]]:
    """Minimal messages plus the simulation messages the schema accepts today."""
    return {name: (MINIMAL[name],) + tuple(m for m in sim_messages()[name] if not validate_message(name, m))
            for name in TOPICS}


def mutate(msg: dict, path: tuple, value: object) -> dict | None:
    """Copy of ``msg`` with ``path`` set to ``value`` (or removed); None when the path does not exist."""
    out = copy.deepcopy(msg)
    node = out
    for key in path[:-1]:
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            return None
    last = path[-1]
    if value is DELETE:
        if not isinstance(node, dict) or last not in node:
            return None
        del node[last]
    elif isinstance(node, dict) or (isinstance(node, list) and isinstance(last, int) and last < len(node)):
        node[last] = copy.deepcopy(value)
    else:
        return None
    return out


def apply_reported_fixes(name: str, msg: dict) -> dict:
    """The fixes proposed for the violations the current code produces; checks that the list is complete."""
    out = copy.deepcopy(msg)
    if name == "factsheet":
        limits = out.setdefault("protocolLimits", {})
        limits.setdefault("maxStringLens", {})           # every member is optional: no explicit limit
        limits.setdefault("maxArrayLens", {})
    elif name == "instantActions":
        for a in out.get("actions", []):
            a.setdefault("actionName", a.get("actionType"))   # defect of the 2.0.0 schema, see SOURCE.md
            a["actionParameters"] = [p for p in a.get("actionParameters", []) if p.get("value") is not None]
    return out


def invalid_examples() -> list[tuple[str, dict, str]]:
    out = []
    for name, path, value, expected in MUTATIONS:
        for base in valid_examples()[name][:6]:
            changed = mutate(base, path, value)
            if changed is not None:
                out.append((name, changed, expected))
    return out


def load_tests(loader, tests, ignore):
    tests.addTests(doctest.DocTestSuite(schema_mod))
    return tests


class LoadingTests(unittest.TestCase):
    def test_six_topics_load_and_compile(self):
        schemas = load_vda5050_schemas()
        self.assertEqual(set(schemas), set(TOPICS))
        for name, s in schemas.items():
            with self.subTest(topic=name):
                self.assertEqual(s["$schema"], "https://json-schema.org/draft/2020-12/schema")
                compile_schema(s)

    def test_each_load_returns_a_fresh_copy(self):
        first = load_vda5050_schemas()
        first["state"]["required"].clear()
        self.assertIn("batteryState", load_vda5050_schemas()["state"]["required"])

    def test_files_match_recorded_upstream_blobs(self):
        """SOURCE.md records the git blob id and SHA-256 of every file as taken from the 2.0.0 tag."""
        rows = re.findall(r"^\| `([^`]+)` \| (\w+) \| `([0-9a-f]{40})` \| `([0-9a-f]{64})` \|$",
                          (SCHEMA_DIR / "SOURCE.md").read_text(encoding="utf-8"), re.M)
        self.assertEqual({r[1] for r in rows}, set(TOPICS))
        self.assertEqual({r[0] for r in rows}, {p.name for p in SCHEMA_DIR.iterdir() if p.name != "SOURCE.md"})
        for file_name, _, blob, sha256 in rows:
            data = (SCHEMA_DIR / file_name).read_bytes()
            with self.subTest(file=file_name):
                self.assertEqual(hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(), blob)
                self.assertEqual(hashlib.sha256(data).hexdigest(), sha256)

    def test_published_factsheet_is_rejected_as_is_and_repaired_on_load(self):
        raw = json.loads((SCHEMA_DIR / "factsheet.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(SchemaError, "misplaced.*headerId"):
            compile_schema(raw)
        repaired = load_vda5050_schemas()["factsheet"]
        self.assertIn("typeSpecification", repaired["properties"])
        limits = repaired["properties"]["protocolLimits"]["properties"]
        self.assertIn("order.nodes", limits["maxArrayLens"]["properties"])
        msg = mutate(MINIMAL["factsheet"], ("protocolLimits", "maxArrayLens"), {"order.nodes": "many"})
        self.assertEqual(validate_message("factsheet", msg),
                         ['$.protocolLimits.maxArrayLens["order.nodes"]: "many" is not of type integer'])

    def test_unknown_version_and_topic(self):
        with self.assertRaises(FileNotFoundError):
            load_vda5050_schemas("9.9.9")
        with self.assertRaises(KeyError):
            validate_message("facility", {})


class CompileTests(unittest.TestCase):
    def test_unsupported_keywords_fail_at_compile_time(self):
        cases = [
            ({"type": "object", "propertyNames": {"pattern": "^x"}}, "propertyNames"),
            ({"properties": {"a": {"typ": "string"}}}, r"#/properties/a: .*'typ'"),
            ({"items": [{"type": "string"}]}, "array form"),
            ({"$ref": "https://example.com/other.json"}, "same document"),
            ({"$ref": "#/definitions/missing"}, "does not resolve"),
            ({"$ref": "#/definitions/b/type", "definitions": {"b": True}}, "does not resolve"),
            ({"type": "float"}, "unknown type"),
            ({"minimum": "0"}, "must be a number"),
            ({"definitions": {"unused": {"uniqueItems": True}}}, "uniqueItems"),   # unused definitions are checked too
            ({"anyOf": [{"type": "string"}, {"if": {}}]}, r"#/anyOf/1: .*'if'"),
            ({"$ref": "#"}, "cycle"),                                    # would recurse without end when validating
            ({"anyOf": [{"type": "string"}, {"$ref": "#"}]}, "cycle"),
            ({"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"allOf": [{"$ref": "#/$defs/a"}]}}}, "cycle"),
            ({"$schema": "http://json-schema.org/draft-07/schema#"}, "only https://json-schema.org/draft/2020-12"),
            ({"$defs": {"x": {"$id": "https://example.com/x"}}}, "embedded schema resources"),
            ({"properties": []}, "properties must be an object"),
            ({"definitions": [{}]}, "definitions must be an object"),
            ({"required": "a"}, "must list strings"),
        ]
        for schema, pattern in cases:
            with self.subTest(schema=schema):
                with self.assertRaisesRegex(SchemaError, pattern):
                    compile_schema(schema)

    def test_recursion_that_descends_into_the_instance_compiles(self):
        tree = {"$ref": "#", "type": "object", "properties": {"child": {"$ref": "#"}}, "required": ["v"]}
        self.assertRaisesRegex(SchemaError, "cycle", compile_schema, tree)          # "$ref": "#" at the root itself
        tree = {"type": "object", "properties": {"child": {"$ref": "#"}}, "required": ["v"]}
        self.assertEqual(validate(tree, {"v": 1, "child": {"child": {"v": 3}}}),
                         ["$.child: missing required property 'v'"])

    def test_repair_never_moves_json_schema_keywords(self):
        with self.assertRaisesRegex(SchemaError, "only misplaced property definitions"):
            schema_mod._relocate_properties({"type": "object", "not": {}, "x": {"type": "string"}}, "")

    def test_keyword_semantics(self):
        for schema, instance, expected in KEYWORD_CASES + OWN_ONLY_CASES:
            with self.subTest(schema=schema, instance=instance):
                problems = validate(schema, instance)
                self.assertEqual(not problems, expected, problems)

    def test_problems_carry_paths(self):
        schema = {"type": "object", "properties": {"nodes": {"type": "array", "items": {
            "type": "object", "required": ["nodeId"], "properties": {"sequenceId": {"type": "integer"}}}}}}
        self.assertEqual(validate(schema, {"nodes": [{"nodeId": "a"}, {"sequenceId": "1"}]}),
                         ["$.nodes[1]: missing required property 'nodeId'",
                          '$.nodes[1].sequenceId: "1" is not of type integer'])


class Vda5050MessageTests(unittest.TestCase):
    def test_valid_examples_pass(self):
        """The minimal examples are independent of the validator. The simulation messages among them were
        selected by it, so their independent check is the comparison with jsonschema further down."""
        for name, examples in valid_examples().items():
            for i, msg in enumerate(examples):
                with self.subTest(topic=name, example=i):
                    self.assertEqual(validate_message(name, msg), [])

    def test_mutations_are_rejected_with_their_path(self):
        applied = {m: 0 for m in range(len(MUTATIONS))}
        for m, (name, path, value, expected) in enumerate(MUTATIONS):
            for base in valid_examples()[name][:6]:
                changed = mutate(base, path, value)
                if changed is None:
                    continue
                applied[m] += 1
                with self.subTest(topic=name, path=path):
                    problems = validate_message(name, changed)
                    self.assertTrue(any(expected in p for p in problems), problems)
        self.assertEqual([m for m, n in applied.items() if n == 0], [], "every mutation applies to its minimal example")

    def test_reported_fixes_cover_every_simulation_message(self):
        for name, msgs in sim_messages().items():
            self.assertTrue(msgs, f"the short run published no {name}")
            left = [(i, validate_message(name, apply_reported_fixes(name, m))[:2]) for i, m in enumerate(msgs)]
            with self.subTest(topic=name):
                self.assertEqual([x for x in left if x[1]], [])

    def test_properties_of_the_2_0_0_schemas(self):
        """Rules of the published 2.0.0 schemas that differ from the specification text, kept as published."""
        # extra fields are allowed everywhere: no schema uses additionalProperties
        self.assertEqual(validate_message("state", {**MINIMAL["state"], "x_safetyField": {"state": "CLEAR"}}), [])
        # instant actions need actionName, not the actionType of the specification text
        spec_style = mutate(MINIMAL["instantActions"], ("actions", 0),
                            {"actionType": "startPause", "actionId": "a", "blockingType": "HARD"})
        self.assertEqual(validate_message("instantActions", spec_style),
                         ["$.actions[0]: missing required property 'actionName'"])
        # actionStatus has no PAUSED although the specification text defines it
        paused = mutate(MINIMAL["state"], ("actionStates",), [{"actionId": "a", "actionStatus": "PAUSED"}])
        self.assertEqual(len(validate_message("state", paused)), 1)
        # visualization and instantActions have no required header fields
        self.assertEqual(validate_message("visualization", {}), [])
        self.assertEqual(validate_message("instantActions", {}), [])
        # format is an annotation: a malformed timestamp is not a schema violation
        self.assertEqual(validate_message("connection", {**MINIMAL["connection"], "timestamp": "yesterday"}), [])


@unittest.skipIf(jsonschema is None, "jsonschema is not installed")
class ReferenceImplementationTests(unittest.TestCase):
    """Pass/fail verdicts must match the reference implementation on the same schemas and instances."""

    def assert_same_verdicts(self, cases: list[tuple[str, object, object]]) -> None:
        validators: dict[int, object] = {}
        disagreements = []
        for label, schema, instance in cases:
            ref = validators.setdefault(id(schema), jsonschema.Draft202012Validator(schema))
            own_ok = not validate(schema, instance)
            if own_ok != ref.is_valid(instance):
                disagreements.append((label, own_ok, validate(schema, instance)[:2]))
        self.assertEqual(disagreements, [])

    def test_vda5050_corpus(self):
        schemas = load_vda5050_schemas()
        cases: list[tuple[str, object, object]] = []
        for name, msgs in sim_messages().items():
            cases += [(f"sim {name}", schemas[name], m) for m in msgs]   # includes messages that fail today
        for name, msgs in valid_examples().items():
            cases += [(f"valid {name}", schemas[name], m) for m in msgs]
        cases += [(f"mutated {name}: {expected}", schemas[name], m) for name, m, expected in invalid_examples()]
        cases += [(f"fixed {name}", schemas[name], apply_reported_fixes(name, m))
                  for name, msgs in sim_messages().items() for m in msgs[:50]]
        kinds = {(label.split()[0], label.split()[1].rstrip(":")) for label, _, _ in cases}
        for kind in ("sim", "valid", "mutated", "fixed"):
            self.assertEqual({t for k, t in kinds if k == kind}, set(TOPICS), f"{kind} cases per topic")
        self.assert_same_verdicts(cases)

    def test_keyword_cases(self):
        self.assert_same_verdicts([(f"case {i}", s, x) for i, (s, x, _) in enumerate(KEYWORD_CASES)])


if __name__ == "__main__":
    unittest.main()
