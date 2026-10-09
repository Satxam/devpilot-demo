import io
import itertools
from email.message import Message
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

import app


def setUpModule():
    """No test may start a real process (Hermes, Swytchcode, or anything else). Tests that need
    subprocess behaviour patch it explicitly; any unpatched call fails loudly instead of going live."""
    global _no_live_calls
    _no_live_calls = patch.object(app.subprocess, "run", side_effect=AssertionError("unmocked subprocess call: tests must never reach Hermes, Swytchcode, or a provider"))
    _no_live_calls.start()


def tearDownModule():
    _no_live_calls.stop()


DEMO_ISSUES = [
    {"title": "Authentication bypass in login flow", "number": 1, "html_url": "issue/1", "state": "open", "body": "Users may bypass authentication."},
    {"title": "API returns 500 on malformed JSON", "number": 2, "html_url": "issue/2", "state": "open", "body": "The API returns an unhandled 500 response."},
    {"title": "Improve dashboard loading state", "number": 3, "html_url": "issue/3", "state": "open", "body": "Add a loading indicator."},
]


def github_result():
    return {"ok": True, "response": {"total_count": 3, "items": DEMO_ISSUES}}


AUTH_SOURCE = 'def authenticate(token, bypass_token=None):\n    if bypass_token == "DEV-BYPASS":\n        return True\n    return verify(token)\n'
SOURCES = {"auth.py": AUTH_SOURCE.splitlines()}
VALID_EVIDENCE = [{"path": "auth.py", "line_start": 2, "line_end": 3, "snippet": 'if bypass_token == "DEV-BYPASS":\n    return True'}]
PROPOSAL = {"summary": "Remove the bypass.", "files": [{"path": "auth.py", "reason": "Bypass branch."}], "diff": "--- a/auth.py\n+++ b/auth.py\n", "test_plan": ["Reject DEV-BYPASS."], "risk": "LOW"}


def review(reviewer, decision="CONFIRMED", evidence=None, affected=None):
    return {"reviewer": reviewer, "decision": decision, "severity": "CRITICAL", "root_cause": "Hard-coded bypass token.",
            "affected_files": [{"path": "auth.py", "impact": "Authentication bypass."}] if affected is None else affected,
            "evidence": VALID_EVIDENCE if evidence is None else evidence, "recommended_fix": "Remove the bypass branch.",
            "validation_plan": ["Reject DEV-BYPASS."]}


def reviewers(decisions=("CONFIRMED", "CONFIRMED", "CONFIRMED")):
    by_role = dict(zip(app.REVIEWERS, decisions))
    return lambda context, reviewer, **_: review(reviewer, by_role[reviewer])


def completed(stdout="", returncode=0, stderr=""):
    return type("Completed", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


class FakeSwytchcode:
    """Stands in for every provider call: GitHub/repository reads succeed, writes are recorded."""

    def __init__(self, jira_ok=True, issues=None, jira_results=None):
        self.calls, self.jira_ok, self.issues, self.jira_results = [], jira_ok, issues, list(jira_results or [])

    def __call__(self, tool, args):
        self.calls.append((tool, args))
        if tool == app.TOOLS["github"]:
            return github_result() if self.issues is None else {"ok": True, "response": {"total_count": len(self.issues), "items": self.issues}}
        if tool == app.TOOLS["jira"] and self.jira_results:
            return self.jira_results.pop(0)
        if tool == app.TOOLS["repository"] and args["path"] == ".":
            return {"ok": True, "response": {"data": [{"name": "auth.py", "path": "auth.py", "type": "file"}]}}
        if tool == app.TOOLS["repository"]:
            return {"ok": True, "response": {"data": {"type": "file", "content": AUTH_SOURCE}}}
        if tool == app.TOOLS["jira"]:
            return {"ok": self.jira_ok, "response": {"key": f"KAN-{len(self.writes())}"}}
        return {"ok": True, "response": {"ok": True}}

    def writes(self):
        return [call for call in self.calls if call[0] in (app.TOOLS["jira"], app.TOOLS["slack"])]


class FakeHandler(app.Handler):
    def __init__(self, headers=None, path="/", body=None):
        self.responses, self.path = [], path
        self.headers = Message()
        defaults = {"Host": f"127.0.0.1:{app.PORT}"}
        if body is not None:
            defaults.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
        for name, value in {**defaults, **(headers or {})}.items():
            if value is not None:
                self.headers[name] = value
        self.rfile = io.BytesIO(body or b"")

    def send_json(self, status, payload):
        self.responses.append((status, payload))


def make_plan(request, tools):
    return {"token": "t", "repo": "Satxam/devpilot-demo", "jira_project": "KAN", "slack_channel": "#engineering",
            "request": request, "tools": tools, "reason": "Hermes plan", "needs_confirmation": False,
            "writes_require_confirmation": False}


class HermesRoutingTests(unittest.TestCase):
    def plan(self, request, tools):
        return make_plan(request, tools)

    def test_informational_request_is_github_only(self):
        request = "Find the open issues in this repository."
        plan = self.plan(request, ["github"])
        with patch.object(app, "run_swytchcode", return_value=github_result()) as execute, \
                patch.object(app, "hermes_reviewer", side_effect=reviewers(("REVIEW_REQUIRED",) * 3)), patch.object(app, "hermes_proposal") as proposal:
            result = app.run_plan(plan)
        self.assertEqual(result["plan"]["tools"], ["github"])
        self.assertEqual(execute.call_count, 1)
        proposal.assert_not_called()

    def run_reviewed(self, request, tools, decisions=("CONFIRMED", "CONFIRMED", "CONFIRMED"), confirmed=False, plan=None):
        fake = FakeSwytchcode()
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=reviewers(decisions)), \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL):
            result = app.run_plan(plan or self.plan(request, tools), confirmed=confirmed)
        return result, fake

    def test_team_communication_request_is_github_then_slack(self):
        result, fake = self.run_reviewed("Tell the engineering team about the critical issue.", ["github", "repository", "slack"])
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(result["plan"]["tools"], ["github", "repository", "slack"])
        self.assertEqual(fake.writes(), [])

    def test_jira_request_is_github_then_jira(self):
        result, fake = self.run_reviewed("Create Jira tickets for the critical issues.", ["github", "repository", "jira"])
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(result["plan"]["tools"], ["github", "repository", "jira"])
        self.assertEqual(fake.writes(), [])

    def test_flagship_request_selects_both_and_executes_after_approval(self):
        request = "Find critical open bugs, create a Jira ticket for each critical bug, and notify the engineering team in Slack."
        pending, fake = self.run_reviewed(request, ["github", "repository", "jira", "slack"])
        self.assertEqual(pending["status"], "confirmation_required")
        self.assertEqual(fake.writes(), [])
        final, fake = self.run_reviewed(request, None, confirmed=True, plan=pending["plan"])
        self.assertEqual(final["status"], "complete")
        self.assertEqual(len(fake.calls), 2)  # 1 Jira ticket (only #1 is critical) + 1 Slack message; no re-reads
        self.assertIn("created 1 Jira ticket(s)", final["timeline"][-1]["detail"])
        self.assertIn("sent 1 Slack notification(s)", final["timeline"][-1]["detail"])

    def test_escalation_request_selects_jira_and_slack(self):
        result, _ = self.run_reviewed("Find critical and high issues and escalate them.", ["github", "repository"])
        self.assertEqual(result["plan"]["tools"], ["github", "repository", "jira", "slack"])
        self.assertEqual(result["status"], "confirmation_required")

    def test_repository_inspection_uses_approved_read_method(self):
        request = "Investigate the authentication bypass issue in this repository."
        plan = self.plan(request, ["github", "repository"])
        root = {"ok": True, "response": {"data": [{"name": "auth.py", "path": "auth.py", "type": "file"}]}}
        content = {"ok": True, "response": {"data": {"type": "file", "content": "def authenticate(token):\n    return True\n"}}}
        with patch.object(app, "run_swytchcode", side_effect=[github_result(), root, content]) as execute, \
                patch.object(app, "hermes_reviewer", side_effect=reviewers()), patch.object(app, "hermes_proposal", return_value=PROPOSAL):
            outcome = app.run_plan(plan)
        self.assertEqual(execute.call_count, 3)
        self.assertEqual(execute.call_args_list[1].args[0], "github.content.get")
        self.assertEqual(execute.call_args_list[1].args[1], {"owner": "Satxam", "repo": "devpilot-demo", "path": "."})
        self.assertEqual(execute.call_args_list[2].args[1], {"owner": "Satxam", "repo": "devpilot-demo", "path": "auth.py"})
        self.assertEqual(outcome["engineering"]["inspected_files"], ["auth.py"])
        self.assertEqual(outcome["status"], "complete")

    def test_demo_severity_is_semantic_and_sorted_for_ui(self):
        analysis = app.summarize_github(github_result())
        self.assertEqual([i["severity"] for i in analysis["issues"]], ["CRITICAL", "HIGH", "NORMAL"])
        self.assertEqual(analysis["issues"][0]["title"], "Authentication bypass in login flow")
        self.assertEqual(analysis["issues"][1]["title"], "API returns 500 on malformed JSON")

    def test_empty_write_configuration_blocks_provider_calls(self):
        request = "Create Jira tickets for the critical issues and notify the engineering team in Slack."
        plan = self.plan(request, ["github", "repository", "jira", "slack"])
        plan["jira_project"] = ""
        plan["slack_channel"] = ""
        result, fake = self.run_reviewed(request, None, plan=plan)
        self.assertEqual(result["status"], "configuration_error")
        self.assertEqual(fake.writes(), [])
        self.assertEqual(result["actions"], {})

    def test_github_query_does_not_include_request(self):
        request = "Find the open issues in this repository and identify which ones need immediate attention."
        self.assertEqual(app.github_args("Satxam/devpilot-demo", request)["q"], "repo:Satxam/devpilot-demo is:issue is:open")
        self.assertNotIn(request, app.github_args("Satxam/devpilot-demo", request)["q"])

    def test_hermes_planning_is_strict_and_non_executing(self):
        with patch.object(app.subprocess, "run") as run, patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"):
            run.return_value = type("Completed", (), {"returncode": 0, "stdout": '{"intent":"find","tools":["github"],"reason":"Read issues.","needs_confirmation":false}', "stderr": ""})()
            decision = app.hermes_decision("Find open issues", "planning")
        self.assertEqual(decision["tools"], ["github"])
        self.assertIn("--safe-mode", run.call_args.args[0])
        self.assertNotIn("swytchcode", run.call_args.args[0])

    def test_repository_calls_use_input_flags(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
        with patch.object(app.subprocess, "run", return_value=completed) as run:
            app.run_swytchcode("github.content.get", {"owner": "Satxam", "repo": "devpilot-demo", "path": "src/auth.py"})
        command = run.call_args.args[0]
        self.assertIn("--input", command)
        self.assertIn("owner=Satxam", command)
        self.assertIn("repo=devpilot-demo", command)
        self.assertIn("path=src/auth.py", command)
        self.assertNotIn("--param", command)

    def test_jira_result_extracts_key_id_and_configured_browse_url(self):
        api = "https://example.atlassian.net/rest/api/3/issue/10004"
        with patch.object(app, "JIRA_BASE_URL", "https://example.atlassian.net/"):
            result = app.extract_jira({"response": {"data": {"key": "KAN-4", "id": "10004", "self": api}}})
        self.assertEqual(result, {"key": "KAN-4", "id": "10004", "url": "https://example.atlassian.net/browse/KAN-4", "api_url": api})

    def test_live_status_helpers_record_completion_and_error(self):
        app.set_status("status-test", "GitHub", "running", "GET open issues")
        app.set_status("status-test", "GitHub", "complete", "Retrieved open issues")
        status = app.public_status("status-test")
        self.assertEqual(status["status"], "running")  # a completed step does not finish the run
        self.assertFalse(status["terminal"])
        self.assertEqual(status["steps"][0]["status"], "complete")
        self.assertEqual(status["steps"][0]["detail"], "Retrieved open issues")

    def test_structured_engineering_analysis_rejects_missing_fields(self):
        with self.assertRaises(RuntimeError):
            app.engineering_analysis_schema({"root_cause": "x"})

    def test_structured_engineering_analysis_accepts_source_evidence(self):
        result = app.engineering_analysis_schema({
            "root_cause": "Development bypass accepts a fixed token.",
            "affected_files": [{"path": "src/auth.py", "impact": "Allows authentication bypass."}],
            "evidence": [{"path": "src/auth.py", "line_start": 8, "line_end": 9, "snippet": 'if bypass_token == "DEV-BYPASS":\\n    return True'}],
            "recommended_fix": "Remove the bypass branch.", "severity": "CRITICAL", "confidence": "HIGH",
            "validation_plan": ["Reject the development token."],
        })
        self.assertEqual(result["evidence"][0]["line_start"], 8)

    def test_nested_repository_entries_and_base64_evidence(self):
        import base64
        encoded = base64.b64encode(b'if bypass_token == "DEV-BYPASS":\n    return True\n').decode()
        root = {"ok": True, "response": {"data": [{"path": "src", "type": "dir"}]}}
        nested = {"ok": True, "response": {"data": [{"path": "src/auth.py", "type": "file"}]}}
        content = {"ok": True, "response": {"data": {"path": "src/auth.py", "type": "file", "encoding": "base64", "content": encoded, "html_url": "https://github.test/src/auth.py"}}}
        plan = self.plan("Investigate authentication bypass", ["github", "repository"])
        with patch.object(app, "run_swytchcode", side_effect=[root, nested, content]):
            _, paths, files, unavailable, scope = app.retrieve_repository_evidence(plan, {})
        self.assertEqual(paths, ["src/auth.py"])
        self.assertEqual(unavailable, [])
        self.assertTrue(scope["complete"])
        self.assertEqual(scope["directories_visited"], ["src"])
        self.assertIn("DEV-BYPASS", app.content_text(files[0]["result"]))
        self.assertEqual(files[0]["url"], "https://github.test/src/auth.py")

    def test_consensus_blocks_disagreement(self):
        reviews = [review("SECURITY"), review("CODE", "FALSE_POSITIVE"), review("TEST")]
        self.assertEqual(app.consensus(reviews, SOURCES)["decision"], "REVIEW_REQUIRED")

    def test_approval_claim_is_single_use_and_expiry_is_enforced(self):
        app.PENDING["claim-test"] = {"token": "claim-test", "created_at": time.time(), "state": "awaiting_approval", "writes_require_confirmation": True, "write_actions": ["jira"]}
        self.assertEqual(app.claim_plan("claim-test", True)[0], "approved")
        self.assertEqual(app.claim_plan("claim-test", True)[0], "not_approvable")
        app.PENDING["claim-expired"] = {"token": "claim-expired", "created_at": 0, "state": "awaiting_approval", "writes_require_confirmation": True, "write_actions": ["jira"]}
        self.assertEqual(app.claim_plan("claim-expired", True)[0], "expired")


class HermesInvocationTests(unittest.TestCase):
    """V1 and V8: supported reasoning levels, validated config, and fail-closed model output."""

    DECISION = '{"intent":"find","tools":["github"],"reason":"Read issues.","needs_confirmation":false}'

    def calls(self):
        return [
            (lambda: app.hermes_decision("Find open issues", "planning"), self.DECISION, "none"),
            (lambda: app.hermes_reviewer("{}", "CODE"), app.json.dumps(review("CODE")), "low"),
            (lambda: app.hermes_proposal("{}"), app.json.dumps(PROPOSAL), "low"),
        ]

    def test_every_hermes_call_uses_a_supported_reasoning_level(self):
        for call, stdout, level in self.calls():
            with patch.object(app.subprocess, "run", return_value=completed(stdout)) as run, patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"):
                call()
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--reasoning") + 1], level)
            self.assertNotIn("minimal", command)
            self.assertIn("--safe-mode", command)
            self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")

    def test_invalid_reasoning_configuration_blocks_hermes(self):
        planning, reviewer, proposal = self.calls()
        for setting, affected in (("HERMES_PLANNING_REASONING", [planning]), ("HERMES_REVIEW_REASONING", [reviewer, proposal])):
            for call, stdout, _ in affected:
                with patch.object(app.subprocess, "run", return_value=completed(stdout)) as run, patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"), \
                        patch.object(app, setting, "minimal"):
                    with self.assertRaisesRegex(RuntimeError, "reasoning level 'minimal'"):
                        call()
                run.assert_not_called()

    def test_missing_model_blocks_every_hermes_call(self):
        for call, stdout, _ in self.calls():
            with patch.object(app.subprocess, "run", return_value=completed(stdout)) as run, patch.object(app, "HERMES_MODEL", ""):
                with self.assertRaisesRegex(RuntimeError, "DEVPILOT_HERMES_MODEL"):
                    call()
            run.assert_not_called()

    def test_timeouts_os_errors_and_failures_become_runtime_errors(self):
        failures = [subprocess.TimeoutExpired("hermes", 120), OSError("hermes not found")]
        for call, _, _ in self.calls():
            for failure in failures:
                with patch.object(app.subprocess, "run", side_effect=failure), patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"):
                    with self.assertRaises(RuntimeError):
                        call()
            with patch.object(app.subprocess, "run", return_value=completed("", 1, "HTTP 502")), patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"):
                with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                    call()

    def test_malformed_model_output_fails_closed_with_runtime_error(self):
        bad_reviews = [
            "not json", "[]", {**review("CODE"), "decision": ["CONFIRMED"]}, {**review("CODE"), "severity": {"x": 1}},
            {**review("CODE"), "evidence": [{**VALID_EVIDENCE[0], "line_start": True}]},
            {**review("CODE"), "evidence": [{**VALID_EVIDENCE[0], "line_start": 0, "line_end": 0}]},
            {**review("CODE"), "evidence": [{**VALID_EVIDENCE[0], "path": ["auth.py"]}]},
            {**review("CODE"), "reviewer": "SECURITY"}, {**review("CODE"), "extra": 1},
        ]
        for value in bad_reviews:
            stdout = value if isinstance(value, str) else app.json.dumps(value)
            with patch.object(app.subprocess, "run", return_value=completed(stdout)), patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"):
                with self.assertRaises(RuntimeError, msg=stdout):
                    app.hermes_reviewer("{}", "CODE")
        bad_decisions = ['{"intent":"x","tools":[["github"]],"reason":"r","needs_confirmation":false}', '{"intent":"x","tools":["shell"],"reason":"r","needs_confirmation":false}']
        bad_proposals = [{**PROPOSAL, "risk": ["LOW"]}, {**PROPOSAL, "summary": 3}, {**PROPOSAL, "test_plan": [1]}]
        for stdout in bad_decisions:
            with patch.object(app.subprocess, "run", return_value=completed(stdout)), patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"):
                with self.assertRaises(RuntimeError):
                    app.hermes_decision("x", "planning")
        for value in bad_proposals:
            with patch.object(app.subprocess, "run", return_value=completed(app.json.dumps(value))), patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"):
                with self.assertRaises(RuntimeError):
                    app.hermes_proposal("{}")


class ConsensusTests(unittest.TestCase):
    """V3 and V4: decision gating and evidence verification."""

    def test_all_reviewer_decision_combinations(self):
        decisions = ("CONFIRMED", "FALSE_POSITIVE", "REVIEW_REQUIRED")
        for combo in itertools.product(decisions, repeat=3):
            with self.subTest(combo=combo):
                result = app.consensus([review(role, decision) for role, decision in zip(app.REVIEWERS, combo)], SOURCES)
                if combo.count("CONFIRMED") >= 2 and "FALSE_POSITIVE" not in combo:
                    expected = "CONFIRMED"
                elif combo.count("FALSE_POSITIVE") == 3:
                    expected = "FALSE_POSITIVE"
                else:
                    expected = "REVIEW_REQUIRED"
                self.assertEqual(result["decision"], expected)

    def test_majority_review_required_never_confirms(self):
        result = app.consensus([review("SECURITY", "REVIEW_REQUIRED"), review("CODE", "REVIEW_REQUIRED"), review("TEST")], SOURCES)
        self.assertEqual(result["decision"], "REVIEW_REQUIRED")

    def test_missing_duplicate_or_malformed_reviews_fail_closed(self):
        valid = [review(role) for role in app.REVIEWERS]
        cases = {
            "two reviews": valid[:2],
            "duplicate role": [valid[0], valid[0], valid[2]],
            "unknown role": [valid[0], valid[1], {**valid[2], "reviewer": "OTHER"}],
            "not a dict": [valid[0], valid[1], "CONFIRMED"],
            "bad decision type": [valid[0], valid[1], {**valid[2], "decision": ["CONFIRMED"]}],
            "missing field": [valid[0], valid[1], {k: v for k, v in valid[2].items() if k != "evidence"}],
            "not a list": None,
        }
        for name, reviews in cases.items():
            with self.subTest(name=name):
                self.assertEqual(app.consensus(reviews, SOURCES)["decision"], "REVIEW_REQUIRED")

    def test_invalid_evidence_blocks_confirmation(self):
        cases = {
            "empty evidence": dict(evidence=[]),
            "unretrieved path": dict(evidence=[{**VALID_EVIDENCE[0], "path": "src/login.py"}]),
            "line beyond file": dict(evidence=[{**VALID_EVIDENCE[0], "line_start": 4, "line_end": 9}]),
            "fabricated snippet": dict(evidence=[{**VALID_EVIDENCE[0], "snippet": "if user.is_admin:\n    return True"}]),
            "snippet from other lines": dict(evidence=[{**VALID_EVIDENCE[0], "line_start": 1, "line_end": 1}]),
            "blank snippet": dict(evidence=[{**VALID_EVIDENCE[0], "snippet": "   \n"}]),
            "unretrieved affected file": dict(affected=[{"path": "src/login.py", "impact": "Invented."}]),
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                reviews = [review("SECURITY"), review("CODE", **overrides), review("TEST")]
                result = app.consensus(reviews, SOURCES)
                self.assertEqual(result["decision"], "REVIEW_REQUIRED")
                self.assertTrue(result["evidence_errors"])

    def test_confirmation_without_any_retrieved_source_is_blocked(self):
        self.assertEqual(app.consensus([review(role) for role in app.REVIEWERS], {})["decision"], "REVIEW_REQUIRED")

    def test_matching_evidence_confirms_and_tolerates_indentation(self):
        loose = [{**VALID_EVIDENCE[0], "snippet": 'if bypass_token == "DEV-BYPASS":\nreturn True'}]
        result = app.consensus([review("SECURITY"), review("CODE", evidence=loose), review("TEST")], SOURCES)
        self.assertEqual(result["decision"], "CONFIRMED")
        self.assertEqual(result["agreement"], "3/3")

    def test_fabricated_evidence_in_a_run_prevents_writes(self):
        fake = FakeSwytchcode()
        fabricated = lambda context, reviewer, **_: review(reviewer, evidence=[{**VALID_EVIDENCE[0], "path": "src/login.py"}])
        plan = make_plan("Create Jira tickets for the critical issues.", ["github", "repository", "jira"])
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=fabricated), \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL) as proposal:
            result = app.run_plan(plan)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["council"]["decision"], "REVIEW_REQUIRED")
        self.assertEqual(result["plan"]["write_actions"], [])
        self.assertEqual(fake.writes(), [])
        proposal.assert_not_called()


