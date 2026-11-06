"""The Library: what each client has on this computer and what it can add, in one list."""

import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from kitden.clients.claude_code import ClaudeCodeAdapter
from kitden.clients.codex import CodexAdapter
from kitden.clients.cursor import CursorAdapter
from kitden.demo import build_demo_home
from kitden.model import FindingType
from kitden.views.findings import find_findings
from kitden.views.inventory import LastGood, build_inventory, default_adapters, keep_last_good
from kitden.views.library import _cell as list_cell
from kitden.views.library import differs, library_list
from kitden.views.load_map import load_map
from kitden.web.app import create_app
from tests.conftest import make_home, write_cursor_fixture_state
from tests.unit.test_local_gateway_views import POST, page_for
from tests.unit.test_skills_catalog import (
    PLUGIN,
    add_cursor_plugin,
    add_skills_marketplace,
    install_in_cursor,
)

ORIGIN = "http://127.0.0.1:47800"
DOCUMENTS = "documents@openai-curated"
GLEAN = "glean@cursor-public"
OFFICIAL = "claude-plugins-official"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _skill(folder: Path, name: str, body: str = "") -> None:
    _write(
        folder / name / "SKILL.md",
        f"---\nname: {name}\ndescription: The {name} skill.\n---\n{body}",
    )


def add_documents(home: Path) -> Path:
    """Codex's curated marketplace with one plugin, documents, holding three skills that work
    alone. Codex has it installed; Claude Code and Cursor cannot install it."""
    curated = home / ".codex/.tmp/plugins"
    entry = {
        "name": "documents",
        "description": "Read and write office files",
        "category": "Productivity",
        "source": {"source": "local", "path": "./plugins/documents"},
    }
    listed = {"name": "openai-curated", "plugins": [entry]}
    _write(curated / ".agents/plugins/marketplace.json", json.dumps(listed))
    for name in ("docx", "pptx", "xlsx"):
        _skill(curated / "plugins/documents/skills", name)
    config = home / ".codex/config.toml"
    config.write_text(config.read_text() + f'\n[plugins."{DOCUMENTS}"]\nenabled = true\n')
    return home


def _home(base_dir: Path, cursor_flags: dict | None = None) -> Path:
    """All three clients, Claude Code's marketplace with toolbox, Codex's with documents, and
    Cursor's glean plugin (installed in Cursor, two skills that work alone)."""
    home = make_home(base_dir, "claude_code/2.1.283", "codex/0.159.2", "cursor/3.21.16")
    write_cursor_fixture_state(home, cursor_flags)
    add_skills_marketplace(home)
    add_documents(home)
    add_cursor_plugin(home)
    install_in_cursor(home, GLEAN)
    return home


@pytest.fixture
def home(tmp_path):
    return _home(tmp_path)


def _rows(answer: dict) -> list[dict]:
    """The grouped List's entries and plugin parts, without changing their contracts."""
    return [r for g in answer["groups"] for i in g["items"] for r in (i, *i.get("parts", []))]


def _row(answer: dict, name: str, source: str | None = None, *, part: bool = False) -> dict:
    matches = [
        r
        for r in _rows(answer)
        if r["name"] == name and source in (None, r.get("source")) and (not part or "id" not in r)
    ]
    assert len(matches) == 1, (name, source, matches)
    return matches[0]


def _cell(state="none", word=None, add=None, reason=None) -> dict:
    return {
        "state": state,
        "where": None,
        "origin": None,
        "route": None,
        "stale_at": None,
        "items": [],
        "word": word,
        "add": add,
        "reason": reason,
    }


CODEX_HINT = "Codex plugins are for all projects."


def _add(
    action,
    label,
    plugin=None,
    skill=None,
    marketplace=None,
    warn=None,
    project_place=False,
    project_hint=None,
    project_only=False,
) -> dict:
    return {
        "action": action,
        "label": label,
        "plugin": plugin,
        "skill": skill,
        "marketplace": marketplace,
        "item": None,
        "warn": warn,
        "project_place": project_place,
        "project_hint": project_hint,
        "project_only": project_only,
    }


def test_only_found_clients_are_columns(tmp_path):
    two = make_home(tmp_path / "two", "claude_code/2.1.283", "codex/0.159.2")
    answer = _list(two)
    assert answer["columns"] == [
        {"id": "claude_code", "name": "Claude Code", "mark": "CC", "hidden": False, "note": None},
        {"id": "codex", "name": "Codex", "mark": "CX", "hidden": False, "note": None},
    ]
    assert _rows(answer)
    assert all(list(r["cells"]) == ["claude_code", "codex"] for r in _rows(answer))

    one = make_home(tmp_path / "one", "codex/0.159.2")
    answer = _list(one)
    assert [c["id"] for c in answer["columns"]] == ["codex"]
    assert _rows(answer)
    assert all(list(r["cells"]) == ["codex"] for r in _rows(answer))

    nothing = tmp_path / "none/home"
    nothing.mkdir(parents=True)
    answer = _list(nothing)
    assert (answer["columns"], answer["groups"]) == ([], [])


def test_codex_plugin_offers_add_skills_to_claude_code(home):
    row = _row(_list(home), "documents")
    assert row["lookup"] == DOCUMENTS
    assert row["cells"]["codex"]["state"] == "on"
    assert row["cells"]["claude_code"] == _cell(
        add=_add("plugin.skills", "+ Add skills", DOCUMENTS)
    )
    # Cursor can't install it either, and Claude Code has no way to it: Cursor gets the skills.
    assert row["cells"]["cursor"]["add"]["label"] == "+ Add skills"


