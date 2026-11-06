"""The Library List: installed plugins, skills, hooks and instruction files.

Rows join the load map (what is on this computer) with the plugin and skill catalogs (what the
marketplaces on this computer offer). Each cell already says the state, or the one action for
that client, or why there is none. Columns are the clients found on this computer, and those
Kitden could not read: their cells say Unknown and offer no add. A row where a client's item has
the name but another source says "Needs review" there and offers no add.
"""

from pathlib import Path
from typing import NamedTuple

from kitden.changes.actions import (
    PLUGIN_INSTALL,
    PLUGIN_SKILLS,
    SKILL_INSTALL,
)
from kitden.clients.base import found_columns, is_home
from kitden.model import CLIENT_WORDS, CLIENTS, ClientStatus, Item, ItemKind, Scope, _hash
from kitden.views.catalog import catalog
from kitden.views.instructions_map import instructions_map
from kitden.views.library_map import OTHER, cursor_imports, library_map
from kitden.views.load_map import MARKS, Row, load_map
from kitden.views.match import Copies, catalog_keys, plugin_copies, plugin_keys, source_keys
from kitden.views.skills import project_skills, skill_catalog
from kitden.views.words import (
    CLIENT_UNREAD,
    NO_ADD_REVIEW,
    NO_ADD_UNREAD,
    NOT_SAME,
    NOT_SHOWN,
    ORIGIN_WORDS,
    PROJECT_TRUST,
    ROUTE_WORDS,
    STATE_WORDS,
)

VIA_CLAUDE_CODE = "Cursor loads Claude Code's plugins. Add it to Claude Code."
SKILL_VIA_CLAUDE_CODE = "Cursor loads Claude Code's plugins. Add the plugin to Claude Code."
OUTSIDE_FOLDER = "This skill uses files outside its folder, so it can't be copied."
# Where a plugin goes into one project when Kitden cannot put it there.
# checked 2026-10-22 (learn.chatgpt.com/docs/plugins): Codex installs a plugin for its whole
# environment; the docs show no per-project install.
CODEX_PROJECT = "Codex plugins are for all projects."
# checked 2026-10-22 (cursor.com/docs/plugins): in Customize you select Install and choose a
# project or user scope. Cursor keeps it in its own database, so Kitden writes nothing.
CURSOR_PROJECT = (
    "To add a plugin to one project in Cursor: Customize → Plugins, Install, "
    "then choose the project scope."
)


def _cell(
    state: str = "none",
    where: str | None = None,
    items: list[str] | None = None,
    word: str | None = None,
    add: dict | None = None,
    reason: str | None = None,
    origin: str | None = None,
    route: str | None = None,
    stale_at: str | None = None,
) -> dict:
    return {
        "state": state,
        "where": where,
        "origin": origin,
        "route": route,
        "stale_at": stale_at,
        "items": items or [],
        "word": word,
        "add": add,
        "reason": reason,
    }


def _add(
    action: str,
    label: str,
    plugin: str | None = None,
    skill: str | None = None,
    marketplace: str | None = None,
    warn: str | None = None,
    item: str | None = None,
    project_place: bool = False,
    project_hint: str | None = None,
    project_only: bool = False,
) -> dict:
    """One add. `project_place`: with a project picked it can go into that project only.
    `project_hint`: where it goes instead, said in a project view. `project_only`: it goes into
    the picked project, with no choice (a skill only that project has)."""
    return {
        "action": action,
        "label": label,
        "plugin": plugin,
        "skill": skill,
        "marketplace": marketplace,
        "item": item,
        "warn": warn,
        "project_place": project_place,
        "project_hint": project_hint,
        "project_only": project_only,
    }


def _plugin_add(
    client: str, label: str, plugin_id: str, marketplace: str | None, warn: str | None = None
) -> dict:
    """A plugin install: Claude Code can put it into one project; Codex cannot."""
    return _add(
        PLUGIN_INSTALL,
        label,
        plugin_id,
        marketplace=marketplace,
        warn=warn,
        project_place=client == "claude_code",
        project_hint=CODEX_PROJECT if client == "codex" else None,
    )


def _row(
    key: str,
    kind: str,
    name: str,
    source: str | None,
    cells: dict[str, dict],
    *,
    lookup: str | None,
    flags: list[dict] | None = None,
) -> dict:
    return {
        "key": key,
        "kind": kind,
        "name": name,
        "source": source,
        "same_name": False,
        "lookup": lookup,
        "flags": flags or [],
        "cells": cells,
    }


