"""HTTP client for FJU Student Information Platform APIs (travellerlink & explorelink)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import mimetypes
from pathlib import Path
import re
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid

from fju_sis.auth import Session, SessionStore, session_from_tokens

API_BASE = "https://travellerlink.fju.edu.tw"
LEAVE_API_BASE = "https://explorelink.fju.edu.tw/stuLeave/api"
SYSTEM_SN = 31
DEFAULT_LCID = 1028
DEFAULT_TIMEOUT = 30
LEAVE_ALLOWED_EXTS = {".pdf", ".jpg", ".jpeg", ".gif", ".png"}
LEAVE_MAX_FILE_BYTES = 2 * 1024 * 1024


class SisError(RuntimeError):
    """Base error for FJU SIS client."""


class AuthError(SisError):
    """Authentication missing, expired, or rejected."""


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str, str, dict[str, str], bytes | None, int], HttpResponse]


def current_academic_semester(now: datetime | None = None) -> tuple[int, int]:
    dt = now or datetime.now()
    if dt.month >= 8:
        return dt.year - 1911, 1
    return dt.year - 1912, (1 if dt.month == 1 else 2)


def default_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: int,
) -> HttpResponse:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return HttpResponse(status=resp.status, headers=hdrs, body=resp.read())
    except urllib.error.HTTPError as exc:
        hdrs = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
        return HttpResponse(status=exc.code, headers=hdrs, body=exc.read())
    except urllib.error.URLError as exc:
        raise SisError(f"無法連線到輔大學生資訊平台：{exc.reason}") from exc


def _extract_error(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errorMessage")
        if isinstance(errors, list) and errors:
            parts = []
            for item in errors:
                if isinstance(item, dict):
                    parts.append(str(item.get("message") or item.get("key") or item))
                elif item:
                    parts.append(str(item))
            if parts:
                return "；".join(parts)
        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
        if isinstance(msg, dict) and msg:
            return "；".join(f"{k}: {v}" for k, v in msg.items())
    return fallback


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise SisError("伺服器回傳了非 JSON 格式的內容") from exc


def _parse_disposition_filename(header_value: str) -> str:
    if not header_value:
        return ""
    match = re.search(r"filename\*=UTF-8''([^;]+)", header_value, re.I)
    if match:
        return urllib.parse.unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename="?([^";]+)"?', header_value, re.I)
    if match:
        return urllib.parse.unquote(match.group(1).strip())
    return ""


def _encode_multipart(
    fields: list[tuple[str, str]],
    files: list[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"----FjuSisBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for name, filename, data, ctype in files:
        safe_name = filename.replace('"', "_")
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{safe_name}"\r\n'.encode("utf-8")
        )
        chunks.append(f"Content-Type: {ctype}\r\n\r\n".encode("utf-8"))
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _write_pdf(dest: Path, body: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".pdf.tmp")
    tmp.write_bytes(body)
    tmp.replace(dest)


class SisClient:
    def __init__(
        self,
        *,
        store: SessionStore | None = None,
        base_url: str = API_BASE,
        leave_base_url: str = LEAVE_API_BASE,
        timeout: int = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
    ):
        self.store = store or SessionStore()
        self.base_url = base_url.rstrip("/")
        self.leave_base_url = leave_base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport or default_transport
        self._session: Session | None = None

    def ldap_login(self, emp_no: str, password: str) -> Session:
        emp_no = emp_no.strip()
        if not emp_no or not password:
            raise AuthError("請輸入學號與密碼。")
        url = f"{self.base_url}/FjuBase/api/Account/LdapLogin"
        body = json.dumps(
            {"empNo": emp_no, "password": password, "systemSn": SYSTEM_SN},
            ensure_ascii=False,
        ).encode("utf-8")
        resp = self._transport(
            "POST",
            url,
            {"Content-Type": "application/json", "Accept": "application/json"},
            body,
            self.timeout,
        )
        data = _parse_json(resp.body) if resp.body else {}
        if resp.status >= 400 or not isinstance(data, dict):
            raise AuthError(_extract_error(data, f"LDAP 登入失敗（HTTP {resp.status}）"))
        result = data.get("result") or {}
        auth_token = str(result.get("auth_token") or "").strip()
        refresh_token = str(result.get("refresh_token") or "").strip()
        if not auth_token:
            raise AuthError(_extract_error(data, "LDAP 登入未取得授權憑證"))
        session = session_from_tokens(auth_token, refresh_token, emp_no=emp_no)
        self.store.save(session)
        self._session = session
        return session

    def refresh_session(self, session: Session) -> Session:
        if not session.refresh_token or not session.emp_no:
            raise AuthError("登入已過期，且沒有可用的續期權杖；請執行 fjusis login 重新登入。")
        query = urllib.parse.urlencode({"refreshToken": session.refresh_token, "empNo": session.emp_no})
        url = f"{self.base_url}/FjuBase/api/Account/RefreshToken?{query}"
        resp = self._transport(
            "POST",
            url,
            {"Accept": "application/json", "Content-Length": "0"},
            b"",
            self.timeout,
        )
        data = _parse_json(resp.body) if resp.body else {}
        if resp.status >= 400 or not isinstance(data, dict):
            raise AuthError(_extract_error(data, "登入已過期，請執行 fjusis login 重新登入。"))
        result = data.get("result") or {}
        auth_token = str(result.get("auth_token") or "").strip()
        refresh_token = str(result.get("refresh_token") or session.refresh_token).strip()
        if not auth_token:
            raise AuthError("登入續期失敗，請執行 fjusis login 重新登入。")
        updated = session_from_tokens(auth_token, refresh_token, emp_no=session.emp_no)
        self.store.save(updated)
        self._session = updated
        return updated

    def require_session(self) -> Session:
        session = self._session or self.store.load()
        if not session or not session.auth_token:
            raise AuthError("尚未登入輔大學生資訊平台；請在終端機執行 fjusis login。")
        if session.is_expired():
            session = self.refresh_session(session)
        self._session = session
        return session

    def _authed_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        accept: str = "application/json",
        base_url: str | None = None,
    ) -> HttpResponse:
        session = self.require_session()
        if isinstance(params, list):
            pairs = [(k, v) for k, v in params if v is not None and v != ""]
        elif isinstance(params, dict):
            pairs = [(k, v) for k, v in params.items() if v is not None and v != ""]
        else:
            pairs = []
        query = urllib.parse.urlencode(pairs, doseq=True)
        root = (base_url or self.base_url).rstrip("/")
        url = f"{root}{path}" + (f"?{query}" if query else "")
        headers = {"Authorization": f"Bearer {session.auth_token}", "Accept": accept}
        if content_type:
            headers["Content-Type"] = content_type
        if body is None and method in {"POST", "PUT", "PATCH"}:
            body = b""
            headers["Content-Length"] = "0"
        resp = self._transport(method, url, headers, body, self.timeout)
        if resp.status == 401 and session.refresh_token:
            session = self.refresh_session(session)
            headers["Authorization"] = f"Bearer {session.auth_token}"
            resp = self._transport(method, url, headers, body, self.timeout)
        if resp.status == 401:
            raise AuthError("登入已失效，請在終端機執行 fjusis login 重新登入。")
        return resp

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json_body: Any = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        body = raw_body
        ctype = content_type
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            ctype = "application/json"
        resp = self._authed_request(
            method,
            path,
            params=params,
            body=body,
            content_type=ctype,
            accept="application/json",
            base_url=base_url,
        )
        data = _parse_json(resp.body) if resp.body else {}
        if resp.status >= 400:
            raise SisError(_extract_error(data, f"API 請求失敗（HTTP {resp.status}）"))
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data

    # --- Identity & Grades & Transcripts ---

    def whoami(self, lcid: int = DEFAULT_LCID) -> dict[str, Any]:
        session = self.require_session()
        info = self._request_json("GET", "/Score/api/GradesInquiry/StuBaseInfo", params={"lcId": lcid})
        return {
            "emp_no": session.emp_no,
            "name": session.name,
            "exp": session.exp,
            "student_info": info,
        }

    def get_grades(
        self,
        *,
        hy: int | str | None = None,
        ht: int | str | None = None,
        lcid: int = DEFAULT_LCID,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"LcId": lcid, "SortBy": "Hy", "Descending": "true"}
        if hy is not None:
            params["Hy"] = str(hy)
        if ht is not None:
            params["Ht"] = str(ht)
        result = self._request_json("GET", "/Score/api/GradesInquiry/Grades", params=params)
        return result if isinstance(result, list) else []

    def get_semester_ranks(self) -> list[dict[str, Any]]:
        result = self._request_json("GET", "/Score/api/GradesInquiry/Rank/Semester")
        return result if isinstance(result, list) else []

    def list_transcripts(self, lcid: int = DEFAULT_LCID) -> list[dict[str, Any]]:
        session = self.require_session()
        result = self._request_json(
            "GET",
            "/Score/api/ServiceDeskDigitalDocProvider/GetAllRecordList",
            params={"stuNo": session.emp_no, "lcId": lcid},
        )
        return result if isinstance(result, list) else []

    def download_transcript(
        self,
        hy: int | str,
        ht: int | str,
        out_dir: Path | str,
        *,
        ranking: bool = True,
        lcid: int = DEFAULT_LCID,
    ) -> dict[str, Any]:
        session = self.require_session()
        resp = self._authed_request(
            "GET",
            "/Score/api/ServiceDeskDigitalDocProvider/ExportSemeTranscriptFile",
            params={
                "hy": str(hy),
                "ht": str(ht),
                "stuNo": session.emp_no,
                "lcid": str(lcid),
                "isRanking": "true" if ranking else "false",
            },
            accept="application/pdf, application/octet-stream, */*",
        )
        if resp.status >= 400:
            detail = ""
            try:
                detail = _extract_error(_parse_json(resp.body), "")
            except SisError:
                pass
            raise SisError(detail or f"下載 {hy}-{ht} 數位成績單失敗（HTTP {resp.status}）")
        if not resp.body.startswith(b"%PDF-"):
            raise SisError(f"{hy}-{ht} 下載回應不是有效的 PDF 檔案（Content-Type: {resp.headers.get('content-type', 'unknown')}）")
        suffix = "含排名" if ranking else "不含排名"
        dest = Path(out_dir).resolve() / f"{hy}-{ht}_學期數位成績單_{suffix}.pdf"
        _write_pdf(dest, resp.body)
        return {
            "hy": int(hy) if str(hy).isdigit() else str(hy),
            "ht": int(ht) if str(ht).isdigit() else str(ht),
            "ranking": ranking,
            "path": str(dest),
            "size": len(resp.body),
            "original_filename": _parse_disposition_filename(resp.headers.get("content-disposition", "")),
        }

    def download_all_transcripts(
        self,
        out_dir: Path | str,
        *,
        ranking: bool = True,
        lcid: int = DEFAULT_LCID,
    ) -> list[dict[str, Any]]:
        records = self.list_transcripts(lcid=lcid)
        downloaded: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for rec in records:
            hy = rec.get("hy")
            ht = rec.get("ht")
            if hy is None or ht is None:
                continue
            key = (str(hy), str(ht))
            if key in seen:
                continue
            seen.add(key)
            downloaded.append(self.download_transcript(hy, ht, out_dir, ranking=ranking, lcid=lcid))
        return downloaded

    # --- Digital Enrollment Certificate ---

    def list_enrollment_certs(self, lcid: int = DEFAULT_LCID) -> dict[str, Any]:
        session = self.require_session()
        result = self._request_json(
            "GET",
            "/Education/api/OnlineStuStatusCertApply/GetStuInfo",
            params={"stuNo": session.emp_no, "lcId": lcid},
        )
        return result if isinstance(result, dict) else {"hisStuStatusInfo": []}

    def download_enrollment_cert(
        self,
        out_dir: Path | str,
        *,
        hy: int | str | None = None,
        ht: int | str | None = None,
        lang: str = "zh",
        lcid: int = DEFAULT_LCID,
    ) -> dict[str, Any]:
        session = self.require_session()
        if hy is None or ht is None:
            info = self.list_enrollment_certs(lcid=lcid)
            history = info.get("hisStuStatusInfo") or []
            chosen = next((x for x in history if x.get("isCurrent")), None) or (
                next((x for x in history if x.get("isReg")), None)
            ) or (history[0] if history else None)
            if not chosen:
                raise SisError("目前查無可下載的數位在學證明學期紀錄。")
            hy = chosen.get("hy")
            ht = chosen.get("ht")
        version = 2 if str(lang).lower() in {"en", "eng", "english", "2"} else 1
        resp = self._authed_request(
            "GET",
            "/Education/api/OnlineStuStatusCertApply/Download",
            params={
                "stuNO": session.emp_no,
                "entAcaYear": str(hy),
                "entAcaTerm": str(ht),
                "version": str(version),
            },
            accept="application/pdf, application/octet-stream, */*",
        )
        if resp.status >= 400:
            detail = ""
            try:
                detail = _extract_error(_parse_json(resp.body), "")
            except SisError:
                pass
            raise SisError(detail or f"下載 {hy}-{ht} 數位在學證明失敗（HTTP {resp.status}）")
        if not resp.body.startswith(b"%PDF-"):
            raise SisError(f"{hy}-{ht} 數位在學證明回應不是有效的 PDF（Content-Type: {resp.headers.get('content-type', 'unknown')}）")
        label = "英文版" if version == 2 else "中文版"
        dest = Path(out_dir).resolve() / f"{hy}-{ht}_數位在學證明_{label}.pdf"
        _write_pdf(dest, resp.body)
        return {
            "hy": int(hy) if str(hy).isdigit() else str(hy),
            "ht": int(ht) if str(ht).isdigit() else str(ht),
            "lang": "en" if version == 2 else "zh",
            "path": str(dest),
            "size": len(resp.body),
            "original_filename": _parse_disposition_filename(resp.headers.get("content-disposition", "")),
        }

    # --- Registration Status & Course Search ---

    def get_registration_status(self, lcid: int = DEFAULT_LCID) -> dict[str, Any]:
        result = self._request_json("GET", "/Register/api/NotFile/Info", params={"lcID": lcid})
        return result if isinstance(result, dict) else {}

    def search_courses(
        self,
        *,
        hy: int | str | None = None,
        ht: int | str | None = None,
        keyword: str | None = None,
        teacher: str | None = None,
        general_edu: bool = False,
        gdpt_group: str | None = None,
        page: int = 1,
        page_size: int = 20,
        lcid: int = DEFAULT_LCID,
    ) -> dict[str, Any]:
        if hy is None or ht is None:
            cur_hy, cur_ht = current_academic_semester()
            hy = cur_hy if hy is None else hy
            ht = cur_ht if ht is None else ht
        if general_edu:
            params: dict[str, Any] = {
                "Hy": str(hy),
                "Ht": str(ht),
                "GDptGroup": gdpt_group,
                "Lcid": str(lcid),
                "PageNumber": str(page),
                "PageSize": str(page_size),
            }
            path = "/FjuCourse/api/CourseStudent/CourseStudentQueryByGDPT"
        else:
            params = {
                "Hy": str(hy),
                "Ht": str(ht),
                "CouCna": keyword,
                "TchCna": teacher,
                "lcid": str(lcid),
                "page": str(page),
                "rowsPerPage": str(page_size),
                "pageNumber": str(page),
                "pageSize": str(page_size),
            }
            path = "/FjuCourse/api/CourseStudent/CourseStudentQueryByBasics"
        result = self._request_json("GET", path, params=params)
        if isinstance(result, dict):
            return result
        return {"result": result if isinstance(result, list) else []}

    # --- Student Leave System (explorelink.fju.edu.tw/stuLeave/api) ---

    def leave_ref_types(self) -> list[dict[str, Any]]:
        res = self._request_json("GET", "/RefList/RefLeave", base_url=self.leave_base_url)
        return res if isinstance(res, list) else []

    def leave_sections(self) -> list[dict[str, Any]]:
        res = self._request_json("GET", "/Course/Section", base_url=self.leave_base_url)
        return res if isinstance(res, list) else []

    def list_leaves(
        self,
        *,
        hy: int | str | None = None,
        ht: int | str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        session = self.require_session()
        if hy is None or ht is None:
            cur_hy, cur_ht = current_academic_semester()
            hy = cur_hy if hy is None else hy
            ht = cur_ht if ht is None else ht
        res = self._request_json(
            "GET",
            "/StuLeave",
            params={
                "hy": str(hy),
                "ht": str(ht),
                "stuKeyword": session.emp_no,
                "pageNumber": str(page),
                "pageSize": str(page_size),
            },
            base_url=self.leave_base_url,
        )
        return res if isinstance(res, dict) else {"result": res if isinstance(res, list) else []}

    def leave_stats(self, *, hy: int | str | None = None, ht: int | str | None = None) -> dict[str, Any]:
        session = self.require_session()
        if hy is None or ht is None:
            cur_hy, cur_ht = current_academic_semester()
            hy = cur_hy if hy is None else hy
            ht = cur_ht if ht is None else ht
        res = self._request_json(
            "GET",
            "/StuLeave/Stat",
            params={"Hy": str(hy), "Ht": str(ht), "StuNo": session.emp_no},
            base_url=self.leave_base_url,
        )
        return res if isinstance(res, dict) else {}

    def _resolve_leave_type(self, leave_type: str | int) -> tuple[int, int]:
        types = self.leave_ref_types()
        target = str(leave_type).strip()
        for item in types:
            if str(item.get("refLeaveSn")) == target or str(item.get("leaveCna") or "").strip() == target:
                docs = item.get("docList") or []
                doc_sn = int(docs[0].get("refDocSn") or 0) if docs else 0
                return int(item["refLeaveSn"]), doc_sn
        for item in types:
            if target and target in str(item.get("leaveCna") or ""):
                docs = item.get("docList") or []
                doc_sn = int(docs[0].get("refDocSn") or 0) if docs else 0
                return int(item["refLeaveSn"]), doc_sn
        if target.isdigit():
            return int(target), 0
        available = "、".join(str(x.get("leaveCna") or x.get("refLeaveSn")) for x in types)
        raise SisError(f"找不到假別「{leave_type}」。可用假別：{available or '無'}")

    def _resolve_section(self, section: str | int, sections: list[dict[str, Any]]) -> int:
        target = str(section).strip().upper()
        for item in sections:
            if (
                str(item.get("sectNo")) == target
                or str(item.get("sectionNo") or "").strip().upper() == target
                or str(item.get("sectionCna") or "").strip().upper() == target
            ):
                return int(item["sectNo"])
        if target.isdigit():
            return int(target)
        raise SisError(f"找不到節次「{section}」。")

    def create_leave_draft(
        self,
        *,
        begin_date: str,
        end_date: str,
        begin_section: str | int,
        end_section: str | int,
        leave_type: str | int = "病假",
        reason: str = "",
        hy: int | str | None = None,
        ht: int | str | None = None,
        leave_kind: int = 10,
        phone: str = "",
        email: str = "",
        attachments: list[str | Path] | None = None,
        auto_select_courses: bool = True,
    ) -> dict[str, Any]:
        """Create a leave request DRAFT (never calls PUT /Confirm automatically)."""
        session = self.require_session()
        if hy is None or ht is None:
            cur_hy, cur_ht = current_academic_semester()
            hy = cur_hy if hy is None else hy
            ht = cur_ht if ht is None else ht
        ref_leave_sn, default_doc_sn = self._resolve_leave_type(leave_type)
        sections = self.leave_sections()
        b_sect = self._resolve_section(begin_section, sections)
        e_sect = self._resolve_section(end_section, sections)
        if not phone or not email:
            try:
                contact = self._request_json(
                    "GET", "/Student/Contact", params={"stuNo": session.emp_no}, base_url=self.leave_base_url
                )
                if isinstance(contact, dict):
                    phone = phone or str(contact.get("phoneNumber") or contact.get("mobile") or "")
                    email = email or str(contact.get("emailAccount") or contact.get("email") or "")
            except SisError:
                pass
        fields: list[tuple[str, str]] = [
            ("leaveApplySn", "0"),
            ("stuNo", session.emp_no),
            ("hy", str(hy)),
            ("ht", str(ht)),
            ("leaveKind", str(leave_kind)),
            ("examKind", "0"),
            ("refLeaveSn", str(ref_leave_sn)),
            ("officialLeaveSn", "0"),
            ("beginDate", begin_date),
            ("endDate", end_date),
            ("beginSectNo", str(b_sect)),
            ("endSectNo", str(e_sect)),
            ("famTypeNo", "0"),
            ("famLevelNo", "0"),
        ]
        if reason.strip():
            fields.append(("leaveReason", reason.strip()))
        if phone.strip():
            fields.append(("phoneNumber", phone.strip()))
        if email.strip():
            fields.append(("emailAccount", email.strip()))
        upload_files: list[tuple[str, str, bytes, str]] = []
        for idx, raw_path in enumerate(attachments or []):
            p = Path(raw_path).resolve()
            if not p.is_file():
                raise SisError(f"找不到佐證檔案：{p}")
            if p.suffix.lower() not in LEAVE_ALLOWED_EXTS:
                raise SisError(f"不支援的佐證檔案格式：{p.name}（僅支援 pdf/jpg/jpeg/gif/png）")
            data = p.read_bytes()
            if len(data) > LEAVE_MAX_FILE_BYTES:
                raise SisError(f"佐證檔案超過 2MB 上限：{p.name}")
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            upload_files.append((f"UploadFiles[{idx}].uploadFile", p.name, data, ctype))
            fields.append((f"UploadFiles[{idx}].refDocSn", str(default_doc_sn)))
        body, ctype = _encode_multipart(fields, upload_files)
        created = self._request_json(
            "POST",
            "/StuLeave",
            raw_body=body,
            content_type=ctype,
            base_url=self.leave_base_url,
        )
        sn = created.get("leaveApplySn") if isinstance(created, dict) else created
        if not sn:
            raise SisError("假單草稿建立失敗：未取得 leaveApplySn。")
        selected_courses: list[dict[str, Any]] = []
        if auto_select_courses:
            try:
                avail = self._request_json("GET", f"/StuLeave/{sn}/SelCou", base_url=self.leave_base_url)
                if isinstance(avail, list):
                    valid = [c for c in avail if c.get("tchNo") not in (None, 0, "0", "")]
                    if valid:
                        self._request_json(
                            "POST",
                            f"/StuLeave/{sn}/SelCou",
                            json_body={"leaveSelCou": valid},
                            base_url=self.leave_base_url,
                        )
                        selected_courses = valid
            except SisError:
                pass
        alerts: Any = []
        try:
            alerts = self._request_json("GET", f"/StuLeave/{sn}/LeaveAlert", base_url=self.leave_base_url)
        except SisError:
            alerts = []
        return {
            "leaveApplySn": sn,
            "submitted": False,
            "draft_only": True,
            "hy": hy,
            "ht": ht,
            "leave_type": leave_type,
            "refLeaveSn": ref_leave_sn,
            "beginDate": begin_date,
            "endDate": end_date,
            "beginSectNo": b_sect,
            "endSectNo": e_sect,
            "attachments": len(upload_files),
            "selected_courses": len(selected_courses),
            "alerts": alerts,
            "portal_url": f"https://sis.fju.edu.tw/#/leave/leaveListUpdate/{sn}",
        }

    def confirm_leave(self, leave_apply_sn: int | str) -> Any:
        """Explicitly confirm and submit a leave draft."""
        return self._request_json("PUT", f"/StuLeave/{leave_apply_sn}/Confirm", base_url=self.leave_base_url)