def test_cursor_add_skills_in_a_project_says_it_is_for_all_projects(home):
    inventory, adapters = build_inventory(home), {a.id: a for a in default_adapters(home)}
    website = str(home / "work/website")
    answer = library_list(inventory, adapters, website, gateway_registered=False, tab="plugin")
    hint = _row(answer, "documents")["cells"]["cursor"]["add"]["project_hint"]
    assert hint.startswith("Adds its skills for all projects. To add a plugin to one project")


def test_cursor_plugin_offers_add_skills_to_claude_code_and_codex(home):
    # A second Cursor plugin with no skills, and a third whose only skill needs the plugin.
    cache = home / ".cursor/plugins/cache/cursor-public"
    _write(cache / "empty/v1/.cursor-plugin/plugin.json", "{}")
    _skill(cache / "rooted/v1/skills", "deploy", "Run ${CURSOR_PLUGIN_ROOT}/go.sh\n")
    for plugin_id in ("empty@cursor-public", "rooted@cursor-public"):
        install_in_cursor(home, plugin_id)
    answer = _list(home)

    glean = _row(answer, "glean")
    assert (glean["source"], glean["lookup"]) == ("cursor-public", GLEAN)
    assert glean["cells"]["cursor"]["state"] == "on"
    for client in ("claude_code", "codex"):
        assert glean["cells"][client] == _cell(add=_add("plugin.skills", "+ Add skills", GLEAN))

    empty = _row(answer, "empty")["cells"]["codex"]
    assert empty == _cell(reason="Codex can't install this plugin, and it has no skills to copy.")
    rooted = _row(answer, "rooted", "cursor-public")["cells"]["claude_code"]
    assert rooted == _cell(
        reason="Claude Code can't install this plugin, and its skills need the rest of it."
    )


def test_add_skills_turns_into_has_its_skills_when_all_are_there(home):
    for name in ("docx", "pptx"):
        _skill(home / ".claude/skills", name)
    row = _row(_list(home), "documents")
    assert row["cells"]["claude_code"]["add"]["label"] == "+ Add skills"  # xlsx is still missing

    _skill(home / ".claude/skills", "xlsx")
    row = _row(_list(home), "documents")
    assert row["cells"]["claude_code"] == _cell(
        "done",
        word="Has its skills",
        reason="Claude Code already has every skill it can take from this plugin.",
    )
    # Cursor reads Claude Code's skills folder, so it has them all as well.
    assert row["cells"]["cursor"] == _cell(
        "done",
        word="Has its skills",
        reason="Cursor already has every skill it can take from this plugin.",
    )


def test_skills_a_client_loads_from_another_plugin_count_as_had(home):
    # Cursor's superpowers holds brainstorming, which Claude Code loads from its own superpowers.
    cursor_copy = home / ".cursor/plugins/cache/cursor-public/superpowers/1a2b3c4/skills"
    _skill(cursor_copy, "brainstorming")
    row = _row(_list(home), "superpowers", "cursor-public")
    assert row["cells"]["claude_code"] == _cell(
        "done",
        word="Has its skills",
        reason="Claude Code already has every skill it can take from this plugin.",
    )
    assert row["cells"]["codex"] == _cell(
        add=_add("plugin.skills", "+ Add skills", "superpowers@cursor-public")
    )

    # One more skill: Claude Code can take that one, and the review says it has a twin.
    _skill(cursor_copy, "planning")
    row = _row(_list(home), "superpowers", "cursor-public")
    warn = (
        "Claude Code already has superpowers from claude-plugins-official. "
        "It will load skills from both."
    )
    assert row["cells"]["claude_code"] == _cell(
        add=_add("plugin.skills", "+ Add skills", "superpowers@cursor-public", warn=warn)
    )
    assert row["cells"]["codex"]["add"]["warn"] is None


def test_a_plugin_in_another_project_is_not_offered_again(home):
    settings = home / "work/website/.claude/settings.json"
    data = json.loads(settings.read_text())
    data["enabledPlugins"][PLUGIN] = True
    settings.write_text(json.dumps(data))
    inventory = build_inventory(home)
    adapters = {a.id: a for a in default_adapters(home)}

    def toolbox(project):
        answer = library_list(inventory, adapters, project, gateway_registered=False, tab="plugin")
        return next(
            (r["cells"]["claude_code"] for r in _rows(answer) if r["name"] == "toolbox"), None
        )

    assert toolbox(str(home / "work/website"))["state"] == "on"
    elsewhere = None  # The grouped List shows only plugins in the selected scope.
    assert toolbox(None) == elsewhere
    assert toolbox(str(home / "work/api")) == elsewhere


def test_own_skill_that_reaches_outside_its_folder_says_why(home):
    _skill(home / ".claude/skills", "notes", "Read ../shared/notes.md\n")
    notes = _row(_list(home), "notes")
    assert notes["source"] == "Claude Code" and notes["cells"]["claude_code"]["state"] == "on"
    assert notes["cells"]["codex"] == _cell(
        reason="This skill uses files outside its folder, so it can't be copied."
    )