def _has(row: Row | None, client: str) -> bool:
    """The load map says the client has the item (loads it, has it off, or may load it)."""
    return row is not None and row.cells[client].state not in ("none", "missing")


def _map_cell(row: Row, client: str) -> dict:
    cell = row.cells[client]
    return _cell(
        cell.state,
        cell.where,
        cell.items,
        reason=cell.reason,
        origin=cell.origin,
        route=cell.route,
        stale_at=cell.stale_at,
    )


# Each builder gives (row, holders): holders are the found clients that have the item.
Built = list[tuple[dict, set[str]]]


def _map_rows(
    map_rows: list[Row],
    kind: str,
    found: list[str],
) -> Built:
    """Hook rows from the load map, with no add action."""
    out = []
    for r in map_rows:
        if str(r.kind) != kind:
            continue
        holders = {c for c in found if _has(r, c)}
        cells = {c: _map_cell(r, c) for c in found}
        row = _row(
            r.key,
            kind,
            r.name,
            r.source,
            cells,
            lookup=r.lookup,
            flags=r.flags,
        )
        out.append((row, holders))
    return out


# Plugins -------------------------------------------------------------------------------------


def _plugin_cell(
    client: str,
    plugin_id: str,
    name: str,
    offer: dict | None,
    routes: dict[str, str],
    skills: list[dict],
    cursor_imports: bool,
    twin: str | None,
) -> dict:
    """What a client without the plugin here can do: add it, get it through Claude Code, or
    take the skills that work alone. Otherwise, why not. A route comes only with an offer.
    `twin` is the other marketplace the client has a plugin of this name from."""
    who = CLIENT_WORDS[client]
    # The client has it in a project this view does not show; adding it again would fail.
    if offer and client in offer["installed"]:
        return _cell(reason=f"{who} has it in another project.")
    route = routes.get(client)
    if offer and route:
        marketplace = offer["marketplace"] if route == "add_marketplace" else None
        warn = f"{who} already has {name} from {twin}. It will load both." if twin else None
        return _cell(add=_plugin_add(client, "+ Add", plugin_id, marketplace, warn))
    # When Claude Code has the plugin, the load map already shows Cursor's copy; when it shows
    # none (an account plugin), Cursor does not import it.
    if client == "cursor" and cursor_imports and "claude_code" in routes:
        return _cell("via", word=STATE_WORDS["via"], reason=VIA_CLAUDE_CODE)
    alone = [s for s in skills if s["self_contained"]]
    if any(client in s["routes"] for s in alone):
        warn = (
            f"{who} already has {name} from {twin}. It will load skills from both."
            if twin
            else None
        )
        return _cell(add=_add(PLUGIN_SKILLS, "+ Add skills", plugin_id, warn=warn))
    # Copied here, or brought by another plugin: either way the client has them.
    if alone and all(client in s["installed"] for s in alone):
        return _cell(
            "done",
            word=STATE_WORDS["done"],
            reason=f"{who} already has every skill it can take from this plugin.",
        )
    why = "its skills need the rest of it" if skills else "it has no skills to copy"
    return _cell(reason=f"{who} can't install this plugin, and {why}.")


def _cursor_in_project(cell: dict) -> dict:
    """A Cursor plugin cell in a project view: say where a plugin goes into one project."""
    if cell["add"]:
        cell["add"]["project_hint"] = f"Adds its skills for all projects. {CURSOR_PROJECT}"
    else:
        cell["reason"] = " ".join(filter(None, [cell["reason"], CURSOR_PROJECT]))
    return cell


