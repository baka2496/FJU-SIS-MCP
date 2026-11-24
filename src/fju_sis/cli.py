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


def cmd_keepalive(args: argparse.Namespace, client: SisClient) -> int:
    session = client.keepalive()
    if args.json:
        _print_json({"ok": True, "emp_no": session.emp_no, "exp": session.exp})
        return 0
    if not args.quiet:
        print(f"已續期！學號：{session.emp_no}，新到期：{_fmt_exp(session.exp)}")
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
        print(f"{item.get('aca', '')}｜平均 {item.get('avgSco', '')}｜班排 {item.get('class', '')}｜系排 {item.get('series', '')}")
    return 0


def cmd_transcript(args: argparse.Namespace, client: SisClient) -> int:
    if args.transcript_cmd == "list":
        records = client.list_transcripts()
        if args.json:
            _print_json(records)
            return 0
        if not records:
            print("目前沒有可下載的數位成績單。")
            return 0
        for rec in records:
            note = rec.get("paymentNote") or ""
            print(f"{rec.get('hy', '')}-{rec.get('ht', '')}｜{rec.get('hyHtDesc') or ''}｜{rec.get('docTypeDesc') or '數位成績單'}" + (f"（{note}）" if note else ""))
        return 0
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
        print(f"已下載：{f['path']}（{round(f['size'] / 1024)} KB）")
    return 0


def cmd_enrollment(args: argparse.Namespace, client: SisClient) -> int:
    if args.enrollment_cmd == "list":
        info = client.list_enrollment_certs()
        if args.json:
            _print_json(info)
            return 0
        rows = info.get("hisStuStatusInfo") or []
        if not rows:
            print("查無學籍與在學證明紀錄。")
            return 0
        for row in rows:
            cur = "（本學期）" if row.get("isCurrent") else ""
            reg = "已註冊" if row.get("isReg") else "未註冊"
            print(f"{row.get('hy')}-{row.get('ht')}{cur}｜{row.get('dptGrdNa', '')}｜{row.get('curNa', '')}｜{reg}")
        return 0
    item = client.download_enrollment_cert(Path(args.out).resolve(), hy=args.hy, ht=args.ht, lang=args.lang)
    if args.json:
        _print_json(item)
        return 0
    print(f"已下載：{item['path']}（{round(item['size'] / 1024)} KB）")
    return 0


def cmd_registration(args: argparse.Namespace, client: SisClient) -> int:
    info = client.get_registration_status()
    if args.json:
        _print_json(info)
        return 0
    status_map = {1: "已完成註冊", None: "目前非註冊查詢期間或未確認"}
    st = status_map.get(info.get("registerStatus"), f"狀態代碼 {info.get('registerStatus')}")
    print(f"{info.get('hy', '')}-{info.get('ht', '')} 註冊狀況：{st}")
    if info.get("startDate") or info.get("endDate"):
        print(f"查詢期間：{info.get('startDate', '')} ~ {info.get('endDate', '')}")
    for err in info.get("errorFields") or []:
        print(f"- {err.get('errCna', '')}（狀態 {err.get('passStatus', '')}）：{err.get('organizerNote', '')}")
    return 0


def cmd_courses(args: argparse.Namespace, client: SisClient) -> int:
    data = client.search_courses(
        hy=args.hy,
        ht=args.ht,
        keyword=args.keyword,
        teacher=args.teacher,
        general_edu=args.gen_edu,
        gdpt_group=args.gdpt_group,
        page=args.page,
        page_size=args.page_size,
    )
    if args.json:
        _print_json(data)
        return 0
    items = data.get("result") or []
    if not items:
        print("查無符合條件的課程。")
        return 0
    for c in items:
        name = c.get("couCNa") or "未命名課程"
        tch = c.get("tchCNa") or ""
        credit = c.get("credit") or ""
        dpt = c.get("avaDptCNa") or c.get("gDptCNa") or ""
        print(f"{c.get('hy', '')}-{c.get('ht', '')}｜{name}｜{tch}｜{credit}學分｜{dpt}")
    return 0