class RunFailureTests(unittest.TestCase):
    """V8: failures are recorded rather than leaving a run stuck."""

    def plan(self, token, request="Create Jira tickets for the critical issues."):
        return {**make_plan(request, ["github", "repository", "jira"]), "token": token, "created_at": time.time(), "state": "running"}

    def test_proposal_timeout_records_error_and_sends_no_writes(self):
        fake = FakeSwytchcode()
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=reviewers()), \
                patch.object(app.subprocess, "run", side_effect=subprocess.TimeoutExpired("hermes", 120)), patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"):
            result = app.run_plan(self.plan("proposal-timeout"))
        self.assertEqual(result["status"], "error")
        self.assertIn("timed out", result["error"])
        self.assertEqual(fake.writes(), [])
        steps = {step["step"]: step["status"] for step in app.public_status("proposal-timeout")["steps"]}
        self.assertEqual(steps["Proposed Fix"], "error")
        self.assertEqual(app.public_status("proposal-timeout")["status"], "error")

    def test_reviewer_failure_marks_its_step_and_sends_no_writes(self):
        fake = FakeSwytchcode()
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=RuntimeError("CODE reviewer response has the wrong schema.")):
            result = app.run_plan(self.plan("reviewer-failure"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(fake.writes(), [])
        steps = {step["step"]: step["status"] for step in app.public_status("reviewer-failure")["steps"]}
        self.assertEqual(steps["Security Reviewer"], "error")

    def test_unexpected_background_exception_is_recorded(self):
        plan = self.plan("unexpected")
        app.PENDING["unexpected"] = plan
        with patch.object(app, "run_plan", side_effect=KeyError("tools")):
            app.Handler._background_run(FakeHandler(), plan, False)
        self.assertEqual(app.RESULTS["unexpected"]["status"], "error")
        self.assertIn("KeyError", app.RESULTS["unexpected"]["error"])
        self.assertEqual(app.public_status("unexpected")["status"], "error")
        self.assertEqual(app.PENDING["unexpected"]["state"], "done")


class ApprovalTests(unittest.TestCase):
    """V2 and V5: approval is required, single-use, atomic, and executes the reviewed finding."""

    def setUp(self):
        self.token = app.uuid.uuid4().hex

    def fresh_plan(self, request="Create Jira tickets for the critical issues."):
        plan = {**make_plan(request, ["github", "repository", "jira"]), "token": self.token, "created_at": time.time(), "state": "planned", "write_actions": []}
        app.PENDING[self.token] = plan
        return plan

    def awaiting_plan(self):
        """Drive a fresh plan through the real background run until it awaits approval."""
        self.fresh_plan()
        fake = FakeSwytchcode()
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=reviewers()), \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL), patch.object(app.threading, "Thread") as thread:
            handler = FakeHandler()
            handler.execute({"token": self.token})
            target, args = thread.call_args.kwargs["target"], thread.call_args.kwargs["args"]
            target(*args)
        self.assertEqual(handler.responses[0][0], 202)
        self.assertEqual(app.PENDING[self.token]["state"], "awaiting_approval")
        self.assertEqual(fake.writes(), [])
        return app.PENDING[self.token]

    def test_fresh_plan_cannot_be_confirmed_directly(self):
        self.fresh_plan()
        handler = FakeHandler()
        with patch.object(app, "run_plan") as run_plan, patch.object(app, "run_swytchcode") as swytchcode:
            handler.execute({"token": self.token, "confirmed": True}, confirmed=True)
        self.assertEqual(handler.responses[0][0], 409)
        run_plan.assert_not_called()
        swytchcode.assert_not_called()
        self.assertEqual(app.PENDING[self.token]["state"], "planned")

    def test_run_plan_refuses_confirmed_execution_of_unreviewed_plan(self):
        plan = self.fresh_plan()
        with patch.object(app, "run_swytchcode") as swytchcode:
            result = app.run_plan(plan, confirmed=True)
        self.assertEqual(result["status"], "error")
        swytchcode.assert_not_called()

    def test_repeated_confirmation_executes_writes_once(self):
        self.awaiting_plan()
        fake = FakeSwytchcode()
        statuses = []
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer") as reviewer, patch.object(app, "hermes_proposal") as proposal:
            for _ in range(3):
                handler = FakeHandler()
                handler.execute({"token": self.token, "confirmed": True}, confirmed=True)
                statuses.append(handler.responses[0][0])
        self.assertEqual(statuses, [200, 409, 409])
        self.assertEqual(len(fake.writes()), 1)  # one Jira ticket for the one critical issue, created once
        reviewer.assert_not_called()
        proposal.assert_not_called()
        self.assertEqual(app.PENDING[self.token]["state"], "done")

    def test_concurrent_confirmations_execute_writes_once(self):
        self.awaiting_plan()
        runs, lock = [], threading.Lock()

        def slow_run(plan, confirmed=False):
            with lock:
                runs.append(confirmed)
            time.sleep(0.05)
            return {"status": "complete", "plan": plan, "actions": {}, "timeline": []}

        barrier, statuses = threading.Barrier(8), []

        def confirm():
            handler = FakeHandler()
            barrier.wait()
            handler.execute({"token": self.token, "confirmed": True}, confirmed=True)
            with lock:
                statuses.append(handler.responses[0][0])

        with patch.object(app, "run_plan", side_effect=slow_run):
            threads = [threading.Thread(target=confirm) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
        self.assertEqual(runs, [True])
        self.assertEqual(sorted(statuses), [200] + [409] * 7)

    def test_duplicate_execute_starts_only_one_background_run(self):
        self.fresh_plan()
        with patch.object(app.threading, "Thread") as thread:
            first, second = FakeHandler(), FakeHandler()
            first.execute({"token": self.token})
            second.execute({"token": self.token})
        self.assertEqual(first.responses[0][0], 202)
        self.assertEqual(second.responses[0][0], 409)
        self.assertEqual(thread.call_count, 1)

    def test_approved_run_executes_the_exact_reviewed_finding(self):
        pending = self.awaiting_plan()
        approved = {key: pending[key] for key in ("reviews", "council", "finding", "write_actions")}
        fake = FakeSwytchcode()
        # A different council answer after approval must never be consulted.
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=reviewers(("FALSE_POSITIVE",) * 3)) as reviewer, \
                patch.object(app, "hermes_proposal") as proposal:
            handler = FakeHandler()
            handler.execute({"token": self.token, "confirmed": True}, confirmed=True)
        status, result = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "complete")
        for key, value in approved.items():
            self.assertEqual(result["plan"][key], value)
        self.assertEqual(result["finding"]["proposed_fix"], PROPOSAL)
        reviewer.assert_not_called()
        proposal.assert_not_called()
        self.assertTrue(all(tool in (app.TOOLS["jira"], app.TOOLS["slack"]) for tool, _ in fake.calls))

    def test_expired_approval_is_refused(self):
        self.awaiting_plan()
        app.PENDING[self.token]["created_at"] = 0
        handler = FakeHandler()
        with patch.object(app, "run_plan") as run_plan:
            handler.execute({"token": self.token, "confirmed": True}, confirmed=True)
        self.assertEqual(handler.responses[0][0], 410)
        run_plan.assert_not_called()