def _plugin_rows(
    map_rows: list[Row],
    offers: list[dict],
    skills: list[dict],
    found: list[str],
    cursor_imports: bool,
    in_project: bool = False,
) -> Built:
    """Plugins on this computer and plugins in its marketplaces, one row per
    name@marketplace: a load map row and an offer with the same id are one row."""
    rows = {r.key: r for r in map_rows if r.kind is ItemKind.PLUGIN}
    offered = {_hash(ItemKind.PLUGIN, o["id"]): o for o in offers}
    by_plugin: dict[str, list[dict]] = {}
    for skill in skills:
        if skill["plugin"]:
            by_plugin.setdefault(skill["plugin"], []).append(skill)
    # Which marketplaces each client already has a plugin of this name from.
    has_name: dict[tuple[str, str], list[str]] = {}
    for row in rows.values():
        for client in found:
            if row.source and _has(row, client):
                has_name.setdefault((row.name, client), []).append(row.source)

    out = []
    for key in [*rows, *(k for k in offered if k not in rows)]:
        row, offer = rows.get(key), offered.get(key)
        plugin_id = offer["id"] if offer else row.lookup
        name = row.name if row else offer["name"]
        source = row.source if row else offer["marketplace"]
        routes = {c: r for c, r in (offer["routes"] if offer else {}).items() if c in found}
        plugin_skills = by_plugin.get(plugin_id, [])
        cells = {}
        for client in found:
            if _has(row, client):
                cells[client] = _map_cell(row, client)
                continue
            others = [m for m in has_name.get((name, client), []) if m != source]
            cells[client] = _plugin_cell(
                client,
                plugin_id,
                name,
                offer,
                routes,
                plugin_skills,
                cursor_imports,
                others[0] if others else None,
            )
            if client == "cursor" and in_project:
                _cursor_in_project(cells[client])
        installed = offer["installed"] if offer else []
        holders = {c for c in found if _has(row, c) or c in installed}
        built = _row(
            key,
            "plugin",
            name,
            source,
            cells,
            lookup=row.lookup if row else None,
            flags=row.flags if row else [],
        )
        out.append((built, holders))
    return out


# Skills --------------------------------------------------------------------------------------


def _installed_route(word: str) -> str | None:
    """The route of the skill list's installed word: "Installed" has none, "Via plugin" is
    from a plugin, "Via Claude Code" through Claude Code's folder."""
    if word == "Via plugin":
        return ROUTE_WORDS["plugin"]
    if word.startswith("Via "):
        return ROUTE_WORDS["folder"].format(word.removeprefix("Via "))
    return None


def _skill_cell(
    client: str,
    skill: dict,
    row: Row | None,
    plugin_routes: dict[str, str],
    cursor_imports: bool,
    no_place: dict[str, str] | None = None,
    where: str | None = None,
) -> dict:
    """The scan's state when it has one; else how the skill list says the client gets it, or
    what the client can add. `no_place`: clients that cannot take a copy in the picked project,
    with what to say instead. `where`: the tag of an installed one: "Only this project" for a
    project's own skill, else None. A skill only the picked project has adds into that project,
    or says why not."""
    if _has(row, client):
        return _map_cell(row, client)
    word = skill["installed"].get(client)
    if word:
        return _cell("on", where=where, route=_installed_route(word))
    if client in skill["routes"] and skill["project"]:
        if client in (no_place or {}):
            return _cell(reason=PROJECT_TRUST.format(name=Path(skill["project"]).name))
        return _cell(add=_add(SKILL_INSTALL, "+ Add", skill=skill["id"], project_only=True))
    if client in skill["routes"]:
        hint = (no_place or {}).get(client)
        add = _add(SKILL_INSTALL, "+ Add", skill=skill["id"], project_place=hint is None)
        return _cell(add={**add, "project_hint": hint})
    # A found client without the skill and without a route: it does not work alone.
    if skill["plugin"] is None:
        return _cell(reason=OUTSIDE_FOLDER)
    route = plugin_routes.get(client)
    if route:
        marketplace = skill["marketplace"] if route == "add_marketplace" else None
        return _cell(add=_plugin_add(client, "+ Add the plugin", skill["plugin"], marketplace))
    if client == "cursor" and cursor_imports and "claude_code" in plugin_routes:
        return _cell("via", word=STATE_WORDS["via"], reason=SKILL_VIA_CLAUDE_CODE)
    return _cell(reason=f"{CLIENT_WORDS[client]} can't install the plugin this skill needs.")


def _not_this_skill(
    cells: dict[str, dict], skill: dict, items: dict[str, Item], copies: Copies
) -> None:
    """A plugin's skill against the client's own of that name: when they share no files and no
    plugin, the client has another skill, so its cell needs review. A part of this plugin is
    this skill without reading its files."""
    on = [c for c, cell in cells.items() if cell["state"] == "on" and cell["items"]]
    if not on or skill["plugin"] is None:
        return
    keys = None
    for client in on:
        theirs = [items[i] for i in cells[client]["items"] if i in items]
        if f"plugin:{skill['plugin']}" in frozenset().union(*map(plugin_keys, theirs)):
            continue
        keys = keys or catalog_keys(skill)
        if keys.isdisjoint(frozenset().union(*(source_keys(i, copies) for i in theirs))):
            cells[client].update(state="review", reason=NOT_SAME)