def test_skill_cells_keep_the_state_the_scan_found(home):
    answer = _list(home)
    # Codex has pixel-art turned off in config.toml.
    codex = _row(answer, "pixel-art")["cells"]["codex"]
    assert (codex["state"], codex["where"]) == ("off", None) and codex["items"]
    # Details can find a skill's definitions by name.
    brandkit = _row(answer, "brandkit")
    assert brandkit["lookup"] == "brandkit"
    assert brandkit["cells"]["claude_code"]["where"] is None
    assert brandkit["cells"]["cursor"]["route"] == "through Claude Code's folder"
    assert brandkit["cells"]["codex"]["add"]["action"] == "skill.install"
    # Where the scan shows nothing, the skill list says how the client gets it.
    superpowers = _row(answer, "brainstorming", f"superpowers@{OFFICIAL}")
    assert superpowers["cells"]["cursor"] == {**_cell("on"), "route": "from a plugin"}


def test_library_and_its_offers_let_other_requests_run_meanwhile(home, monkeypatch):
    # The first read of every skill takes seconds; other requests must not wait for it.
    import kitden.web.app as page

    started, release = threading.Event(), threading.Event()

    def slow(real):
        def wait(*args, **kwargs):
            started.set()
            assert release.wait(5), "no other request could run meanwhile"
            return real(*args, **kwargs)

        return wait

    monkeypatch.setattr(page, "library_list", slow(page.library_list))
    monkeypatch.setattr(page, "library_offers", slow(page.library_offers))
    app = create_app(lambda: build_inventory(home), token="tok", port=47800, home=home)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        client.get("/?token=tok")
        for path in ("/api/library-list?kind=plugin", "/api/library-offers"):
            started.clear()
            release.clear()
            with ThreadPoolExecutor(1) as pool:
                slow_read = pool.submit(client.get, path)
                assert started.wait(5)
                assert client.get("/api/clients").status_code == 200
                release.set()
                assert slow_read.result().status_code == 200, path


# Instructions ---------------------------------------------------------------------------------


def test_the_instructions_tab_lists_files_per_client(home):
    answer = _list(home, "instructions")
    names = {(r["name"], r["category"]) for r in _rows(answer)}
    assert {
        ("CLAUDE.md", None),
        ("AGENTS.md", None),
        ("review", "command"),
        ("reviewer", "agent"),
    } <= names
    cells = [c for r in _rows(answer) for c in r["cells"].values()]
    assert all(c["add"] is None for c in cells)
    assert {c["word"] for c in cells if c["state"] != "none"} == {"Reads it"}


def test_project_agents_md_keeps_its_own_readers_without_a_flag(home):
    site = str(home / "work/site")
    inventory = build_inventory(home)
    assert not [f for f in find_findings(inventory, site) if "AGENTS.md" in f.names]
    answer = library_list(
        inventory,
        {a.id: a for a in default_adapters(home)},
        site,
        gateway_registered=False,
        tab="instructions",
    )
    project_group = next(g for g in answer["groups"] if g["key"] == "project")
    (row,) = [r for r in project_group["items"] if r["name"] == "AGENTS.md"]
    assert (row["cells"]["codex"]["word"], row["cells"]["cursor"]["word"]) == (
        None,
        "Reads it",
    )
    assert row["flags"] == []


def test_a_command_and_an_agent_with_one_name_are_two_rows(home):
    _write(home / ".claude/agents/review.md", "---\nname: review\n---\nReview code.\n")
    rows = [r for r in _rows(_list(home, "instructions")) if r["name"] == "review"]
    assert sorted(r["category"] for r in rows) == ["agent", "command"]
    assert len({r["id"] for r in rows}) == 2


# Unknown: what Kitden could not read ----------------------------------------------------------


class _BrokenCodex(CodexAdapter):
    def scan(self):
        raise ValueError("a format Kitden does not know")


def test_a_client_that_could_not_be_read_keeps_its_column_as_unknown(home):
    inventory = build_inventory(
        home, adapters=[ClaudeCodeAdapter(home), _BrokenCodex(home), CursorAdapter(home)]
    )
    rows = load_map(inventory, None)
    assert {r.cells["codex"].state for r in rows} == {"unk"}
    assert {r.cells["codex"].reason for r in rows} == {"Could not read Codex."}
    adapters = {a.id: a for a in default_adapters(home)}
    answer = library_list(inventory, adapters, None, gateway_registered=False, tab="plugin")
    assert [(c["id"], c["note"]) for c in answer["columns"]] == [
        ("claude_code", None),
        ("codex", "Could not read"),
        ("cursor", None),
    ]
    cells = [r["cells"]["codex"] for r in _rows(answer)]
    assert cells and all(c["state"] == "unk" and c["add"] is None for c in cells)


def _preview_github_into(home, client):
    with page_for(home) as page:
        github = next(
            i
            for i in page.get("/api/items?kind=mcp_server").json()
            if (i["client"], i["name"]) == ("claude_code", "github")
        )
        body = {
            "action": "mcp.add",
            "targets": [{"client": client}],
            "params": {"item": github["id"]},
        }
        return page.post("/api/changes/preview", json=body, headers=POST)


def test_a_preview_into_a_file_that_could_not_be_read_says_why(home):
    (home / ".cursor/mcp.json").write_text("{ not json")
    answer = _preview_github_into(home, "cursor")
    assert (answer.status_code, answer.json()) == (
        422,
        {"detail": "Can't add while mcp.json can't be read."},
    )


def test_a_preview_into_a_broken_codex_config_is_refused_not_a_crash(home):
    # Codex stays found; the file is a problem.
    (home / ".codex/config.toml").write_text("not = [toml\n")
    answer = _preview_github_into(home, "codex")
    assert (answer.status_code, answer.json()) == (
        422,
        {"detail": "Can't add while config.toml can't be read."},
    )