class LifecycleTests(unittest.TestCase):
    """V6, V7 and V13: terminal states, run vs step status, and the real orchestration path."""

    def setUp(self):
        self.token = app.uuid.uuid4().hex

    def pending(self, request="Create Jira tickets for the critical issues.", tools=("github", "repository", "jira"), **overrides):
        plan = {**make_plan(request, list(tools)), "token": self.token, "created_at": time.time(), "state": "running", "write_actions": [], **overrides}
        app.PENDING[self.token] = plan
        with app.LOCK:
            app.RUNS[self.token] = {**app._new_run(self.token, "planned"), "steps": [{"step": "Hermes Planning", "status": "complete", "detail": "plan"}]}
        return plan

    def snapshotting(self, fake, snapshots):
        """Wrap a fake provider so every provider call records what a poller would see at that moment."""
        def call(tool, args):
            snapshots.append(app.public_status(self.token))
            return fake(tool, args)
        return call

    def test_intermediate_completed_steps_keep_the_run_running(self):
        app.start_run(self.token)
        for step in ("Request", "Hermes Planning", "GitHub Issue Search", "Consensus"):
            app.set_status(self.token, step, "complete", "done")
            status = app.public_status(self.token)
            self.assertEqual(status["status"], "running")
            self.assertFalse(status["terminal"])
            self.assertFalse(status["result_ready"])
        app.set_status(self.token, "Source Inspection", "error", "No source files retrieved")
        self.assertEqual(app.public_status(self.token)["status"], "running")

    def test_no_poll_during_a_run_sees_a_terminal_status_or_a_stale_result(self):
        plan = self.pending()
        snapshots, fake = [], FakeSwytchcode()
        review_snapshots = []

        def reviewer(context, role, **_):
            review_snapshots.append(app.public_status(self.token))
            return review(role)

        with patch.object(app, "run_swytchcode", side_effect=self.snapshotting(fake, snapshots)), patch.object(app, "hermes_reviewer", side_effect=reviewer), \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL):
            result = app.execute_plan(plan, False)
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(len(snapshots), 3)
        self.assertEqual(len(review_snapshots), 3)
        for snapshot in snapshots + review_snapshots:
            self.assertEqual(snapshot["status"], "running")
            self.assertFalse(snapshot["terminal"])
            self.assertFalse(snapshot["result_ready"])

    def test_terminal_status_always_has_a_persisted_result(self):
        plan = self.pending(request="Find open issues.", tools=("github", "repository"))
        with patch.object(app, "run_swytchcode", side_effect=FakeSwytchcode()), patch.object(app, "hermes_reviewer", side_effect=reviewers()), \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL):
            result = app.execute_plan(plan, False)
        status = app.public_status(self.token)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(status["status"], "complete")
        self.assertTrue(status["terminal"])
        self.assertTrue(status["result_ready"])
        self.assertIs(app.RESULTS[self.token], result)
        self.assertEqual(app.PENDING[self.token]["state"], "done")
        self.assertEqual(status["steps"][-1]["step"], "Final Result")

    def test_configuration_error_is_terminal_and_reports_its_error(self):
        plan = self.pending(request="Create Jira tickets for the critical issues.", jira_project="")
        with patch.object(app, "run_swytchcode", side_effect=FakeSwytchcode()) as provider, patch.object(app, "hermes_reviewer", side_effect=reviewers()), \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL):
            result = app.execute_plan(plan, False)
        status = app.public_status(self.token)
        self.assertEqual(result["status"], "configuration_error")
        self.assertEqual(status["status"], "configuration_error")
        self.assertTrue(status["terminal"])
        self.assertTrue(status["result_ready"])
        self.assertIn("Jira project key", status["error"])
        self.assertEqual(app.PENDING[self.token]["state"], "done")
        self.assertFalse(any(call.args[0] in (app.TOOLS["jira"], app.TOOLS["slack"]) for call in provider.call_args_list))

    def test_status_endpoint_reports_terminal_configuration_error(self):
        self.test_configuration_error_is_terminal_and_reports_its_error()
        handler = FakeHandler()
        handler.path = f"/api/status?token={self.token}"
        handler.do_GET()
        status, payload = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["terminal"])
        self.assertEqual(payload["status"], "configuration_error")
        handler = FakeHandler()
        handler.path = f"/api/result?token={self.token}"
        handler.do_GET()
        self.assertEqual(handler.responses[0][1]["status"], "configuration_error")

    def test_confirmation_required_is_published_together_with_an_approvable_plan(self):
        plan = self.pending()
        with patch.object(app, "run_swytchcode", side_effect=FakeSwytchcode()), patch.object(app, "hermes_reviewer", side_effect=reviewers()), \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL):
            app.execute_plan(plan, False)
        with app.LOCK:  # one consistent view, as a poller racing the run would get
            run_status, state = app.RUNS[self.token]["status"], app.PENDING[self.token]["state"]
        self.assertEqual((run_status, state), ("confirmation_required", "awaiting_approval"))
        status = app.public_status(self.token)
        self.assertTrue(status["terminal"])
        self.assertEqual(status["steps"][-1], {"step": "Confirmation", "status": "confirmation_required", "detail": "Approve Jira/Slack writes"})
        self.assertEqual(app.claim_plan(self.token, True)[0], "approved")

    def test_approved_run_restarts_as_running_until_writes_finish(self):
        plan = self.pending()
        with patch.object(app, "run_swytchcode", side_effect=FakeSwytchcode()), patch.object(app, "hermes_reviewer", side_effect=reviewers()), \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL):
            app.execute_plan(plan, False)
        outcome, approved = app.claim_plan(self.token, True)
        self.assertEqual(outcome, "approved")
        snapshots = []
        with patch.object(app, "run_swytchcode", side_effect=self.snapshotting(FakeSwytchcode(), snapshots)):
            result = app.execute_plan(approved, True)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(snapshots), 1)  # one Jira ticket for the one critical issue
        for snapshot in snapshots:
            self.assertEqual(snapshot["status"], "running")
            self.assertFalse(snapshot["terminal"])
            self.assertFalse(snapshot["result_ready"])  # the earlier confirmation_required result is not served
        self.assertTrue(app.public_status(self.token)["terminal"])
        self.assertEqual(app.PENDING[self.token]["state"], "done")

    def test_reviewer_failure_is_terminal_with_its_error(self):
        plan = self.pending()
        with patch.object(app, "run_swytchcode", side_effect=FakeSwytchcode()), \
                patch.object(app, "hermes_reviewer", side_effect=RuntimeError("Hermes SECURITY reviewer invocation timed out after 120s.")):
            result = app.execute_plan(plan, False)
        status = app.public_status(self.token)
        self.assertEqual(result["status"], "error")
        self.assertEqual((status["status"], status["terminal"]), ("error", True))
        self.assertIn("timed out", status["error"])
        self.assertEqual(app.PENDING[self.token]["state"], "done")

    def test_proposal_failure_is_terminal_with_its_error(self):
        plan = self.pending()
        with patch.object(app, "run_swytchcode", side_effect=FakeSwytchcode()) as provider, patch.object(app, "hermes_reviewer", side_effect=reviewers()), \
                patch.object(app, "hermes_proposal", side_effect=RuntimeError("Proposed code fix has the wrong schema.")):
            result = app.execute_plan(plan, False)
        status = app.public_status(self.token)
        self.assertEqual(result["status"], "error")
        self.assertEqual((status["status"], status["terminal"]), ("error", True))
        self.assertIn("wrong schema", status["error"])
        self.assertEqual({s["step"]: s["status"] for s in status["steps"]}["Proposed Fix"], "error")
        self.assertFalse(any(call.args[0] in (app.TOOLS["jira"], app.TOOLS["slack"]) for call in provider.call_args_list))

    def test_unexpected_exception_is_terminal_with_its_error(self):
        plan = self.pending()
        with patch.object(app, "run_plan", side_effect=ValueError("bad payload")):
            app.execute_plan(plan, False)
        status = app.public_status(self.token)
        self.assertEqual((status["status"], status["terminal"], status["result_ready"]), ("error", True, True))
        self.assertIn("ValueError", status["error"])

    def test_unknown_result_status_is_recorded_as_error(self):
        app.start_run(self.token)
        result = app.persist_result(self.token, {"status": "half-done", "plan": {}, "timeline": []})
        self.assertEqual(result["status"], "error")
        self.assertEqual(app.public_status(self.token)["status"], "error")
        self.assertTrue(app.public_status(self.token)["terminal"])

    def test_full_orchestration_calls_each_reviewer_and_the_proposal_for_real(self):
        plan = self.pending()
        with patch.object(app, "run_swytchcode", side_effect=FakeSwytchcode()), patch.object(app, "hermes_reviewer", side_effect=reviewers()) as reviewer, \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL) as proposal, \
                patch.object(app, "hermes_decision", side_effect=AssertionError("planning must not be re-run during orchestration")):
            result = app.execute_plan(plan, False)
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual([call.args[1] for call in reviewer.call_args_list], list(app.REVIEWERS))
        context = reviewer.call_args_list[0].args[0]
        self.assertIn("DEV-BYPASS", context)  # reviewers receive the retrieved source
        proposal.assert_called_once_with(context)
        self.assertEqual(result["council"]["decision"], "CONFIRMED")
        self.assertEqual(result["finding"]["proposed_fix"], PROPOSAL)

    def test_production_code_has_no_mock_detection(self):
        import inspect
        source = inspect.getsource(app)
        self.assertNotIn("mock_calls", source)
        self.assertNotIn("unittest", source)

    def test_terminal_statuses_cover_every_result_status(self):
        self.assertEqual(app.TERMINAL_STATUSES, {"complete", "error", "configuration_error", "confirmation_required", "cancelled"})



AUTH_FAILURE = app.swytchcode_failure("jira.api.issue.create", app.swytchcode_error("jira.api.issue.create", '{"error":"token expired","category":"auth","retryable":false}'))


def jira_created(key):
    return {"ok": True, "response": {"data": {"key": key, "id": "1", "self": f"https://example.atlassian.net/rest/api/3/issue/{key}"}}}


