"""Zero-dependency stdio JSON-RPC 2.0 MCP server for FJU SIS."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from typing import Any, TextIO

from fju_sis import __version__
from fju_sis.client import AuthError, SisClient, SisError

PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "sis_whoami",
        "description": "Check current FJU SIS login session and student basic info.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sis_grades",
        "description": "Fetch course grades from FJU SIS (all semesters or filtered by academic year/semester).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hy": {"type": "integer", "description": "Optional ROC academic year, e.g. 114"},
                "ht": {"type": "integer", "description": "Optional semester number, e.g. 1 or 2"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sis_ranks",
        "description": "Fetch semester averages and class/department rankings from FJU SIS.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sis_transcript_list",
        "description": "List all downloadable official digital semester transcripts from FJU SIS.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sis_transcript_download",
        "description": "Download official digital semester transcript PDF(s) from FJU SIS.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "all": {"type": "boolean", "description": "Download all available semesters (default true if hy/ht omitted)"},
                "hy": {"type": "integer", "description": "ROC academic year, e.g. 114"},
                "ht": {"type": "integer", "description": "Semester number, e.g. 1 or 2"},
                "ranking": {"type": "boolean", "description": "Include ranking on the PDF (default true)"},
                "out_dir": {"type": "string", "description": "Directory to save downloaded PDF file(s)"},
            },
            "additionalProperties": False,
        },
    },
]


def _call_tool(client: SisClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "sis_whoami":
            data: Any = client.whoami()
        elif name == "sis_grades":
            data = client.get_grades(hy=arguments.get("hy"), ht=arguments.get("ht"))
        elif name == "sis_ranks":
            data = client.get_semester_ranks()
        elif name == "sis_transcript_list":
            data = client.list_transcripts()
        elif name == "sis_transcript_download":
            out_dir = Path(arguments.get("out_dir") or (Path(tempfile.gettempdir()) / "fju-sis-transcripts"))
            ranking = bool(arguments.get("ranking", True))
            if arguments.get("hy") is not None and arguments.get("ht") is not None:
                data = {"files": [client.download_transcript(arguments["hy"], arguments["ht"], out_dir, ranking=ranking)]}
            else:
                data = {"files": client.download_all_transcripts(out_dir, ranking=ranking)}
        else:
            return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}]}
    except (AuthError, SisError, OSError) as exc:
        return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}


def handle_message(client: SisClient, msg: dict[str, Any]) -> dict[str, Any] | None:
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fju-sis-mcp", "version": __version__},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        result = _call_tool(client, str(params.get("name") or ""), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve_stdio(
    client: SisClient | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    sis = client or SisClient()
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    for raw in inp:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()
            continue
        reply = handle_message(sis, msg)
        if reply is not None:
            out.write(json.dumps(reply, ensure_ascii=False) + "\n")
            out.flush()
