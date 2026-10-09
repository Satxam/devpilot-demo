# DevPilot

DevPilot is a local, loopback-only web application that acts as an autonomous AI software engineer. Give it a GitHub repository and a natural-language request. It reads the repository's open issues and source, has three independent Hermes reviewers check a finding against the retrieved code, proposes a fix as a reviewable diff, and, only after you approve the exact saved actions, creates a Jira ticket and posts a Slack message.

Hermes does the reasoning. Swytchcode performs every provider call. DevPilot never calls GitHub, Jira, or Slack HTTP APIs directly, and it never modifies a repository.

## Architecture

| Part | Role |
|---|---|
| `app.py` | Python standard-library HTTP server and orchestration. Calls Hermes in safe mode (no tools) and delegates provider calls to the Swytchcode CLI. |
| `index.html` | Single-page UI in plain HTML/CSS/JavaScript, with no dependencies or external resources. |
| `test_app.py` | Python unit tests; every Hermes and Swytchcode call is mocked. |
| `tests/ui_check.js`, `tests/make_fixtures.py` | Frontend regression checks driven by payloads from the real backend with mocked providers. |

Swytchcode methods used (all must be enabled in the project's Swytchcode tooling):

- `github.issue.list1`: issue search (`GET /search/issues`), paged
- `github.content.get`: read-only repository tree and file contents
- `jira.api.issue.create`: Jira issue creation
- `slack.chat.postmessage.create`: Slack message posting

### Request flow

1. **Planning.** One strict-JSON Hermes call decides which read tools the request needs (`github`, `repository`). It cannot select or perform writes.
2. **Issue retrieval.** All open issues are paged in 100 at a time, sorted by creation date. Paging stops when results run out, a page repeats, an error occurs after page 1, or GitHub's 1,000-result search cap is reached; the last three mark the list *incomplete*.
3. **Mode and target.** DevPilot runs in one of two modes:
   - **Issue investigation** (the default) investigates one GitHub issue and its relevant source.
   - **Repository analysis** is used when the request names no issue and asks to analyze, audit, scan, or review the repository/codebase. It works even with no open issues. It explores up to 25 folders and 1,500 tree entries, skips vendor and build folders (`node_modules`, `dist`, `build`, …), and fetches up to 12 prioritized source files (HTML included). The inspection scope is recorded and shown: folders not visited, candidate files not fetched, skipped folders, and any access failure. Coverage is reported as partial whenever anything was left out. It is a bounded inspection, never a complete security audit.

   In issue mode, a number in the request (`#2`, `issue 2`, `issue number 2`) is matched exactly, so `#2` never matches `#20` or a year. Without a number, the most severe open issue is used. If a requested issue isn't found, writes are blocked; the message says whether the issue doesn't exist or the list was incomplete.
4. **Source evidence.** In issue mode DevPilot walks a bounded part of the repository tree (8 folders) and fetches up to five likely source files. If the repository can't be read (private, missing, or no access), the actual error is reported and no review runs. A file counts as evidence only if its complete UTF-8 text was returned. Files that failed, were omitted (GitHub omits content over 1 MB), are binary, or have a size mismatch are listed with the reason.
5. **Engineering Council.** Security, Code, and Test reviewers each get the *same* evidence snapshot and return strict JSON.
   - A finding is `CONFIRMED` only if at least two reviewers confirm it, none says `FALSE_POSITIVE`, and every citation checks out against the retrieved source: the file was retrieved, the lines exist, and the snippet matches those exact lines. `line_end` is the last line included, so a snippet spans exactly `line_end − line_start + 1` lines.
   - A malformed reply gets one corrective retry. A citation that doesn't match gets one correction request, which states where the quoted text actually is. The corrected reply is verified again, and nothing is corrected automatically. If it still doesn't match, consensus stays blocked.
   - Malformed or missing reviews fail closed as `REVIEW_REQUIRED`.
   - With no usable source, the review is not run and nothing can be written.
6. **Proposed fix.** For a confirmed finding, Hermes proposes a unified diff. It must modify only retrieved files, stay within their real line counts, and touch a file cited as evidence; otherwise the run fails. **The diff is a preview only. DevPilot never applies it to any repository, and approving Jira/Slack actions does not apply it.**
7. **Write selection.** External actions are selected only when the request explicitly asks for them: an action verb plus a target. Negations cover their whole clause ("do not create Jira tickets or send Slack messages" selects nothing). Questions and discussion ("explain how…", "why…") select nothing, and Hermes' planning output can't add a write. There are three kinds:
   - **Jira ticket** ("raise/create a Jira ticket"): only for a confirmed, evidence-verified finding, either the investigated issue or a confirmed repository finding (labelled as not linked to a GitHub issue).
   - **Finding alert** ("send a Slack notification"): only for a confirmed finding. It is withheld if a planned Jira ticket fails.
   - **Run-status notification** ("send a Slack status notification when the analysis completes", "post the run status to Slack"): reports how the run actually ended (confirmed, consensus blocked, insufficient evidence, or failed) and its coverage. It never describes an unconfirmed problem as confirmed. It is available even for blocked or failed runs.
8. **Approval.** The exact Jira payload and Slack message are saved in the plan and shown in full. Nothing is sent until you approve that saved plan.
9. **Execution.** The saved actions run exactly as approved, without re-running the council. The finding alert is withheld unless every planned Jira ticket was created. The run-status message is sent as saved, plus one line with the real Jira result (`Jira: KEY (link)` or `Jira: ticket not created (reason)`). A Jira key and link appear only after Jira has actually created the ticket. Results report each created issue's key and browser link, or a classified error.

### Context budget

Reviewer and proposal context is valid JSON of at most 100,000 bytes, because Hermes receives the prompt as one command-line argument and Linux caps that at 128 KiB. Each section has its own budget: request 4,000; target issue 8,000; source files 70,000; repository root listing 5,000; other open issues 5,000; inspection scope 2,000. Issue text may be shortened, and is flagged when it is. Source files are included whole or omitted with a reason; they are never cut. A citation of an omitted file blocks writes.

### Jira tickets: one reviewed issue per run

A confirmed finding is evidence about one issue only. Each run therefore plans **exactly one** Jira ticket, for the issue the Engineering Council reviewed and confirmed. If the request asks for bulk escalation ("escalate them", "tickets for each critical issue"), the other matching issues are listed in the approval panel as **blocked**, with the reason, and are never ticketed. Run DevPilot on each of those issues to review them. The server refuses to execute any plan whose ticket list isn't exactly that one reviewed issue.

### Approval states, expiry, and cancellation

A plan moves through `planned → running → awaiting_approval → executing → done`, or to `cancelled`.

- Approval and cancellation take the same lock, so a plan can be approved at most once, and an approve racing a cancel has exactly one winner.
- A plan expires **15 minutes** after it was created. The approval panel shows the server's expiry time and stops offering Approve once it passes. The server rejects late approvals with `410`; viewing a plan never extends it.
- **Cancel plan** (`POST /api/cancel`) is offered only when the server says the plan is cancellable, meaning not yet started or awaiting approval. A running review or an executing approval cannot be interrupted.
- Finished runs and long-expired plans are removed from memory after an hour, keeping at most 200 finished runs. Running, executing, and still-approvable plans are never removed.

### HTTP API

All endpoints are same-origin JSON on the loopback interface.

| Endpoint | Purpose |
|---|---|
| `POST /api/plan` | `{repo, request, jira_project?, slack_channel?}` → planning result |
| `POST /api/execute` | `{token}` → starts the background review (`202`) |
| `GET /api/status?token=` | Live steps plus `terminal` and `result_ready` |
| `GET /api/result?token=` | Final result, including the plan's live `state`, `expires_at`, `approvable`, `cancellable` |
| `POST /api/confirm` | `{token}` → executes the approved saved plan |
| `POST /api/cancel` | `{token}` → invalidates a plan that has not started or is awaiting approval |

## Security model

DevPilot has **no user authentication**. It is safe only as a single-user tool on your own machine:

- The server refuses to start unless `DEVPILOT_HOST` is a loopback address (`127.0.0.1`, `::1`, `localhost`).
- The `Host` header must be a loopback name for the server's port (`421` otherwise), which blocks DNS-rebinding attacks.
- API requests with a foreign `Origin` or a cross-site `Sec-Fetch-Site` are refused (`403`).
- POST bodies must be `application/json` (`415`), have a valid `Content-Length` (`411`), and be at most 64 KiB (`413`). Oversized bodies are not read.
- Plan tokens must be exactly 32 lowercase hex characters (`400`). On `/api/execute`, only a literal `"confirmed": true` counts as a confirmation.
- Inputs are validated before Hermes runs:
  - the request is at most 4,000 characters;
  - the repository must be `OWNER/REPO`;
  - the Jira key must be 2–20 letters, digits, or underscores (it is upper-cased);
  - the Slack channel must be a name or an ID.
- Responses carry `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-store`. The page also gets a strict Content-Security-Policy.
- Private plan data (raw GitHub responses, fetched source) is never sent to the browser. Responses are copied under the state lock.
- Swytchcode and Hermes error text is redacted (Bearer/Basic tokens, `token=`/`api_key:`-style values, Slack/GitHub token formats, long secrets). The response body of a failed provider call is never returned. Requests are not logged.
- Swytchcode manages provider credentials. DevPilot stores none.

## Setup

Run every command below from the directory that contains `app.py` (in the `devpilot-demo` repository, that is `devpilot/`). The server, tests, and fixture generator locate `index.html` and each other relative to that directory.

Requirements:

- **Python 3.10+** (tested with 3.12). Standard library only.
- **Node.js 18+** for the frontend checks only (tested with 24). Built-ins only; no `npm install`.
- **Swytchcode CLI**, logged in, with the four methods above enabled for this project. Connect each provider in a terminal; DevPilot cannot do this for you:
  ```bash
  swytchcode auth connect github
  swytchcode auth connect jira
  swytchcode auth connect slack
  ```
  If a call fails with category `auth`, the UI shows the matching `swytchcode auth connect <provider>` command.
- **Hermes CLI** with a configured model. DevPilot runs `hermes chat -q … --oneshot --quiet --safe-mode --max-turns 1 --reasoning <level> --model <model>`.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DEVPILOT_HERMES_MODEL` | *(required)* | Hermes model, e.g. `gpt-5.6-luna`. Every Hermes call is blocked if unset. |
| `DEVPILOT_HERMES_PLANNING_REASONING` | `none` | Reasoning level for the planning call. |
| `DEVPILOT_HERMES_REVIEW_REASONING` | `low` | Reasoning level for the three reviewers and the proposed fix. |
| `DEVPILOT_JIRA_BASE_URL` | *(unset)* | Your Jira site, e.g. `https://your-team.atlassian.net`, used only for browser links (`…/browse/KEY`). Must be a plain `https://` URL with no credentials or query string. If unset or invalid, results show the key without a link. |
| `DEVPILOT_HOST` | `127.0.0.1` | Bind address; must be loopback. |
| `DEVPILOT_PORT` | `8765` | Port. |

Supported reasoning levels are `none`, `low`, `medium`, `high`, `xhigh`, and `max`. `minimal` is **not** supported by `gpt-5.6-luna`. Any other value blocks the call with an explanatory error rather than being sent.

### Run

```bash
export DEVPILOT_HERMES_MODEL=gpt-5.6-luna
export DEVPILOT_JIRA_BASE_URL=https://your-team.atlassian.net   # optional
python3 app.py
```

Open http://127.0.0.1:8765.

## Tests

Both suites are local and offline. Every Hermes, Swytchcode, GitHub, Jira, and Slack interaction is mocked, and any unmocked subprocess call fails the test instead of running.

```bash
python3 -m unittest -v test_app   # backend: approval, consensus, evidence, lifecycle, retrieval, integrations, HTTP boundary
node tests/ui_check.js            # frontend: runs index.html's real script against backend-generated payloads
```

`tests/ui_check.js` runs `tests/make_fixtures.py` to produce fixtures from the real backend, then exercises:

- the approval preview, including blocked bulk issues, the exact Slack text, and expiry;
- approve, cancel, and approved-elsewhere states;
- Jira failures with Slack withheld;
- review-required, insufficient-evidence, configuration, and proposal-failure results;
- diff and evidence rendering;
- fail-closed approval cases;
- 409, 410, 500, and malformed responses;
- polling edge cases and the stale-status race;
- escaping of untrusted text.

Pass a fixture file (`node tests/ui_check.js fixtures.json`) to reuse one.

## Limitations

- **One finding per run.** Only the target issue (or, in repository analysis, the single most significant confirmed finding) is reviewed and can be ticketed; bulk requests list the rest as blocked.
- **Repository analysis needs agreement.** Three reviewers who pick different problems produce disjoint evidence, which blocks consensus. This is reported, never overridden.
- **Diffs are previews.** Nothing creates branches, applies patches, runs the target repository's tests, or opens pull requests.
- **Writes aren't verified.** Jira and Slack results are reported as Swytchcode returned them; there is no read-back.
- **Retrying a failed write needs a new plan.** Re-planning reviews the issue again.
- **Large repositories.** Issues beyond GitHub's 1,000-result search cap can't be reached; the list is reported as incomplete. Only the repository's root listing goes into the reviewer context.
- **Write intent is detected from English patterns.** A question such as "how do I create a ticket?" still counts as a request (approval is still required).
- **Snippet prefixes.** Reviewer snippets that copy the `N| ` line-number prefix fail evidence checks. This fails closed.
- **Slack mentions.** Plain-text `@channel` is not escaped; it relies on Slack's default of not converting it into a mention. `<!channel>`-style markup is escaped.
- **State is in memory only** and lost on restart. There is no authentication, so keep it on loopback.
- **Execution blocks the request.** Approved Jira/Slack execution runs inside the `/api/confirm` request; the UI follows progress by polling status.

## Controlled live smoke test

The automated tests never touch live services. Before relying on DevPilot, run this once, by hand, against **throwaway** targets:

1. **Prepare isolated targets:** a test GitHub repository with a few issues (one describing a real, small bug in a file), a scratch Jira project, and a private test Slack channel.
2. **Check setup:** run `swytchcode list tooling` and confirm the four methods are enabled. Connect providers with `swytchcode auth connect <provider>` if needed. Set `DEVPILOT_HERMES_MODEL` and start `python3 app.py`.
3. **Read-only run first:** "Investigate the bug in issue #N". Confirm that the GitHub and repository stages complete, all three reviewers run, the evidence matches the file, and no approval is offered.
4. **Single write:** "Create a Jira ticket for issue #N and notify the team in Slack", with the scratch project and test channel. Before approving, check that:
   - the approval panel shows one ticket and the exact Slack text;
   - the expiry time is shown.
5. **Approve.** Verify the Jira issue and Slack message exist and match the preview, and that the result links to the issue.
6. **Negative checks:**
   - Cancel a second plan and confirm nothing was sent.
   - Let a plan expire and confirm Approve disappears and `/api/confirm` returns `410`.
   - Disconnect Jira and confirm the auth error and the withheld Slack message.
7. **Clean up:** delete the test Jira issue and Slack message, and stop the server.