class IntegrationTests(unittest.TestCase):
    """V9, V10, V12, V15, V16 and V17: the write path and provider integration."""

    def flow(self, request, issues=None, jira_results=None, approve=True):
        """Plan, review, and (optionally) approve and execute a request with mocked providers and reviewers."""
        plan = make_plan(request, ["github", "repository"])
        pending_fake = FakeSwytchcode(issues=issues)
        with patch.object(app, "run_swytchcode", side_effect=pending_fake), patch.object(app, "hermes_reviewer", side_effect=reviewers()), \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL):
            pending = app.run_plan(plan)
        if not approve or pending["status"] != "confirmation_required":
            return pending, None, pending_fake
        fake = FakeSwytchcode(issues=issues, jira_results=jira_results)
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "JIRA_BASE_URL", "https://example.atlassian.net"):
            final = app.run_plan(pending["plan"], confirmed=True)
        return pending, final, fake

    def calls_to(self, fake, name):
        return [args for tool, args in fake.calls if tool == app.TOOLS[name]]

    # V12: explicit write intent ------------------------------------------------------------
    def test_write_intent_table(self):
        cases = {
            "Create Jira tickets for the critical issues.": {"jira"},
            "Open a ticket for issue #2": {"jira"},
            "File a Jira issue for the login bug": {"jira"},
            "Track the auth bypass in Jira": {"jira"},
            "Notify the engineering team in Slack": {"slack"},
            "Tell the engineering team about the critical issue.": {"slack"},
            "Post a summary to #security": {"slack"},
            "Send the finding to the incident channel": {"slack"},
            "Create a Jira ticket and notify the team in Slack": {"jira", "slack"},
            "Find critical and high issues and escalate them.": {"jira", "slack"},
            "Escalate the authentication bypass": {"jira", "slack"},
            "Track down the login bug and explain the error message": set(),
            "Explain the error message returned by the API": set(),
            "Find open issues and identify which ones need immediate attention.": set(),
            "Investigate the authentication bypass issue in this repository.": set(),
            "Create an issue summary for me": set(),
            "Which issues were escalated last week?": set(),
            "Explain our escalation policy": set(),
            "Post the stack trace from the logs here": set(),
            "Do not create Jira tickets, just tell me what is wrong": set(),
            "Investigate the bug but don't notify the team in Slack": set(),
            "Check the slack variable in the scheduler": set(),
            "Review the post-mortem notes for the team": set(),
        }
        for request, expected in cases.items():
            with self.subTest(request=request):
                self.assertEqual(app.explicitly_requested_writes(request), expected)

    def test_bulk_intent_table(self):
        cases = {"Create Jira tickets for the critical issues.": True, "Create a Jira ticket for each critical bug": True,
                 "Escalate them": True, "Find critical and high issues and escalate them.": True,
                 "Create a Jira ticket for issue #2": False, "Escalate the authentication bypass": False,
                 "Open a ticket for the login bug": False}
        for request, expected in cases.items():
            with self.subTest(request=request):
                self.assertEqual(app.bulk_requested(request), expected)

    def test_false_positive_intent_runs_select_no_integrations(self):
        pending, final, fake = self.flow("Track down the login bug and explain the error message")
        self.assertEqual(pending["status"], "complete")
        self.assertEqual(pending["plan"]["write_actions"], [])
        self.assertIsNone(final)
        self.assertEqual(fake.writes(), [])

    # V10: exact issue targeting ---------------------------------------------------------------
    def test_requested_issue_number_is_exact(self):
        cases = {"Investigate #2": 2, "Look at issue 20": 20, "issue #2 again": 2, "Issue number 7": 7, "fix #20 first": 20,
                 "Regression since 2021": None, "Find critical bugs from 2024": None, "Find open issues": None}
        for request, expected in cases.items():
            with self.subTest(request=request):
                self.assertEqual(app.requested_issue_number(request), expected)

    def test_target_issue_never_matches_a_different_number(self):
        analysis = app.summarize_github({"ok": True, "response": {"items": [
            {"title": "Authentication bypass in admin", "number": 20, "html_url": "issue/20", "state": "open", "body": "security"},
            {"title": "Crash in 2022 export", "number": 2022, "html_url": "issue/2022", "state": "open", "body": "crash"},
            {"title": "API returns 500", "number": 2, "html_url": "issue/2", "state": "open", "body": "error"}]}})
        self.assertEqual(app.target_issue(analysis, "Investigate #2 (reported in 2022)")[0]["number"], 2)
        target, note = app.target_issue(analysis, "Investigate #3")
        self.assertIsNone(target)
        self.assertIn("#3", note)
        self.assertEqual(app.target_issue(analysis, "Find the worst open issue")[0]["number"], 20)  # most severe, not first

    def test_finding_and_ticket_follow_the_investigated_issue(self):
        issues = [
            {"title": "Authentication bypass in admin panel", "number": 20, "html_url": "https://github.test/issues/20", "state": "open", "body": "security"},
            {"title": "Login accepts a development bypass token", "number": 2, "html_url": "https://github.test/issues/2", "state": "open", "body": "unhandled error"},
        ]
        pending, final, fake = self.flow("Investigate issue #2 and create a Jira ticket for it", issues=issues, jira_results=[jira_created("KAN-9")])
        finding = pending["finding"]
        self.assertEqual(finding["title"], "Login accepts a development bypass token")
        self.assertEqual(finding["issue"]["number"], 2)
        self.assertEqual(pending["plan"]["target_issue"]["number"], 2)
        jira_calls = self.calls_to(fake, "jira")
        self.assertEqual(len(jira_calls), 1)
        fields = jira_calls[0]["body"]["fields"]
        self.assertIn("Login accepts a development bypass token", fields["summary"])
        description = app.json.dumps(fields["description"])
        for expected in ("GitHub issue #2", "https://github.test/issues/2", "Root cause: Hard-coded bypass token.", "auth.py lines 2-3", "DEV-BYPASS", "Recommended fix:"):
            self.assertIn(expected, description)
        self.assertNotIn("admin panel", description)
        self.assertEqual(final["status"], "complete")

    def test_single_ticket_request_does_not_ticket_other_severe_issues(self):
        pending, final, fake = self.flow("Open a Jira ticket for the authentication bypass")
        self.assertEqual([t["issue_number"] for t in pending["plan"]["jira_tickets"]], [1])
        self.assertEqual(len(self.calls_to(fake, "jira")), 1)

    def test_bulk_request_tickets_only_the_reviewed_issue_and_blocks_the_rest(self):
        pending, final, fake = self.flow("Find critical and high issues and escalate them.", jira_results=[jira_created("KAN-1")])
        self.assertEqual([(t["issue_number"], t["reviewed"]) for t in pending["plan"]["jira_tickets"]], [(1, True)])
        blocked = pending["plan"]["blocked_jira_tickets"]
        self.assertEqual([b["issue_number"] for b in blocked], [2])  # #3 is NORMAL: not part of the request
        self.assertIn("Not independently reviewed", blocked[0]["reason"])
        self.assertIn("Run DevPilot on issue #2", blocked[0]["reason"])
        self.assertIn("not be ticketed", app.json.dumps(pending["timeline"]))
        self.assertEqual(final["status"], "complete")
        self.assertEqual(len(self.calls_to(fake, "jira")), 1)
        self.assertEqual(len(self.calls_to(fake, "slack")), 1)

    def test_critical_only_bulk_request_excludes_high_issues(self):
        pending, _, _ = self.flow("Create a Jira ticket for each critical bug", approve=False)
        self.assertEqual([t["issue_number"] for t in pending["plan"]["jira_tickets"]], [1])

    def test_jira_payload_is_well_formed(self):
        long_title = "A" * 400 + "\nsecond line"
        finding = {"consensus": {"agreement": "3/3"}, "confidence": "HIGH", "root_cause": "x", "evidence": VALID_EVIDENCE, "recommended_fix": "y", "validation_plan": ["", "z"]}
        ticket = app.jira_ticket("KAN", {"title": long_title, "number": 5, "url": "u", "severity": "HIGH", "body": ""}, finding)
        with self.assertRaises(ValueError):
            app.jira_ticket("KAN", {"title": "t", "number": 6}, None)
        summary = ticket["body"]["fields"]["summary"]
        self.assertLessEqual(len(summary), 255)
        self.assertNotIn("\n", summary)

        def texts(node):
            if isinstance(node, dict):
                if node.get("type") == "text":
                    yield node["text"]
                for child in node.get("content", []):
                    yield from texts(child)
        self.assertTrue(all(text.strip() for text in texts(ticket["body"]["fields"]["description"])))

    # V9: Jira-to-Slack dependency ---------------------------------------------------------------
    def test_jira_failure_withholds_dependent_slack_notification(self):
        request = "Create a Jira ticket for the authentication bypass and notify the engineering team in Slack"
        _, final, fake = self.flow(request, jira_results=[AUTH_FAILURE])
        self.assertEqual(final["status"], "error")
        self.assertEqual(self.calls_to(fake, "slack"), [])
        self.assertIn("no Jira issue was created", final["error"])
        self.assertIn("swytchcode auth connect jira", final["error"])
        self.assertIn("Slack notification withheld", final["error"])
        self.assertEqual(final["timeline"][-2]["status"], "skipped")

    def test_one_confirmed_issue_cannot_authorize_tickets_for_other_issues(self):
        pending, _, _ = self.flow("Find critical and high issues and escalate them.", approve=False)
        plan = pending["plan"]
        other = app.summarize_github(github_result())["issues"][1]
        forged_reviewed = app.jira_ticket("KAN", other, plan["finding"])  # the #1 finding attached to #2
        cases = {
            "extra ticket for another issue": [*plan["jira_tickets"], forged_reviewed],
            "ticket only for another issue": [forged_reviewed],
            "target ticket marked unreviewed": [{**plan["jira_tickets"][0], "reviewed": False}],
            "two tickets for the target": [plan["jira_tickets"][0], plan["jira_tickets"][0]],
        }
        for name, tickets in cases.items():
            with self.subTest(name=name):
                forged = {**plan, "jira_tickets": tickets}
                self.assertFalse(app.approved_plan_is_reviewed(forged))
                fake = FakeSwytchcode()
                with patch.object(app, "run_swytchcode", side_effect=fake):
                    result = app.run_plan(forged, confirmed=True)
                self.assertEqual(result["status"], "error")
                self.assertEqual(fake.calls, [])

    def test_independent_slack_request_is_sent_without_jira(self):
        _, final, fake = self.flow("Notify the engineering team in Slack about the critical issue.")
        self.assertEqual(final["status"], "complete")
        self.assertEqual(self.calls_to(fake, "jira"), [])
        text = self.calls_to(fake, "slack")[0]["body"]["text"]
        self.assertIn("DevPilot confirmed finding: Authentication bypass in login flow (#1, CRITICAL)", text)
        self.assertIn("Root cause: Hard-coded bypass token.", text)
        self.assertIn("auth.py lines 2-3", text)
        self.assertNotIn("Jira", text)

    def test_slack_message_after_jira_links_configured_ticket(self):
        request = "Create a Jira ticket for the authentication bypass and notify the engineering team in Slack"
        _, final, fake = self.flow(request, jira_results=[jira_created("KAN-7")])
        self.assertEqual(final["status"], "complete")
        self.assertIn("Jira: KAN-7 (https://example.atlassian.net/browse/KAN-7)", self.calls_to(fake, "slack")[0]["body"]["text"])

    def test_no_eligible_issue_sends_no_notification(self):
        pending, final, fake = self.flow("Notify the engineering team in Slack about open issues.", issues=[])
        self.assertEqual(pending["status"], "complete")
        self.assertEqual(pending["plan"]["write_actions"], [])
        self.assertIsNone(final)
        self.assertEqual(fake.writes(), [])
        self.assertIsNone(app.slack_message(None, None, None))
        self.assertIsNone(app.slack_message({"root_cause": "x"}, {"title": "  "}, None))

    def test_requested_issue_missing_blocks_writes(self):
        pending, final, fake = self.flow("Create a Jira ticket for issue #42")
        self.assertEqual(pending["plan"]["write_actions"], [])
        self.assertIn("Requested issue #42", app.json.dumps(pending["timeline"]))
        self.assertIsNone(final)

    # V15: Slack escaping ----------------------------------------------------------------------
    def test_slack_mentions_and_links_in_titles_are_escaped(self):
        issues = [{"title": "<!channel> Authentication bypass & <https://evil.test|click>", "number": 1, "html_url": "https://github.test/issues/1", "state": "open", "body": "security"}]
        _, final, fake = self.flow("Notify the engineering team in Slack", issues=issues)
        text = self.calls_to(fake, "slack")[0]["body"]["text"]
        self.assertNotIn("<!channel>", text)
        self.assertNotIn("<https://evil.test", text)
        self.assertIn("&lt;!channel&gt; Authentication bypass &amp; &lt;https://evil.test|click&gt;", text)
        self.assertTrue(text.startswith("DevPilot confirmed finding: "))
        self.assertEqual(app.slack_escape("Fix <@U123> & <!here>"), "Fix &lt;@U123&gt; &amp; &lt;!here&gt;")
        self.assertEqual(app.slack_escape("Plain readable text."), "Plain readable text.")

    # V16: Jira browser links --------------------------------------------------------------------
    def test_jira_browse_url_uses_validated_configuration(self):
        cases = {
            "https://example.atlassian.net": "https://example.atlassian.net/browse/KAN-4",
            "https://example.atlassian.net/": "https://example.atlassian.net/browse/KAN-4",
            "https://jira.example.com/jira": "https://jira.example.com/jira/browse/KAN-4",
            "": None, "example.atlassian.net": None, "http://example.atlassian.net": None,
            "https://user:pass@example.atlassian.net": None, "https://example.atlassian.net/?x=1": None,
            "javascript:alert(1)": None, "https://example.atlassian.net/<script>": None,
        }
        for base, expected in cases.items():
            with self.subTest(base=base), patch.object(app, "JIRA_BASE_URL", base):
                self.assertEqual(app.jira_browse_url("KAN-4"), expected)
        with patch.object(app, "JIRA_BASE_URL", "https://example.atlassian.net"):
            for key in ("kan-4", "KAN-0", "KAN", "../KAN-4", None):
                self.assertIsNone(app.jira_browse_url(key))

    def test_missing_jira_configuration_still_reports_the_key(self):
        with patch.object(app, "JIRA_BASE_URL", ""):
            ref = app.extract_jira(jira_created("KAN-5"))
        self.assertEqual((ref["key"], ref["url"]), ("KAN-5", None))
        self.assertEqual(ref["api_url"], "https://example.atlassian.net/rest/api/3/issue/KAN-5")  # kept, but never used as a browser link
        self.assertIn("Jira: KAN-5", app.slack_message({"root_cause": "x", "evidence": []}, {"title": "T", "number": 1, "severity": "HIGH"}, [ref]))

    # V17: Swytchcode error classification ---------------------------------------------------------
    def exec_failure(self, stderr="", stdout="", returncode=1, tool="jira.api.issue.create"):
        with patch.object(app.subprocess, "run", return_value=completed(stdout, returncode, stderr)):
            return app.run_swytchcode(tool, {"body": {}})

    def test_auth_failure_is_classified_with_reconnect_guidance(self):
        result = self.exec_failure('{"error":"OAuth token expired","category":"auth","retryable":false}')
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "auth")
        self.assertFalse(result["error"]["retryable"])
        self.assertEqual(result["error"]["provider"], "jira")
        self.assertIn("swytchcode auth connect jira", result["error"]["action"])
        self.assertIn("auth: OAuth token expired", app.describe_failure(result))

    def test_structured_error_after_log_lines_and_retryability_are_preserved(self):
        result = self.exec_failure('debug: sending request\n{"error":"upstream timeout","category":"network","retryable":true}', tool="slack.chat.postmessage.create")
        self.assertEqual((result["error"]["category"], result["error"]["retryable"]), ("network", True))
        self.assertIn("safe to retry", result["error"]["action"])
        self.assertEqual(result["error"]["provider"], "slack")

    def test_unstructured_unknown_and_local_failures_are_classified(self):
        self.assertEqual(self.exec_failure("segfault")["error"]["category"], "internal")
        self.assertEqual(self.exec_failure('{"error":"x","category":"weird"}')["error"]["category"], "internal")
        with patch.object(app.subprocess, "run", side_effect=subprocess.TimeoutExpired("swytchcode", 90)):
            timeout = app.run_swytchcode("jira.api.issue.create", {"body": {}})
        self.assertEqual((timeout["error"]["category"], timeout["error"]["retryable"]), ("network", True))
        with patch.object(app.subprocess, "run", side_effect=OSError("No such file")):
            missing = app.run_swytchcode("jira.api.issue.create", {"body": {}})
        self.assertEqual(missing["error"]["category"], "internal")
        self.assertEqual(self.exec_failure(stdout="not json", returncode=0)["error"]["category"], "internal")

    def test_errors_never_expose_credentials_or_failed_response_bodies(self):
        stderr = '{"error":"401 from https://api.test Authorization: Bearer eyJhbGciOi.payload.sig token=abc123secret api_key: \\"k-999\\" xoxb-1234-abcd ghp_ABCDEF123","category":"auth","retryable":false}'
        result = self.exec_failure(stderr, stdout='{"errorMessages":["denied"],"debug":{"password":"hunter2","session":"s3cr3t-session"}}')
        dumped = app.json.dumps(result)
        for secret in ("eyJhbGciOi", "abc123secret", "k-999", "xoxb-1234", "ghp_ABCDEF", "hunter2", "s3cr3t-session"):
            self.assertNotIn(secret, dumped)
        self.assertIn("[REDACTED]", result["error"]["message"])
        self.assertEqual(result["error"]["category"], "auth")

    def test_successful_calls_are_unchanged(self):
        result = self.exec_failure(stdout='{"data":{"key":"KAN-1"}}', returncode=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["response"], {"data": {"key": "KAN-1"}})
        self.assertNotIn("error", result)



def open_issue(number, title=None, body="Add a loading indicator."):
    return {"title": title or f"Improve dashboard widget {number}", "number": number, "html_url": f"https://github.test/issues/{number}", "state": "open", "body": body}


def source_file(text, **extra):
    return {"ok": True, "response": {"data": {"type": "file", "content": text, **extra}}}


class PagedSwytchcode(FakeSwytchcode):
    """Serves GitHub search pages from a list of issues, plus an optional set of repository files."""

    def __init__(self, issues, total=True, fail_page=None, repeat=False, flag_incomplete=False, files=None, **kwargs):
        super().__init__(**kwargs)
        self.all, self.total, self.fail_page, self.repeat, self.flag_incomplete, self.files = issues, total, fail_page, repeat, flag_incomplete, files

    def __call__(self, tool, args):
        if tool == app.TOOLS["github"]:
            self.calls.append((tool, args))
            page, per_page = args["page"], args["per_page"]
            if page == self.fail_page:
                return app.swytchcode_failure(tool, app.swytchcode_error(tool, '{"error":"Bad credentials","category":"auth","retryable":false}'))
            chunk = self.all[:per_page] if self.repeat else self.all[(page - 1) * per_page:page * per_page]
            response = {"items": chunk, "incomplete_results": self.flag_incomplete}
            if self.total:
                response["total_count"] = len(self.all) if self.total is True else self.total
            return {"ok": True, "response": response}
        if tool == app.TOOLS["repository"] and self.files is not None:
            self.calls.append((tool, args))
            if args["path"] == ".":
                return {"ok": True, "response": {"data": [{"path": path, "type": "file"} for path in self.files]}}
            return self.files[args["path"]]
        return super().__call__(tool, args)

    def pages(self):
        return [args["page"] for tool, args in self.calls if tool == app.TOOLS["github"]]


class IssueRetrievalTests(unittest.TestCase):
    """Requested-issue discovery across pages, with exact matching preserved."""

    def test_requested_issue_beyond_the_first_page_is_found_and_targeted(self):
        issues = [open_issue(n) for n in range(1, 151)]
        issues[136] = open_issue(137, "Authentication bypass in login flow", "Users may bypass authentication.")
        fake = PagedSwytchcode(issues)
        plan = make_plan("Investigate issue #137 and create a Jira ticket for it", ["github", "repository"])
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=reviewers()) as reviewer, \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL):
            result = app.run_plan(plan)
        self.assertEqual(fake.pages(), [1, 2])
        self.assertEqual(len(result["analysis"]["issues"]), 150)
        self.assertEqual(result["plan"]["target_issue"]["number"], 137)
        self.assertEqual(result["finding"]["issue"]["number"], 137)
        self.assertEqual([t["issue_number"] for t in result["plan"]["jira_tickets"]], [137])
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(app.json.loads(reviewer.call_args.args[0])["target_issue"]["number"], 137)

    def test_issue_search_requests_full_sorted_pages(self):
        fake = PagedSwytchcode([open_issue(1)])
        with patch.object(app, "run_swytchcode", side_effect=fake):
            app.fetch_open_issues("o/r")
        args = fake.calls[0][1]
        self.assertEqual((args["per_page"], args["page"], args["sort"], args["order"]), (100, 1, "created", "asc"))
        self.assertEqual(args["q"], "repo:o/r is:issue is:open")

    def fetch(self, fake):
        with patch.object(app, "run_swytchcode", side_effect=fake):
            return app.fetch_open_issues("o/r")

    def test_pagination_terminates_on_total_count_short_page_or_empty_page(self):
        for count, total, expected_pages in ((200, True, [1, 2]), (200, False, [1, 2, 3]), (250, True, [1, 2, 3]), (0, True, [1]), (99, False, [1])):
            with self.subTest(count=count, total=total):
                fake = PagedSwytchcode([open_issue(n) for n in range(1, count + 1)], total=total)
                result = self.fetch(fake)
                self.assertEqual(fake.pages(), expected_pages)
                self.assertEqual(len(result["response"]["items"]), count)
                self.assertEqual(result["pagination"]["stop"], "exhausted")
                self.assertTrue(result["pagination"]["complete"])

    def test_duplicate_page_stops_and_marks_the_list_incomplete(self):
        fake = PagedSwytchcode([open_issue(n) for n in range(1, 101)], total=500, repeat=True)
        result = self.fetch(fake)
        self.assertEqual(fake.pages(), [1, 2])
        self.assertEqual(len(result["response"]["items"]), 100)
        self.assertEqual((result["pagination"]["stop"], result["pagination"]["complete"]), ("duplicate_page", False))

    def test_page_limit_stops_and_marks_the_list_incomplete(self):
        fake = PagedSwytchcode([open_issue(n) for n in range(1, 1201)])
        result = self.fetch(fake)
        self.assertEqual(fake.pages(), list(range(1, 11)))
        self.assertEqual((result["pagination"]["stop"], result["pagination"]["complete"]), ("page_limit", False))

    def test_later_page_error_keeps_earlier_pages_and_marks_incomplete(self):
        fake = PagedSwytchcode([open_issue(n) for n in range(1, 251)], fail_page=2)
        result = self.fetch(fake)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["response"]["items"]), 100)
        self.assertEqual((result["pagination"]["stop"], result["pagination"]["complete"]), ("error", False))
        self.assertEqual(result["pagination"]["error"]["category"], "auth")

    def test_first_page_error_fails_the_run_with_a_classified_reason(self):
        fake = PagedSwytchcode([open_issue(1)], fail_page=1)
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer") as reviewer:
            result = app.run_plan(make_plan("Find open issues", ["github"]))
        self.assertEqual(result["status"], "error")
        self.assertIn("swytchcode auth connect github", result["error"])
        reviewer.assert_not_called()

    def test_github_incomplete_results_flag_marks_the_list_incomplete(self):
        result = self.fetch(PagedSwytchcode([open_issue(1)], flag_incomplete=True))
        self.assertFalse(result["pagination"]["complete"])

    def test_missing_issue_message_depends_on_whether_the_list_is_complete(self):
        complete = app.summarize_github(self.fetch(PagedSwytchcode([open_issue(n) for n in range(1, 30)])))
        target, note = app.target_issue(complete, "Investigate #250")
        self.assertIsNone(target)
        self.assertIn("is not an open issue in this repository", note)
        partial = app.summarize_github(self.fetch(PagedSwytchcode([open_issue(n) for n in range(1, 251)], fail_page=2)))
        target, note = app.target_issue(partial, "Investigate #250")
        self.assertIsNone(target)
        self.assertIn("incomplete (error)", note)
        self.assertIn("100 open issue(s) retrieved", note)
        self.assertEqual(app.target_issue(partial, "Investigate #20")[0]["number"], 20)  # exact match, not #2 or #200

    def test_missing_requested_issue_blocks_writes_in_a_run(self):
        fake = PagedSwytchcode([open_issue(n) for n in range(1, 251)], fail_page=3)
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=reviewers()), \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL):
            result = app.run_plan(make_plan("Create a Jira ticket for issue #260", ["github", "repository"]))
        self.assertEqual(result["plan"]["write_actions"], [])
        self.assertIn("issue list is incomplete", app.json.dumps(result["timeline"]))
        self.assertEqual(fake.writes(), [])


