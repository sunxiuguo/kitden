"""Adding a skill or a plugin into one project: the project place, verify, Undo."""

import json

import pytest
from starlette.testclient import TestClient

from kitden.views.inventory import build_inventory
from kitden.web.app import create_app
from tests.conftest import make_home, write_cursor_fixture_state
from tests.unit.test_api_plugin_install import LINTER, FakeCli
from tests.unit.test_catalog import add_marketplaces
from tests.unit.test_demo_mode import demo_page, scan_done, until
from tests.unit.test_skills_catalog import PLUGIN, add_skills_marketplace

ORIGIN = "http://127.0.0.1:47800"
POST = {"Origin": ORIGIN, "X-Tool-Request": "1"}
TIDY = f"tidy@{PLUGIN}"
MARKETPLACE = ".claude/plugins/marketplaces/claude-plugins-official/.claude-plugin/marketplace.json"
HOUSE = "---\nname: house-style\ndescription: Our style.\n---\nKeep it plain.\n"


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = make_home(tmp_path, "claude_code/2.1.283", "codex/0.159.2", "cursor/3.21.16")
    write_cursor_fixture_state(home, {})
    add_marketplaces(home)
    linter = json.loads((home / MARKETPLACE).read_text())["plugins"]
    add_skills_marketplace(home)
    # Both helpers write the same marketplace file: list the plugins of both.
    listed = json.loads((home / MARKETPLACE).read_text())
    listed["plugins"] += linter
    (home / MARKETPLACE).write_text(json.dumps(listed))
    run = FakeCli(home)
    app = create_app(lambda: build_inventory(home), token="tok", port=47800, home=home, run=run)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as c:
        c.get("/?token=tok")
        c.home, c.run = home, run
        yield c


def pid(client, folder):
    return next(p["id"] for p in client.get("/api/projects").json() if p["path"] == folder)


def _library_items(client, project=None):
    params = {"project": project} if project else {}
    response = client.get("/api/library-list", params=params)
    assert response.status_code == 200
    return [item for group in response.json()["groups"] for item in group["items"]]


def _preview(client, action, chosen, params):
    body = {"action": action, "targets": [{"client": c} for c in chosen], "params": params}
    return client.post("/api/changes/preview", json=body, headers=POST)


def test_a_skill_goes_into_the_project_folder(client):
    home, website = client.home, str(client.home / "work/website")
    params = {"skill": TIDY, "project": pid(client, website)}
    change = _preview(client, "skill.install", ["claude_code"], params).json()
    step = change["steps"][0]
    assert (step["scope"], step["path"], step["reaches"]) == (
        "project",
        f"{website}/.claude/skills/tidy",
        ["claude_code", "cursor"],
    )
    assert change["shared_file_warning"] is True
    apply = client.post(f"/api/changes/{change['id']}/apply", headers=POST).json()
    assert apply["state"] == "verified"
    rows = _library_items(client, pid(client, website))
    tidy = next(r for r in rows if r["name"] == "tidy")
    cell = tidy["cells"]["claude_code"]
    assert (cell["state"], cell["where"]) == ("on", "Only this project")
    everywhere = _library_items(client)
    assert not any(r["name"] == "tidy" for r in everywhere)
    undo = client.post(f"/api/changes/{change['id']}/undo", headers=POST).json()
    assert undo["state"] == "undone"
    assert not (home / "work/website/.claude/skills/tidy").exists()


def test_codex_and_cursor_project_folders(client):
    api = str(client.home / "work/api")  # Codex trusts it
    params = {"skill": TIDY, "project": pid(client, api)}
    codex = _preview(client, "skill.install", ["codex"], params).json()
    assert (codex["steps"][0]["path"], codex["steps"][0]["reaches"]) == (
        f"{api}/.agents/skills/tidy",
        ["codex", "cursor"],
    )
    cursor = _preview(client, "skill.install", ["cursor"], params).json()
    assert (cursor["steps"][0]["path"], cursor["steps"][0]["reaches"]) == (
        f"{api}/.cursor/skills/tidy",
        ["cursor"],
    )


def test_a_codex_project_copy_needs_trust(client):
    website = str(client.home / "work/website")
    params = {"skill": TIDY, "project": pid(client, website)}
    answer = _preview(client, "skill.install", ["codex"], params)
    assert (answer.status_code, answer.json()["detail"]) == (
        422,
        "Codex does not trust website yet, so it would not load the skill there.",
    )


def test_a_claude_code_plugin_goes_into_the_project(client):
    website = str(client.home / "work/website")
    settings = client.home / "work/website/.claude/settings.json"
    params = {"plugin": LINTER, "project": pid(client, website)}
    change = _preview(client, "plugin.install", ["claude_code"], params).json()
    step = change["steps"][0]
    assert step["command"] == f"claude plugin install {LINTER} -s project --json"
    assert (step["scope"], step["path"]) == ("project", f"{website}/.claude/settings.json")
    assert change["shared_file_warning"] is True
    # Cursor imports only Claude Code's user plugins, so the review does not promise it.
    assert [a["client"] for a in change["after"]] == ["claude_code"]
    apply = client.post(f"/api/changes/{change['id']}/apply", headers=POST).json()
    assert apply["state"] == "verified"
    assert json.loads(settings.read_text())["enabledPlugins"][LINTER] is True
    assert client.run.calls[-1][-4:] == [LINTER, "-s", "project", "--json"]
    undo = client.post(f"/api/changes/{change['id']}/undo", headers=POST).json()
    assert undo["state"] == "undone"
    assert client.run.calls[-1][1:3] == ["plugin", "uninstall"]
    assert LINTER not in json.loads(settings.read_text())["enabledPlugins"]


