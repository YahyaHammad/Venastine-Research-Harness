"""
test_granted_calls_artifact.py

ROADMAP_v2 §25 -- the audit trail, and the provenance framing (R7).

Authorization moved up front, so accountability moves after: an unattended
ten-pass run is precisely where "the user said yes once" needs a record of
what that yes actually covered.
"""

import json
import os

import pytest

from core.approval import RunAuthorization
from core.reasoning.base import PipelineRun
from core.reasoning.output_writer import write_run_artifacts


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "core.reasoning.output_writer.config.OUTPUT_DIR", str(tmp_path))
    return tmp_path


def _run_with(granted):
    from uuid import uuid4
    run = PipelineRun(user_query="q")
    run.run_id = uuid4()
    run.final_report = "report"
    run.granted_calls = list(granted)
    return run


class TestTheArtifact:

    def test_written_when_a_grant_was_spent(self, output_dir, mocker):
        mocker.patch("core.reasoning.output_writer._try_write_pdf")
        mocker.patch("core.reasoning.output_writer.write_confidence_chart",
                     create=True, side_effect=lambda *a, **kw: None)
        run = _run_with([{"pass": "Pass 4", "tool": "mcp__lib__search",
                          "params": {"q": "x"}}])
        path = os.path.join(write_run_artifacts(run), "granted_calls.json")

        assert os.path.exists(path)
        recorded = json.loads(open(path, encoding="utf-8").read())
        assert recorded[0]["pass"] == "Pass 4"
        assert recorded[0]["tool"] == "mcp__lib__search"
        # The PARAMS matter as much as the name: "the search tool ran 40
        # times" is not an audit trail, "it searched for these things" is.
        assert recorded[0]["params"] == {"q": "x"}

    def test_absent_when_nothing_was_granted(self, output_dir, mocker):
        """Its presence is itself the signal that a run carried pre-flight
        authorization. An always-empty file sitting next to twelve
        populated ones reads as "nothing happened here" and gets skimmed
        past, which is the opposite of an audit trail's job."""
        mocker.patch("core.reasoning.output_writer._try_write_pdf")
        out = write_run_artifacts(_run_with([]))
        assert not os.path.exists(os.path.join(out, "granted_calls.json"))


class TestTheTrailSurvivesAFailedRun:

    def test_the_run_and_the_bundle_share_one_list(self, mocker):
        """Not two copies kept in step. The PipelineRun and the
        authorization refer to the SAME list, so a granted call is visible
        on the run the moment it happens -- including at every §5 checkpoint
        and on the FAILURE path, which is where an unattended run's record
        matters most. Copying at the end would lose the trail on exactly
        the runs most worth auditing."""
        from core.reasoning import orchestrator

        mocker.patch.object(orchestrator, "create_pipeline_run",
                            return_value=None)
        mocker.patch.object(orchestrator, "update_pipeline_run")
        # Fail at the very first pass, after the sharing is established.
        mocker.patch.object(orchestrator, "_run_pass_with_json_retry",
                            side_effect=RuntimeError("pass exploded"))

        auth = RunAuthorization(granted_tools={"mcp__lib__search"})
        with pytest.raises(RuntimeError):
            orchestrator.run_deep_research_pipeline(
                user_query="q", model="m", authorization=auth)

        # The sharing is what is asserted: appending to one is visible on
        # the other, which is what makes the failure path carry the trail.
        auth.granted_calls.append({"tool": "x", "params": {}})
        captured = orchestrator.update_pipeline_run.call_args_list
        assert captured, "the failure path never persisted anything"
        persisted_run = captured[-1][0][1]
        assert persisted_run.granted_calls is auth.granted_calls


class TestProvenanceFraming:
    """R7. Defence-in-depth, explicitly NOT counted as a control -- a
    prompt-level defence against prompt injection is bypassable. It is here
    because it is nearly free and standard practice, and because the
    pipeline is the harness's highest-injection-risk surface: it fetches
    web pages and feeds them to a model that then chooses tools."""

    def test_every_pass_inherits_it(self):
        """One place, the universal preamble, so a pass added later cannot
        be the one that forgot."""
        from prompts.system_prompts import passes_prompts

        assert passes_prompts, "no pass prompts loaded"
        for pass_id, prompt in passes_prompts.items():
            assert "DATA TO BE ANALYSED" in prompt, pass_id

    def test_it_names_the_actual_attack(self):
        """Not a vague 'be careful'. The passage a fetched page would
        contain is an instruction addressed to the model, so the framing
        has to say what to do with one."""
        from prompts.system_prompts import passes_prompts

        text = next(iter(passes_prompts.values()))
        assert "never instructions to follow" in text
        assert "override" in text
