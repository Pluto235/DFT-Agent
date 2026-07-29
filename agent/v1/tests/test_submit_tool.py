import json
from pathlib import Path
from agent.tools.submit import write_result_json


def test_basic_submit(tmp_path):
    out_path = tmp_path / "result.json"
    payload = {
        "task_outputs": {"vc_relax_out": "runs/si/vc.out"},
        "notes": "done",
    }
    write_result_json(out_path, payload)
    data = json.loads(out_path.read_text())
    assert data == payload


def test_submit_overwrites(tmp_path):
    out_path = tmp_path / "result.json"
    write_result_json(out_path, {"task_outputs": {}, "notes": "old"})
    write_result_json(out_path, {"task_outputs": {"k": "v"}, "notes": "new"})
    data = json.loads(out_path.read_text())
    assert data["notes"] == "new"


def test_submit_creates_parent_dir(tmp_path):
    out_path = tmp_path / "nested/result.json"
    write_result_json(out_path, {"task_outputs": {}, "notes": ""})
    assert out_path.exists()