def test_codex_has_no_project_place_for_a_plugin(client):
    website = str(client.home / "work/website")
    params = {"plugin": LINTER, "project": pid(client, website)}
    answer = _preview(client, "plugin.install", ["codex"], params)
    assert (answer.status_code, answer.json()["detail"]) == (
        422,
        "Codex has no project place for a plugin here; add it for all projects.",
    )
    unknown = _preview(client, "plugin.install", ["codex"], {"plugin": LINTER, "project": "nope"})
    assert (unknown.status_code, unknown.json()["detail"]) == (422, "Unknown project.")


def test_catalog_offers_preview_the_supported_project_places(client):
    website = pid(client, str(client.home / "work/website"))
    offers = client.get("/api/library-offers").json()["offers"]
    tidy = next(r for r in offers if r["id"] == TIDY)
    assert {"claude_code", "cursor"} <= tidy["routes"].keys()
    for who in ("claude_code", "cursor"):
        response = _preview(client, "skill.install", [who], {"skill": TIDY, "project": website})
        assert response.status_code == 200
        change = response.json()
        assert change["steps"][0]["scope"] == "project"
        client.post(f"/api/changes/{change['id']}/cancel", headers=POST)
    linter = next(r for r in offers if r["id"] == LINTER)
    assert {"claude_code", "codex"} <= linter["routes"].keys()
    response = _preview(
        client, "plugin.install", ["claude_code"], {"plugin": LINTER, "project": website}
    )
    assert response.status_code == 200
    assert response.json()["steps"][0]["scope"] == "project"
    refused = _preview(client, "plugin.install", ["codex"], {"plugin": LINTER, "project": website})
    assert (refused.status_code, refused.json()["detail"]) == (
        422,
        "Codex has no project place for a plugin here; add it for all projects.",
    )


def test_cursor_plugin_cells_say_where_to_add_per_project(client):
    """Cursor keeps its plugins in its own database: in a project view its cell says where."""
    website = pid(client, str(client.home / "work/website"))
    plugins = [r for r in _library_items(client, website) if r["kind"] == "plugin"]
    cursor = [
        r["cells"]["cursor"]
        for r in plugins
        if r["cells"]["cursor"]["state"] in ("none", "via", "done")
    ]
    assert cursor
    for cell in cursor:
        said = (cell["add"] or {}).get("project_hint") or cell["reason"] or ""
        assert "Customize → Plugins, Install, then choose the project scope." in said, cell
    everywhere = [r for r in _library_items(client) if r["kind"] == "plugin"]
    for row in everywhere:
        cell = row["cells"]["cursor"]
        assert "Customize" not in ((cell["add"] or {}).get("project_hint") or cell["reason"] or "")


def test_an_add_without_a_project_stays_for_all_projects(client):
    change = _preview(client, "skill.install", ["claude_code"], {"skill": TIDY}).json()
    assert change["steps"][0]["path"] == f"{client.home}/.claude/skills/tidy"
    assert change["steps"][0]["scope"] == "user"


def test_catalog_skill_preview_requires_codex_project_trust(client):
    """An offered skill can go into a trusted project, but not an untrusted one."""
    offers = client.get("/api/library-offers").json()["offers"]
    tidy = next(r for r in offers if r["id"] == TIDY)
    assert "codex" in tidy["routes"]
    website = pid(client, str(client.home / "work/website"))
    refused = _preview(client, "skill.install", ["codex"], {"skill": TIDY, "project": website})
    assert (refused.status_code, refused.json()["detail"]) == (
        422,
        "Codex does not trust website yet, so it would not load the skill there.",
    )
    api = pid(client, str(client.home / "work/api"))
    response = _preview(client, "skill.install", ["codex"], {"skill": TIDY, "project": api})
    assert response.status_code == 200
    assert response.json()["steps"][0]["scope"] == "project"


def test_installed_skill_cells_explain_codex_project_trust(client):
    change = _preview(client, "skill.install", ["claude_code"], {"skill": TIDY}).json()
    applied = client.post(f"/api/changes/{change['id']}/apply", headers=POST).json()
    assert applied["state"] == "verified"
    website = pid(client, str(client.home / "work/website"))
    rows = _library_items(client, website)
    add = next(r for r in rows if r["name"] == "tidy")["cells"]["codex"]["add"]
    assert (add["project_place"], add["project_hint"]) == (
        False,
        "Adds for all projects. To add it to website only, trust website in Codex first.",
    )
    api = pid(client, str(client.home / "work/api"))
    rows = _library_items(client, api)
    add = next(r for r in rows if r["name"] == "tidy")["cells"]["codex"]["add"]
    assert (add["project_place"], add["project_hint"]) == (True, None)
    undone = client.post(f"/api/changes/{change['id']}/undo", headers=POST).json()
    assert undone["state"] == "undone"