class ReviewContextTests(unittest.TestCase):
    """Per-section context budgets, whole-file evidence, one snapshot for every reviewer."""

    def run_with(self, fake, request="Create a Jira ticket for the authentication bypass", reviewer_fn=None, proposal=PROPOSAL):
        contexts = []

        def reviewer(context, role, **_):
            contexts.append(context)
            return (reviewer_fn or (lambda context, role, **_: review(role)))(context, role)

        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=reviewer), \
                patch.object(app, "hermes_proposal", return_value=proposal) as proposer:
            result = app.run_plan(make_plan(request, ["github", "repository"]))
        return result, contexts, proposer

    def critical_issue(self, body="Users may bypass authentication."):
        return open_issue(1, "Authentication bypass in login flow", body)

    def test_every_reviewer_and_the_proposal_receive_the_same_snapshot(self):
        result, contexts, proposer = self.run_with(PagedSwytchcode([self.critical_issue(), open_issue(2)]))
        self.assertEqual(len(contexts), 3)
        self.assertEqual(len(set(contexts)), 1)
        proposer.assert_called_once_with(contexts[0])
        context = app.json.loads(contexts[0])
        self.assertEqual(context["target_issue"]["number"], 1)
        self.assertEqual([f["path"] for f in context["source_files"]], ["auth.py"])
        snapshot = result["council"]["evidence_snapshot"]
        self.assertEqual(snapshot["sha256"], app.hashlib.sha256(contexts[0].encode()).hexdigest())
        self.assertEqual(snapshot["files"][0]["sha256"], app.hashlib.sha256(AUTH_SOURCE.encode()).hexdigest())
        self.assertEqual(result["finding"]["evidence_snapshot"], snapshot["sha256"])
        self.assertEqual(result["finding"]["issue"]["number"], context["target_issue"]["number"])

    def test_numbered_source_matches_the_lines_used_for_evidence_checks(self):
        _, contexts, _ = self.run_with(PagedSwytchcode([self.critical_issue()]))
        numbered = app.json.loads(contexts[0])["source_files"][0]["numbered_source"].splitlines()
        self.assertEqual(numbered[1], '2|     if bypass_token == "DEV-BYPASS":')
        self.assertEqual(len(numbered), len(AUTH_SOURCE.splitlines()))

    def test_large_issue_and_issue_list_are_budgeted_and_source_stays_intact(self):
        issues = [self.critical_issue(body="Users may bypass authentication. " + "x" * 60_000)] + [open_issue(n, f"Widget {n} " + "y" * 200) for n in range(2, 900)]
        result, contexts, _ = self.run_with(PagedSwytchcode(issues))
        context = app.json.loads(contexts[0])  # valid JSON, never cut mid-structure
        self.assertLessEqual(len(contexts[0].encode()), app.CONTEXT_BUDGET)
        self.assertTrue(context["target_issue"]["body_truncated"])
        self.assertGreater(context["other_open_issues"]["omitted"], 0)
        self.assertEqual(context["source_files"][0]["numbered_source"].count("\n") + 1, len(AUTH_SOURCE.splitlines()))
        self.assertEqual(result["council"]["decision"], "CONFIRMED")

    def test_file_too_large_for_the_budget_is_omitted_whole_not_truncated(self):
        big = "\n".join(f"value_{n} = '{'z' * 40}'" for n in range(3000))
        fake = PagedSwytchcode([self.critical_issue()], files={"auth.py": source_file(AUTH_SOURCE), "big.py": source_file(big)})
        cites_big = lambda context, role, **_: review(role, evidence=[*VALID_EVIDENCE, {"path": "big.py", "line_start": 1, "line_end": 1, "snippet": "value_0"}])
        result, contexts, _ = self.run_with(fake, reviewer_fn=cites_big)
        context = app.json.loads(contexts[0])
        self.assertEqual([f["path"] for f in context["source_files"]], ["auth.py"])
        self.assertEqual(context["omitted_source_files"][0]["path"], "big.py")
        self.assertIn("omitted rather than truncated", context["omitted_source_files"][0]["reason"])
        self.assertNotIn("value_0", contexts[0])
        self.assertEqual(result["council"]["decision"], "REVIEW_REQUIRED")  # citing a file reviewers never saw
        self.assertEqual(result["plan"]["write_actions"], [])

    def test_no_source_that_fits_skips_the_review_and_blocks_writes(self):
        huge = AUTH_SOURCE + "\n".join(f"# padding line {n} {'p' * 60}" for n in range(2000))
        result, contexts, proposer = self.run_with(PagedSwytchcode([self.critical_issue()], files={"auth.py": source_file(huge)}))
        self.assertEqual(contexts, [])
        proposer.assert_not_called()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["council"]["decision"], "REVIEW_REQUIRED")
        self.assertIn("evidence is never truncated", result["council"]["summary"])
        self.assertEqual(result["plan"]["write_actions"], [])

    def test_unavailable_or_incomplete_sources_are_not_evidence(self):
        cases = {
            "content omitted": (source_file("", encoding="none", size=2_000_000), "content was not returned"),
            "size mismatch": (source_file(AUTH_SOURCE, size=len(AUTH_SOURCE) + 50), "content is incomplete"),
            "binary": (source_file(app.base64.b64encode(b"\xff\xfe\x00binary").decode(), encoding="base64"), "not UTF-8"),
            "fetch failed": (app.swytchcode_failure("github.content.get", app.swytchcode_error("github.content.get", '{"error":"Not Found","category":"not_found"}')), "retrieval failed"),
            "directory": ({"ok": True, "response": {"data": {"type": "dir", "content": "x"}}}, "not a single file"),
        }
        for name, (response, reason) in cases.items():
            with self.subTest(name=name):
                self.assertIn(reason, app.source_text(response)[1])
                result, contexts, _ = self.run_with(PagedSwytchcode([self.critical_issue()], files={"auth.py": response}))
                self.assertEqual(contexts, [])
                self.assertIn(reason, result["council"]["summary"])
                self.assertEqual(result["plan"]["write_actions"], [])
                self.assertEqual(result["plan"]["_unavailable_files"][0]["path"], "auth.py")

    def test_base64_source_with_matching_size_is_accepted(self):
        encoded = app.base64.b64encode(AUTH_SOURCE.encode()).decode()
        wrapped = "\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60))  # GitHub wraps base64 lines
        self.assertEqual(app.source_text(source_file(wrapped, encoding="base64", size=len(AUTH_SOURCE)))[0], AUTH_SOURCE)

    def test_no_repository_inspection_means_no_review_and_no_writes(self):
        fake = PagedSwytchcode([self.critical_issue()])
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer") as reviewer:
            result = app.run_plan(make_plan("Create a Jira ticket for the authentication bypass", ["github"]))
        reviewer.assert_not_called()
        self.assertIn("No source files were retrieved", result["council"]["summary"])
        self.assertEqual(result["plan"]["write_actions"], [])

    def test_proposed_fix_must_match_retrieved_files_and_evidence(self):
        cases = {
            "unretrieved file": {**PROPOSAL, "diff": "--- a/src/login.py\n+++ b/src/login.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"},
            "hunk past end of file": {**PROPOSAL, "diff": "--- a/auth.py\n+++ b/auth.py\n@@ -40,2 +40,1 @@\n-a\n-b\n+c\n"},
            "empty diff": {**PROPOSAL, "diff": ""},
            "file list not in diff or source": {**PROPOSAL, "files": [{"path": "src/other.py", "reason": "x"}]},
        }
        for name, proposal in cases.items():
            with self.subTest(name=name):
                fake = PagedSwytchcode([self.critical_issue()])
                result, _, _ = self.run_with(fake, proposal=proposal)
                self.assertEqual(result["status"], "error")
                self.assertIn("inconsistent with the retrieved source", result["error"])
                self.assertEqual(fake.writes(), [])

    def test_proposed_fix_may_add_a_new_test_file(self):
        proposal = {**PROPOSAL, "files": [{"path": "auth.py", "reason": "fix"}, {"path": "tests/test_auth.py", "reason": "regression"}],
                    "diff": "--- a/auth.py\n+++ b/auth.py\n@@ -2,2 +2,0 @@\n-    if bypass_token == \"DEV-BYPASS\":\n-        return True\n--- /dev/null\n+++ b/tests/test_auth.py\n@@ -0,0 +1,1 @@\n+def test_bypass(): pass\n"}
        result, _, _ = self.run_with(PagedSwytchcode([self.critical_issue()]), proposal=proposal)
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(result["finding"]["proposed_fix"], proposal)



class SavedPlanTests(unittest.TestCase):
    """Phase 5 backend support: the approval preview shows the saved plan that actually executes."""

    def pending(self, request):
        fake = FakeSwytchcode()
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=reviewers()), \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL):
            return app.run_plan(make_plan(request, ["github", "repository"]))

    def execute(self, plan, jira_results=None):
        fake = FakeSwytchcode(jira_results=jira_results)
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "JIRA_BASE_URL", "https://example.atlassian.net"):
            return app.run_plan(plan, confirmed=True), fake

    def sent_slack(self, fake):
        return [args["body"] for tool, args in fake.calls if tool == app.TOOLS["slack"]]

    def test_slack_only_plan_saves_the_exact_text_that_is_sent(self):
        pending = self.pending("Notify the engineering team in Slack about the critical issue.")
        saved = pending["plan"]["slack_plan"]
        self.assertEqual((saved["channel"], saved["appends_jira_links"], saved["requires_jira_success"]), ("#engineering", False, False))
        final, fake = self.execute(pending["plan"])
        self.assertEqual(self.sent_slack(fake), [{"channel": "#engineering", "text": saved["text"]}])
        self.assertEqual(final["actions"]["slack"]["text"], saved["text"])

    def test_slack_after_jira_sends_saved_text_plus_only_the_created_ticket_line(self):
        pending = self.pending("Create a Jira ticket for the authentication bypass and notify the engineering team in Slack")
        saved = pending["plan"]["slack_plan"]
        self.assertTrue(saved["appends_jira_links"] and saved["requires_jira_success"])
        self.assertNotIn("Jira:", saved["text"])
        final, fake = self.execute(pending["plan"], jira_results=[jira_created("KAN-3")])
        self.assertEqual(self.sent_slack(fake)[0]["text"], saved["text"] + "\nJira: KAN-3 (https://example.atlassian.net/browse/KAN-3)")

    def test_execution_uses_the_saved_text_not_a_rebuilt_one(self):
        pending = self.pending("Notify the engineering team in Slack about the critical issue.")
        plan = {**pending["plan"], "finding": {**pending["plan"]["finding"], "root_cause": "CHANGED AFTER APPROVAL"}}
        _, fake = self.execute(plan)
        self.assertNotIn("CHANGED AFTER APPROVAL", self.sent_slack(fake)[0]["text"])
        self.assertEqual(self.sent_slack(fake)[0]["text"], pending["plan"]["slack_plan"]["text"])

    def test_approval_requires_a_saved_slack_message(self):
        pending = self.pending("Notify the engineering team in Slack about the critical issue.")
        for broken in (None, {"channel": "#engineering", "text": ""}):
            with self.subTest(slack_plan=broken):
                plan = {**pending["plan"], "slack_plan": broken}
                self.assertFalse(app.approved_plan_is_reviewed(plan))
                result, fake = self.execute(plan)
                self.assertEqual(result["status"], "error")
                self.assertEqual(fake.calls, [])

    def test_saved_tickets_carry_preview_metadata_and_unchanged_payload(self):
        pending = self.pending("Find critical and high issues and escalate them.")
        tickets, blocked = pending["plan"]["jira_tickets"], pending["plan"]["blocked_jira_tickets"]
        self.assertEqual([(t["issue_number"], t["severity"], t["reviewed"]) for t in tickets], [(1, "CRITICAL", True)])
        self.assertEqual((blocked[0]["issue_title"], blocked[0]["severity"]), ("API returns 500 on malformed JSON", "HIGH"))
        self.assertEqual(tickets[0]["issue_url"], "issue/1")
        final, fake = self.execute(pending["plan"], jira_results=[jira_created("KAN-1")])
        self.assertEqual([args["body"] for tool, args in fake.calls if tool == app.TOOLS["jira"]], [t["body"] for t in tickets])

    def test_created_jira_issues_report_key_and_browser_link(self):
        pending = self.pending("Create a Jira ticket for the authentication bypass")
        final, _ = self.execute(pending["plan"], jira_results=[jira_created("KAN-8")])
        item = final["actions"]["jira"][0]
        self.assertEqual((item["ref"]["key"], item["ref"]["url"]), ("KAN-8", "https://example.atlassian.net/browse/KAN-8"))
        failed, _ = self.execute(self.pending("Create a Jira ticket for the authentication bypass")["plan"], jira_results=[AUTH_FAILURE])
        self.assertNotIn("ref", failed["actions"]["jira"][0])