def test_the_medium_sample_names_its_unreadable_file_above_the_table(tmp_path):
    home = build_demo_home(tmp_path, "medium")  # Part 1: work/website/.cursor/mcp.json is cut short
    inventory = build_inventory(home)
    adapters = {a.id: a for a in default_adapters(home)}

    def answer(project):
        return library_list(inventory, adapters, project, gateway_registered=False, tab="plugin")

    website = answer(str(home / "work/website"))
    assert "Could not read mcp.json; its items are not shown." in website["notes"]
    # The active preview still permits adding into Cursor's readable all-projects file.
    assert _preview_github_into(home, "cursor").status_code == 200
    # The file belongs to website, so All projects and the other projects do not name it.
    assert not any(n.startswith("Could not read mcp.json") for n in answer(None)["notes"])


# The last good read, kept while the page runs ------------------------------------------------


def _known_projects(home: Path, *folders: Path) -> None:
    """Claude Code lists these folders as projects too."""
    path = home / ".claude.json"
    state = json.loads(path.read_text())
    state["projects"].update({str(f): {} for f in folders})
    path.write_text(json.dumps(state))


def _notes(home: Path, project: Path | None) -> list[str]:
    inventory = build_inventory(home)
    adapters = {a.id: a for a in default_adapters(home)}
    where = str(project) if project else None
    answer = library_list(inventory, adapters, where, gateway_registered=False, tab="plugin")
    return answer["notes"]


def test_a_broken_user_file_is_named_when_the_home_folder_is_a_known_project(home):
    # Anyone who ran claude in ~ has it in the project list; ~/.cursor/mcp.json is still the user's.
    _known_projects(home, home)
    (home / ".cursor/mcp.json").write_text("{ not json")
    line = "Could not read mcp.json; its items are not shown."
    assert line in _notes(home, None)
    assert line in _notes(home, home / "work/website")


def test_a_broken_project_file_belongs_to_the_deepest_project(home):
    work, website = home / "work", home / "work/website"
    _known_projects(home, work)
    (website / ".claude/settings.json").write_text("{ not json")
    line = "Could not read settings.json; its items are not shown."
    assert line in _notes(home, website)
    assert line not in _notes(home, work)
    assert line not in _notes(home, None)


def _after_a_break(tmp_path: Path, broken: str):
    """The medium sample read well, then read again with `broken` given a stray comma."""
    home = build_demo_home(tmp_path, "medium")
    last = LastGood()
    keep_last_good(build_inventory(home), last, "2026-10-22T14:02:00+00:00")
    path = home / broken
    path.write_text("," + path.read_text())
    inventory = keep_last_good(build_inventory(home), last, "2026-10-22T14:05:00+00:00")
    adapters = {a.id: a for a in default_adapters(home)}

    def cell(project: str, kind: str, name: str, client: str, source: str | None = None):
        if kind in ("mcp_server", "instructions"):
            rows = load_map(inventory, str(home / "work" / project))
            return vars(
                next(r for r in rows if r.name == name and str(r.kind) == kind).cells[client]
            )
        answer = library_list(
            inventory,
            adapters,
            str(home / "work" / project),
            gateway_registered=False,
            tab="plugin",
            show="all",
        )
        return _row(answer, name, source)["cells"][client]

    return cell


def test_project_items_stay_while_claude_json_is_broken(tmp_path):
    # Claude Code reads a project's files only for the projects ~/.claude.json lists.
    cell = _after_a_break(tmp_path, ".claude.json")
    for kind, name, source in (
        ("plugin", "superpowers", "demo-market"),
        ("skill", "brainstorming", "superpowers@demo-market"),
        ("skill", "planning", "superpowers@demo-market"),
    ):
        off = cell("website", kind, name, "claude_code", source)
        assert (off["state"], off["where"], off["stale_at"]) == (
            "off",
            "This project",
            "2026-10-22T14:02:00+00:00",
        ), name
    docs = cell("atlas", "mcp_server", "docs", "claude_code")
    assert (docs["where"], docs["stale_at"]) == ("This project", "2026-10-22T14:02:00+00:00")


def test_project_items_stay_while_codex_config_is_broken(tmp_path):
    # Codex reads a project's skills and AGENTS.md only for projects config.toml trusts.
    cell = _after_a_break(tmp_path, ".codex/config.toml")
    style = cell("atlas", "skill", "api-style", "codex")
    assert (style["state"], style["where"]) == ("on", "Only this project")
    assert style["reason"].startswith("From the last good read at ")
    agents = cell("atlas", "instructions", "AGENTS.md", "codex")
    assert (agents["where"], agents["stale_at"]) == (
        "Only this project",
        "2026-10-22T14:02:00+00:00",
    )


def _toolbox_cell(home: Path, project: Path) -> dict:
    # Install it in Codex so the grouped List exposes Claude Code's missing-plugin action.
    config = home / ".codex/config.toml"
    if PLUGIN not in config.read_text():
        config.write_text(config.read_text() + f'\n[plugins."{PLUGIN}"]\nenabled = true\n')
    inventory = build_inventory(home)
    adapters = {a.id: a for a in default_adapters(home)}
    answer = library_list(inventory, adapters, str(project), gateway_registered=False, tab="plugin")
    return _row(answer, "toolbox")["cells"]["claude_code"]


def test_an_add_with_a_project_place_is_held_only_when_both_places_are_broken(home):
    website = home / "work/website"
    assert _toolbox_cell(home, website)["add"]["project_place"] is True
    (home / ".claude/settings.json").write_text("{ not json")
    assert _toolbox_cell(home, website)["add"]["action"] == "plugin.install"  # the project's reads
    (website / ".claude/settings.json").write_text("{ not json")
    held = _toolbox_cell(home, website)
    assert (held["state"], held["add"], held["reason"]) == (
        "unk",
        None,
        "Can't add while settings.json can't be read.",
    )