def test_a_skill_add_into_a_known_project_on_another_disk_previews(client, tmp_path):
    site = tmp_path / "other-disk/site"  # outside the home folder
    site.mkdir(parents=True)
    settings_path = client.home / ".claude.json"
    settings = json.loads(settings_path.read_text())
    settings.setdefault("projects", {})[str(site)] = {"hasTrustDialogAccepted": True}
    settings_path.write_text(json.dumps(settings))
    client.post("/api/rescan", headers=POST)  # a fixture home: no held servers to ask
    params = {"skill": TIDY, "project": pid(client, str(site))}
    change = _preview(client, "skill.install", ["claude_code"], params)
    assert change.status_code == 200
    assert all(step["path"].startswith(str(site)) for step in change.json()["steps"])


def _project_skill(client, project):
    """A skill in the project's own Claude Code folder; Claude Code knows the project."""
    folder = client.home / f"work/{project}/.claude/skills/house-style"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(HOUSE)
    settings_path = client.home / ".claude.json"
    settings = json.loads(settings_path.read_text())
    known = {"hasTrustDialogAccepted": True}
    settings.setdefault("projects", {})[str(client.home / f"work/{project}")] = known
    settings_path.write_text(json.dumps(settings))
    client.post("/api/rescan", headers=POST)  # a fixture home: no held servers to ask


def test_a_project_skill_copies_into_the_same_project(client):
    _project_skill(client, "api")  # Codex trusts api
    api = str(client.home / "work/api")
    params = {"skill": "project/house-style", "project": pid(client, api)}
    change = _preview(client, "skill.install", ["codex"], params)
    assert change.json()["steps"][0]["path"] == f"{api}/.agents/skills/house-style"
    refused = _preview(client, "skill.install", ["codex"], {"skill": "project/house-style"})
    assert (refused.status_code, refused.json()["detail"]) == (
        422,
        "This skill is in one project only. Pick that project to add it.",
    )
    website = pid(client, str(client.home / "work/website"))
    elsewhere = {"skill": "project/house-style", "project": website}
    missing = _preview(client, "skill.install", ["codex"], elsewhere)
    assert (missing.status_code, missing.json()["detail"]) == (
        422,
        "This skill is not on this computer.",
    )


def test_a_project_skill_for_codex_in_an_untrusted_project_says_trust_first(client):
    _project_skill(client, "untrusted")
    untrusted = pid(client, str(client.home / "work/untrusted"))
    rows = _library_items(client, untrusted)
    codex = next(r for r in rows if r["name"] == "house-style")["cells"]["codex"]
    assert (codex["add"], codex["reason"]) == (
        None,
        "To add it to untrusted, trust untrusted in Codex first.",
    )


def test_a_project_skill_copy_says_when_cursor_would_load_it_twice(tmp_path):
    # The medium sample: Codex and Cursor load atlas/.agents/skills/api-style, and Cursor reads
    # Claude Code's project folder too, so a copy for Claude Code reaches Cursor a second time.
    with demo_page(tmp_path) as page:
        until(lambda: scan_done(page), 20)
        atlas = pid(page, str(page.home / "work/atlas"))
        params = {"skill": "project/api-style", "project": atlas}
        change = _preview(page, "skill.install", ["claude_code"], params).json()
        assert change["steps"][0]["reaches"] == ["claude_code", "cursor"]
        cursor = next(e for e in change["after"] if e["client"] == "cursor")
        assert (cursor["word"], cursor["note"]) == (
            "Loads twice",
            "Cursor reads two folders that hold this skill, so it loads the skill twice.",
        )
        page.post(f"/api/changes/{change['id']}/cancel", headers=POST)


def test_a_client_with_the_skill_folder_on_disk_gets_no_copy(client):
    # site is Cursor's project only: Cursor reads site/.claude/skills, Claude Code does not list
    # site, yet a copy for Claude Code would land on the folder that is there.
    folder = client.home / "work/site/.claude/skills/house-style"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(HOUSE)
    client.post("/api/rescan", headers=POST)  # a fixture home: no held servers to ask
    site = pid(client, str(client.home / "work/site"))
    rows = _library_items(client, site)
    cells = next(r for r in rows if r["name"] == "house-style")["cells"]
    assert cells["cursor"]["state"] == "on"
    assert (cells["claude_code"]["add"], cells["claude_code"]["state"]) == (None, "on")


def test_cursor_gets_no_copy_of_a_skill_it_reads_from_claude_codes_project_folder(client):
    # website/.claude/skills/site-style is Claude Code's; Cursor reads that folder too.
    website = pid(client, str(client.home / "work/website"))
    rows = _library_items(client, website)
    cursor = next(r for r in rows if r["name"] == "site-style")["cells"]["cursor"]
    assert (cursor["add"], cursor["state"], cursor["route"]) == (
        None,
        "on",
        "through Claude Code's folder",
    )
