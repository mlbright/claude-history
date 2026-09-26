#!/usr/bin/env python3
"""Index Claude Code transcripts into SQLite and full-text search them.

Claude Code writes every session to ~/.claude/projects/<project>/<session>.jsonl
(subagents to .../<session>/subagents/agent-*.jsonl) and deletes them after
`cleanupPeriodDays`. This tool copies the conversational content (prompts,
assistant replies, tool calls and tool results) into an SQLite database with an
FTS5 index. Rows are never removed when a transcript disappears, so the
database doubles as a long-term archive.

Indexing is incremental: each file's byte offset is remembered and only newly
appended lines are read on the next run. Every command refreshes the index
first unless --no-refresh is given.

Stdlib only; needs Python 3.9+ and an SQLite build with FTS5.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence

DEFAULT_DB = Path(os.environ.get("CLAUDE_HISTORY_DB", Path(__file__).resolve().parent / "history.db"))
DEFAULT_PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"

KINDS = ("prompt", "text", "thinking", "tool_use", "tool_result", "summary")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path   TEXT PRIMARY KEY,
    offset INTEGER NOT NULL  -- bytes already indexed, always at a line boundary
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    project_dir TEXT,        -- directory name under ~/.claude/projects
    cwd         TEXT,
    git_branch  TEXT,
    title       TEXT,        -- latest AI-generated session title
    first_ts    TEXT,
    last_ts     TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    uuid        TEXT NOT NULL,     -- transcript record uuid
    block       INTEGER NOT NULL,  -- index of the content block within the record
    session_id  TEXT NOT NULL,
    agent_id    TEXT,              -- set for subagent (sidechain) messages
    ts          TEXT,              -- ISO-8601 UTC
    role        TEXT NOT NULL,     -- user | assistant
    kind        TEXT NOT NULL,     -- prompt | text | thinking | tool_use | tool_result | summary
    tool        TEXT,              -- tool name for tool_use / tool_result
    tool_use_id TEXT,
    is_meta     INTEGER NOT NULL DEFAULT 0,  -- injected by Claude Code rather than typed
    cwd         TEXT,
    git_branch  TEXT,
    text        TEXT NOT NULL,
    UNIQUE (uuid, block)
);
CREATE INDEX IF NOT EXISTS messages_session_ts ON messages (session_id, ts);
CREATE INDEX IF NOT EXISTS messages_tool_use_id ON messages (tool_use_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5 (
    text, content='messages', content_rowid='id', tokenize='porter unicode61'
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    new = not db_path.exists()
    conn = sqlite3.connect(db_path)
    if new:
        # Transcripts hold file contents and command output; keep the archive private.
        os.chmod(db_path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


# ----------------------------------------------------------------------- parse


def render_tool_input(inp: object) -> str:
    if not isinstance(inp, dict):
        return json.dumps(inp, ensure_ascii=False)
    return "\n".join(f"{k}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}" for k, v in inp.items())


def render_tool_result(content: object) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for item in content or []:
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
        else:
            parts.append(f"[{item.get('type')}]")
    return "\n".join(parts)


def extract_blocks(rec: dict) -> Iterator[dict]:
    """Yield one searchable row per content block of a user/assistant record."""
    role = rec["type"]
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    for i, b in enumerate(content or []):
        btype = b.get("type")
        row = {"block": i, "tool": None, "tool_use_id": None}
        if btype == "text":
            if role == "user":
                row["kind"] = "summary" if rec.get("isCompactSummary") else "prompt"
            else:
                row["kind"] = "text"
            row["text"] = b.get("text", "")
        elif btype == "thinking":
            # Current transcripts store thinking redacted (empty); index it if that ever changes.
            row["kind"], row["text"] = "thinking", b.get("thinking", "")
        elif btype == "tool_use":
            row.update(kind="tool_use", tool=b.get("name"), tool_use_id=b.get("id"))
            row["text"] = render_tool_input(b.get("input"))
        elif btype == "tool_result":
            row.update(kind="tool_result", tool_use_id=b.get("tool_use_id"))
            row["text"] = render_tool_result(b.get("content"))
        else:
            continue  # images, documents, etc.
        if row["text"].strip():
            yield row


# ----------------------------------------------------------------------- index


def iter_transcripts(projects_dir: Path) -> Iterator[Path]:
    for root, _dirs, files in os.walk(projects_dir):
        for name in files:
            if name.endswith(".jsonl"):
                yield Path(root) / name


def lookup_tool(conn: sqlite3.Connection, tool_use_id: Optional[str]) -> Optional[str]:
    if not tool_use_id:
        return None
    row = conn.execute(
        "SELECT tool FROM messages WHERE tool_use_id = ? AND kind = 'tool_use' LIMIT 1", (tool_use_id,)
    ).fetchone()
    return row["tool"] if row else None


def index_file(conn: sqlite3.Connection, path: Path, projects_dir: Path) -> int:
    """Index lines appended to `path` since the last run. Returns rows added."""
    row = conn.execute("SELECT offset FROM files WHERE path = ?", (str(path),)).fetchone()
    offset = row["offset"] if row else 0
    if path.stat().st_size < offset:  # truncated or replaced: rescan; UNIQUE(uuid, block) drops duplicates
        offset = 0
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1  # leave a partially written last line for next time
    if end == 0:
        return 0

    project_dir = path.relative_to(projects_dir).parts[0]
    file_agent_id = path.stem.removeprefix("agent-") if path.parent.name == "subagents" else None
    tool_names: dict[str, str] = {}
    added = 0
    with conn:
        for line in data[:end].splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rtype, sid = rec.get("type"), rec.get("sessionId")
            if not sid:
                continue
            if rtype == "ai-title" and rec.get("aiTitle"):
                conn.execute(
                    "INSERT INTO sessions (session_id, project_dir, title) VALUES (?, ?, ?) "
                    "ON CONFLICT (session_id) DO UPDATE SET title = excluded.title",
                    (sid, project_dir, rec["aiTitle"]),
                )
                continue
            if rtype not in ("user", "assistant") or not rec.get("uuid"):
                continue
            ts, cwd, branch = rec.get("timestamp"), rec.get("cwd"), rec.get("gitBranch")
            conn.execute(
                """INSERT INTO sessions (session_id, project_dir, cwd, git_branch, first_ts, last_ts)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (session_id) DO UPDATE SET
                     cwd = COALESCE(sessions.cwd, excluded.cwd),
                     git_branch = COALESCE(excluded.git_branch, sessions.git_branch),
                     first_ts = MIN(COALESCE(sessions.first_ts, excluded.first_ts),
                                    COALESCE(excluded.first_ts, sessions.first_ts)),
                     last_ts = MAX(COALESCE(sessions.last_ts, excluded.last_ts),
                                   COALESCE(excluded.last_ts, sessions.last_ts))""",
                (sid, project_dir, cwd, branch, ts, ts),
            )
            for b in extract_blocks(rec):
                if b["kind"] == "tool_use":
                    tool_names[b["tool_use_id"]] = b["tool"]
                elif b["kind"] == "tool_result":
                    b["tool"] = tool_names.get(b["tool_use_id"]) or lookup_tool(conn, b["tool_use_id"])
                cur = conn.execute(
                    """INSERT OR IGNORE INTO messages
                       (uuid, block, session_id, agent_id, ts, role, kind, tool, tool_use_id,
                        is_meta, cwd, git_branch, text)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        rec["uuid"], b["block"], sid, rec.get("agentId") or file_agent_id, ts,
                        rtype, b["kind"], b["tool"], b["tool_use_id"], int(bool(rec.get("isMeta"))),
                        cwd, branch, b["text"],
                    ),
                )
                if cur.rowcount:
                    conn.execute("INSERT INTO messages_fts (rowid, text) VALUES (?, ?)", (cur.lastrowid, b["text"]))
                    added += 1
        conn.execute(
            "INSERT INTO files (path, offset) VALUES (?, ?) ON CONFLICT (path) DO UPDATE SET offset = excluded.offset",
            (str(path), offset + end),
        )
    return added


