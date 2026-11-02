"""Combined CTL-09 browser-contract and CTL-07 workflow API tests."""
from __future__ import annotations

from typing import Any

from scripts import diary_lock_dashboard as dashboard
from scripts import diary_lock_ui as ui
from scripts.test_diary_lock_dashboard import controller_reply, owner_headers, web
from scripts.test_diary_lock_ui import (
    SESSION_COOKIE,
    action,
    authorize,
    confirm,
    request,
    services,
)


def test_dashboard_mount_data_drives_real_fresh_workflow(services: Any, web: Any) -> None:
    dashboard_server, sources = web
    sources.controller = controller_reply("LOCKED")
    services.controller.state = "LOCKED"

    status, headers, page = dashboard_server.request("GET", "/lock/", owner_headers())
    assert status == 200
    assert b'id="lock-workflow"' in page
    assert b'<script src="/lock/workflow.js"></script><script src="/lock/dashboard.js"></script>' in page
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "connect-src 'self'" in headers["Content-Security-Policy"]

    status, _, dashboard_script = dashboard_server.request("GET", "/lock/dashboard.js", owner_headers())
    assert status == 200
    for contract in (b"DiaryLockWorkflow.mount", b"getReport", b"getState", b"refresh", b"/lock/api/status"):
        assert contract in dashboard_script

    services.auth_state.add("owner")
    status, workflow_script = request(
        services.ui_server, "GET", "/lock/workflow.js", {SESSION_COOKIE: "owner"},
    )
    assert status == 200
    assert b"function mount({element, getReport, getState, refresh})" in workflow_script

    status, _, body = dashboard_server.request("GET", "/lock/api/status", owner_headers())
    assert status == 200
    import json
    snapshot = json.loads(body)
    get_report = snapshot["report"]
    get_state = snapshot["controller"]
    assert get_state["state"] == "LOCKED"
    assert get_report["incomplete"] is False
    assert get_report["required_groups"]
    assert all("classifications" in group for group in get_report["required_groups"])

    class DashboardReview:
        def review(self) -> dict[str, Any]:
            return get_report

    services.ui_server.reports = DashboardReview()
    cookies = authorize(services)
    fingerprints = [group["fingerprint"] for group in get_report["required_groups"]]
    for fingerprint in fingerprints:
        status, _ = request(
            services.ui_server,
            "POST",
            "/lock/unlock/acknowledge",
            cookies,
            action("X-Diary-Acknowledge-Review"),
            {"review_id": get_report["review_id"], "groups": [fingerprint]},
        )
        assert status == 200
    status, unlocked = confirm(services, cookies)
    assert status == 200 and unlocked["state"] == "ATTENDED"

    status, extended = request(
        services.ui_server,
        "POST",
        "/lock/api/extend",
        cookies,
        action("X-Diary-Extend-Attendance"),
    )
    assert status == 200 and extended["state"] == "ATTENDED"
    assert services.controller.extensions == 1
    assert dashboard.ENFORCEMENT_LABEL.encode() in page
    assert ui.WORKFLOW_JS.encode() == workflow_script


def test_dashboard_script_survives_a_missing_workflow_asset(web: Any) -> None:
    """CTL-09 deploys before CTL-07: /lock/workflow.js may not be served yet."""
    server, _ = web
    status, _, script = server.request("GET", "/lock/dashboard.js", owner_headers())
    assert status == 200
    body = script.decode("utf-8")
    assert "!window.DiaryLockWorkflow || typeof window.DiaryLockWorkflow.mount !== 'function'" in body
    assert "Owner workflow is unavailable" in body
    assert 'document.getElementById(\'workflow-error\')' in body
    assert body.index("try {") < body.index("DiaryLockWorkflow.mount({") < body.index("} catch (error)")
    assert "/lock/api/status" in body