def cmd_leave(args: argparse.Namespace, client: SisClient) -> int:
    sub = args.leave_cmd
    if sub == "types":
        rows = client.leave_ref_types()
        if args.json:
            _print_json(rows)
            return 0
        for r in rows:
            print(f"{r.get('refLeaveSn')}｜{r.get('leaveCna')}（上限 {r.get('maxDay', '-')} 天）")
        return 0
    if sub == "sections":
        rows = client.leave_sections()
        if args.json:
            _print_json(rows)
            return 0
        for r in rows:
            print(f"{r.get('sectNo')}（{r.get('sectionNo')}）｜{r.get('sectionCna')}｜{r.get('sectionStartTime')}~{r.get('sectionEndTime')}")
        return 0
    if sub == "list":
        data = client.list_leaves(hy=args.hy, ht=args.ht)
        if args.json:
            _print_json(data)
            return 0
        rows = data.get("result") or []
        if not rows:
            print("查無請假紀錄。")
            return 0
        for r in rows:
            print(
                f"#{r.get('leaveApplySn')}｜{r.get('leaveNa', '')}｜{r.get('beginDate', '')}({r.get('beginSectNa', '')})~"
                f"{r.get('endDate', '')}({r.get('endSectNa', '')})｜共 {r.get('totalSect', '')} 節｜{r.get('applyStatusNa', '')}"
            )
        return 0
    if sub == "stat":
        data = client.leave_stats(hy=args.hy, ht=args.ht)
        if args.json:
            _print_json(data)
            return 0
        print(
            f"請假總節數：{data.get('sumLeaveSect', 0)} 節"
            f"（已核准 {data.get('sumLeaveSectYes', 0)}／未核准 {data.get('sumLeaveSectNo', 0)}）"
        )
        for c in data.get("statLeaveCouList") or []:
            print(f"- {c.get('couCna', '')}：請假 {c.get('cntLeaveSect', 0)} 節（已核准 {c.get('cntLeaveSectYes', 0)}）")
        return 0
    if sub == "draft":
        result = client.create_leave_draft(
            begin_date=args.begin_date,
            end_date=args.end_date,
            begin_section=args.begin_section,
            end_section=args.end_section,
            leave_type=args.type,
            reason=args.reason or "",
            hy=args.hy,
            ht=args.ht,
            leave_kind=args.leave_kind,
            phone=args.phone or "",
            email=args.email or "",
            attachments=args.file or [],
        )
        if args.json:
            _print_json(result)
            return 0
        print(f"✅ 已建立請假草稿 #{result['leaveApplySn']}（{result['leave_type']}，已勾選 {result['selected_courses']} 堂課，附件 {result['attachments']} 份）")
        print("⚠️ 目前僅為草稿，尚未正式送出！請至以下網址確認後送出，或執行 fjusis leave confirm <編號> --yes：")
        print(result["portal_url"])
        return 0
    if sub == "confirm":
        if not args.yes:
            raise SisError("正式送出假單必須加上 --yes 參數以確認。")
        res = client.confirm_leave(args.sn)
        if args.json:
            _print_json({"confirmed": True, "sn": args.sn, "result": res})
            return 0
        print(f"✅ 假單 #{args.sn} 已正式送出審核。")
        return 0
    return 2


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

    p_keep = sub.add_parser("keepalive", help="主動續期權杖並回寫系統認證管理員（適合排程定期執行）")
    p_keep.add_argument("--quiet", action="store_true")
    p_keep.add_argument("--json", action="store_true")

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

    p_enr = sub.add_parser("enrollment", help="查詢與下載官方數位在學證明 PDF")
    enr_sub = p_enr.add_subparsers(dest="enrollment_cmd", required=True)
    p_enr_list = enr_sub.add_parser("list", help="列出各學期在學狀態")
    p_enr_list.add_argument("--json", action="store_true")
    p_enr_dl = enr_sub.add_parser("download", help="下載數位在學證明 PDF")
    p_enr_dl.add_argument("--hy", type=int, help="指定學年度（不填預設本學期）")
    p_enr_dl.add_argument("--ht", type=int, help="指定學期（不填預設本學期）")
    p_enr_dl.add_argument("--lang", choices=["zh", "en"], default="zh", help="版本：zh 中文版（預設）／en 英文版")
    p_enr_dl.add_argument("--out", default=".", help="輸出資料夾（預設目前目錄）")
    p_enr_dl.add_argument("--json", action="store_true")

    p_reg = sub.add_parser("registration", help="查詢當學期註冊狀況")
    p_reg.add_argument("--json", action="store_true")

    p_cou = sub.add_parser("courses", help="查詢全校開課與通識課程")
    p_cou.add_argument("--hy", type=int, help="學年度")
    p_cou.add_argument("--ht", type=int, help="學期")
    p_cou.add_argument("--keyword", help="課程名稱關鍵字")
    p_cou.add_argument("--teacher", help="教師姓名關鍵字")
    p_cou.add_argument("--gen-edu", action="store_true", help="改查通識課程")
    p_cou.add_argument("--gdpt-group", help="通識領域代碼（搭配 --gen-edu）")
    p_cou.add_argument("--page", type=int, default=1)
    p_cou.add_argument("--page-size", type=int, default=20)
    p_cou.add_argument("--json", action="store_true")

    p_lv = sub.add_parser("leave", help="請假查詢、統計與建立假單草稿")
    lv_sub = p_lv.add_subparsers(dest="leave_cmd", required=True)
    p_lv_t = lv_sub.add_parser("types", help="列出可用假別與代碼")
    p_lv_t.add_argument("--json", action="store_true")
    p_lv_s = lv_sub.add_parser("sections", help="列出節次代碼與時間")
    p_lv_s.add_argument("--json", action="store_true")
    p_lv_l = lv_sub.add_parser("list", help="查詢請假申請紀錄")
    p_lv_l.add_argument("--hy", type=int)
    p_lv_l.add_argument("--ht", type=int)
    p_lv_l.add_argument("--json", action="store_true")
    p_lv_st = lv_sub.add_parser("stat", help="查詢請假節數統計")
    p_lv_st.add_argument("--hy", type=int)
    p_lv_st.add_argument("--ht", type=int)
    p_lv_st.add_argument("--json", action="store_true")
    p_lv_dr = lv_sub.add_parser("draft", help="建立請假草稿（絕不自動正式送出）")
    p_lv_dr.add_argument("--hy", type=int)
    p_lv_dr.add_argument("--ht", type=int)
    p_lv_dr.add_argument("--type", default="病假", help="假別名稱或代碼（預設：病假）")
    p_lv_dr.add_argument("--begin-date", required=True, help="開始日期 YYYY-MM-DD")
    p_lv_dr.add_argument("--end-date", required=True, help="結束日期 YYYY-MM-DD")
    p_lv_dr.add_argument("--begin-section", required=True, help="開始節次（例如 1 或 D1）")
    p_lv_dr.add_argument("--end-section", required=True, help="結束節次（例如 2 或 D2）")
    p_lv_dr.add_argument("--reason", default="", help="請假事由")
    p_lv_dr.add_argument("--leave-kind", type=int, default=10, help="請假性質代碼（10=一般請假，20=考試請假）")
    p_lv_dr.add_argument("--phone", default="")
    p_lv_dr.add_argument("--email", default="")
    p_lv_dr.add_argument("--file", action="append", default=[], help="佐證文件路徑（pdf/jpg/png，<=2MB，可重複帶入）")
    p_lv_dr.add_argument("--json", action="store_true")
    p_lv_cf = lv_sub.add_parser("confirm", help="正式送出既有請假草稿（需加 --yes）")
    p_lv_cf.add_argument("sn", type=int, help="假單草稿編號 leaveApplySn")
    p_lv_cf.add_argument("--yes", action="store_true", help="確認正式送出假單")
    p_lv_cf.add_argument("--json", action="store_true")

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
        if args.command == "keepalive":
            return cmd_keepalive(args, sis)
        if args.command == "grades":
            return cmd_grades(args, sis)
        if args.command == "ranks":
            return cmd_ranks(args, sis)
        if args.command == "transcript":
            return cmd_transcript(args, sis)
        if args.command == "enrollment":
            return cmd_enrollment(args, sis)
        if args.command == "registration":
            return cmd_registration(args, sis)
        if args.command == "courses":
            return cmd_courses(args, sis)
        if args.command == "leave":
            return cmd_leave(args, sis)
        parser.error("未知的指令")
        return 2
    except (AuthError, SisError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