def _no_skill_place(adapters, project: str | None) -> dict[str, str]:
    """Codex loads a project's skills only when it trusts the project (codex.py), so in an
    untrusted one its skill add goes to all projects and says why."""
    codex = adapters.get("codex")
    if project is None or codex is None or codex.trusts(project):
        return {}
    name = Path(project).name
    hint = f"Adds for all projects. To add it to {name} only, trust {name} in Codex first."
    return {"codex": hint}


def _skill_rows(
    map_rows: list[Row],
    offers: list[dict],
    skills: list[dict],
    found: list[str],
    cursor_imports: bool,
    items: dict[str, Item],
    copies: Copies,
    no_place: dict[str, str] | None = None,
    in_project: bool = False,
    project_rows: list[dict] | tuple = (),
) -> Built:
    """Every skill in the skill catalog, then the picked project's own skills the catalog has
    no row of that name for (`project_rows`), plus skills the load map has that neither has.

    A catalog row takes the cells, flags and lookup of the load map row with its name, where
    that row has them; a plugin's skill that is not the one a client loads needs review there.
    The first one also takes its key, so links from the Overview still find it; keys stay unique.
    """
    mapped = {r.name: r for r in map_rows if r.kind is ItemKind.SKILL}
    routes = {o["id"]: o["routes"] for o in offers}
    catalog_names = {s["name"] for s in skills}
    skills = skills + [s for s in project_rows if s["name"] not in catalog_names]
    used: set[str] = set()
    out = []
    for skill in skills:
        # The skill list reads no project: its installed words come from user skills and from
        # plugins of any scope. In a project an installed cell carries no tag; a project row's
        # says "Only this project".
        where = ORIGIN_WORDS["project"] if in_project and skill["project"] else None
        row = mapped.get(skill["name"])
        key = row.key if row and row.key not in used else _hash(ItemKind.SKILL, skill["id"])
        used.add(key)
        plugin_routes = {c: r for c, r in routes.get(skill["plugin"], {}).items() if c in found}
        holders = {c for c in found if _has(row, c) or c in skill["installed"]}
        cells = {
            c: _skill_cell(c, skill, row, plugin_routes, cursor_imports, no_place, where)
            for c in found
        }
        _not_this_skill(cells, skill, items, copies)
        built = _row(
            key,
            "skill",
            skill["name"],
            skill["from"],
            cells,
            lookup=row.lookup if row else None,
            # A project's own skill is in no marketplace: Details reads what the clients load.
            flags=row.flags if row else [],
        )
        out.append((built, holders))
    named = {s["name"] for s in skills}
    extra = [r for r in map_rows if r.kind is ItemKind.SKILL and r.name not in named]
    return out + _map_rows(extra, "skill", found)


# What Kitden could not read -----------------------------------------------------------------


def _unread_cells(rows: list[dict], by_key: dict[str, Row], columns: list[str], found: list[str]):
    """Give each row a cell per client Kitden could not read: the load map row's cell when the
    row has one (Unknown, or kept from the last good read), else Unknown. Never an add. Cells
    keep the column order."""
    unread = [c for c in columns if c not in found]
    for row in rows:
        known = by_key.get(row["key"])
        for client in unread:
            row["cells"][client] = (
                _map_cell(known, client)
                if known
                else _cell("unk", reason=CLIENT_UNREAD.format(CLIENT_WORDS[client]))
            )
        row["cells"] = {c: row["cells"][c] for c in columns}


def _read_notes(inventory, project: str | None, homes: list[Path]) -> list[str]:
    """One line per file Kitden could not read in this view. A file inside known projects
    belongs to the deepest one and shows only when it is picked; the home folder is not a
    project here, as its own files are the user's. A client not read at all has no line (its
    column says it), nor a file whose items are kept from the last good read (its cells say it).
    """
    folders = [
        (p.path, Path(p.path))
        for p in inventory.projects
        if not any(is_home(Path(p.path), h) for h in homes)
    ]
    kept = {i.source.path for i in inventory.items if i.stale_at}
    lines: list[str] = []
    for problem in inventory.problems:
        if not problem.path or problem.path in kept:
            continue
        path = Path(problem.path)
        inside = [(name, f) for name, f in folders if path.is_relative_to(f)]
        owner = max(inside, key=lambda pair: len(pair[1].parts))[0] if inside else None
        line = NOT_SHOWN.format(path.name)
        if owner in (None, project) and line not in lines:
            lines.append(line)
    return lines


