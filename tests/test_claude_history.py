import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_history as ch  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


def rec(uuid, rtype, content, **extra):
    return {
        "type": rtype,
        "uuid": uuid,
        "sessionId": SID,
        "timestamp": extra.pop("ts", "2026-09-26T12:00:00.000Z"),
        "cwd": "/home/u/proj",
        "gitBranch": "main",
        "message": {"role": rtype, "content": content},
        **extra,
    }


SESSION = [
    {"type": "ai-title", "aiTitle": "VXLAN DF bit", "sessionId": SID},
    rec("u1", "user", "Do the VXLAN interfaces have their DF bit set?"),
    rec("a1", "assistant", [{"type": "text", "text": "They are created in net_config/vxlan.py without df."}]),
    rec("a2", "assistant", [{"type": "tool_use", "id": "T1", "name": "Bash", "input": {"command": "ip -d link"}}]),
    rec("u2", "user", [{"type": "tool_result", "tool_use_id": "T1", "content": "vxlan id 1337 dstport 4789"}]),
    rec("u3", "user", "Base directory for this skill: frobnicate", isMeta=True),
    rec("a3", "assistant", [{"type": "thinking", "thinking": "", "signature": "x"}]),
]
SUBAGENT = [rec("s1", "assistant", [{"type": "text", "text": "subagent found quuxfile"}], isSidechain=True)]


def write_jsonl(path, records, trailer=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        for r in records:
            f.write(json.dumps(r).encode() + b"\n")
        f.write(trailer)


class ClaudeHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects = root / "projects"
        self.main = self.projects / "-home-u-proj" / f"{SID}.jsonl"
        self.sub = self.projects / "-home-u-proj" / SID / "subagents" / "agent-abc123.jsonl"
        write_jsonl(self.main, SESSION)
        write_jsonl(self.sub, SUBAGENT)
        self.conn = ch.connect(root / "history.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def kinds(self):
        return [tuple(r) for r in self.conn.execute("SELECT uuid, kind, tool FROM messages ORDER BY id")]

    def test_indexes_each_block_kind_and_skips_empty_thinking(self):
        self.assertEqual(ch.refresh(self.conn, self.projects), 6)
        self.assertEqual(
            self.kinds(),
            [
                ("u1", "prompt", None),
                ("a1", "text", None),
                ("a2", "tool_use", "Bash"),
                ("u2", "tool_result", "Bash"),
                ("u3", "prompt", None),
                ("s1", "text", None),
            ],
        )
        s = self.conn.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual((s["title"], s["cwd"], s["git_branch"]), ("VXLAN DF bit", "/home/u/proj", "main"))

    def test_search_stems_and_highlights(self):
        ch.refresh(self.conn, self.projects)
        rows = ch.search(self.conn, "interface df")
        self.assertEqual(len(rows), 1)
        self.assertIn("«interfaces»", rows[0]["snip"])
        self.assertEqual(rows[0]["title"], "VXLAN DF bit")

    def test_search_falls_back_to_literal_terms_for_non_fts_syntax(self):
        ch.refresh(self.conn, self.projects)
        self.assertEqual([r["kind"] for r in ch.search(self.conn, "net_config/vxlan.py")], ["text"])
        self.assertEqual(len(ch.search(self.conn, "dstport:4789")), 1)

    def test_search_filters(self):
        ch.refresh(self.conn, self.projects)
        self.assertEqual([r["tool"] for r in ch.search(self.conn, "vxlan", kinds=["tool_result"])], ["Bash"])
        self.assertEqual(ch.search(self.conn, "vxlan", project="elsewhere"), [])
        self.assertEqual(ch.search(self.conn, "vxlan", since="2026-09-27"), [])
        self.assertEqual(ch.search(self.conn, "frobnicate"), [])
        self.assertEqual(len(ch.search(self.conn, "frobnicate", include_meta=True)), 1)

    def test_subagent_rows_carry_agent_id_and_parent_session(self):
        ch.refresh(self.conn, self.projects)
        row = ch.search(self.conn, "quuxfile")[0]
        self.assertEqual((row["agent_id"], row["session_id"]), ("abc123", SID))

    def test_incremental_waits_for_complete_line_and_is_idempotent(self):
        ch.refresh(self.conn, self.projects)
        line = json.dumps(rec("u9", "user", "late addition zebra")).encode()
        write_jsonl(self.main, SESSION, trailer=line[:20])
        self.assertEqual(ch.refresh(self.conn, self.projects), 0)
        write_jsonl(self.main, SESSION, trailer=line + b"\n")
        self.assertEqual(ch.refresh(self.conn, self.projects), 1)
        self.assertEqual(ch.refresh(self.conn, self.projects), 0)
        self.assertEqual(len(ch.search(self.conn, "zebra")), 1)

    def test_rewritten_shorter_file_is_rescanned_without_duplicates(self):
        ch.refresh(self.conn, self.projects)
        write_jsonl(self.main, SESSION[:2] + [rec("u8", "user", "fresh walrus")])
        self.assertEqual(ch.refresh(self.conn, self.projects), 1)
        self.assertEqual(len(ch.search(self.conn, "vxlan interfaces")), 1)

    def test_rows_survive_transcript_deletion(self):
        ch.refresh(self.conn, self.projects)
        self.main.unlink()
        ch.refresh(self.conn, self.projects)
        self.assertEqual(len(ch.search(self.conn, "DF bit")), 1)


if __name__ == "__main__":
    unittest.main()