class CancelTests(unittest.TestCase):
    """POST /api/cancel: atomic, single-winner against approval, never interrupts running work."""

    def setUp(self):
        self.token = app.uuid.uuid4().hex

    def plan_in(self, state):
        plan = {**make_plan("Create a Jira ticket for the authentication bypass", ["github", "repository"]), "token": self.token,
                "created_at": time.time(), "state": state, "writes_require_confirmation": state == "awaiting_approval", "write_actions": ["jira"]}
        app.PENDING[self.token] = plan
        app.start_run(self.token)
        return plan

    def cancel(self):
        handler = FakeHandler()
        handler.cancel({"token": self.token})
        return handler.responses[0]

    def test_cancel_awaiting_plan_records_terminal_result_and_blocks_approval(self):
        self.plan_in("awaiting_approval")
        status, result = self.cancel()
        self.assertEqual((status, result["status"]), (200, "cancelled"))
        self.assertIn("no Jira or Slack action was sent", result["timeline"][-1]["detail"])
        run = app.public_status(self.token)
        self.assertEqual((run["status"], run["terminal"], run["result_ready"]), ("cancelled", True, True))
        handler = FakeHandler()
        with patch.object(app, "run_plan") as run_plan:
            handler.execute({"token": self.token, "confirmed": True}, confirmed=True)
        self.assertEqual(handler.responses[0][0], 409)
        run_plan.assert_not_called()

    def test_cancel_planned_plan_prevents_it_from_starting(self):
        self.plan_in("planned")
        self.assertEqual(self.cancel()[0], 200)
        handler = FakeHandler()
        with patch.object(app.threading, "Thread") as thread:
            handler.execute({"token": self.token})
        self.assertEqual(handler.responses[0][0], 409)
        thread.assert_not_called()

    def test_running_executing_done_and_cancelled_plans_cannot_be_cancelled(self):
        for state in ("running", "executing", "done", "cancelled"):
            with self.subTest(state=state):
                self.plan_in(state)
                status, payload = self.cancel()
                self.assertEqual(status, 409)
                self.assertIn(state, payload["error"])
                self.assertEqual(app.PENDING[self.token]["state"], state)

    def test_repeated_cancel_succeeds_once(self):
        self.plan_in("awaiting_approval")
        self.assertEqual([self.cancel()[0] for _ in range(3)], [200, 409, 409])

    def test_unknown_plan_is_not_found(self):
        self.assertEqual(self.cancel()[0], 404)

    def test_concurrent_cancel_and_approve_have_exactly_one_winner(self):
        for _ in range(20):
            self.token = app.uuid.uuid4().hex
            self.plan_in("awaiting_approval")
            runs, lock, outcomes = [], threading.Lock(), []

            def slow_run(plan, confirmed=False):
                with lock:
                    runs.append(confirmed)
                time.sleep(0.01)
                return app.persist_result(plan["token"], {"status": "complete", "plan": plan, "actions": {}, "timeline": []})

            barrier = threading.Barrier(6)

            def act(kind):
                handler = FakeHandler()
                barrier.wait()
                if kind == "cancel":
                    handler.cancel({"token": self.token})
                else:
                    handler.execute({"token": self.token, "confirmed": True}, confirmed=True)
                with lock:
                    outcomes.append((kind, handler.responses[0][0]))

            with patch.object(app, "run_plan", side_effect=slow_run):
                threads = [threading.Thread(target=act, args=(kind,)) for kind in ("cancel", "approve") * 3]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
            winners = [outcome for outcome in outcomes if outcome[1] == 200]
            self.assertEqual(len(winners), 1, outcomes)
            self.assertEqual(len(runs), 1 if winners[0][0] == "approve" else 0)
            self.assertEqual(app.public_status(self.token)["status"], "complete" if winners[0][0] == "approve" else "cancelled")

    def test_cancel_route_is_wired(self):
        self.plan_in("awaiting_approval")
        handler = FakeHandler(path="/api/cancel", body=app.json.dumps({"token": self.token}).encode())
        handler.do_POST()
        self.assertEqual(handler.responses[0][0], 200)
        self.assertEqual(handler.responses[0][1]["status"], "cancelled")



def http(method, path, payload=None, headers=None, raw=None):
    """Drive the real handler for one request; returns (status, payload)."""
    body = raw if raw is not None else (app.json.dumps(payload).encode() if payload is not None else None)
    handler = FakeHandler(headers=headers, path=path, body=body)
    (handler.do_POST if method == "POST" else handler.do_GET)()
    return handler.responses[0]