def _add_file(adapters, client: str, add: dict, project: str | None) -> str | None:
    """The settings file an add's step edits, put in `project` (None: all projects). The client's
    own step maker builds the step, which never runs. None for a skill copy (it writes only
    folders), or when the client cannot make the step."""
    adapter = adapters.get(client)
    try:
        if adapter is None:
            return None
        if add["action"] == PLUGIN_INSTALL:
            step = adapter.plugin_install_step(add["plugin"], project)
        else:
            return None
    except ValueError:
        return None
    return step.edit.path if step.edit else None


def _no_add_in_review(rows: list[dict]) -> None:
    """In a skill row with a Needs review cell no cell offers an add: it would
    copy one side without asking. Every empty cell says so, unless it says why already."""
    for row in rows:
        cells = row["cells"].values()
        if row["kind"] != "skill" or all(c["state"] != "review" for c in cells):
            continue
        for cell in cells:
            if cell["add"] or (cell["state"] == "none" and not cell["reason"]):
                cell["add"] = None
                cell["reason"] = NO_ADD_REVIEW


def _hold_adds(rows: list[dict], adapters, broken: set[str], project: str | None) -> None:
    """No add into a file Kitden could not read: the client may have the item there, so its
    cell is Unknown and says why. Adds into the client's other files stay. An add that can go
    into this project or all projects is held only when every place Kitden can add into is
    broken."""
    if not broken:
        return
    files: dict[tuple, str | None] = {}  # one step per client, action and place

    def file(client: str, add: dict, place: str | None) -> str | None:
        key = (client, add["action"], place)
        if key not in files:
            files[key] = _add_file(adapters, client, add, place)
        return files[key]

    for row in rows:
        for client, cell in row["cells"].items():
            add = cell["add"]
            if not add:
                continue
            places = [project, None] if project is not None and add["project_place"] else [None]
            usable = [f for f in (file(client, add, p) for p in places) if f]
            if usable and all(f in broken for f in usable):
                cell["state"] = "unk"
                cell["add"] = None
                cell["reason"] = NO_ADD_UNREAD.format(Path(usable[0]).name)


# Filters -------------------------------------------------------------------------------------


def _mark_same_names(rows: list[dict]) -> None:
    seen: dict[str, int] = {}
    for row in rows:
        seen[row["name"]] = seen.get(row["name"], 0) + 1
    for row in rows:
        row["same_name"] = seen[row["name"]] > 1


def _in_view(row: dict, holders: set[str], visible: list[str]) -> bool:
    """Not a row only hidden clients have, and a shown client has it or can do something."""
    if holders and not holders.intersection(visible):
        return False
    return any(row["cells"][c]["state"] != "none" or row["cells"][c]["add"] for c in visible)


def _needs_attention(row: dict) -> bool:
    return any(f.get("level") == "warning" for f in row["flags"])


def _notes(rows: list[dict], visible: list[str]) -> list[str]:
    """One line per client and marketplace an add brings in first."""
    pairs = {
        (c, r["cells"][c]["add"]["marketplace"])
        for r in rows
        for c in visible
        if r["cells"][c]["add"] and r["cells"][c]["add"]["marketplace"]
    }
    return [
        f"{CLIENT_WORDS[c]} gets the {m} marketplace the first time you add one of its plugins."
        for c, m in sorted(pairs, key=lambda p: (CLIENTS.index(p[0]), p[1]))
    ]


class _Tables(NamedTuple):
    """Every kind's rows with their cells, and what they are read against."""

    columns: list[str]  # the found clients and those Kitden could not read
    found: list[str]
    hidden: frozenset[str]
    visible: list[str]  # found clients that are not hidden
    homes: list[Path]
    rows: dict[str, list[dict]]  # by kind, only the rows in view
    by_key: dict[str, Row]