def test_a_place_kitden_cannot_add_into_does_not_keep_an_add_offered(home):
    # The project folder is gone, so the add can go to all projects only; that file is broken.
    (home / ".claude/settings.json").write_text("{ not json")
    cell = _toolbox_cell(home, home / "work/deleted-project")
    assert (cell["add"], cell["reason"]) == (None, "Can't add while settings.json can't be read.")


def test_a_definition_kept_from_the_last_good_read_cannot_be_changed(home):
    with page_for(home) as page:
        (home / ".claude.json").write_text("{ broken")
        page.post("/api/rescan", headers=POST)
        github = next(
            i
            for i in page.get("/api/items?kind=mcp_server").json()
            if (i["client"], i["name"]) == ("claude_code", "github")
        )
        assert github["stale_at"]
        body = {"action": "mcp.remove_direct", "targets": [{"item": github["id"]}]}
        answer = page.post("/api/changes/preview", json=body, headers=POST)
    assert (answer.status_code, answer.json()) == (
        422,
        {"detail": "Kitden can't change this until it reads .claude.json again."},
    )


def test_a_plugin_skill_is_not_review_after_the_marketplace_moves_on(home):
    # The marketplace copy says "Think first."; Claude Code's cache copy is another version.
    row = _row(_list(home), "brainstorming", f"superpowers@{OFFICIAL}")
    assert (row["cells"]["claude_code"]["state"], row["cells"]["claude_code"]["route"]) == (
        "on",
        "from a plugin",
    )


@pytest.mark.parametrize("folder", [".agents/skills", ".codex/skills"])
def test_a_plugin_skill_kitden_copied_is_still_that_plugins_skill(home, folder):
    # Kitden's "+ Add" copies the marketplace's folder; Claude Code loads its own cache copy.
    market = home / ".claude/plugins/marketplaces/claude-plugins-official/plugins/superpowers"
    shutil.copytree(market / "skills/brainstorming", home / folder / "brainstorming")
    inventory = build_inventory(home)
    row = next(r for r in load_map(inventory, None) if r.name == "brainstorming")
    assert "review" not in {c.state for c in row.cells.values()}
    shown = _row(_list(home), "brainstorming", f"superpowers@{OFFICIAL}", part=True)
    assert "review" not in {c["state"] for c in shown["cells"].values()}
    notes = {f.type for f in find_findings(inventory) if "brainstorming" in f.names}
    assert FindingType.SAME_NAME_OTHER_SOURCE not in notes


def test_a_finder_file_does_not_make_a_skill_copy_need_review(home):
    _skill(home / ".claude/skills", "lint-rules", "Lint first.")
    _skill(home / ".codex/skills", "lint-rules", "Lint first.")
    (home / ".codex/skills/lint-rules/.DS_Store").write_bytes(b"\0\0\0\1Bud1")
    row = next(r for r in load_map(build_inventory(home), None) if r.name == "lint-rules")
    assert (row.cells["claude_code"].state, row.cells["codex"].state) == ("on", "on")
    shown = _row(_list(home), "lint-rules")
    assert "review" not in {c["state"] for c in shown["cells"].values()}


def test_a_copy_made_before_a_plugin_update_does_not_flag_the_plugins_skill(home):
    # Codex has a copy of Claude Code's plugin skill (Cursor reads it too); then the plugin's
    # files change. The plugin is the source; the old copy drifted.
    cache = home / ".claude/plugins/cache/claude-plugins-official/superpowers/7.0.0/skills"
    shutil.copytree(cache / "brainstorming", home / ".codex/skills/brainstorming")
    skill_md = cache / "brainstorming/SKILL.md"
    skill_md.write_text(skill_md.read_text() + "\nA newer version.\n")
    inventory = build_inventory(home)
    row = next(r for r in load_map(inventory, None) if r.name == "brainstorming")
    assert {c: row.cells[c].state for c in ("claude_code", "codex", "cursor")} == {
        "claude_code": "on",
        "codex": "on",
        "cursor": "on",
    }
    shown = _row(_list(home), "brainstorming", f"superpowers@{OFFICIAL}", part=True)
    assert shown["cells"]["claude_code"]["state"] == "on"
    notes = {f.type for f in find_findings(inventory) if "brainstorming" in f.names}
    assert FindingType.SAME_NAME_OTHER_SOURCE not in notes


def test_a_project_only_skill_adds_into_the_project(tmp_path):
    home = build_demo_home(tmp_path, "medium")
    inventory, adapters = build_inventory(home), {a.id: a for a in default_adapters(home)}
    atlas_path = str(home / "work/atlas")
    atlas = library_list(inventory, adapters, atlas_path, gateway_registered=False, tab="plugin")
    style = _row(atlas, "api-style")
    add = style["cells"]["claude_code"]["add"]
    assert (add["action"], add["skill"], add["project_only"]) == (
        "skill.install",
        "project/api-style",
        True,
    )
    assert (add["project_place"], add["project_hint"]) == (False, None)
    assert style["cells"]["codex"]["where"] == "Only this project"
    assert style["source"] == "atlas"
    everywhere = library_list(inventory, adapters, None, gateway_registered=False, tab="plugin")
    assert not any(r["name"] == "api-style" for r in _rows(everywhere))


# The List (spec 5.4) ---------------------------------------------------------------------------


