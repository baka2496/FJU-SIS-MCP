"""Command-line interface for FJU Student Information Platform (fjusis)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import sys
from typing import Any

from fju_sis.auth import SessionStore, session_from_tokens
from fju_sis.client import AuthError, SisClient, SisError


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _fmt_exp(exp: int) -> str:
    if not exp:
        return "未知"
    return datetime.fromtimestamp(exp, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def cmd_login(args: argparse.Namespace, client: SisClient) -> int:
    if args.token:
        auth_token = getpass.getpass("auth_token（不顯示）: ").strip()
        if not auth_token:
            raise AuthError("未輸入 auth_token。")
        refresh_token = getpass.getpass("refresh_token（選填，不顯示，直接按 Enter 略過）: ").strip()
        session = session_from_tokens(auth_token, refresh_token)
        if not session.emp_no:
            session.emp_no = input("學號: ").strip()
        client.store.save(session)
        print(f"已將登入狀態存入系統認證管理員（學號：{session.emp_no or '未提供'}，到期：{_fmt_exp(session.exp)}）。")
        return 0
    emp_no = (args.emp_no or input("學號（LDAP 帳號）: ")).strip()
    password = getpass.getpass("LDAP 密碼（不顯示）: ")
    session = client.ldap_login(emp_no, password)
    print(f"登入成功！已將權杖存入系統認證管理員（不保存密碼）。學號：{session.emp_no}，到期：{_fmt_exp(session.exp)}")
    return 0


def cmd_logout(args: argparse.Namespace, client: SisClient) -> int:
    removed = client.store.delete()
    print("已清除本機登入狀態。" if removed else "本機原本就沒有登入狀態。")
    return 0


def cmd_whoami(args: argparse.Namespace, client: SisClient) -> int:
    info = client.whoami()
    if args.json:
        _print_json(info)
        return 0
    print(f"學號：{info.get('emp_no') or '未知'}")
    if info.get("name"):
        print(f"姓名：{info['name']}")
    print(f"權杖到期：{_fmt_exp(int(info.get('exp') or 0))}")
    return 0


def cmd_grades(args: argparse.Namespace, client: SisClient) -> int:
    grades = client.get_grades(hy=args.hy, ht=args.ht)
    if args.json:
        _print_json(grades)
        return 0
    if not grades:
        print("查無成績資料。")
        return 0
    for item in grades:
        hy = item.get("hy", "")
        ht = item.get("htPeriod", "")
        name = item.get("couCNa", "未命名課程")
        credit = item.get("credit", "")
        score = item.get("scoreDisplay", "未評定")
        req = item.get("reqSelCNa", "")
        print(f"{hy}-{ht}｜{name}（{req} {credit}學分）：{score}")
    return 0


def cmd_ranks(args: argparse.Namespace, client: SisClient) -> int:
    ranks = client.get_semester_ranks()
    if args.json:
        _print_json(ranks)
        return 0
    if not ranks:
        print("查無學期排名資料。")
        return 0
    for item in ranks:
        aca = item.get("aca", "")
        avg = item.get("avgSco", "")
        cls = item.get("class", "")
        ser = item.get("series", "")
        print(f"{aca}｜平均 {avg}｜班排 {cls}｜系排 {ser}")
    return 0


def cmd_transcript_list(args: argparse.Namespace, client: SisClient) -> int:
    records = client.list_transcripts()
    if args.json:
        _print_json(records)
        return 0
    if not records:
        print("目前沒有可下載的數位成績單。")
        return 0
    for rec in records:
        hy = rec.get("hy", "")
        ht = rec.get("ht", "")
        desc = rec.get("hyHtDesc") or f"{hy}-{ht}"
        doc = rec.get("docTypeDesc") or "數位成績單"
        note = rec.get("paymentNote") or ""
        print(f"{hy}-{ht}｜{desc}｜{doc}" + (f"（{note}）" if note else ""))
    return 0


def cmd_transcript_download(args: argparse.Namespace, client: SisClient) -> int:
    out_dir = Path(args.out).resolve()
    ranking = not args.no_ranking
    if args.all:
        files = client.download_all_transcripts(out_dir, ranking=ranking)
    else:
        if args.hy is None or args.ht is None:
            raise SisError("請指定 --all 下載全部學期，或同時指定 --hy <學年> 與 --ht <學期>。")
        files = [client.download_transcript(args.hy, args.ht, out_dir, ranking=ranking)]
    if args.json:
        _print_json({"files": files})
        return 0
    for f in files:
        kb = round(f["size"] / 1024)
        print(f"已下載：{f['path']}（{kb} KB）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fjusis",
        description="輔仁大學學生資訊平台（sis.fju.edu.tw）CLI 與 MCP 工具",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="登入並將權杖存入系統認證管理員（不存密碼）")
    p_login.add_argument("--emp-no", help="學號（不填則互動輸入）")
    p_login.add_argument("--token", action="store_true", help="改用貼上 auth_token / refresh_token 方式登入")

    sub.add_parser("logout", help="清除本機系統認證管理員內的登入狀態")

    p_who = sub.add_parser("whoami", help="檢查目前登入狀態")
    p_who.add_argument("--json", action="store_true")

    p_grades = sub.add_parser("grades", help="查詢歷年或指定學期各科成績")
    p_grades.add_argument("--hy", type=int, help="學年度（例如 114）")
    p_grades.add_argument("--ht", type=int, help="學期（例如 1 或 2）")
    p_grades.add_argument("--json", action="store_true")

    p_ranks = sub.add_parser("ranks", help="查詢各學期平均與排名")
    p_ranks.add_argument("--json", action="store_true")

    p_tr = sub.add_parser("transcript", help="查詢與下載官方數位成績單 PDF")
    tr_sub = p_tr.add_subparsers(dest="transcript_cmd", required=True)

    p_tr_list = tr_sub.add_parser("list", help="列出所有可下載的數位成績單")
    p_tr_list.add_argument("--json", action="store_true")

    p_tr_dl = tr_sub.add_parser("download", help="下載數位成績單 PDF")
    p_tr_dl.add_argument("--all", action="store_true", help="下載所有可下載學期的數位成績單")
    p_tr_dl.add_argument("--hy", type=int, help="指定學年度（例如 114）")
    p_tr_dl.add_argument("--ht", type=int, help="指定學期（例如 1 或 2）")
    p_tr_dl.add_argument("--no-ranking", action="store_true", help="下載不含排名版本（預設含排名）")
    p_tr_dl.add_argument("--out", default=".", help="輸出資料夾（預設目前目錄）")
    p_tr_dl.add_argument("--json", action="store_true")

    sub.add_parser("serve", help="以 stdio 啟動 MCP Server")
    return parser


def main(argv: list[str] | None = None, *, client: SisClient | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        from fju_sis.mcp_server import serve_stdio

        serve_stdio(client=client or SisClient(store=SessionStore()))
        return 0
    sis = client or SisClient(store=SessionStore())
    try:
        if args.command == "login":
            return cmd_login(args, sis)
        if args.command == "logout":
            return cmd_logout(args, sis)
        if args.command == "whoami":
            return cmd_whoami(args, sis)
        if args.command == "grades":
            return cmd_grades(args, sis)
        if args.command == "ranks":
            return cmd_ranks(args, sis)
        if args.command == "transcript":
            if args.transcript_cmd == "list":
                return cmd_transcript_list(args, sis)
            if args.transcript_cmd == "download":
                return cmd_transcript_download(args, sis)
        parser.error("未知的指令")
        return 2
    except (AuthError, SisError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