def _tables(
    inventory, adapters, project: str | None, *, gateway_registered: bool, hidden: frozenset[str]
) -> _Tables:
    """The rows of every kind tab, before any finish: `hidden` columns leave out a row only they
    have, and hiding every found client hides none."""
    status = {c.id: c.status for c in inventory.clients}
    found = found_columns(inventory)
    columns = [c for c in CLIENTS if c in found or status.get(c) is ClientStatus.UNREADABLE]
    if hidden.issuperset(found):
        hidden = frozenset()
    visible = [c for c in found if c not in hidden]
    homes = list({a.home for a in adapters.values()})
    imports = cursor_imports(adapters, found)

    map_rows = load_map(inventory, project, gateway_registered=gateway_registered)
    offers = catalog(inventory, adapters)
    skills = skill_catalog(inventory, adapters)
    built = {
        "plugin": _plugin_rows(
            map_rows, offers, skills, found, imports, in_project=project is not None
        ),
        "skill": _skill_rows(
            map_rows,
            offers,
            skills,
            found,
            imports,
            {i.id: i for i in inventory.items},
            plugin_copies(inventory),
            _no_skill_place(adapters, project),
            in_project=project is not None,
            project_rows=project_skills(inventory, adapters, project) if project else [],
        ),
        # Kitden adds no hooks, so a hook's dash has no reason.
        "hook": _map_rows(map_rows, "hook", found),
    }
    by_key = {r.key: r for r in map_rows}
    # A shown client Kitden could not read makes a row visible only with items kept from its
    # last good read: they are on this computer.
    unread = [c for c in columns if c not in found and c not in hidden]
    rows = {}
    for k, pairs in built.items():
        _mark_same_names([r for r, _ in pairs])
        rows[k] = []
        for r, holders in pairs:
            known = by_key.get(r["key"])
            kept = known and any(known.cells[c].stale_at for c in unread)
            if not kept and not _in_view(r, holders, visible):
                continue
            rows[k].append(r)
    return _Tables(columns, found, hidden, visible, homes, rows, by_key)


def _finish(t: _Tables, kind: str, inventory, adapters, project: str | None) -> list[dict]:
    """A kind's rows with their last cells: Unknown for a client Kitden could not read, no add
    into a file it could not read, and no add in a row that needs review. After the view, so a
    held add keeps its row where it was."""
    rows = t.rows[kind]
    _unread_cells(rows, t.by_key, t.columns, t.found)
    broken = {p.path for p in inventory.problems if p.path}
    _hold_adds(rows, adapters, broken, project)
    _no_add_in_review(rows)
    return rows


def _columns(t: _Tables) -> list[dict]:
    return [
        {
            "id": c,
            "name": CLIENT_WORDS[c],
            "mark": MARKS[c],
            "hidden": c in t.hidden,
            "note": None if c in t.found else STATE_WORDS["unread"],
        }
        for c in t.columns
    ]


# The List ------------------------------------------------------------------------------------

# The List's tabs (spec 5.4): every plugin, skill and hook by where it comes from, or the
# instruction files. It shows what is installed, or only what differs between the clients.
LIST_TABS = ("plugin", "instructions")
LIST_SHOWS = ("mine", "diff")
# A cell for Differences: the client has it, has it off, or lacks it. Unknown, Needs review and a
# cell kept from the last good read always differ.
CELL_CLASS = {
    "on": "has",
    "gw": "has",
    "done": "has",
    "off": "off",
    "none": "lacks",
    "via": "lacks",
}
ALWAYS_DIFFERS = ("unk", "review")
# A part of a plugin with no row of its own shows its plugin's states, with no add.
FOLLOWS = ("on", "off", "unk", "review", "gw", "via")
# What a plugin holds, by its key in the Library map answer, and the kind of its part row.
PART_KINDS = {
    "skills": "skill",
    "commands": "command",
    "agents": "agent",
    "hooks": "hook",
    "mcp_servers": "mcp_server",
}
READS_IT = "Reads it"
FILE_ICONS = {"all": "layers", "project": "folder", "commands": "square-terminal"}
# A map client's link, as a cell when the tables have no row for the item.
BY_STATES = {"on": "on", "off": "off", "unknown": "unk", "via": "via"}


def differs(cells: dict[str, dict], visible: list[str]) -> bool:
    """The cells of the shown clients differ: one has it and another not, or off next to on.
    A cell Kitden does not know, one that needs review, and one kept from the last good read
    (in any column) always differ."""
    if any(cell["stale_at"] for cell in cells.values()):
        return True
    shown = [cells[c] for c in visible if c in cells]
    if any(cell["state"] in ALWAYS_DIFFERS for cell in shown):
        return True
    return len({CELL_CLASS.get(cell["state"], "lacks") for cell in shown}) > 1


def _by_cells(item: dict, columns: list[str], found: list[str]) -> dict[str, dict]:
    """Cells from the map's links, for an item the tables have no row for."""
    out = {}
    for client in columns:
        state = BY_STATES.get(item["by"].get(client), "none")
        if client not in found and state == "none":
            out[client] = _cell("unk", reason=CLIENT_UNREAD.format(CLIENT_WORDS[client]))
        else:
            out[client] = _cell(state, word=STATE_WORDS["via"] if state == "via" else None)
    return out