class RequestBoundaryTests(unittest.TestCase):
    """V18: host/origin checks, body limits, strict tokens, and input validation at the HTTP boundary."""

    def test_non_loopback_host_headers_are_refused(self):
        for host in ("evil.test", "evil.test:8765", f"192.168.1.5:{app.PORT}", "127.0.0.1:1", "", None):
            with self.subTest(host=host):
                self.assertEqual(http("GET", "/api/health", headers={"Host": host})[0], 421)
                self.assertEqual(http("POST", "/api/plan", {"repo": "o/r", "request": "x"}, headers={"Host": host})[0], 421)
        for host in (f"127.0.0.1:{app.PORT}", f"localhost:{app.PORT}", f"[::1]:{app.PORT}", f"LOCALHOST:{app.PORT}"):
            self.assertEqual(http("GET", "/api/health", headers={"Host": host})[0], 200)

    def test_cross_origin_and_cross_site_api_requests_are_refused(self):
        for headers in ({"Origin": "https://evil.test"}, {"Origin": "null"}, {"Origin": f"http://127.0.0.1:{app.PORT + 1}"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers), patch.object(app, "hermes_decision") as planner:
                self.assertEqual(http("POST", "/api/plan", {"repo": "o/r", "request": "x"}, headers=headers)[0], 403)
                self.assertEqual(http("GET", "/api/status?token=" + "a" * 32, headers=headers)[0], 403)
                planner.assert_not_called()
        same = {"Origin": f"http://127.0.0.1:{app.PORT}", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(http("GET", "/api/health", headers=same)[0], 200)

    def test_post_bodies_must_be_bounded_json_objects(self):
        cases = [
            ({"Content-Type": "text/plain"}, b'{"repo":"o/r"}', 415),
            ({"Content-Type": None}, b'{"repo":"o/r"}', 415),
            ({"Content-Length": None}, b'{}', 411),
            ({"Content-Length": "-1"}, b'{}', 411),
            ({"Content-Length": str(app.MAX_BODY_BYTES + 1)}, b'{}', 413),
            ({"Content-Length": "50"}, b'{}', 400),
            ({}, b'{not json', 400),
            ({}, b'\xff\xfe', 400),
            ({}, b'["a"]', 400),
        ]
        for headers, raw, expected in cases:
            with self.subTest(headers=headers, raw=raw), patch.object(app, "hermes_decision") as planner:
                handler = FakeHandler(headers=headers, path="/api/plan", body=raw)
                handler.do_POST()
                self.assertEqual(handler.responses[0][0], expected)
                planner.assert_not_called()

    def test_oversized_body_is_not_read(self):
        handler = FakeHandler(headers={"Content-Length": str(10 * 1024 * 1024)}, path="/api/plan", body=b"{}")
        handler.rfile = type("NoRead", (), {"read": lambda self, n: self.fail()})()
        handler.do_POST()
        self.assertEqual(handler.responses[0][0], 413)

    def test_tokens_are_strictly_parsed(self):
        token = app.uuid.uuid4().hex
        for query in ("", "?token=", "?token=abc", f"?token={token}&token={token}", f"?token={token.upper()}", f"?token={token}x", "?tok=" + token):
            with self.subTest(query=query):
                self.assertEqual(http("GET", "/api/status" + query)[0], 400)
                self.assertEqual(http("GET", "/api/result" + query)[0], 400)
        self.assertEqual(http("GET", f"/api/status?token={token}")[0], 200)
        self.assertEqual(http("GET", f"/api/result?token={token}")[0], 404)
        for bad in (None, 7, ["x"], "../" + token, token + " "):
            with self.subTest(bad=bad), patch.object(app, "claim_plan") as claim, patch.object(app, "cancel_plan") as cancel:
                self.assertEqual(http("POST", "/api/confirm", {"token": bad})[0], 400)
                self.assertEqual(http("POST", "/api/cancel", {"token": bad})[0], 400)
                claim.assert_not_called()
                cancel.assert_not_called()

    def test_only_a_literal_true_confirms(self):
        token = app.uuid.uuid4().hex
        with patch.object(app, "claim_plan", return_value=("missing", None)) as claim:
            http("POST", "/api/execute", {"token": token, "confirmed": "false"})
        self.assertEqual(claim.call_args.args, (token, False))

    def test_plan_inputs_are_validated_before_hermes_runs(self):
        cases = {
            "request too long": {"repo": "o/r", "request": "x" * (app.MAX_REQUEST_CHARS + 1)},
            "non-string request": {"repo": "o/r", "request": ["x"]},
            "repo too long": {"repo": "o/" + "r" * 300, "request": "x"},
            "bad jira key": {"repo": "o/r", "request": "x", "jira_project": "KAN-1; drop"},
            "bad slack channel": {"repo": "o/r", "request": "x", "slack_channel": "<!channel>"},
        }
        for name, payload in cases.items():
            with self.subTest(name=name), patch.object(app, "hermes_decision") as planner:
                self.assertEqual(http("POST", "/api/plan", payload)[0], 400)
                planner.assert_not_called()
        decision = {"intent": "x", "tools": ["github"], "reason": "r", "needs_confirmation": False}
        with patch.object(app, "hermes_decision", return_value=decision):
            status, body = http("POST", "/api/plan", {"repo": "o/r", "request": "x", "jira_project": " kan ", "slack_channel": "#eng-alerts"})
        self.assertEqual(status, 200)
        self.assertEqual((body["plan"]["jira_project"], body["plan"]["slack_channel"]), ("KAN", "#eng-alerts"))

    def test_responses_carry_security_headers(self):
        sent = []

        class Recorder(FakeHandler):
            send_json = app.Handler.send_json

            def _send(self, status, body, content_type, extra=None):
                sent.append((status, content_type, {**app.Handler.SECURITY_HEADERS, **(extra or {})}))

        Recorder(path="/").do_GET()
        Recorder(path="/api/health").do_GET()
        page, api = sent
        self.assertIn("frame-ancestors 'none'", page[2]["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", page[2]["Content-Security-Policy"])
        for _, _, headers in sent:
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_server_refuses_to_bind_beyond_loopback(self):
        for host, ok in (("127.0.0.1", True), ("::1", True), ("localhost", True), ("127.0.0.2", True),
                         ("0.0.0.0", False), ("::", False), ("192.168.1.5", False), ("example.com", False)):
            self.assertEqual(app.is_loopback_host(host), ok, host)
        with patch.object(app, "HOST", "0.0.0.0"), patch.object(app, "ThreadingHTTPServer") as server:
            with self.assertRaises(SystemExit):
                app.main()
        server.assert_not_called()

    def test_hermes_errors_are_redacted(self):
        stderr = "HTTP 401 Authorization: Bearer sk-live-abcdef123456 token=xyz98765 session_id: 20261008_x"
        with patch.object(app.subprocess, "run", return_value=completed("", 1, stderr)), patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"):
            with self.assertRaises(RuntimeError) as caught:
                app.hermes_decision("x", "planning")
        self.assertNotIn("sk-live-abcdef", str(caught.exception))
        self.assertNotIn("xyz98765", str(caught.exception))
        self.assertIn("session 20261008_x", str(caught.exception))


class StateRetentionTests(unittest.TestCase):
    """V18: cleanup of finished state that never touches active runs or approvable plans."""

    def setUp(self):
        self.saved = (dict(app.PENDING), dict(app.RUNS), dict(app.RESULTS))
        app.PENDING.clear(); app.RUNS.clear(); app.RESULTS.clear()

    def tearDown(self):
        for store, saved in zip((app.PENDING, app.RUNS, app.RESULTS), self.saved):
            store.clear(); store.update(saved)

    def add(self, state, created_at, completed_at=None, terminal=None):
        token = app.uuid.uuid4().hex
        app.PENDING[token] = {"token": token, "state": state, "created_at": created_at, "writes_require_confirmation": True, "write_actions": ["jira"]}
        done = terminal if terminal is not None else state in ("done", "cancelled", "awaiting_approval")
        run_status = "complete" if done else "planned" if state == "planned" else "running"  # as /api/plan and execute_plan set it
        app.RUNS[token] = {**app._new_run(token, run_status), "terminal": done, **({"completed_at": completed_at} if completed_at else {})}
        if done:
            app.RESULTS[token] = {"status": "complete"}
        return token

    def test_finished_and_long_expired_state_is_removed_and_active_state_kept(self):
        now = 1_000_000.0
        old = now - app.RETENTION_SECONDS - app.PLAN_TTL - 10
        keep = {
            "running (very old)": self.add("running", old),
            "executing (very old)": self.add("executing", old),
            "awaiting, not expired": self.add("awaiting_approval", now - 60),
            "planned, not expired": self.add("planned", now - 60, terminal=False),
            "done recently": self.add("done", now - 120, completed_at=now - 60),
            "awaiting, just expired": self.add("awaiting_approval", now - app.PLAN_TTL - 60),
        }
        drop = {
            "done long ago": self.add("done", old, completed_at=old + 5),
            "cancelled long ago": self.add("cancelled", old, completed_at=old + 5),
            "awaiting, expired long ago": self.add("awaiting_approval", old),
            "planned, expired long ago": self.add("planned", old, terminal=False),
        }
        removed = app.prune_state(now)
        self.assertEqual(sorted(removed), sorted(drop.values()))
        for name, token in keep.items():
            self.assertIn(token, app.PENDING, name)
        for token in drop.values():
            self.assertNotIn(token, app.PENDING)
            self.assertNotIn(token, app.RUNS)
            self.assertNotIn(token, app.RESULTS)

    def test_finished_run_cap_removes_oldest_finished_only(self):
        now = 1_000_000.0
        finished = [self.add("done", now - 100 + i, completed_at=now - 100 + i) for i in range(5)]
        active = self.add("running", now - 5000)
        approvable = self.add("awaiting_approval", now - 10)
        with patch.object(app, "MAX_FINISHED_RUNS", 2):
            removed = app.prune_state(now)
        self.assertEqual(sorted(removed), sorted(finished[:3]))
        self.assertTrue({active, approvable, *finished[3:]} <= set(app.PENDING))

    def test_expired_plan_stays_unusable_until_removed(self):
        now = time.time()
        token = self.add("awaiting_approval", now - app.PLAN_TTL - 30)
        app.prune_state(now)
        self.assertEqual(app.claim_plan(token, True)[0], "expired")
        view = app.client_payload(token, {"plan": app.PENDING[token]})["plan"]
        self.assertTrue(view["expired"])
        self.assertFalse(view["approvable"])

    def test_requests_trigger_cleanup(self):
        token = self.add("done", 0, completed_at=1)
        http("GET", "/api/health")
        self.assertNotIn(token, app.PENDING)


class ClientViewTests(unittest.TestCase):
    """What the browser receives: private fields removed, live state and expiry added, copied under the lock."""

    def test_private_fields_removed_and_live_state_added(self):
        token = app.uuid.uuid4().hex
        created = time.time() - 100
        app.PENDING[token] = {"token": token, "state": "awaiting_approval", "created_at": created, "writes_require_confirmation": True, "write_actions": ["jira"], "_file_contents": ["secret source"]}
        stale_copy = {**app.PENDING[token], "state": "running"}  # results keep the plan as it was mid-run
        view = app.client_payload(token, {"status": "confirmation_required", "plan": stale_copy})
        plan = view["plan"]
        self.assertNotIn("_file_contents", plan)
        self.assertEqual(plan["state"], "awaiting_approval")
        self.assertEqual(plan["expires_at"], created + app.PLAN_TTL)
        self.assertTrue(plan["approvable"] and plan["cancellable"] and not plan["expired"])
        self.assertAlmostEqual(view["server_time"], time.time(), delta=5)
        for state, approvable, cancellable in (("planned", False, True), ("running", False, False), ("executing", False, False), ("done", False, False)):
            app.PENDING[token]["state"] = state
            plan = app.client_payload(token, {"plan": stale_copy})["plan"]
            self.assertEqual((plan["approvable"], plan["cancellable"]), (approvable, cancellable), state)

    def test_views_never_extend_expiry(self):
        token = app.uuid.uuid4().hex
        app.PENDING[token] = {"token": token, "state": "awaiting_approval", "created_at": 50.0, "writes_require_confirmation": True, "write_actions": ["jira"]}
        for _ in range(3):
            self.assertEqual(app.client_payload(token, {"plan": app.PENDING[token]})["plan"]["expires_at"], 50.0 + app.PLAN_TTL)
        self.assertEqual(app.PENDING[token]["created_at"], 50.0)

    def test_view_is_copied_only_while_holding_the_state_lock(self):
        # Another thread holds the lock while it approves (as claim_plan does). The view must wait for it and
        # include the change, rather than serializing the live plan mid-update.
        token = app.uuid.uuid4().hex
        plan = {"token": token, "state": "awaiting_approval", "created_at": time.time(), "writes_require_confirmation": True, "write_actions": ["jira"]}
        app.PENDING[token] = plan
        holding = threading.Event()

        def approve_slowly():
            with app.LOCK:
                holding.set()
                time.sleep(0.15)
                plan["approved_at"] = 123.0
                plan["state"] = "executing"

        worker = threading.Thread(target=approve_slowly)
        worker.start()
        holding.wait(2)
        view = app.client_payload(token, {"plan": plan})
        worker.join()
        self.assertEqual(view["plan"]["approved_at"], 123.0)
        self.assertEqual(view["plan"]["state"], "executing")
        self.assertFalse(view["plan"]["approvable"])

    def test_http_responses_use_the_client_view(self):
        token = app.uuid.uuid4().hex
        app.PENDING[token] = {"token": token, "state": "done", "created_at": time.time()}
        app.RESULTS[token] = {"status": "complete", "plan": {"token": token, "_github_result": {"big": 1}, "state": "running"}}
        status, body = http("GET", f"/api/result?token={token}")
        self.assertEqual(status, 200)
        self.assertNotIn("_github_result", body["plan"])
        self.assertEqual(body["plan"]["state"], "done")
        self.assertFalse(body["plan"]["cancellable"])


class PlanningPromptTests(unittest.TestCase):
    def test_planning_prompt_has_no_post_result_phase(self):
        with patch.object(app.subprocess, "run", return_value=completed('{"intent":"x","tools":["github"],"reason":"r","needs_confirmation":false}')) as run, \
                patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"):
            app.hermes_decision("Find open issues", "planning")
        prompt = run.call_args.args[0][run.call_args.args[0].index("-q") + 1]
        self.assertNotIn("second phase", prompt)
        self.assertNotIn("post-result", prompt)
        import inspect
        self.assertNotIn("post-result", inspect.getsource(app))



LIVE_INVESTIGATION = ("Investigate authentication bypass issue #1 in this repository. Retrieve the actual GitHub issue and source code. "
                      "Have the Security Reviewer, Code Reviewer, and Test Reviewer independently analyze the same evidence. "
                      "Reach consensus before presenting a finding. If confirmed, show the root cause, affected source lines, "
                      "recommended fix, validation plan, and proposed code diff. ")


class ReviewerSchemaTests(unittest.TestCase):
    """Live blocker 1: the CODE reviewer replied with "reviewer": "SECURITY" and the run failed with "wrong schema"."""

    def call(self, *stdouts):
        with patch.object(app.subprocess, "run", side_effect=[completed(o) for o in stdouts]) as run, patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"):
            try:
                return app.hermes_reviewer("{\"snapshot\": 1}", "CODE"), run
            except RuntimeError as exc:
                return exc, run

    def prompt(self, run, i):
        argv = run.call_args_list[i].args[0]
        return argv[argv.index("-q") + 1]

    def test_prompt_names_this_reviewer_literally(self):
        for role in app.REVIEWERS:
            text = app.reviewer_prompt("ctx", role)
            self.assertIn(f'"reviewer":"{role}"', text)
            self.assertNotIn("SECURITY|CODE|TEST", text)
            self.assertIn(f'"reviewer" must be exactly "{role}"', text)
            self.assertIn("WITHOUT the \"N| \" line-number prefix", text)

    def test_prompt_rules_match_the_validator(self):
        text = app.reviewer_prompt("ctx", "CODE")
        example = app.json.loads(text.split("this example (the values shown for decision and severity are examples): ")[1].split("\n")[0])
        self.assertEqual(tuple(example), app.REVIEW_FIELDS)
        self.assertEqual(app.review_schema_errors(example, "CODE"), [])
        for value in (*app.REVIEW_DECISIONS, *app.REVIEW_SEVERITIES):
            self.assertIn(value, text)

    def test_the_live_mislabelled_reply_is_retried_once_and_accepted_when_corrected(self):
        live_shape = app.json.dumps({**review("CODE"), "reviewer": "SECURITY"})  # exactly what the live CODE reviewer returned
        result, run = self.call(live_shape, app.json.dumps(review("CODE")))
        self.assertEqual(result["reviewer"], "CODE")
        self.assertEqual(run.call_count, 2)
        retry = self.prompt(run, 1)
        self.assertIn("reviewer must be 'CODE', got 'SECURITY'", retry)
        self.assertIn('{"snapshot": 1}', retry)  # same evidence snapshot
        self.assertNotIn("previous reply was rejected", self.prompt(run, 0))

    def test_retry_is_bounded_and_then_fails_closed(self):
        bad = app.json.dumps({**review("CODE"), "reviewer": "SECURITY"})
        exc, run = self.call(bad, bad)
        self.assertIsInstance(exc, app.HermesOutputError)
        self.assertEqual(run.call_count, 2)
        self.assertIn("still invalid after 1 repair attempt", str(exc))
        self.assertIn("reviewer must be 'CODE', got 'SECURITY'", str(exc))

    def test_invocation_failures_are_not_retried(self):
        with patch.object(app.subprocess, "run", side_effect=subprocess.TimeoutExpired("hermes", 120)) as run, patch.object(app, "HERMES_MODEL", "gpt-5.6-luna"):
            with self.assertRaises(RuntimeError) as caught:
                app.hermes_reviewer("ctx", "CODE")
        self.assertNotIsInstance(caught.exception, app.HermesOutputError)
        self.assertEqual(run.call_count, 1)

    def test_validation_errors_name_fields_and_types_without_quoting_content(self):
        secretish = "sk-live-" + "x" * 40
        cases = {
            "missing": ({k: v for k, v in review("CODE").items() if k not in ("evidence", "severity")}, ["missing field(s): severity, evidence"]),
            "extra": ({**review("CODE"), "confidence": "HIGH"}, ["unexpected field(s): confidence"]),
            "types": ({**review("CODE"), "root_cause": ["x"], "affected_files": "auth.py", "validation_plan": [1]}, ["root_cause must be a string, got list", "affected_files must be a list, got str", "validation_plan must contain only strings"]),
            "enum": ({**review("CODE"), "decision": "LIKELY", "severity": secretish}, ["decision must be one of CONFIRMED, FALSE_POSITIVE, REVIEW_REQUIRED, got 'LIKELY'", "severity must be one of CRITICAL, HIGH, NORMAL, got str"]),
            "evidence": ({**review("CODE"), "evidence": [{**VALID_EVIDENCE[0], "line_start": 3, "line_end": 2}, {"path": "auth.py"}]}, ["evidence[0] line_start and line_end", "evidence[1] must be an object with exactly"]),
            "not an object": (["CODE"], ["the reply must be a JSON object, not list"]),
        }
        for name, (value, expected) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(app.HermesOutputError) as caught:
                    app._review_schema(value, "CODE")
                for fragment in expected:
                    self.assertIn(fragment, str(caught.exception))
                self.assertNotIn(secretish, str(caught.exception))

    def test_valid_output_and_single_json_fence_are_accepted(self):
        valid = app.json.dumps(review("CODE"))
        for text in (valid, f"```json\n{valid}\n```", f"```\n{valid}\n```", f"  \n{valid}\n  "):
            with self.subTest(text=text[:12]):
                result, run = self.call(text)
                self.assertEqual(result, review("CODE"))
                self.assertEqual(run.call_count, 1)

    def test_malformed_json_and_prose_wrappers_are_rejected(self):
        valid = app.json.dumps(review("CODE"))
        for text in ("not json", f"Here is my review:\n{valid}", f"```json\n{valid}\n```\nThanks!", valid[:-1], "```json\n```"):
            with self.subTest(text=text[:20]):
                exc, run = self.call(text, text)
                self.assertIsInstance(exc, app.HermesOutputError)
                self.assertEqual(run.call_count, 2)

    def test_malformed_code_review_fails_the_run_closed(self):
        calls = []

        def reviewer(context, role, **_):
            calls.append(role)
            if role == "CODE":
                raise app.HermesOutputError("CODE reviewer response has the wrong schema: reviewer must be 'CODE', got 'SECURITY' (still invalid after 1 repair attempt)")
            return review(role)

        fake = FakeSwytchcode()
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=reviewer), patch.object(app, "hermes_proposal") as proposal:
            result = app.run_plan(make_plan(LIVE_INVESTIGATION + "Raise a Jira ticket and send a Slack notification.", ["github", "repository"]))
        self.assertEqual(result["status"], "error")
        self.assertIn("reviewer must be 'CODE', got 'SECURITY'", result["error"])
        self.assertEqual(calls, ["SECURITY", "CODE"])  # TEST and consensus never run on a broken council
        self.assertNotIn("council", result)
        self.assertNotIn("finding", result)
        proposal.assert_not_called()
        self.assertEqual(result["plan"].get("write_actions", []), [])  # nothing planned (make_plan omits the key /api/plan adds)
        self.assertEqual(result["plan"]["requested_writes"], ["jira", "slack"])
        self.assertEqual(fake.writes(), [])


class WriteIntentRegressionTests(unittest.TestCase):
    """Live blocker 2 and the negation bug found while tracing it."""

    def test_intent_table(self):
        cases = {
            # positives, including the exact live wording
            LIVE_INVESTIGATION + "Do not modify the repository, and raise a ticket on Jira and send Slack notification.": {"jira", "slack"},
            "do not modify the repository, and raise a ticket on Jira and send Slack notification.": {"jira", "slack"},
            "Raise a Jira ticket.": {"jira"},
            "Create a Jira ticket for issue #1.": {"jira"},
            "Send a Slack notification.": {"slack"},
            "Post a Slack notification to the channel.": {"slack"},
            "Create a Jira ticket but do not send Slack messages.": {"jira"},
            "Don't raise a ticket; just notify the team in Slack.": {"slack"},
            # negations always win
            "Do not create Jira tickets or send Slack messages.": set(),
            LIVE_INVESTIGATION + "Do not modify the repository. Do not create Jira tickets or send Slack messages.": set(),
            "Do not send Slack messages.": set(),
            "Never post to Slack or create tickets.": set(),
            "Do not create Jira tickets and send Slack messages.": set(),
            "Avoid sending anything to Slack and skip creating Jira tickets.": set(),
            # questions and explanations mention the tools without asking for a write
            "Should I create a Jira ticket for this?": set(),
            "Can you send a Slack notification?": set(),
            "Explain how to create a Jira ticket.": set(),
            "Describe why we send Slack notifications for outages.": set(),
            "What happens when we escalate an issue?": set(),
        }
        for request, expected in cases.items():
            with self.subTest(request=request[-70:]):
                self.assertEqual(app.explicitly_requested_writes(request), expected)

    def flow(self, request, tools, **plan_extra):
        fake = FakeSwytchcode()
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=reviewers()), \
                patch.object(app, "hermes_proposal", return_value=PROPOSAL):
            return app.run_plan({**make_plan(request, tools), **plan_extra}), fake

    def test_exact_live_request_saves_both_actions_for_approval(self):
        result, fake = self.flow(LIVE_INVESTIGATION + "Do not modify the repository, and raise a ticket on Jira and send Slack notification.", ["github", "repository"])
        self.assertEqual(result["status"], "confirmation_required")
        plan = result["plan"]
        self.assertEqual(plan["write_actions"], ["jira", "slack"])
        self.assertEqual(plan["requested_writes"], ["jira", "slack"])
        self.assertEqual([t["issue_number"] for t in plan["jira_tickets"]], [1])
        self.assertTrue(plan["slack_plan"]["text"] and plan["slack_plan"]["requires_jira_success"])
        self.assertTrue(app.approved_plan_is_reviewed(plan))
        self.assertEqual(fake.writes(), [])  # nothing is sent before approval

    def test_planner_cannot_authorize_writes_the_user_did_not_request(self):
        for request in (LIVE_INVESTIGATION + "Do not modify the repository. Do not create Jira tickets or send Slack messages.", "Investigate issue #1."):
            with self.subTest(request=request[-50:]):
                result, fake = self.flow(request, ["github", "repository", "jira", "slack"], needs_confirmation=True, reason="Hermes says Jira and Slack are requested.")
                self.assertEqual(result["plan"]["write_actions"], [])
                self.assertNotEqual(result["status"], "confirmation_required")
                self.assertEqual(fake.writes(), [])

    def test_user_request_selects_writes_even_if_the_planner_omits_them(self):
        result, _ = self.flow("Investigate issue #1, raise a Jira ticket and send a Slack notification.", ["github", "repository"])
        self.assertEqual(result["plan"]["write_actions"], ["jira", "slack"])

    def test_missing_destination_errors_only_for_requested_actions(self):
        result, _ = self.flow("Investigate issue #1 and raise a Jira ticket.", ["github", "repository"], slack_channel="")
        self.assertEqual(result["status"], "confirmation_required")
        result, _ = self.flow("Investigate issue #1 and send a Slack notification.", ["github", "repository"], jira_project="")
        self.assertEqual(result["status"], "confirmation_required")
        result, _ = self.flow("Investigate issue #1, raise a Jira ticket and send a Slack notification.", ["github", "repository"], jira_project="", slack_channel="")
        self.assertEqual(result["status"], "configuration_error")
        self.assertEqual(result["errors"], ["Jira was selected, but no Jira project key was provided.", "Slack was selected, but no Slack channel was provided."])



MINT_QR_SCRIPT = "\n".join([f"// helper line {n}" for n in range(1, 78)] + [  # lines 78-88 mirror the live script.js
    "function saveToHistory(data, timestamp) {",
    "    if (qrHistory.length > 0 && qrHistory[0].fullData === data) return;",
    "",
    "    const newEntry = {",
    "        fullData: data,",
    "        displayData: data.length > 25 ? data.substring(0, 25) + '...' : data,",
    "        time: timestamp",
    "    };",
    "",
    "    qrHistory.unshift(newEntry);",
    "}",
]) + "\n"
MINT_LINES = MINT_QR_SCRIPT.splitlines()
# What the live Test Reviewer sent: lines 81-84 cited, but the snippet is lines 81-85 (it includes the closing "};").
LIVE_BAD_CITATION = {"path": "script.js", "line_start": 81, "line_end": 84, "snippet": "\n".join(MINT_LINES[80:85])}
GOOD_CITATION = {**LIVE_BAD_CITATION, "line_end": 85}


def mint_review(role, evidence=GOOD_CITATION, decision="CONFIRMED"):
    return {**review(role, decision), "evidence": [evidence], "affected_files": [{"path": "script.js", "impact": "Stored history entries."}]}


class EvidenceMatchingTests(unittest.TestCase):
    """Live Mint-Qr failure: TEST cited script.js 81-84 with a five-line snippet that is really lines 81-85."""

    def test_live_citation_fails_with_an_exact_explanation(self):
        self.assertEqual(MINT_LINES[84], "    };")
        errors = app.evidence_errors(mint_review("TEST", LIVE_BAD_CITATION), {"script.js": MINT_LINES})
        self.assertEqual(len(errors), 1)
        self.assertIn("does not match 'script.js' lines 81-84", errors[0])
        self.assertIn("the snippet has 5 line(s) but lines 81-84 cover 4", errors[0])
        self.assertIn("that text appears at lines 81-85", errors[0])

    def test_correct_citation_matches_and_strict_checks_remain(self):
        sources = {"script.js": MINT_LINES}
        self.assertEqual(app.evidence_errors(mint_review("TEST", GOOD_CITATION), sources), [])
        for bad in ({**GOOD_CITATION, "line_start": 82}, {**GOOD_CITATION, "path": "app.js"}, {**GOOD_CITATION, "line_end": 999},
                    {**GOOD_CITATION, "snippet": "    const newEntry = {\n        fullData: hacked,"}):
            with self.subTest(bad=bad):
                self.assertTrue(app.evidence_errors(mint_review("TEST", bad), sources))

    def test_live_failure_pattern_still_blocks_consensus(self):
        reviews = [mint_review("SECURITY"), mint_review("CODE"), mint_review("TEST", LIVE_BAD_CITATION)]
        result = app.consensus(reviews, {"script.js": MINT_LINES})
        self.assertEqual(result["decision"], "REVIEW_REQUIRED")
        self.assertIn("lines 81-85", result["evidence_errors"][0])

    def test_prompt_states_the_inclusive_line_rule(self):
        text = app.reviewer_prompt("ctx", "TEST")
        self.assertIn("line_end is the number of the LAST line your snippet includes", text)
        self.assertIn("exactly line_end - line_start + 1 lines", text)

    def council(self, replies):
        calls = []

        def reviewer(context, role, correction="", repairs=None):
            calls.append((role, correction, repairs))
            reply = replies[role].pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with patch.object(app, "hermes_reviewer", side_effect=reviewer):
            reviews, result = app.engineering_council("snapshot", {"script.js": MINT_LINES})
        return reviews, result, calls

    def test_mismatched_citation_gets_one_correction_request_and_can_recover(self):
        replies = {"SECURITY": [mint_review("SECURITY")], "CODE": [mint_review("CODE")], "TEST": [mint_review("TEST", LIVE_BAD_CITATION), mint_review("TEST")]}
        reviews, result, calls = self.council(replies)
        self.assertEqual(result["decision"], "CONFIRMED")
        test_calls = [c for c in calls if c[0] == "TEST"]
        self.assertEqual(len(test_calls), 2)
        self.assertIn("that text appears at lines 81-85", test_calls[1][1])
        self.assertEqual(test_calls[1][2], 0)  # the correction itself gets no further schema retries
        self.assertEqual([c[0] for c in calls].count("SECURITY"), 1)  # other reviewers are not re-run

    def test_uncorrected_or_malformed_correction_keeps_consensus_blocked(self):
        for second in (mint_review("TEST", LIVE_BAD_CITATION), app.HermesOutputError("TEST reviewer response has the wrong schema: missing field(s): evidence")):
            with self.subTest(second=type(second).__name__):
                replies = {"SECURITY": [mint_review("SECURITY")], "CODE": [mint_review("CODE")], "TEST": [mint_review("TEST", LIVE_BAD_CITATION), second]}
                reviews, result, calls = self.council(replies)
                self.assertEqual(result["decision"], "REVIEW_REQUIRED")
                self.assertEqual([c[0] for c in calls].count("TEST"), 2)


def repo_tree(files, dirs=None):
    """A fake repository: {path: text}. Directories are derived from the paths."""
    class Repo(FakeSwytchcode):
        def __call__(self, tool, args):
            if tool == app.TOOLS["repository"]:
                self.calls.append((tool, args))
                path = args["path"]
                if self.fail_root and path == ".":
                    return app.swytchcode_failure(tool, app.swytchcode_error(tool, '{"error":"Not Found","category":"not_found","retryable":false}'))
                if path in files:
                    return source_file(files[path])
                prefix = "" if path == "." else path.rstrip("/") + "/"
                children = {}
                for name in files:
                    if name.startswith(prefix):
                        rest = name[len(prefix):]
                        child = rest.split("/", 1)[0]
                        children[prefix + child] = "dir" if "/" in rest else "file"
                return {"ok": True, "response": {"data": [{"path": p, "type": t} for p, t in sorted(children.items())]}}
            return super().__call__(tool, args)
    return Repo


class RepositoryAnalysisTests(unittest.TestCase):
    """Repository mode: bounded source discovery with visible scope, even with no open issues."""

    def run_repo(self, request, files, issues=(), fail_root=False, reviewer_fn=None):
        fake = repo_tree(files)(issues=list(issues))
        fake.fail_root = fail_root
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=reviewer_fn or (lambda c, role, **_: mint_review(role))) as reviewer, \
                patch.object(app, "hermes_proposal", return_value={**PROPOSAL, "files": [{"path": "script.js", "reason": "fix"}], "diff": "--- a/script.js\n+++ b/script.js\n@@ -81,5 +81,5 @@\n"}):
            result = app.run_plan(make_plan(request, ["github"]))
        return result, fake, reviewer

    def test_mode_detection(self):
        for request, mode in (("Analyze the repository for security problems", "repository"), ("Audit this codebase", "repository"),
                              ("Scan the source code for bugs and notify the team in Slack", "repository"), ("Investigate issue #1 in this repository", "issue"),
                              ("Do not analyze the repository; check issue #2", "issue"), ("Find critical issues and escalate them", "issue")):
            self.assertEqual(app.analysis_mode(request), mode, request)

    def test_repository_analysis_works_with_no_open_issues(self):
        result, fake, reviewer = self.run_repo("Analyze the repository for security issues", {"script.js": MINT_QR_SCRIPT, "index.html": "<html></html>\n", "README.md": "# Mint QR\n"})
        plan = result["plan"]
        self.assertEqual(plan["mode"], "repository")
        self.assertEqual(result["analysis"]["issues"], [])
        self.assertEqual(sorted(plan["inspection_scope"]["files_selected"]), ["index.html", "script.js"])  # README is not source
        self.assertEqual(reviewer.call_count, 3)
        context = app.json.loads(reviewer.call_args.args[0])
        self.assertEqual((context["mode"], context["target_issue"]), ("repository", None))
        self.assertEqual(result["council"]["decision"], "CONFIRMED")
        self.assertEqual(result["finding"]["title"][:28], "CRITICAL finding in script.j")
        self.assertEqual(plan["write_actions"], [])  # nothing requested, nothing planned
        self.assertEqual(plan["run_outcome"]["coverage"], "complete")

    def test_repository_finding_can_be_ticketed_only_after_review_and_approval(self):
        result, fake, _ = self.run_repo("Analyze the repository and raise a Jira ticket for confirmed findings", {"script.js": MINT_QR_SCRIPT})
        self.assertEqual(result["status"], "confirmation_required")
        plan = result["plan"]
        self.assertEqual(plan["target_issue"]["kind"], "repository_finding")
        ticket = plan["jira_tickets"][0]
        self.assertEqual((ticket["subject"], ticket["issue_number"], ticket["reviewed"]), ("repository_finding", None, True))
        self.assertIn("not linked to a GitHub issue", app.json.dumps(ticket["body"]))
        self.assertTrue(app.approved_plan_is_reviewed(plan))
        self.assertFalse(app.approved_plan_is_reviewed({**plan, "mode": "issue"}))
        self.assertFalse(app.approved_plan_is_reviewed({**plan, "jira_tickets": [{**ticket, "subject": "github_issue"}]}))
        self.assertEqual(fake.writes(), [])

    def test_vendor_directories_are_skipped_and_limits_make_coverage_partial(self):
        files = {f"src/module_{i}.js": "x\n" for i in range(20)}
        files.update({"node_modules/lib/index.js": "x\n", **{f"pkg{i}/a.js": "x\n" for i in range(30)}})
        result, fake, _ = self.run_repo("Analyze the repository", files)
        scope = result["plan"]["inspection_scope"]
        self.assertIn("node_modules", scope["skipped_directories"])
        self.assertFalse(any("node_modules" in args["path"] for tool, args in fake.calls if tool == app.TOOLS["repository"]))
        self.assertEqual(len(scope["files_selected"]), app.DISCOVERY_LIMITS["repository"]["files"])
        self.assertGreater(scope["files_not_selected_count"] + scope["directories_not_visited_count"], 0)
        self.assertFalse(scope["complete"])
        self.assertEqual(result["plan"]["run_outcome"]["coverage"], "partial")
        repo_reads = sum(1 for tool, _ in fake.calls if tool == app.TOOLS["repository"])
        limits = app.DISCOVERY_LIMITS["repository"]
        self.assertLessEqual(repo_reads, 1 + limits["directories"] + limits["files"])  # API use stays bounded

    def test_repository_access_failure_is_reported_and_blocks_findings(self):
        result, fake, reviewer = self.run_repo("Analyze the repository and raise a Jira ticket", {"script.js": MINT_QR_SCRIPT}, fail_root=True)
        self.assertEqual(reviewer.call_count, 0)
        self.assertIn("repository access failed", result["council"]["summary"])
        self.assertIn("not_found: Not Found", result["council"]["summary"])
        self.assertEqual(result["plan"]["inspection_scope"]["access_error"], "not_found: Not Found")
        self.assertEqual(result["plan"]["write_actions"], [])
        self.assertEqual(fake.writes(), [])