def _list(home: Path, tab: str = "plugin", project: str | None = None, **options) -> dict:
    inventory = build_inventory(home)
    adapters = {a.id: a for a in default_adapters(home)}
    return library_list(inventory, adapters, project, gateway_registered=False, tab=tab, **options)


def _items(answer: dict) -> dict[str, dict]:
    return {i["name"]: i for g in answer["groups"] for i in g["items"]}


def test_the_list_has_the_two_tabs_and_one_column_per_client(tmp_path):
    home = build_demo_home(tmp_path, "medium")
    answer = _list(home)
    assert [c["id"] for c in answer["columns"]] == ["claude_code", "codex", "cursor"]
    # One group row per source, as the map draws them: marketplaces, Other plugins (here only the
    # account's), then Your skills and Your settings. Every item has one cell per client.
    assert [(g["key"], g["kind"]) for g in answer["groups"]] == [
        ("marketplace:cursor-public", "marketplace"),
        ("marketplace:demo-market", "marketplace"),
        ("marketplace:openai-bundled", "marketplace"),
        ("other", "other"),  # Claude account plugins and connectors
        ("skills", "skills"),
        ("settings", "settings"),
    ]
    for item in _items(answer).values():
        assert set(item["cells"]) == {"claude_code", "codex", "cursor"}
    # MCP servers are on the Gateway now: none here.
    assert not any(i["kind"] == "mcp_server" for i in _items(answer).values())


def test_a_plugin_row_holds_its_parts_with_their_own_cells_or_its_plugins(tmp_path):
    home = build_demo_home(tmp_path, "medium")
    items = _items(_list(home))
    superpowers = items["superpowers"]
    assert superpowers["kind"] == "plugin" and superpowers["count"] == 2
    assert superpowers["cells"]["codex"]["add"]["action"] == "plugin.install"
    # A skill part has its skill row's cells: Codex can take a copy of it.
    planning = next(p for p in superpowers["parts"] if p["name"] == "planning")
    assert (planning["key"], planning["kind"]) == ("skills", "skill")
    assert planning["cells"]["codex"]["add"]["action"] == "skill.install"
    # Any other part follows its plugin, with no add.
    index = next(p for p in items["pr-review"]["parts"] if p["name"] == "pr-index")
    assert index["kind"] == "mcp_server"
    assert {c: cell["state"] for c, cell in index["cells"].items()} == {
        "claude_code": "off",
        "codex": "none",
        "cursor": "off",
    }
    assert not any(cell["add"] for cell in index["cells"].values())


def test_hooks_sit_under_your_settings_and_marketplaces_are_group_rows(tmp_path):
    home = build_demo_home(tmp_path, "medium")
    answer = _list(home)
    settings = next(g for g in answer["groups"] if g["key"] == "settings")
    assert settings["name"] == "Your settings"
    assert {i["kind"] for i in settings["items"]} == {"hook"}
    assert "PreToolUse: Bash" in {i["name"] for i in settings["items"]}
    market = next(g for g in answer["groups"] if g["key"] == "marketplace:demo-market")
    assert (market["name"], market["icon"]) == ("demo-market", "store")
    # Items carry what a link from another page selects them by.
    assert _items(answer)["superpowers"]["lookup"] == "superpowers@demo-market"


def test_differs_by_class_unknown_review_and_a_kept_cell():
    both = ["claude_code", "codex"]
    on, off, none = list_cell("on"), list_cell("off"), list_cell()
    assert not differs({"claude_code": on, "codex": on}, both)
    assert not differs({"claude_code": none, "codex": list_cell(add={"label": "+ Add"})}, both)
    assert differs({"claude_code": on, "codex": none}, both)
    assert differs({"claude_code": on, "codex": off}, both)
    assert differs({"claude_code": on, "codex": list_cell("unk")}, both)
    assert differs({"claude_code": on, "codex": list_cell("review")}, both)
    assert differs({"claude_code": on, "codex": on, "cursor": list_cell("on", stale_at="t")}, both)
    # Only the shown clients count.
    assert not differs({"claude_code": on, "codex": none}, ["claude_code"])


def test_differences_shows_only_rows_whose_cells_differ_and_a_plugin_whose_part_does(tmp_path):
    # Review Focus, on the large sample.
    # Nothing there is in all three clients: every item differs. With Codex hidden, the plugins
    # Claude Code and Cursor both load do not, unless a part does.
    home = build_demo_home(tmp_path, "large")
    assert len(_items(_list(home, show="diff"))) == len(_items(_list(home)))
    hidden = frozenset({"codex"})
    everything = _items(_list(home, hidden=hidden))
    diff = _list(home, show="diff", hidden=hidden)
    shown = _items(diff)
    assert shown and len(shown) < len(everything)
    assert "plugin-01" not in shown
    columns = [c["id"] for c in diff["columns"] if not c["hidden"] and not c["note"]]
    assert columns == ["claude_code", "cursor"]
    for name, item in everything.items():
        parts = item.get("parts", [])
        own = differs(item["cells"], columns)
        assert (name in shown) == (own or any(differs(p["cells"], columns) for p in parts)), name
    # No group is left with nothing in it.
    assert all(g["items"] for g in diff["groups"])


def test_a_plugin_differs_when_only_one_of_its_parts_does(tmp_path, monkeypatch):
    import kitden.views.library as lib

    home = build_demo_home(tmp_path, "medium")
    real = lib._parts

    def same_plugin_cells(item, cells, skills):
        parts = real(item, cells, skills)
        for client in cells:
            cells[client] = list_cell("on")
        return parts

    monkeypatch.setattr(lib, "_parts", same_plugin_cells)
    shown = _items(_list(home, show="diff"))
    # superpowers is On everywhere now, but Codex lacks its skills: it still differs.
    assert "superpowers" in shown
    assert not differs(shown["superpowers"]["cells"], ["claude_code", "codex", "cursor"])


