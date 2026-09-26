# claude-history

Full-text search over your Claude Code conversations.

Claude Code saves each session as JSONL under `~/.claude/projects/` and deletes
transcripts older than `cleanupPeriodDays` (default 30). `claude_history.py`
copies the conversational content into an SQLite database with an FTS5 index.
Rows are never deleted when a transcript goes away, so the database is also an
archive that outlives Claude Code's cleanup.

Only the Python standard library is needed (Python 3.9+, SQLite with FTS5).

## Install

```sh
ln -s ~/claude-history/claude_history.py ~/.local/bin/claude-history
```

The database location follows the real script path, so the symlink still uses
`~/claude-history/history.db`.

## Usage

```sh
claude-history index                      # index new transcript lines (incremental, fast)
claude-history search vxlan df bit        # best matches first
claude-history search -r -p cvuv "config restore"          # newest first, sessions whose directory contains "cvuv"
claude-history search -k prompt --since 2026-09-01 terraform  # only things you typed
claude-history search -k tool_result 'NEAR(traceback error, 10)'  # FTS5 syntax works too
claude-history sessions -n 10             # recent sessions with titles
claude-history show 184f025f              # read a session (id prefix is enough)
claude-history show 184f025f --tools      # ...including tool calls and results
```

`search`, `show` and `sessions` index anything new before running, so the
results are always current; pass `--no-refresh` to skip that.

Queries use [FTS5 syntax](https://www.sqlite.org/fts5.html#full_text_query_syntax):
`AND`/`OR`/`NOT`, `"exact phrase"`, `prefix*`, `NEAR(a b)`. Matching is
case-insensitive and stems English words (`interfaces` matches `interface`).
Anything that isn't valid FTS5, such as `net_config/vxlan.py` or `host:port`,
is searched as plain words; `--literal` forces that.

By default the database is `history.db` next to the script. Override it with
`--db` or `CLAUDE_HISTORY_DB`. The transcript directory follows
`CLAUDE_CONFIG_DIR`, or pass `--projects-dir`.

## Keeping the archive complete

Transcripts are only preserved once they've been indexed. A `SessionEnd` hook
in `~/.claude/settings.json` indexes every session as it closes, so nothing is
lost even if you don't search for longer than `cleanupPeriodDays`:

```json
"hooks": {
  "SessionEnd": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "\"$HOME/claude-history/claude_history.py\" index >/dev/null 2>>\"$HOME/claude-history/hook-errors.log\" || true",
          "timeout": 60
        }
      ]
    }
  ]
}
```

Failures are appended to `hook-errors.log`; an empty file means every run succeeded.

## What gets indexed

One row per content block of each `user`/`assistant` record:

| kind          | source                                                     |
|---------------|------------------------------------------------------------|
| `prompt`      | text you sent (and text Claude Code injected, flagged `is_meta`) |
| `text`        | Claude's replies                                           |
| `tool_use`    | tool calls, with the tool name and its input               |
| `tool_result` | tool output, tagged with the tool name                     |
| `summary`     | compaction summaries                                       |
| `thinking`    | thinking text (current transcripts store it redacted, so this is usually empty and skipped) |

Subagent transcripts are attached to their parent session with `agent_id` set.
Images and bookkeeping records (file snapshots, mode changes, etc.) are skipped.
Text injected by Claude Code (skill bodies, hook output) is hidden from search
unless you pass `--include-meta`.

## Querying with sqlite3

The schema is at the top of `claude_history.py`. `messages_fts` is an
external-content FTS5 table over `messages.text`.

```sql
-- tool usage by project
SELECT cwd, tool, COUNT(*) FROM messages WHERE kind = 'tool_use' GROUP BY 1, 2 ORDER BY 3 DESC;

-- every Bash command that mentioned terraform
SELECT m.ts, m.text FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
WHERE messages_fts MATCH 'terraform' AND m.tool = 'Bash' AND m.kind = 'tool_use';
```

## Privacy

Transcripts contain everything that passed through a session: file contents,
command output and anything secret among them. The database is created
mode 0600 and is git-ignored; keep it local.

## Tests

```sh
python3 -m unittest discover -s tests
```
