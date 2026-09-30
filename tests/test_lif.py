"""VDMA LIF export and import: official schema, round trip, repository site files, malformed input."""
from __future__ import annotations

import copy
import doctest
import json
import math
from pathlib import Path
import re
import tempfile
import unittest
import warnings

from fleet import lif as lif_mod
from fleet.layout import Edge, Layout, Node
from fleet.lif import (STATION_ACTIONS, LIFError, LIFWarning, export_lif, export_site_extras, import_lif, lane_key,
                       validate_lif)
from fleet.site_dc import dc_layout
from tools.export_lif import SITES, export, site_documents

try:
    import jsonschema
except ImportError:      # the runtime is stdlib-only; the schema check runs where jsonschema exists
    jsonschema = None

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "fleet" / "schemas" / "lif" / "LIF.schema"
STAMP = "2026-01-01T00:00:00.00Z"
VT = {"FORKLIFT": {"vehicleTypeId": "F"}, "CARRIER": {"vehicleTypeId": "C"}}


def load_tests(loader, tests, ignore):
    tests.addTests(doctest.DocTestSuite(lif_mod))
    return tests


def docs(site: str) -> tuple[Layout, dict, dict]:
    lif, extras = site_documents(site, "1", STAMP)
    return SITES[site]["build"](), lif, extras


def tiny_layout(prefix: str = "", classes: frozenset | None = None) -> Layout:
    """Four nodes and every edge shape LIF alone cannot restore: a curved edge longer than the straight
    line, a non-standard lane key, a two-way flag without a reverse edge and an explicit set of every
    class. ``classes`` overrides the class set of every edge."""
    nodes = {prefix + n: Node(prefix + n, x, y, k) for n, x, y, k in
             (("A", 0.0, 0.0, "park"), ("B", 4.0, 0.0, "cross"), ("C", 4.0, 3.0, "charger"), ("D", 0.0, 3.0, "cross"))}
    rows = [("A", "B", None, 1.0, None, False, frozenset()),
            ("B", "A", None, 1.0, None, False, frozenset()),
            ("B", "C", 5.0, 0.5, None, True, frozenset({"CARRIER"})),
            ("C", "D", None, 0.5, "north-lane", True, frozenset({"CARRIER"})),
            ("D", "A", None, 1.0, None, False, frozenset({"FORKLIFT", "CARRIER"}))]
    edges = {}
    for a, b, length, speed, lane, oneway, cls in rows:
        a, b = prefix + a, prefix + b
        straight = round(math.hypot(nodes[a].x - nodes[b].x, nodes[a].y - nodes[b].y), 3)
        edges[f"{a}>{b}"] = Edge(f"{a}>{b}", a, b, length or straight, speed, lane or lane_key(a, b), oneway,
                                 cls if classes is None else classes)
    return Layout(nodes, edges, [], {}, {}, width=5.0, height=4.0, site="tiny")


def tiny_docs(prefix: str = "", classes: frozenset | None = None, layout_id: str = "tiny") -> tuple[dict, dict]:
    layout = tiny_layout(prefix, classes)
    lif = export_lif(layout, VT, layout_id, layout_id, "1", timestamp=STAMP)
    return json.loads(json.dumps(lif)), json.loads(json.dumps(export_site_extras(layout, layout_id, VT)))


