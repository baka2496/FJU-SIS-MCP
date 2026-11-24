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
    {
        "name": "sis_enrollment_list",
        "description": "List student enrollment history and downloadable digital enrollment certificate semesters.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sis_enrollment_download",
        "description": "Download official digital enrollment certificate PDF (Chinese or English).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hy": {"type": "integer", "description": "Optional ROC academic year (defaults to current semester)"},
                "ht": {"type": "integer", "description": "Optional semester number (defaults to current semester)"},
                "lang": {"type": "string", "enum": ["zh", "en"], "description": "Language version (default zh)"},
                "out_dir": {"type": "string", "description": "Directory to save downloaded PDF"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sis_registration",
        "description": "Check current semester registration status and any pending checklist items.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sis_courses_search",
        "description": "Search FJU offered courses or general education courses.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hy": {"type": "integer"},
                "ht": {"type": "integer"},
                "keyword": {"type": "string", "description": "Course title keyword"},
                "teacher": {"type": "string", "description": "Instructor name keyword"},
                "general_edu": {"type": "boolean", "description": "Search general education courses"},
                "page": {"type": "integer"},
                "page_size": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sis_leave_list",
        "description": "List student leave applications for a semester.",
        "inputSchema": {
            "type": "object",
            "properties": {"hy": {"type": "integer"}, "ht": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "sis_leave_stat",
        "description": "Get student leave section statistics by course for a semester.",
        "inputSchema": {
            "type": "object",
            "properties": {"hy": {"type": "integer"}, "ht": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "sis_leave_draft",
        "description": "Create a student leave request DRAFT (never confirms or submits automatically).",
        "inputSchema": {
            "type": "object",
            "required": ["begin_date", "end_date", "begin_section", "end_section"],
            "properties": {
                "hy": {"type": "integer"},
                "ht": {"type": "integer"},
                "leave_type": {"type": "string", "description": "Leave type name or code, e.g. 病假, 事假"},
                "begin_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "begin_section": {"type": "string", "description": "Start section number/code, e.g. 1 or D1"},
                "end_section": {"type": "string", "description": "End section number/code, e.g. 2 or D2"},
                "reason": {"type": "string"},
                "attachments": {"type": "array", "items": {"type": "string"}},
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
        elif name == "sis_enrollment_list":
            data = client.list_enrollment_certs()
        elif name == "sis_enrollment_download":
            out_dir = Path(arguments.get("out_dir") or (Path(tempfile.gettempdir()) / "fju-sis-enrollment"))
            data = client.download_enrollment_cert(
                out_dir, hy=arguments.get("hy"), ht=arguments.get("ht"), lang=str(arguments.get("lang") or "zh")
            )
        elif name == "sis_registration":
            data = client.get_registration_status()
        elif name == "sis_courses_search":
            data = client.search_courses(
                hy=arguments.get("hy"),
                ht=arguments.get("ht"),
                keyword=arguments.get("keyword"),
                teacher=arguments.get("teacher"),
                general_edu=bool(arguments.get("general_edu", False)),
                page=int(arguments.get("page") or 1),
                page_size=int(arguments.get("page_size") or 20),
            )
        elif name == "sis_leave_list":
            data = client.list_leaves(hy=arguments.get("hy"), ht=arguments.get("ht"))
        elif name == "sis_leave_stat":
            data = client.leave_stats(hy=arguments.get("hy"), ht=arguments.get("ht"))
        elif name == "sis_leave_draft":
            data = client.create_leave_draft(
                begin_date=str(arguments["begin_date"]),
                end_date=str(arguments["end_date"]),
                begin_section=arguments["begin_section"],
                end_section=arguments["end_section"],
                leave_type=arguments.get("leave_type") or "病假",
                reason=str(arguments.get("reason") or ""),
                hy=arguments.get("hy"),
                ht=arguments.get("ht"),
                attachments=arguments.get("attachments") or [],
            )
        else:
            return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}]}
    except (AuthError, SisError, OSError, KeyError) as exc:
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