def _following(cells: dict[str, dict]) -> dict[str, dict]:
    """A part's cells that follow its plugin: the plugin's states, no add and no reason."""
    out = {}
    for client, cell in cells.items():
        state = cell["state"] if cell["state"] in FOLLOWS else "none"
        word = cell["word"] if state == cell["state"] else None
        out[client] = _cell(state, cell["where"], word=word, stale_at=cell["stale_at"])
    return out


def _parts(item: dict, cells: dict[str, dict], skills: dict[tuple, dict]) -> list[dict]:
    """A plugin's parts, in the map's order: a skill with a row of its own (the skill table's
    row with its name from this plugin) has that row's cells and flags; any other part follows
    the plugin. When Kitden read nothing inside the plugin, its parts are the skill rows from
    it."""
    inside = item.get("inside")
    if inside is None:
        mine = [r["name"] for (source, _), r in skills.items() if source == item.get("plugin")]
        inside = {"skills": mine}
    out = []
    for key, kind in PART_KINDS.items():
        for name in inside.get(key, []):
            own = skills.get((item.get("plugin"), name)) if kind == "skill" else None
            part = {"key": key, "kind": kind, "name": name, "flags": own["flags"] if own else []}
            if own:
                part.update(
                    source=own["source"],
                    same_name=own["same_name"],
                    lookup=own["lookup"],
                    cells=own["cells"],
                )
            else:
                part.update(source=item["source"], same_name=False, cells=_following(cells))
            out.append(part)
    return out


def _account_rows(tables: dict[str, list[dict]], shown: set[str], t: _Tables, items) -> list:
    """Plugin rows the map leaves out because Claude Code keeps them in the account (the
    "Claude account plugins and connectors" row, Unknown): every item of the row is an
    account's. They go under "Other plugins", with the reason Kitden gives for them; one that
    only hidden clients have is left out."""
    by_id = {i.id: i for i in items}
    out = []
    for row in tables["plugin"]:
        known = t.by_key.get(row["key"])
        cells = known.cells if known else {}
        ids = [i for cell in cells.values() for i in cell.items]
        if row["key"] in shown or not ids:
            continue
        if any(i not in by_id or by_id[i].scope is not Scope.ACCOUNT for i in ids):
            continue
        if not {c for c in cells if cells[c].items}.intersection(t.visible):
            continue
        out.append(
            {
                "id": row["key"],
                "kind": "plugin",
                "name": row["name"],
                "source": row["source"],
                "same_name": row["same_name"],
                "lookup": row["lookup"] or row["name"],
                "flags": row["flags"],
                "count": None,
                "cells": row["cells"],
                "parts": [],
                "account": True,
                "reason": by_id[ids[0]].change_hint,
            }
        )
    return out


def _item_groups(
    answer: dict, tables: dict[str, list[dict]], t: _Tables, found_items
) -> list[dict]:
    """The Library map's groups and items, each item with the cells of its row in the plugin,
    skill or hook table, and a plugin's parts; the plugins Claude Code keeps in the account join
    "Other plugins" (`_account_rows`). An item only hidden clients have is left out."""
    rows = {r["key"]: r for kind in ("plugin", "skill", "hook") for r in tables[kind]}
    skills = {(r["source"], r["name"]): r for r in tables["skill"]}
    out = []
    for group in answer["groups"]:
        items = []
        for item in group["items"]:
            holders = [c for c, state in item["by"].items() if state != "via"]
            if holders and not set(holders).intersection(t.visible):
                continue
            row = rows.get(item["id"])
            cells = row["cells"] if row else _by_cells(item, t.columns, t.found)
            entry = {
                "id": item["id"],
                "kind": item["kind"],
                "name": item["name"],
                "source": row["source"] if row else item["source"],
                "same_name": row["same_name"] if row else False,
                "lookup": (row["lookup"] if row else None) or item.get("plugin") or item["name"],
                "flags": row["flags"] if row else [],
                "count": item.get("count"),
                "cells": cells,
            }
            if item["kind"] == "plugin":
                entry["parts"] = _parts(item, cells, skills)
            items.append(entry)
        keys = ("key", "name", "icon", "kind")
        out.append({**{k: group[k] for k in keys}, "items": items})
    shown = {i["id"] for g in answer["groups"] for i in g["items"]}
    account = _account_rows(tables, shown, t, found_items)
    if account:
        other = next((g for g in out if g["key"] == OTHER["key"]), None)
        if other is None:
            other = {**OTHER, "items": []}
            at = sum(1 for g in out if g["kind"] == "marketplace")
            out.insert(at, other)
        other["items"].extend(account)
    return out


