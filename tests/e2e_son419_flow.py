"""End-to-end proof of the SON-419 routes over PostgreSQL wire protocol.

Boots the FastAPI app against an ephemeral PGlite wire server (same pattern
as e2e_auth_flow.py) and drives the real HTTP routes: instruction
versioning + in-effect-since, task list privacy, tasks, the activity feed,
settings (profile/password/company/team/billing/integrations), CSV import
preview + commit, and onboarding state.
"""

from __future__ import annotations

import importlib
import os
import traceback
from urllib.parse import unquote

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import database

database.engine = create_async_engine(
    os.environ["MANAGER_DATABASE_URL"],
    pool_pre_ping=False,
    connect_args={"statement_cache_size": 0},
)
database.SessionFactory = async_sessionmaker(database.engine, expire_on_commit=False)

main = importlib.import_module("app.main")

original_set_tenant_scope = main.set_tenant_scope


async def _pglite_set_tenant_scope(session: object, scope: object) -> None:
    await session.execute(text("SET LOCAL ROLE manager_app"))
    await original_set_tenant_scope(session, scope)


main.set_tenant_scope = _pglite_set_tenant_scope


def _run() -> None:
    with TestClient(main.app, base_url="https://testserver") as client:
        # Register an org + owner (this drives Organization with onboarding_state).
        reg = client.post(
            "/auth/register",
            json={
                "account_type": "business",
                "full_name": "Owner 419",
                "email": "owner-419@example.com",
                "password": "correct-horse-battery-staple",
                "company_name": "Sonnia 419 Demo",
                "country": "United Kingdom",
            },
        )
        assert reg.status_code == 201, reg.text
        verification_url = reg.json()["development_url"]
        assert verification_url
        verified = client.post(
            "/auth/verify-email",
            json={"token": unquote(verification_url.rsplit("/", maxsplit=1)[-1])},
        )
        assert verified.status_code == 200, verified.text

        # /instructions: create + edit -> version history + in-effect-since.
        created = client.post(
            "/api/instructions",
            json={
                "title": "Follow-up cadence",
                "body": "Call back within one business day.",
                "slug": "Follow-up within a day",
                "applies_to": "all",
            },
        )
        assert created.status_code == 201, created.text
        first = created.json()
        assert first["slug"] == "follow-up-within-a-day"
        assert first["version"] == 1
        assert first["effective_from"] is not None
        assert first["active"] is True

        edited = client.patch(
            f"/api/instructions/{first['slug']}",
            json={"body": "Call back within four hours."},
        )
        assert edited.status_code == 200, edited.text
        second = edited.json()
        assert second["version"] == 2
        assert second["active"] is True

        active_list = client.get("/api/instructions")
        assert active_list.status_code == 200
        # Only v2 is active now.
        assert len(active_list.json()["items"]) == 1
        assert active_list.json()["items"][0]["version"] == 2

        history = client.get(f"/api/instructions/{first['slug']}/history")
        assert history.status_code == 200
        versions = [item["version"] for item in history.json()["items"]]
        assert 1 in versions and 2 in versions

        # /tasks + task-lists: private default, visibility toggle present.
        my_list = client.post(
            "/api/task-lists",
            json={"name": "My list", "visibility": "private"},
        )
        assert my_list.status_code == 201, my_list.text
        team_list = client.post(
            "/api/task-lists",
            json={"name": "Team board", "visibility": "shared"},
        )
        assert team_list.status_code == 201, team_list.text
        lists = client.get("/api/task-lists")
        assert lists.status_code == 200
        names = {item["name"] for item in lists.json()}
        assert "My list" in names and "Team board" in names

        task = client.post(
            "/api/tasks",
            json={
                "title": "Prepare deck",
                "description": "**Markdown** body",
                "visibility": "private",
            },
        )
        assert task.status_code == 201, task.text
        assert task.json()["owner_user_id"] is not None
        assert task.json()["visibility"] == "private"

        shared_task = client.post(
            "/api/tasks",
            json={"title": "Team task", "visibility": "shared"},
        )
        assert shared_task.status_code == 201, shared_task.text
        tasks = client.get("/api/tasks")
        assert tasks.status_code == 200
        titles = {item["title"] for item in tasks.json()}
        assert "Prepare deck" in titles and "Team task" in titles

        # /activity: one feed, shows the writes we just made.
        feed = client.get("/api/activity")
        assert feed.status_code == 200
        actions = {item["action"] for item in feed.json()["items"]}
        assert {"instruction_created", "instruction_updated", "task_created"} <= actions

        # Settings: profile + company.
        profile = client.get("/api/settings/profile")
        assert profile.status_code == 200
        assert profile.json()["email"] == "owner-419@example.com"
        assert profile.json()["onboarding_state"] == "pending"

        company = client.get("/api/settings/company")
        assert company.status_code == 200
        assert company.json()["name"] == "Sonnia 419 Demo"
        assert company.json()["department_count"] == 1

        patched_company = client.patch(
            "/api/settings/company", json={"duplicate_call_protection": "block"}
        )
        assert patched_company.status_code == 200
        assert patched_company.json()["duplicate_call_protection"] == "block"

        team = client.get("/api/settings/team")
        assert team.status_code == 200
        assert len(team.json()) == 1

        billing = client.get("/api/settings/billing")
        assert billing.status_code == 200
        assert billing.json()["action"] == "contact_us"

        integrations = client.get("/api/settings/integrations")
        assert integrations.status_code == 200

        sessions = client.get("/api/settings/security/sessions")
        assert sessions.status_code == 200
        assert any(item["current"] for item in sessions.json())

        # Change password + security event log.
        pw = client.post(
            "/api/settings/security/password",
            json={
                "current_password": "correct-horse-battery-staple",
                "new_password": "a-brand-new-stable-password",
            },
        )
        assert pw.status_code == 204, pw.text
        events = client.get("/api/settings/security/events")
        assert events.status_code == 200
        assert any(item["event_type"] == "password_changed" for item in events.json())

        # CSV import: preview then commit (dedupe by email/phone).
        rows = [
            {
                "index": 0,
                "values": {"Name": "Ada", "Email": "ada@x.io", "Phone": "+6590000001"},
            },
            {
                "index": 1,
                "values": {"Name": "Bob", "Email": "bob@x.io", "Phone": "+6590000002"},
            },
            {
                "index": 2,
                "values": {
                    "Name": "Ada Again",
                    "Email": "ada@x.io",
                    "Phone": "+6590000001",
                },
            },
        ]
        preview = client.post(
            "/api/contacts/import/preview",
            json={
                "filename": "leads.csv",
                "columns": ["Name", "Email", "Phone"],
                "rows": rows,
            },
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["created_estimate"] == 2
        assert preview.json()["merged_estimate"] == 1

        mapping = preview.json()["mapping"]
        committed = client.post(
            "/api/contacts/import",
            json={
                "filename": "leads.csv",
                "columns": ["Name", "Email", "Phone"],
                "rows": rows,
                "mapping": mapping,
            },
        )
        assert committed.status_code == 201, committed.text
        summary = committed.json()
        assert summary["created_count"] == 2
        assert summary["merged_count"] == 1
        assert summary["skipped_count"] == 0

        # Importing the same rows again merges against the DB, creating nothing.
        second_commit = client.post(
            "/api/contacts/import",
            json={
                "filename": "leads.csv",
                "columns": ["Name", "Email", "Phone"],
                "rows": rows,
                "mapping": mapping,
            },
        )
        assert second_commit.status_code == 201, second_commit.text
        assert second_commit.json()["created_count"] == 0
        assert second_commit.json()["merged_count"] == 3

        # Onboarding flow: pending -> instructions -> import -> invite -> complete.
        for state in ("instructions", "import", "invite", "complete"):
            res = client.patch("/api/onboarding", json={"state": state})
            assert res.status_code == 200, res.text
        final = client.get("/api/onboarding")
        assert final.status_code == 200
        assert final.json()["state"] == "complete"
        assert final.json()["next"] is None

        print(
            "SON-419 E2E PASS: instructions versioning, task privacy, activity feed,"
            " settings, CSV dedupe, onboarding."
        )


if __name__ == "__main__":
    try:
        _run()
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1) from exc