class RoundTripTests(unittest.TestCase):
    @unittest.skipIf(jsonschema is None, "jsonschema is not installed")
    def test_export_passes_official_schema(self):
        validator = jsonschema.Draft7Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
        for site in SITES:
            with self.subTest(site=site):
                _, lif, _ = docs(site)
                self.assertEqual([f"{list(e.path)}: {e.message}" for e in validator.iter_errors(lif)], [])

    def test_export_meets_specification_rules(self):
        for site in SITES:
            with self.subTest(site=site):
                self.assertEqual(validate_lif(docs(site)[1]), [])

    def test_import_restores_layout(self):
        for site in SITES:
            layout, lif, extras = docs(site)
            with warnings.catch_warnings():
                warnings.simplefilter("error", LIFWarning)      # our own files use nothing it cannot apply
                got = import_lif(lif, extras)
            for name in ("nodes", "edges", "zones", "doors", "racks", "coverage", "walls", "no_stop",
                         "width", "height", "site", "two_way"):
                with self.subTest(site=site, field=name):
                    self.assertEqual(getattr(got, name), getattr(layout, name))
            with self.subTest(site=site, field="check"):
                self.assertEqual(got.check(), [])
            with self.subTest(site=site, field="whole layout incl. edge order"):
                self.assertTrue(got == layout)

    def test_lif_carries_classes_speed_direction_and_stations(self):
        layout, lif, _ = docs("dc")
        lay = lif["layouts"][0]
        ids = {c: vt["vehicleTypeId"] for c, vt in SITES["dc"]["vehicleTypes"].items()}
        edges = {e["edgeId"]: e for e in lay["edges"]}
        pairs = {(e["startNodeId"], e["endNodeId"]) for e in lay["edges"]}
        self.assertEqual(set(edges), set(layout.edges))
        self.assertTrue(any(e.classes for e in layout.edges.values()))
        for e in layout.edges.values():
            props = edges[e.id]["vehicleTypeEdgeProperties"]
            self.assertEqual({p["vehicleTypeId"] for p in props}, {ids[c] for c in (e.classes or ids)}, e.id)
            self.assertTrue(all(p["maxSpeed"] == e.max_speed for p in props), e.id)
            # a two-way lane is two directed edges, a one-way lane only one
            self.assertEqual((e.end, e.start) in pairs, not e.oneway, e.id)
        station_nodes = {n for s in lay["stations"] for n in s["interactionNodeIds"]}
        self.assertEqual(station_nodes, set(layout.nodes_of_kind(*STATION_ACTIONS)))
        nodes = {n["nodeId"]: n for n in lay["nodes"]}
        for cid in layout.nodes_of_kind("charger"):
            actions = {a["actionType"] for p in nodes[cid]["vehicleTypeNodeProperties"] for a in p["actions"]}
            self.assertIn("startCharging", actions)

    def test_lif_alone_is_routable(self):
        layout, lif, _ = docs("dc")
        ids = {c: vt["vehicleTypeId"] for c, vt in SITES["dc"]["vehicleTypes"].items()}
        got = import_lif(lif)
        for e in layout.edges.values():
            g = got.edges[e.id]
            self.assertEqual((g.start, g.end, g.length, g.max_speed, g.lane, g.oneway),
                             (e.start, e.end, e.length, e.max_speed, e.lane, e.oneway))
            # without the sidecar the vehicle type lists stay closed and vehicleTypeIds stand in for classes
            self.assertEqual(g.classes, frozenset(ids[c] for c in (e.classes or ids)))
        self.assertEqual(got.check(), [])
        self.assertEqual(got.nodes_of_kind("charger"), layout.nodes_of_kind("charger"))
        self.assertEqual((got.zones, got.doors), ({}, {}))


class SidecarEdgeTests(unittest.TestCase):
    def test_edges_lif_cannot_describe_come_back_from_sidecar(self):
        layout = tiny_layout()
        lif, extras = tiny_docs()
        # curved length, non-standard lane, two-way flag without reverse edge, explicit full class set
        self.assertEqual(set(extras["edges"]), {"B>C", "C>D", "D>A"})
        got = import_lif(lif, extras)
        self.assertEqual((got.nodes, got.edges), (layout.nodes, layout.edges))

    def test_restriction_to_one_of_two_types_survives(self):
        lif, extras = tiny_docs(classes=frozenset({"CARRIER"}))
        for side in (extras, None):
            got = import_lif(lif, side)
            with self.subTest(sidecar=side is not None):
                self.assertTrue(all(not got.allowed(e, "FORKLIFT") and not got.allowed(e, "F")
                                    for e in got.edges.values()))

    def test_differing_type_speeds_collapse_to_lowest(self):
        lif, extras = tiny_docs()
        props = lif["layouts"][0]["edges"][0]["vehicleTypeEdgeProperties"]
        self.assertEqual({p["vehicleTypeId"] for p in props}, {"F", "C"})
        props[0]["maxSpeed"] = 0.4
        self.assertEqual(import_lif(lif, extras).edges["A>B"].max_speed, 0.4)