def test_needs_attention_still_filters_the_list(home):
    # The same rows as the plugin and skill tables flag: Cursor loads superpowers twice, and
    # docker-expert from two folders.
    answer = _list(home, attention=True)
    flagged = [(i["name"], i["source"]) for g in answer["groups"] for i in g["items"]]
    assert flagged == [
        ("superpowers", OFFICIAL),
        ("superpowers", "cursor-public"),
        ("docker-expert", "Codex and Cursor"),
    ]
    assert answer["attention"] == _list(home)["attention"] == 3


def test_the_list_search_finds_a_name_or_a_part(tmp_path):
    home = build_demo_home(tmp_path, "medium")
    assert set(_items(_list(home, q="PLAN"))) == {"superpowers"}
    assert set(_items(_list(home, q="brand"))) == {"brandkit"}


def test_the_instructions_list_has_one_row_per_file(tmp_path):
    home = build_demo_home(tmp_path, "medium")
    answer = _list(home, "instructions")
    assert [(g["key"], g["name"], g["icon"]) for g in answer["groups"]] == [
        ("all", "All projects", "layers"),
        ("commands", "Commands and agents", "square-terminal"),
    ]
    files = answer["groups"][0]["items"]
    assert [(f["kind"], f["name"]) for f in files] == [("file", "CLAUDE.md"), ("file", "AGENTS.md")]
    words = {f["name"]: {c: cell["word"] for c, cell in f["cells"].items()} for f in files}
    assert words["CLAUDE.md"] == {"claude_code": "Reads it", "codex": None, "cursor": None}
    # The user's commands and agents are rows too, under their own group; an Overview link
    # selects a row by its item's name.
    commands = answer["groups"][1]["items"]
    assert [(f["kind"], f["category"], f["name"], f["names"]) for f in commands] == [
        ("file", "command", "review", ["review"]),
        ("file", "agent", "reviewer", ["reviewer"]),
    ]
    assert commands[0]["cells"]["claude_code"]["word"] == "Reads it"
    # A project's own files come in their own group.
    project = str(home / "work/atlas")
    groups = _list(home, "instructions", project)["groups"]
    assert [g["key"] for g in groups] == ["all", "project", "commands"]
    assert groups[1]["name"] == "Only this project · atlas"


def test_api_library_list_takes_a_tab_and_show_diff(tmp_path):
    home = build_demo_home(tmp_path, "medium")
    app = create_app(lambda: build_inventory(home), token="tok", port=47800, home=home)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        client.get("/?token=tok")
        answer = client.get("/api/library-list?kind=plugin").json()
        assert set(answer) == {"project", "columns", "groups", "attention", "notes"}
        # An old tab or show choice gets Plugins and skills, as installed.
        old = client.get("/api/library-list?kind=marketplace&show=new").json()
        assert old == answer
        diff = client.get("/api/library-list?kind=plugin&show=diff").json()
        assert {i["name"] for g in diff["groups"] for i in g["items"]} <= {
            i["name"] for g in answer["groups"] for i in g["items"]
        }
        files = client.get("/api/library-list?kind=instructions").json()
        assert [i["kind"] for g in files["groups"] for i in g["items"]] == ["file"] * 4
        hidden = client.get("/api/library-list?kind=plugin&hidden=codex,cursor").json()
        assert [c["id"] for c in hidden["columns"] if c["hidden"]] == ["codex", "cursor"]


def test_a_plugin_kitden_read_nothing_inside_keeps_its_skills_and_account_plugins_show(home):
    # Codex's documents: the map has no `inside` for it, so its parts are the skill rows from it,
    # with their own cells: Claude Code and Cursor can take a copy.
    items = _items(_list(home))
    documents = items["documents"]
    assert documents["count"] is None
    assert [(p["kind"], p["name"]) for p in documents["parts"]] == [
        ("skill", "docx"),
        ("skill", "pptx"),
        ("skill", "xlsx"),
    ]
    docx = documents["parts"][0]
    assert docx["cells"]["claude_code"]["add"]["action"] == "skill.install"
    assert docx["flags"] == []
    # Search and Differences see them too.
    assert "documents" in _items(_list(home, q="pptx"))
    assert "documents" in _items(_list(home, show="diff"))
    # Claude Code keeps some plugins in the account: Unknown, under "Other plugins".
    other = next(g for g in _list(home)["groups"] if g["key"] == "other")
    account = next(
        i for i in other["items"] if i["name"] == "Claude account plugins and connectors"
    )
    assert account["cells"]["claude_code"]["state"] == "unk"


def test_a_plugin_whose_part_has_a_warning_needs_attention():
    from kitden.views.library import _warns

    warning = [{"text": "Loads twice", "level": "warning"}]
    assert _warns({"flags": [], "parts": [{"flags": warning}]})
    assert _warns({"flags": warning})
    assert not _warns({"flags": [], "parts": [{"flags": []}]})


