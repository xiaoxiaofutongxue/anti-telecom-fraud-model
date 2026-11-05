import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET

import networkx as nx
import numpy as np

from fourflow.core import load_dataset,build_case_graph,temporal_paths,parse_time
from fourflow.analytics import amount_summary,principal_scenario,detect_patterns
from fourflow.synthetic import create_dataset
from fourflow.store import Store
from fourflow.service import analysis_graph
from fourflow.model import TemporalHeteroGNN,encode_graph,FEATURE_NAMES,train_model
from fourflow.reporting import export_report,csv_cell

ROOT=Path(__file__).resolve().parents[1]


class SystemTests(unittest.TestCase):
    def setUp(self):
        (ROOT/"runtime"/"test_workspace").mkdir(parents=True,exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(prefix="fourflow_test_",dir=ROOT/"runtime"/"test_workspace")
        self.base=Path(self.temporary.name)
        self.dataset_path=create_dataset(self.base/"data",3)
        self.dataset=load_dataset(self.dataset_path)
        self.store=Store(self.base/"audit.sqlite3")
        self.graph=build_case_graph(self.dataset,"C001")

    def tearDown(self):self.temporary.cleanup()

    def test_four_flows_and_provenance(self):
        self.assertEqual({d["flow"] for u,v,k,d in self.graph.edges(keys=True,data=True)},{"funds","goods","information","people"})
        for u,v,key,data in self.graph.edges(keys=True,data=True):
            self.assertIsNotNone(self.dataset.source("C001",data["source_id"]))
        self.assertNotEqual(self.dataset.fingerprint,"")

    def test_pending_identities_excluded(self):
        self.assertFalse(any(d.get("verified")=="pending" for u,v,k,d in self.graph.edges(keys=True,data=True)))
        candidate=build_case_graph(self.dataset,"C001",include_unverified=True)
        self.assertTrue(any(d.get("verified")=="pending" for u,v,k,d in candidate.edges(keys=True,data=True)))

    def test_currency_and_derived_edge_deduplication(self):
        result=amount_summary(self.graph)
        expected=sum(r["_amount"] for r in self.dataset.rows("funds","C001") if r["currency"]=="USDT")
        self.assertEqual(result["gross_by_currency"]["USDT"],expected)
        self.assertEqual(str(result["gross_by_currency"]["CNY"]),"700.00")
        self.assertEqual(sum(r["net"] for r in result["addresses"]),0)

    def test_as_of_prevents_future_edges(self):
        cutoff="2026-01-01T08:05:00+08:00"
        graph=build_case_graph(self.dataset,"C001",as_of=cutoff)
        self.assertTrue(all(parse_time(d["timestamp"])<=parse_time(cutoff) for u,v,k,d in graph.edges(keys=True,data=True)))
        self.assertLess(graph.number_of_edges(),self.graph.number_of_edges())

    def test_temporal_path_and_specific_transactions(self):
        graph=nx.MultiDiGraph()
        def add(u,v,key,time):graph.add_edge(u,v,key=key,relation="transfer",timestamp=time,currency="USDT",verified="recorded")
        add("A","B","T1","2026-01-01T09:00:00+08:00")
        add("A","B","T2","2026-01-01T09:10:00+08:00")
        add("B","C","T3","2026-01-01T08:59:00+08:00")
        self.assertEqual(temporal_paths(graph,"A","C",currency="USDT")["paths"],[])
        self.assertEqual(len(temporal_paths(graph,"A","B",currency="USDT")["paths"]),2)
        self.assertEqual(len(temporal_paths(graph,"A","C",chronological=False)["paths"]),2)
        with self.assertRaises(ValueError):temporal_paths(graph,"A","MISSING")

    def test_duplicate_and_invalid_amount_rejected(self):
        path=self.dataset_path/"funds.csv"
        content=path.read_text(encoding="utf-8-sig"); lines=content.splitlines()
        path.write_text(content+lines[1]+"\n",encoding="utf-8-sig")
        with self.assertRaisesRegex(ValueError,"编号重复"):load_dataset(self.dataset_path)
        rows=list(csv.DictReader(io.StringIO(content)))
        rows[0]["amount"]="NaN"
        with path.open("w",encoding="utf-8-sig",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        with self.assertRaises(ValueError):load_dataset(self.dataset_path)

    def test_source_traversal_and_content_fingerprint(self):
        first=self.dataset.fingerprint
        proof=self.dataset_path/"evidence"/"C001.txt"
        proof.write_text(proof.read_text(encoding="utf-8")+"\n修订",encoding="utf-8")
        self.assertNotEqual(first,load_dataset(self.dataset_path).fingerprint)
        sources=self.dataset_path/"sources.csv"
        sources.write_text(sources.read_text(encoding="utf-8-sig").replace("evidence/C001.txt","../outside.txt"),encoding="utf-8-sig")
        with self.assertRaises(ValueError):load_dataset(self.dataset_path)

    def test_review_changes_only_target_identity_and_not_history(self):
        self.store.review(self.dataset.fingerprint,"C001","identity:I4","已核实线索","S001：已人工核对控制关系","验证人员")
        now=analysis_graph(self.dataset,"C001",self.store)
        self.assertTrue(any(d["record_id"]=="I4" and d["verified"]=="verified" for u,v,k,d in now.edges(keys=True,data=True)))
        historical=analysis_graph(self.dataset,"C001",self.store,"2026-01-02T00:00:00+08:00")
        self.assertFalse(any(d["record_id"]=="I4" for u,v,k,d in historical.edges(keys=True,data=True)))
        self.assertFalse(any(d["record_id"]=="R2" for u,v,k,d in now.edges(keys=True,data=True)))

    def test_stable_finding_identifiers(self):
        full=detect_patterns(self.graph)
        before=build_case_graph(self.dataset,"C001",as_of="2026-01-01T09:00:00+08:00")
        partial=detect_patterns(before)
        shared={(r['kind'],tuple(r['nodes']),tuple(r["record_ids"])):r["id"] for r in full}
        for row in partial:
            key=(row['kind'],tuple(row['nodes']),tuple(row['record_ids']))
            if key in shared:self.assertEqual(row['id'],shared[key])

    def test_principal_bounds_and_invalid_inputs(self):
        result=principal_scenario("100","0.5","0.8","USDT",["S001"])
        self.assertEqual((result["lower"],result["upper"]),("125.00","200.00"))
        with self.assertRaises(ValueError):principal_scenario("100",0,.8,"USDT",["S001"])
        with self.assertRaises(ValueError):principal_scenario("100",.5,.8,"USDT",[])

    def test_gnn_nonzero_gradients_match_finite_difference(self):
        model=TemporalHeteroGNN(); tensor=encode_graph(self.graph)
        _,grad=model.gradient(tensor,1)
        for key in ["self1","r1_transfer","r2_transfer","out"]:
            index=np.unravel_index(np.abs(grad[key]).argmax(),grad[key].shape)
            self.assertGreater(abs(grad[key][index]),1e-10)
            initial=model.params[key][index];eps=1e-5
            model.params[key][index]=initial+eps;z=model.forward(tensor)[0];plus=np.logaddexp(0,z)-z
            model.params[key][index]=initial-eps;z=model.forward(tensor)[0];minus=np.logaddexp(0,z)-z
            model.params[key][index]=initial
            self.assertAlmostEqual((plus-minus)/(2*eps),grad[key][index],places=6)

    def test_report_bundle_escapes_content_and_has_checksums(self):
        self.dataset.tables["cases"][0]["title"]='<script>alert(1)</script>'
        output=export_report(self.dataset,self.graph,self.store,self.base/"exports")
        html=(output/"report.html").read_text(encoding="utf-8")
        self.assertNotIn('<script>alert(1)</script>',html)
        self.assertIn("&lt;script&gt;",html)
        ET.parse(output/"graph.svg")
        checks=json.loads((output/"checksums.json").read_text())
        for name,expected in checks.items():self.assertEqual(hashlib.sha256((output/name).read_bytes()).hexdigest(),expected)
        self.assertTrue(output.with_suffix(".zip").is_file())
        self.assertTrue(csv_cell("=1+1").startswith("'"))

    def test_model_serialization_integrity_and_real_data_guard(self):
        model=TemporalHeteroGNN()
        model.metadata={"hidden":12,"feature_names":FEATURE_NAMES,"origin":"synthetic","model_id":"validation-model"}
        directory=self.base/"model";model.save(directory)
        loaded=TemporalHeteroGNN.load(directory)
        self.assertAlmostEqual(model.probability(self.graph),loaded.probability(self.graph),places=12)
        self.dataset.manifest["origin"]="real"
        self.assertFalse(loaded.assess(self.dataset,self.graph)["available"])
        weights=directory/"weights.npz"
        weights.write_bytes(weights.read_bytes()+b"tamper")
        with self.assertRaisesRegex(ValueError,"校验失败"):TemporalHeteroGNN.load(directory)

    def test_training_rejects_insufficient_labels_and_shared_accounts(self):
        with self.assertRaisesRegex(ValueError,"20"):train_model(self.dataset,self.base/"model",epochs=1)
        training=load_dataset(create_dataset(self.base/"training",24))
        training.tables["funds"][0]["from_id"]="C020-W00"
        with self.assertRaisesRegex(ValueError,"共享地址"):train_model(training,self.base/"model",epochs=1)


if __name__=="__main__":unittest.main(verbosity=2)
