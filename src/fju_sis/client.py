"""HTTP client for FJU Student Information Platform API (travellerlink.fju.edu.tw)."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

from fju_sis.auth import Session, SessionStore, session_from_tokens

API_BASE = "https://travellerlink.fju.edu.tw"
SYSTEM_SN = 31
DEFAULT_LCID = 1028
DEFAULT_TIMEOUT = 30


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
            return "；".join(str(x) for x in errors if x)
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


class SisClient:
    def __init__(
        self,
        *,
        store: SessionStore | None = None,
        base_url: str = API_BASE,
        timeout: int = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
    ):
        self.store = store or SessionStore()
        self.base_url = base_url.rstrip("/")
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
        params: dict[str, Any] | None = None,
        accept: str = "application/json",
    ) -> HttpResponse:
        session = self.require_session()
        query = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        )
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        headers = {"Authorization": f"Bearer {session.auth_token}", "Accept": accept}
        resp = self._transport(method, url, headers, None, self.timeout)
        if resp.status == 401 and session.refresh_token:
            session = self.refresh_session(session)
            headers["Authorization"] = f"Bearer {session.auth_token}"
            resp = self._transport(method, url, headers, None, self.timeout)
        if resp.status == 401:
            raise AuthError("登入已失效，請在終端機執行 fjusis login 重新登入。")
        return resp

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._authed_request("GET", path, params=params, accept="application/json")
        data = _parse_json(resp.body) if resp.body else {}
        if resp.status >= 400:
            raise SisError(_extract_error(data, f"API 請求失敗（HTTP {resp.status}）"))
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data

    def whoami(self, lcid: int = DEFAULT_LCID) -> dict[str, Any]:
        session = self.require_session()
        info = self._get_json("/Score/api/GradesInquiry/StuBaseInfo", {"lcId": lcid})
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
        result = self._get_json("/Score/api/GradesInquiry/Grades", params)
        return result if isinstance(result, list) else []

    def get_semester_ranks(self) -> list[dict[str, Any]]:
        result = self._get_json("/Score/api/GradesInquiry/Rank/Semester")
        return result if isinstance(result, list) else []

    def list_transcripts(self, lcid: int = DEFAULT_LCID) -> list[dict[str, Any]]:
        session = self.require_session()
        result = self._get_json(
            "/Score/api/ServiceDeskDigitalDocProvider/GetAllRecordList",
            {"stuNo": session.emp_no, "lcId": lcid},
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
        dest_dir = Path(out_dir).resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)
        suffix = "含排名" if ranking else "不含排名"
        filename = f"{hy}-{ht}_學期數位成績單_{suffix}.pdf"
        dest = dest_dir / filename
        tmp = dest.with_suffix(".pdf.tmp")
        tmp.write_bytes(resp.body)
        tmp.replace(dest)
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