def refresh(conn: sqlite3.Connection, projects_dir: Path, verbose: bool = False) -> int:
    if not projects_dir.is_dir():
        sys.exit(f"transcript directory not found: {projects_dir}")
    total = 0
    # Parents before subagents so a subagent's tool results can resolve names indexed earlier.
    for path in sorted(iter_transcripts(projects_dir), key=lambda p: (len(p.parts), str(p))):
        try:
            n = index_file(conn, path, projects_dir)
        except OSError as err:  # e.g. deleted by Claude Code's cleanup mid-scan
            print(f"skipping {path}: {err}", file=sys.stderr)
            continue
        if n and verbose:
            print(f"{n:6d}  {path}")
        total += n
    return total


# ---------------------------------------------------------------------- search


def quote_terms(query: str) -> str:
    """Quote every term so punctuation like `vxlan.py` or `a:b` is matched literally."""
    return " ".join('"' + t.replace('"', '""') + '"' for t in query.split())


def utc_bound(value: str) -> str:
    """YYYY-MM-DD or ISO timestamp (local time unless it has an offset) -> ISO UTC prefix."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    kinds: Optional[Sequence[str]] = None,
    project: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    include_meta: bool = False,
    recent: bool = False,
    literal: bool = False,
    limit: int = 20,
    highlight: tuple[str, str] = ("«", "»"),
) -> list[sqlite3.Row]:
    where, params = ["messages_fts MATCH ?"], []
    if kinds:
        where.append(f"m.kind IN ({', '.join('?' * len(kinds))})")
        params += kinds
    if project:
        where.append("(m.cwd LIKE ? OR s.project_dir LIKE ?)")
        params += [f"%{project}%"] * 2
    if since:
        where.append("m.ts >= ?")
        params.append(utc_bound(since))
    if until:
        where.append("m.ts < ?")
        params.append(utc_bound(until))
    if not include_meta:
        where.append("m.is_meta = 0")
    sql = f"""
        SELECT m.id, m.session_id, m.agent_id, m.ts, m.role, m.kind, m.tool, m.cwd, s.title,
               snippet(messages_fts, 0, ?, ?, '…', 24) AS snip
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.rowid
        LEFT JOIN sessions s ON s.session_id = m.session_id
        WHERE {' AND '.join(where)}
        ORDER BY {'m.ts DESC' if recent else 'bm25(messages_fts)'}
        LIMIT ?"""

    def run(q: str) -> list[sqlite3.Row]:
        return conn.execute(sql, [*highlight, q, *params, limit]).fetchall()

    if literal:
        return run(quote_terms(query))
    try:
        return run(query)
    except sqlite3.OperationalError:
        # Not valid FTS5 syntax (e.g. `net_config/vxlan.py`, `ip:port`): match the words literally.
        return run(quote_terms(query))


# ---------------------------------------------------------------------- output


def local_time(ts: Optional[str]) -> str:
    if not ts:
        return "?"
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")


def short_path(p: Optional[str]) -> str:
    if not p:
        return "?"
    home = str(Path.home())
    return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p


def label(row: sqlite3.Row) -> str:
    text = f"{row['role']} {row['kind']}"
    if row["tool"]:
        text += f" ({row['tool']})"
    if row["agent_id"]:
        text += " [subagent]"
    return text


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def cmd_index(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    added = refresh(conn, args.projects_dir, verbose=args.verbose)
    msgs, sessions = conn.execute(
        "SELECT (SELECT COUNT(*) FROM messages), (SELECT COUNT(*) FROM sessions)"
    ).fetchone()
    print(f"added {added} messages; {args.db} holds {msgs} messages from {sessions} sessions")


def cmd_search(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    tty = sys.stdout.isatty()
    hl = ("\033[1;33m", "\033[0m") if tty else ("«", "»")
    dim, reset = ("\033[2m", "\033[0m") if tty else ("", "")
    rows = search(
        conn,
        " ".join(args.query),
        kinds=args.kind,
        project=args.project,
        since=args.since,
        until=args.until,
        include_meta=args.include_meta,
        recent=args.recent,
        literal=args.literal,
        limit=args.limit,
        highlight=hl,
    )
    if not rows:
        print("no matches", file=sys.stderr)
        sys.exit(1)
    for r in rows:
        print(f"{local_time(r['ts'])}  {short_path(r['cwd'])}  {r['title'] or ''}")
        print(f"  {dim}{label(r)} · {r['session_id'][:8]}{reset}")
        print(f"  {one_line(r['snip'])}\n")


def cmd_show(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    matches = conn.execute(
        "SELECT * FROM sessions WHERE session_id LIKE ? ORDER BY last_ts DESC", (args.session + "%",)
    ).fetchall()
    if len(matches) != 1:
        sys.exit(f"{len(matches)} sessions match {args.session!r}; give more of the id")
    s = matches[0]
    print(f"# {s['title'] or '(untitled)'}")
    print(f"session {s['session_id']}  {short_path(s['cwd'])}  branch {s['git_branch'] or '?'}")
    print(f"{local_time(s['first_ts'])} → {local_time(s['last_ts'])}")
    print(f"resume: cd {short_path(s['cwd'])} && claude --resume {s['session_id']}\n")

    kinds = ["prompt", "text", "summary"] + (["tool_use", "tool_result"] if args.tools or args.full else [])
    where = [f"kind IN ({', '.join('?' * len(kinds))})"]
    if not args.full:
        where += ["is_meta = 0", "agent_id IS NULL"]
    rows = conn.execute(
        f"SELECT * FROM messages WHERE session_id = ? AND {' AND '.join(where)} ORDER BY ts, id",
        (s["session_id"], *kinds),
    )
    for r in rows:
        text = r["text"]
        if r["kind"] in ("tool_use", "tool_result") and not args.full:
            lines = text.splitlines()
            text = "\n".join(lines[: args.max_lines]) + (f"\n… ({len(lines) - args.max_lines} more lines)"
                                                          if len(lines) > args.max_lines else "")
        print(f"── {label(r)}  {local_time(r['ts'])}")
        print(text.rstrip() + "\n")


def cmd_sessions(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    params: list = []
    where = ""
    if args.project:
        where = "WHERE s.cwd LIKE ? OR s.project_dir LIKE ?"
        params += [f"%{args.project}%"] * 2
    rows = conn.execute(
        f"""SELECT s.*, COUNT(m.id) AS n FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.session_id AND m.kind IN ('prompt', 'text')
            {where} GROUP BY s.session_id ORDER BY s.last_ts DESC LIMIT ?""",
        (*params, args.limit),
    )
    for r in rows:
        print(f"{local_time(r['last_ts'])}  {r['session_id'][:8]}  {r['n']:4d} msgs  "
              f"{short_path(r['cwd']):40.40}  {r['title'] or ''}")


# ------------------------------------------------------------------------ main


def main(argv: Optional[Sequence[str]] = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"database file (default: {DEFAULT_DB})")
    common.add_argument("--projects-dir", type=Path, default=DEFAULT_PROJECTS,
                        help=f"Claude Code transcript directory (default: {DEFAULT_PROJECTS})")
    common.add_argument("--no-refresh", action="store_true", help="don't index new transcript lines first")

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", parents=[common], help="index new transcript lines")
    p.add_argument("-v", "--verbose", action="store_true", help="list files that added rows")
    p.set_defaults(func=cmd_index, no_refresh=True)

    p = sub.add_parser("search", parents=[common], help="full-text search",
                       description="Full-text search. QUERY uses SQLite FTS5 syntax (AND, OR, NOT, "
                       '"exact phrase", prefix*, NEAR(a b)); anything that isn\'t valid FTS5 is matched '
                       "as plain words. Matching is case-insensitive and stems English words.")
    p.add_argument("query", nargs="+")
    p.add_argument("-k", "--kind", action="append", choices=KINDS,
                   help="only these message kinds (repeatable); default all")
    p.add_argument("-p", "--project", help="substring of the working directory")
    p.add_argument("--since", help="YYYY-MM-DD or ISO time, inclusive (local time)")
    p.add_argument("--until", help="YYYY-MM-DD or ISO time, exclusive (local time)")
    p.add_argument("-r", "--recent", action="store_true", help="newest first instead of best match first")
    p.add_argument("-l", "--literal", action="store_true", help="treat QUERY as plain words, not FTS5 syntax")
    p.add_argument("--include-meta", action="store_true", help="include text Claude Code injected (skills, hooks)")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", parents=[common], help="print one session's conversation")
    p.add_argument("session", help="session id or unique prefix")
    p.add_argument("-t", "--tools", action="store_true", help="include tool calls and (truncated) results")
    p.add_argument("-f", "--full", action="store_true",
                   help="everything, untruncated, including subagent and injected messages")
    p.add_argument("--max-lines", type=int, default=12, help="lines per tool call/result with --tools")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("sessions", parents=[common], help="list sessions, most recent first")
    p.add_argument("-p", "--project", help="substring of the working directory")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.set_defaults(func=cmd_sessions)

    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        if not args.no_refresh:
            refresh(conn, args.projects_dir)
        args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:  # e.g. piped into `head`
        sys.stderr.close()