def _file_cell(file: dict, client: str, found: list[str]) -> dict:
    if file["by"].get(client):
        return _cell("on", word=READS_IT)
    if client not in found:
        return _cell("unk", reason=CLIENT_UNREAD.format(CLIENT_WORDS[client]))
    return _cell()


def _file_groups(answer: dict, t: _Tables) -> list[dict]:
    """The instruction files by where they apply, then the commands and agents, one cell per
    client: "Reads it", nothing, or Unknown for a client Kitden could not read. `category` is a
    command's or an agent's ("command", "agent"); `names` the items an Overview link selects it
    by."""
    out = []
    for group in answer["groups"]:
        items = []
        for file in group["files"]:
            if not set(file["by"]).intersection(t.visible) and set(file["by"]) & t.hidden:
                continue
            cells = {c: _file_cell(file, c, t.found) for c in t.columns}
            items.append(
                {
                    "id": file["id"],
                    "kind": "file",
                    "category": file.get("kind"),
                    "name": file["name"],
                    "names": file["names"],
                    "flags": [],
                    "cells": cells,
                }
            )
        icon = FILE_ICONS[group["key"]]
        out.append(
            {
                "key": group["key"],
                "name": group["name"],
                "icon": icon,
                "kind": "files",
                "items": items,
            }
        )
    return out


def _named(item: dict, q: str) -> bool:
    """The item's name, or one of its parts', holds the search text."""
    text = q.strip().lower()
    names = [item["name"], *(p["name"] for p in item.get("parts", []))]
    return any(text in name.lower() for name in names)


def _warns(item: dict) -> bool:
    """An item needs attention when it, or one of its parts, has a warning."""
    return _needs_attention(item) or any(_needs_attention(p) for p in item.get("parts", []))


def _item_differs(item: dict, visible: list[str]) -> bool:
    """An item differs when its cells do; a plugin also when one of its parts does."""
    return differs(item["cells"], visible) or any(
        differs(p["cells"], visible) for p in item.get("parts", [])
    )


def library_list(
    inventory,
    adapters,
    project: str | None,
    *,
    gateway_registered: bool,
    tab: str,
    show: str = "mine",
    q: str = "",
    hidden: frozenset[str] = frozenset(),
    attention: bool = False,
) -> dict:
    """The List (spec 5.4): one tab's groups, each with its items, one cell per client.

    "plugin": the Library map's groups (marketplaces, Other plugins, Your skills, Your settings,
    Only this project) with their plugins, skills and hooks, each with the cells of the plugin,
    skill and hook tables, and a plugin's parts. "instructions": the instruction files by where
    they apply. `show` "diff" keeps the items that differ (`differs`); `attention` keeps those
    with a warning, and `attention` in the answer counts them; `q` keeps an item whose name, or
    a part's, holds the text. A group with no item left is left out.
    """
    t = _tables(inventory, adapters, project, gateway_registered=gateway_registered, hidden=hidden)
    if tab == "instructions":
        groups = _file_groups(instructions_map(inventory, adapters, project), t)
        show = "mine"
    else:
        tables = {
            k: _finish(t, k, inventory, adapters, project) for k in ("plugin", "skill", "hook")
        }
        answer = library_map(inventory, adapters, project)
        groups = _item_groups(answer, tables, t, inventory.items)
    matched = []
    for group in groups:
        kept = [i for i in group["items"] if not q.strip() or _named(i, q)]
        if show == "diff":
            kept = [i for i in kept if _item_differs(i, t.visible)]
        matched.append({**group, "items": kept})
    warned = sum(_warns(i) for g in matched for i in g["items"])
    if attention:
        matched = [{**g, "items": [i for i in g["items"] if _warns(i)]} for g in matched]
    shown = [g for g in matched if g["items"]]
    cells = [r for g in shown for i in g["items"] for r in (i, *i.get("parts", []))]
    return {
        "columns": _columns(t),
        "groups": shown,
        "attention": warned,
        "notes": _read_notes(inventory, project, t.homes) + _notes(cells, t.visible),
    }
