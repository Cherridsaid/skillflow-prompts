from pathlib import Path
from types import SimpleNamespace

from skillflow.benchmarks import run, update_readmes
from skillflow.reviewer import run_codex_review


def test_reviewer_preserves_original_crlf_bytes(monkeypatch):
    raw = b'{\r\n"verdict":"JUSTE",\r\n"reason":"synthetic",\r\n"references":[],\r\n"obligations":{}\r\n}'

    def execute(command, directory, env, timeout, prompt):
        # The frozen packet is too large for a command-line argument, so it reaches the
        # engine on stdin. The reviewer must still send it, whatever the engine.
        assert prompt, "the reviewer must hand the engine its prompt"
        target = Path(command[command.index("--output-last-message") + 1])
        target.write_bytes(raw)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("skillflow.reviewer._run_bounded", execute)
    monkeypatch.setattr("skillflow.reviewer.shutil.which", lambda name, path=None: "/engine")
    observed = {}

    class Client:
        def call(self, action, arguments, *_):
            if action == "review_read":
                return {
                    "ok": True,
                    "result": {"objects": [], "obligations": {}, "instruction": "synthetic"},
                }
            if action == "flow_status":
                return {"ok": True, "result": {"revision": 1}}
            observed.update(arguments)
            return {"ok": True}

    run_codex_review(Client(), "case", "review", model="synthetic-not-called")
    assert observed["raw"].encode() == raw


def test_readme_generator_preserves_editorial_bytes(tmp_path):
    campaign = tmp_path / "campaign"
    run(campaign, "agents", 1, Path.cwd())
    before = b"Editorial\r\n\r\n"
    after = b"\r\nUntouched ending.\r\n"
    for filename in ("README.md", "README.fr.md"):
        (tmp_path / filename).write_bytes(
            before + b"<!-- MEASUREMENTS:START -->\r\nold\r\n<!-- MEASUREMENTS:END -->" + after
        )
    update_readmes(tmp_path, campaign)
    for filename in ("README.md", "README.fr.md"):
        data = (tmp_path / filename).read_bytes()
        assert data.startswith(before) and data.endswith(after)
    assert update_readmes(tmp_path, campaign, check=True)["ok"]


def test_missing_reviewer_engine_is_named_not_inferred(monkeypatch):
    """A reviewer that cannot run is a refusal, never an implicit favorable verdict."""
    import pytest

    from skillflow.util import FlowError

    monkeypatch.setattr("skillflow.reviewer.shutil.which", lambda name, path=None: None)

    class Client:
        def call(self, action, arguments, *_):
            if action == "review_read":
                return {
                    "ok": True,
                    "result": {"objects": [], "obligations": {}, "instruction": "synthetic"},
                }
            raise AssertionError("no submission is expected")

    with pytest.raises(FlowError) as failure:
        run_codex_review(Client(), "case", "review", model="synthetic-not-called")
    assert failure.value.code == "REVIEW_ENGINE_MISSING"
    assert failure.value.corrections


def test_a_reviewer_that_never_answers_is_a_named_refusal(monkeypatch):
    """Observed on 2026-09-16: the Windows launcher kept the real engine alive, its open
    pipe made the bounded wait unbounded, and the timeout escaped as a raw traceback."""
    import subprocess

    import pytest

    from skillflow.reviewer import run_external_review
    from skillflow.util import FlowError

    killed = []

    class Child:
        pid = 4321
        returncode = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def communicate(self, payload=None, timeout=None):
            raise subprocess.TimeoutExpired("engine", timeout or 1)

        def kill(self):
            killed.append("kill")

    monkeypatch.setattr("skillflow.reviewer.shutil.which", lambda name, path=None: "/engine")
    monkeypatch.setattr("skillflow.reviewer.subprocess.Popen", lambda *a, **k: Child())
    monkeypatch.setattr(
        "skillflow.reviewer.subprocess.run", lambda *a, **k: killed.append("taskkill")
    )

    class Client:
        def call(self, action, arguments, *_):
            if action == "review_read":
                return {
                    "ok": True,
                    "result": {"objects": [], "obligations": {}, "instruction": "synthetic"},
                }
            raise AssertionError("no submission is expected")

    with pytest.raises(FlowError) as failure:
        run_external_review(
            Client(), "case", "review", engine="codex", model="synthetic", timeout=1
        )
    assert failure.value.code == "REVIEW_TIMEOUT"
    assert "kill" in killed