def test_an_instruction_file_cell_is_unknown_for_a_client_kitden_could_not_read(home):
    inventory = build_inventory(
        home, adapters=[ClaudeCodeAdapter(home), _BrokenCodex(home), CursorAdapter(home)]
    )
    adapters = {a.id: a for a in default_adapters(home)}
    answer = library_list(inventory, adapters, None, gateway_registered=False, tab="instructions")
    files = [f for g in answer["groups"] for f in g["items"]]
    assert files and {f["cells"]["codex"]["state"] for f in files} == {"unk"}
    assert {f["cells"]["codex"]["reason"] for f in files} == {"Could not read Codex."}
    # A client Kitden read and that does not read the file: nothing.
    claude = next(f for f in files if f["name"] == "CLAUDE.md")
    assert claude["cells"]["cursor"]["state"] == "none"


def test_the_account_row_says_why_and_goes_with_its_hidden_column(tmp_path):
    home = build_demo_home(tmp_path, "medium")
    other = next(g for g in _list(home)["groups"] if g["key"] == "other")
    account = other["items"][0]
    assert (account["name"], account["account"]) == ("Claude account plugins and connectors", True)
    assert account["reason"] == (
        "Stored in your Claude account. Change it in the Claude app or on claude.ai."
    )
    # Only Claude Code has it: with Claude Code hidden it is left out, and so is its group.
    hidden = _list(home, hidden=frozenset({"claude_code"}))
    assert "other" not in [g["key"] for g in hidden["groups"]]


def test_cursor_gets_nothing_through_claude_code_when_claude_code_is_not_found(home):
    """Cursor loads Claude Code's plugins only when Claude Code is on this computer: without it
    the Library offers nothing "Via Claude Code", as the maps do."""
    import dataclasses

    from kitden.model import ClientStatus

    inventory = build_inventory(home)
    inventory.clients = [
        dataclasses.replace(c, status=ClientStatus.NOT_FOUND) if c.id == "claude_code" else c
        for c in inventory.clients
    ]
    adapters = {a.id: a for a in default_adapters(home)}
    answer = library_list(inventory, adapters, None, gateway_registered=False, tab="plugin")
    assert "Via Claude Code" not in json.dumps(answer)
    assert "loads Claude Code's plugins" not in json.dumps(answer)


def test_same_name_plugins_keep_separate_rows_and_warn_before_adding(home):
    config = home / ".codex/config.toml"
    config.write_text(
        config.read_text() + f'\n[plugins."superpowers@{OFFICIAL}"]\nenabled = true\n'
    )
    _skill(home / ".cursor/plugins/cache/cursor-public/superpowers/1a2b3c4/skills", "planning")
    answer = _list(home)
    plugins = [i for g in answer["groups"] for i in g["items"] if i["name"] == "superpowers"]
    assert {i["source"] for i in plugins} == {OFFICIAL, "cursor-public"}
    assert all(i["same_name"] for i in plugins)
    assert len({i["id"] for i in plugins}) == 2
    cursor = next(i for i in plugins if i["source"] == "cursor-public")
    assert "already has superpowers" in cursor["cells"]["codex"]["add"]["warn"]


def test_hiding_clients_drops_their_items_but_hiding_everyone_hides_none(home):
    shown = _list(home)
    hidden = _list(home, hidden=frozenset({"codex"}))
    assert "documents" in _items(shown) and "documents" not in _items(hidden)
    assert next(c for c in hidden["columns"] if c["id"] == "codex")["hidden"]
    everyone = _list(home, hidden=frozenset({"claude_code", "codex", "cursor"}))
    assert everyone == shown


def test_failed_client_keeps_installed_plugin_cells_from_last_good_read(home):
    last = LastGood()
    keep_last_good(build_inventory(home), last, "2026-10-22T14:02:00+00:00")
    broken = [ClaudeCodeAdapter(home), _BrokenCodex(home), CursorAdapter(home)]
    inventory = keep_last_good(
        build_inventory(home, adapters=broken), last, "2026-10-22T14:05:00+00:00"
    )
    adapters = {a.id: a for a in default_adapters(home)}
    answer = library_list(inventory, adapters, None, gateway_registered=False, tab="plugin")
    # The load map retains the failed client's definitions; the List still marks its column.
    documents = next(r for r in load_map(inventory, None) if r.name == "documents")
    codex = documents.cells["codex"]
    assert (codex.state, codex.stale_at) == ("on", "2026-10-22T14:02:00+00:00")
    assert codex.reason.startswith("From the last good read at ")
    assert next(c for c in answer["columns"] if c["id"] == "codex")["note"] == "Could not read"
    assert not any(n.startswith("Could not read") for n in answer["notes"])


def test_review_skill_keeps_adds_blocked_in_the_grouped_list(home):
    _skill(home / ".claude/skills", "lint-rules", "Lint first.")
    _skill(home / ".codex/skills", "lint-rules", "Different instructions.")
    row = _row(_list(home), "lint-rules")
    assert "review" in {cell["state"] for cell in row["cells"].values()}
    assert all(cell["add"] is None for cell in row["cells"].values())


def test_retired_library_route_is_not_registered(home):
    app = create_app(lambda: build_inventory(home), token="tok", port=47800, home=home)
    assert "/api/library" not in {route.path for route in app.routes}
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        client.get("/?token=tok")
        for path in ("/api/library", "/api/library?kind=plugin&show=all"):
            assert client.get(path).status_code == 404
        assert client.get("/api/library-list").status_code == 200
        assert client.get("/api/library-map").status_code == 200


def test_installed_plugin_notes_follow_visible_clients_and_search(home):
    note = (
        "Codex gets the claude-plugins-official marketplace "
        "the first time you add one of its plugins."
    )
    assert _list(home)["notes"].count(note) == 1
    assert note not in _list(home, hidden=frozenset({"codex"}))["notes"]
    assert note not in _list(home, q="documents")["notes"]