class RunStatusNotificationTests(unittest.TestCase):
    """Run-status notifications are separate from finding escalation, explicitly requested, and approval-gated."""

    STATUS = "Send a Slack status notification when the analysis completes."

    def test_status_intent_is_separate_from_finding_alerts(self):
        cases = {
            self.STATUS: {"slack_status"},
            "Notify the team in Slack when the analysis is done.": {"slack_status"},
            "Post the run status to #eng.": {"slack_status"},
            "Send Slack notification.": {"slack"},
            "Raise a Jira ticket, send a Slack notification, and post the run outcome to Slack.": {"jira", "slack", "slack_status"},
            "Do not send a Slack status notification.": set(),
            "Should we post the run status to Slack?": set(),
        }
        for request, expected in cases.items():
            with self.subTest(request=request):
                self.assertEqual(app.explicitly_requested_writes(request), expected)

    def flow(self, request, reviewer_fn=None, jira_results=None, approve=True, proposal=PROPOSAL):
        fake = FakeSwytchcode()
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=reviewer_fn or reviewers()), \
                patch.object(app, "hermes_proposal", **({"side_effect": proposal} if isinstance(proposal, Exception) else {"return_value": proposal})):
            pending = app.run_plan(make_plan(request, ["github", "repository"]))
        self.assertEqual(fake.writes(), [])  # nothing is ever sent before approval
        if not approve or pending["status"] != "confirmation_required":
            return pending, None, None
        fake = FakeSwytchcode(jira_results=jira_results)
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "JIRA_BASE_URL", "https://example.atlassian.net"):
            final = app.run_plan(pending["plan"], confirmed=True)
        return pending, final, fake

    def sent(self, fake):
        return [args["body"]["text"] for tool, args in fake.calls if tool == app.TOOLS["slack"]]

    def test_blocked_investigation_previews_an_accurate_status_and_no_ticket(self):
        blocked = lambda c, role, **_: review(role, "REVIEW_REQUIRED")
        pending, final, fake = self.flow("Investigate issue #1, raise a Jira ticket, and " + self.STATUS[0].lower() + self.STATUS[1:], reviewer_fn=blocked)
        self.assertEqual(pending["status"], "confirmation_required")
        plan = pending["plan"]
        self.assertEqual(plan["write_actions"], ["slack_status"])
        self.assertEqual(plan["jira_tickets"], [])
        text = plan["slack_status_plan"]["text"]
        self.assertIn("consensus was blocked, so no vulnerability was confirmed", text)
        self.assertNotIn("confirmed a finding", text)
        self.assertNotIn("Jira:", text)
        self.assertEqual(self.sent(fake), [text])
        self.assertFalse(any(tool == app.TOOLS["jira"] for tool, _ in fake.calls))
        self.assertEqual(final["status"], "complete")

    def test_confirmed_finding_previews_ticket_alert_and_status_and_reports_real_key(self):
        request = "Investigate issue #1, raise a Jira ticket, send a Slack notification, and post the run status to Slack."
        pending, final, fake = self.flow(request, jira_results=[jira_created("KAN-5")])
        plan = pending["plan"]
        self.assertEqual(plan["write_actions"], ["jira", "slack", "slack_status"])
        self.assertIn("confirmed a finding", plan["slack_status_plan"]["text"])
        self.assertTrue(plan["slack_status_plan"]["appends_jira_outcome"])
        self.assertNotIn("KAN-", plan["slack_status_plan"]["text"] + plan["slack_plan"]["text"])  # no key exists before approval
        alert, status = self.sent(fake)
        self.assertTrue(alert.endswith("Jira: KAN-5 (https://example.atlassian.net/browse/KAN-5)"))
        self.assertTrue(status.endswith("Jira: KAN-5 (https://example.atlassian.net/browse/KAN-5)"))
        self.assertEqual(status.rsplit("\n", 1)[0], plan["slack_status_plan"]["text"])

    def test_jira_failure_never_produces_a_message_claiming_a_ticket(self):
        request = "Investigate issue #1, raise a Jira ticket, send a Slack notification, and post the run status to Slack."
        pending, final, fake = self.flow(request, jira_results=[AUTH_FAILURE])
        texts = self.sent(fake)
        self.assertEqual(len(texts), 1)  # the finding alert is withheld; only the status report is sent
        self.assertTrue(texts[0].endswith("Jira: ticket not created (auth)"))
        self.assertNotIn("KAN-", texts[0])
        self.assertEqual(final["status"], "error")
        self.assertIn("Slack notification withheld", final["error"])

    def test_failed_run_can_report_failure_status_only(self):
        broken = lambda c, role, **_: (_ for _ in ()).throw(app.HermesOutputError("CODE reviewer response has the wrong schema: missing field(s): evidence")) if role == "CODE" else review(role)
        pending, final, fake = self.flow("Investigate issue #1, raise a Jira ticket, and " + self.STATUS[0].lower() + self.STATUS[1:], reviewer_fn=broken)
        self.assertEqual(pending["status"], "confirmation_required")
        self.assertEqual(pending["plan"]["write_actions"], ["slack_status"])
        self.assertIn("wrong schema", pending["error"])
        self.assertIn("The run failed before completing; no vulnerability was confirmed.", pending["plan"]["slack_status_plan"]["text"])
        self.assertEqual(self.sent(fake), [pending["plan"]["slack_status_plan"]["text"]])
        self.assertFalse(any(tool == app.TOOLS["jira"] for tool, _ in fake.calls))
        self.assertFalse(any(tool == app.TOOLS["github"] for tool, _ in fake.calls))  # the approved status send re-reads nothing

    def test_failed_run_without_status_request_stays_an_error(self):
        broken = lambda c, role, **_: (_ for _ in ()).throw(app.HermesOutputError("bad")) if role == "CODE" else review(role)
        pending, _, _ = self.flow("Investigate issue #1 and raise a Jira ticket.", reviewer_fn=broken)
        self.assertEqual(pending["status"], "error")

    def test_partial_coverage_is_stated_and_audit_not_claimed(self):
        plan = {**make_plan("Analyze the repository", []), "mode": "repository", "inspection_scope": {"complete": False, "files_not_selected_count": 7, "directories_not_visited_count": 2}, "inspected_files": ["script.js"]}
        outcome = app.run_outcome(plan, "blocked", "Contradictory evidence requires review.")
        text = app.status_message(plan, outcome, None, None)
        self.assertEqual(outcome["coverage"], "partial")
        self.assertIn("partial: 2 folder(s) not inspected; 7 candidate file(s) not fetched", text)
        self.assertIn("not a complete security audit", text)
        self.assertIn("no vulnerability was confirmed", text)

    def test_status_plans_are_approval_gated_and_immutable(self):
        blocked = lambda c, role, **_: review(role, "REVIEW_REQUIRED")
        pending, _, _ = self.flow("Investigate issue #1. " + self.STATUS, reviewer_fn=blocked, approve=False)
        plan = pending["plan"]
        self.assertTrue(app.approved_plan_is_reviewed(plan))
        for broken in ({**plan, "slack_status_plan": None}, {**plan, "run_outcome": None}, {**plan, "write_actions": ["slack_status", "slack_status"]},
                       {**plan, "write_actions": ["jira", "slack_status"]}):  # Jira still needs a confirmed finding
            self.assertFalse(app.approved_plan_is_reviewed(broken))
        tampered = {**plan, "run_outcome": {**plan["run_outcome"], "status": "confirmed"}}
        fake = FakeSwytchcode()
        with patch.object(app, "run_swytchcode", side_effect=fake):
            app.run_plan(tampered, confirmed=True)
        self.assertEqual(self.sent(fake), [plan["slack_status_plan"]["text"]])  # the saved text is sent, not a rebuilt one

    def test_missing_channel_errors_for_requested_status(self):
        blocked = lambda c, role, **_: review(role, "REVIEW_REQUIRED")
        fake = FakeSwytchcode()
        with patch.object(app, "run_swytchcode", side_effect=fake), patch.object(app, "hermes_reviewer", side_effect=blocked):
            result = app.run_plan({**make_plan("Investigate issue #1. " + self.STATUS, ["github", "repository"]), "slack_channel": ""})
        self.assertEqual(result["status"], "configuration_error")
        self.assertEqual(result["errors"], ["Slack was selected, but no Slack channel was provided."])


if __name__ == "__main__":
    unittest.main()