class MultiLayoutTests(unittest.TestCase):
    def setUp(self):
        self.doc, self.extras_a = tiny_docs("a", frozenset({"CARRIER"}), "A")
        self.doc["layouts"].append(tiny_docs("b", layout_id="B")[0]["layouts"][0])

    def test_layout_is_chosen_by_id_and_keeps_its_restrictions(self):
        self.assertEqual(validate_lif(self.doc), [])
        with self.assertRaisesRegex(LIFError, "pass layout_id"):
            import_lif(self.doc)
        with self.assertRaisesRegex(LIFError, "no layout Z"):
            import_lif(self.doc, layout_id="Z")
        for extras in (self.extras_a, None):
            got = import_lif(self.doc, extras, layout_id="A")
            self.assertEqual(set(got.nodes), {"aA", "aB", "aC", "aD"})
            self.assertFalse(any(got.allowed(e, "FORKLIFT") or got.allowed(e, "F") for e in got.edges.values()))

    def test_edge_into_another_layout_is_refused(self):
        self.doc["layouts"][0]["edges"][0]["endNodeId"] = "bB"
        self.assertEqual(validate_lif(self.doc), [])      # legal LIF: a transition between layouts
        with self.assertRaisesRegex(LIFError, "another layout"):
            import_lif(self.doc, layout_id="A")


class SiteFileTests(unittest.TestCase):
    def test_repository_files_match_code(self):
        for site in SITES:
            with self.subTest(site=site):
                lif_path, extras_path = ROOT / "sites" / f"{site}.lif.json", ROOT / "sites" / f"{site}.site.json"
                on_disk = json.loads(lif_path.read_text(encoding="utf-8"))
                lif, extras = site_documents(site, on_disk["layouts"][0]["layoutVersion"],
                                             on_disk["metaInformation"]["exportTimestamp"])
                hint = f"sites/{site}.* are stale; run: python3 tools/export_lif.py --site {site} " \
                       f"--out sites/{site}.lif.json"
                self.assertTrue(on_disk == lif, hint)
                self.assertTrue(json.loads(extras_path.read_text(encoding="utf-8")) == extras, hint)
                self.assertEqual(import_lif(on_disk, extras).check(), [])

    def test_export_bumps_version_only_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, side = Path(tmp) / "grid.lif.json", Path(tmp) / "grid.site.json"
            self.assertIn("LIF updated (layoutVersion 1)", export("grid", out, side))
            first = out.read_bytes()
            self.assertIn("LIF unchanged (layoutVersion 1)", export("grid", out, side))
            self.assertEqual(out.read_bytes(), first)
            doc = json.loads(first)
            doc["layouts"][0]["nodes"][0]["nodePosition"]["x"] += 1.0
            out.write_text(json.dumps(doc), encoding="utf-8")
            self.assertIn("LIF updated (layoutVersion 2)", export("grid", out, side))
            doc["layouts"][0]["layoutVersion"] = "2a"
            out.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(SystemExit):
                export("grid", out, side)


