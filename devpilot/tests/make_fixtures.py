"""Generate frontend fixtures by running the real DevPilot backend against mocked providers.

Every payload is what the browser actually receives: results go through app.client_payload and statuses
through app.public_status. Hermes, Swytchcode, Jira, Slack, and GitHub are all mocked; any subprocess call
raises, so nothing here can reach a live service.

Usage: python3 tests/make_fixtures.py OUTPUT.json   (run from the repository root; tests/ui_check.js does this)
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import test_app as t  # noqa: E402

BULK = "Find critical and high issues, escalate them, and notify the engineering team in Slack."
out = {}


def capture(name, token, result):
    out[name] = {"result": app.client_payload(token, result), "status": app.public_status(token)}


def pending_run(name, request, issues=None, reviewer_fn=None, proposal=t.PROPOSAL, files=None, created_at=None, fake=None, **plan_extra):
    token = app.uuid.uuid4().hex
    plan = {**t.make_plan(request, ["github", "repository"]), "token": token, "created_at": created_at or time.time(), "state": "running", "write_actions": [], **plan_extra}
    app.PENDING[token] = plan
    app.start_run(token)
    proposer = {"side_effect": proposal} if isinstance(proposal, Exception) else {"return_value": proposal}
    with patch.object(app, "run_swytchcode", side_effect=fake or t.PagedSwytchcode(issues if issues is not None else t.DEMO_ISSUES, files=files)), \
            patch.object(app, "hermes_reviewer", side_effect=reviewer_fn or t.reviewers()), patch.object(app, "hermes_proposal", **proposer):
        result = app.execute_plan(plan, False)
    capture(name, token, result)
    return token, result


def approved_run(name, token, jira_results=None):
    outcome, claimed = app.claim_plan(token, True)
    assert outcome == "approved", outcome
    with patch.object(app, "run_swytchcode", side_effect=t.FakeSwytchcode(jira_results=jira_results)), patch.object(app, "JIRA_BASE_URL", "https://example.atlassian.net"):
        capture(name, token, app.execute_plan(claimed, True))


def main(path):
    with patch.object(app.subprocess, "run", side_effect=AssertionError("fixture generation must never start a process")):
        token, _ = pending_run("bulk", BULK)
        approved_run("bulk_final", token, [t.jira_created("KAN-11")])
        token, _ = pending_run("jirafail", "Create a Jira ticket for the authentication bypass and notify the engineering team in Slack")
        approved_run("jirafail_final", token, [t.AUTH_FAILURE])
        pending_run("review_required", "Create a Jira ticket for the authentication bypass",
                    reviewer_fn=lambda c, role, **_: t.review(role, evidence=[{**t.VALID_EVIDENCE[0], "path": "src/login.py"}]) if role == "CODE" else t.review(role, "REVIEW_REQUIRED"))
        pending_run("insufficient", "Create a Jira ticket for the authentication bypass", files={"auth.py": t.source_file(t.AUTH_SOURCE, size=999)})
        pending_run("config", "Create a Jira ticket for the authentication bypass", jira_project="")
        pending_run("proposal_fail", "Create a Jira ticket for the authentication bypass", proposal=RuntimeError("Hermes proposal invocation timed out after 120s."))
        pending_run("informational", "Investigate the authentication bypass")

        def code_reviewer_breaks(context, role, **_):
            if role == "CODE":
                raise app.HermesOutputError("CODE reviewer response has the wrong schema: reviewer must be 'CODE', got 'SECURITY' (still invalid after 1 repair attempt)")
            return t.review(role)
        pending_run("reviewer_fail", "Investigate issue #1, raise a ticket on Jira and send Slack notification.", reviewer_fn=code_reviewer_breaks)
        xss = [{"title": "<img src=x onerror=alert(1)> Authentication bypass", "number": 1, "html_url": "javascript:alert(1)", "state": "open", "body": "security <script>alert(2)</script>"}]
        token, _ = pending_run("xss", "Notify the engineering team in Slack", issues=xss)
        approved_run("xss_final", token)
        token, _ = pending_run("bulk_cancel", BULK)
        outcome, cancelled = app.cancel_plan(token)
        assert outcome == "cancelled", outcome
        capture("bulk_cancel_done", token, cancelled)
        pending_run("expired", BULK, created_at=time.time() - app.PLAN_TTL - 60)
        # Run-status notifications and repository analysis
        status_request = "Investigate issue #1, raise a Jira ticket, and send a Slack status notification when the analysis completes."
        token, _ = pending_run("status_blocked", status_request, reviewer_fn=lambda c, role, **_: t.review(role, "REVIEW_REQUIRED"))
        approved_run("status_blocked_final", token)
        token, _ = pending_run("all_three", "Investigate issue #1, raise a Jira ticket, send a Slack notification, and post the run status to Slack.")
        approved_run("all_three_jirafail", token, [t.AUTH_FAILURE])

        def breaks(context, role, **_):
            if role == "CODE":
                raise app.HermesOutputError("CODE reviewer response has the wrong schema: missing field(s): evidence")
            return t.review(role)
        pending_run("status_failed", status_request, reviewer_fn=breaks)
        mint_fix = {**t.PROPOSAL, "files": [{"path": "script.js", "reason": "fix"}], "diff": "--- a/script.js\n+++ b/script.js\n@@ -81,5 +81,5 @@\n"}
        repo = t.repo_tree({"script.js": t.MINT_QR_SCRIPT, "README.md": "# Mint QR\n"})(issues=[])
        repo.fail_root = False
        pending_run("repo_finding", "Analyze the repository and raise a Jira ticket for confirmed findings.", reviewer_fn=lambda c, role, **_: t.mint_review(role), proposal=mint_fix, fake=repo)
        denied = t.repo_tree({"script.js": t.MINT_QR_SCRIPT})(issues=[])
        denied.fail_root = True
        pending_run("repo_denied", "Analyze the repository.", fake=denied)
        # Approved in another tab: the stored result still says confirmation_required, but the live plan is executing.
        token, pending = pending_run("approved_elsewhere", BULK)
        assert app.claim_plan(token, True)[0] == "approved"
        capture("approved_elsewhere", token, pending)
    Path(path).write_text(json.dumps(out, default=str))
    for name, value in out.items():
        print(f"{name:20} {value['result']['status']:22} {value['status']['status']}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
