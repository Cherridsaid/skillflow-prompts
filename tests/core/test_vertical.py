from skillflow.bootstrap import create_demo
from skillflow.demo import DemoSession, run_demo


def test_vertical_demo_preserves_evidence_and_rejects_changed_bytes(tmp_path):
    result = run_demo(tmp_path / "demo")
    assert result["receipt_verified"] and result["modified_file_rejected"]
    states = {x["label"]: x["status"] for x in result["trace"]}
    assert states["validated"]["validated"] == 2
    assert states["invalidated_and_delivery_refused"]["validated"] == 0
    assert states["resumed_without_reset"]["attempts"] == 3
    assert states["delivered"]["attempts"] == 5


def test_two_other_contracts_work_without_core_changes(tmp_path):
    root = create_demo(tmp_path / "other")
    quality = DemoSession(root, "demo-quality")
    quality.submit('{"name":"Synthetic widget","quantity":4}', "application/json", "item.json")
    quality.check("validate-data")
    quality.deliver(approve=False)
    assert quality.status()["state"] == "DELIVERED"
    third = DemoSession(root, "demo-editorial")
    third.submit("Summary\nExample release.\nLimitations\nNo semantic assurance.")
    third.check("headings")
    third.deliver(approve=False)
    assert third.status()["state"] == "DELIVERED"