class MalformedLIFTests(unittest.TestCase):
    def setUp(self):
        self.lif, self.extras = tiny_docs()

    def test_edge_to_missing_node(self):
        _, lif, extras = docs("grid")
        edge = lif["layouts"][0]["edges"][0]
        edge["endNodeId"] = "NOWHERE"
        with self.assertRaisesRegex(LIFError, rf"edge {re.escape(edge['edgeId'])}: end node NOWHERE is unknown"):
            import_lif(lif, extras)

    def test_duplicate_node_id(self):
        _, lif, _ = docs("grid")
        nodes = lif["layouts"][0]["nodes"]
        nodes.append(copy.deepcopy(nodes[5]))
        with self.assertRaisesRegex(LIFError, rf"duplicate nodeId {re.escape(nodes[5]['nodeId'])}"):
            import_lif(lif)

    def test_wrong_types_are_reported_not_raised(self):
        def lay(d):
            return d["layouts"][0]

        cases = {
            "layoutId list": lambda d: lay(d).update(layoutId=["x"]),
            "node vehicleTypeId list": lambda d: lay(d)["nodes"][0]["vehicleTypeNodeProperties"][0].update(
                vehicleTypeId=["a"]),
            "edge vehicleTypeId object": lambda d: lay(d)["edges"][0]["vehicleTypeEdgeProperties"][0].update(
                vehicleTypeId={}),
            "startNodeId list": lambda d: lay(d)["edges"][0].update(startNodeId=["A"]),
            "endNodeId object": lambda d: lay(d)["edges"][0].update(endNodeId={}),
            "interaction node list": lambda d: lay(d)["stations"][0].update(interactionNodeIds=[["A"]]),
            "actions number": lambda d: lay(d)["nodes"][0]["vehicleTypeNodeProperties"][0].update(actions=5),
            "lifVersion number": lambda d: d["metaInformation"].update(lifVersion=1),
            "no layouts": lambda d: d.update(layouts=[]),
            "no nodes": lambda d: lay(d).update(nodes=[], edges=[], stations=[]),
            "empty document": lambda d: d.clear(),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                doc = copy.deepcopy(self.lif)
                mutate(doc)
                self.assertNotEqual(validate_lif(doc), [])
                with self.assertRaises(LIFError):
                    import_lif(doc, self.extras)

    def test_malformed_sidecar(self):
        cases = {
            "list": lambda x: [x],
            "nodeKinds list": lambda x: {**x, "nodeKinds": []},
            "override not an object": lambda x: {**x, "edges": {"A>B": 5}},
            "override length text": lambda x: {**x, "edges": {"A>B": {"length": "abc"}}},
            "door not an object": lambda x: {**x, "doors": ["x"]},
            "width text": lambda x: {**x, "width": "88"},
            "vehicle type unmapped": lambda x: {**x, "vehicleTypes": {"C": "CARRIER"}},
            "other layout": lambda x: {**x, "layoutId": "dc"},
            "zone without polygon": lambda x: {**x, "zones": [{"id": "Z", "name": "Z", "kind": "aisle"}]},
        }
        for label, mutate in cases.items():
            with self.subTest(label), self.assertRaises(LIFError):
                import_lif(self.lif, mutate(copy.deepcopy(self.extras)))

    def test_member_it_cannot_apply_warns_or_fails_in_strict_mode(self):
        prop = self.lif["layouts"][0]["edges"][0]["vehicleTypeEdgeProperties"][0]
        prop["loadRestriction"] = {"loaded": False, "unloaded": True}
        with self.assertWarnsRegex(LIFWarning, "loadRestriction x1"):
            import_lif(self.lif, self.extras)
        with self.assertRaisesRegex(LIFError, "loadRestriction"):
            import_lif(self.lif, self.extras, strict=True)

    def test_edge_without_speed_needs_default(self):
        edge = self.lif["layouts"][0]["edges"][0]
        for p in edge["vehicleTypeEdgeProperties"]:
            del p["maxSpeed"]
        with self.assertRaisesRegex(LIFError, "no maxSpeed"):
            import_lif(self.lif, self.extras)
        with self.assertRaisesRegex(LIFError, "default_speed"):
            import_lif(self.lif, self.extras, default_speed=0.0)
        self.assertEqual(import_lif(self.lif, self.extras, default_speed=0.8).edges[edge["edgeId"]].max_speed, 0.8)

    def test_export_needs_every_restricted_class(self):
        with self.assertRaisesRegex(ValueError, "FORKLIFT"):
            export_lif(dc_layout(), {"CARRIER": {"vehicleTypeId": "AMR-TOTE"}}, "dc", "dc", "1")


if __name__ == "__main__":
    unittest.main()
