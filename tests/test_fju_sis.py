"""Unit tests for FJU-SIS-MCP (offline, synthetic data only)."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fju_sis.auth import CredentialStore, Session, SessionStore, session_from_tokens
from fju_sis.cli import main as cli_main
from fju_sis.client import AuthError, HttpResponse, SisClient, SisError
from fju_sis.mcp_server import handle_message, serve_stdio


def make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


class MemoryStore(SessionStore):
    def __init__(self, session: Session | None = None):
        super().__init__(target="FJU-SIS-MCP/test-memory")
        self._saved = session

    def save(self, session: Session) -> None:
        self._saved = session

    def load(self) -> Session | None:
        return self._saved

    def delete(self) -> bool:
        had = self._saved is not None
        self._saved = None
        return had


class MemoryCredStore(CredentialStore):
    def __init__(self, creds: tuple[str, str] | None = None):
        super().__init__(target="FJU-SIS-MCP/test-cred-memory")
        self._creds = creds

    def save(self, emp_no: str, password: str) -> None:
        self._creds = (emp_no, password)

    def load(self) -> tuple[str, str] | None:
        return self._creds

    def delete(self) -> bool:
        had = self._creds is not None
        self._creds = None
        return had


class AuthAndStoreTests(unittest.TestCase):
    def test_session_from_tokens_decodes_claims(self):
        exp = int(time.time()) + 3600
        tok = make_jwt({
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name": "411000001",
            "CName": "測試同學",
            "exp": exp,
        })
        session = session_from_tokens(tok, "ref-1")
        self.assertEqual(session.emp_no, "411000001")
        self.assertEqual(session.name, "測試同學")
        self.assertEqual(session.exp, exp)
        self.assertFalse(session.is_expired())

    def test_native_credential_store_roundtrip_and_cleanup(self):
        # Exercises real Windows Credential Manager (or 0600 POSIX file) with an isolated test target.
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(
                target="FJU-SIS-MCP/unit-test-temp",
                posix_path=Path(tmp) / "session.json",
            )
            sample = Session(emp_no="411000001", auth_token="tok-abc", refresh_token="ref-xyz", exp=9999999999, name="測試")
            try:
                store.save(sample)
                loaded = store.load()
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.emp_no, "411000001")
                self.assertEqual(loaded.auth_token, "tok-abc")
                self.assertEqual(loaded.refresh_token, "ref-xyz")
            finally:
                store.delete()
            self.assertIsNone(store.load())


class ClientTests(unittest.TestCase):
    def test_ldap_login_stores_tokens_not_password(self):
        tok = make_jwt({"exp": int(time.time()) + 3600, "CName": "測試同學"})
        seen = []

        def transport(method, url, headers, body, timeout):
            seen.append((method, url, json.loads(body.decode("utf-8"))))
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"statusCode": 200, "result": {"auth_token": tok, "refresh_token": "r1"}}).encode(),
            )

        store = MemoryStore()
        client = SisClient(store=store, transport=transport)
        session = client.ldap_login("411000001", "secret-pw")
        self.assertEqual(session.emp_no, "411000001")
        self.assertEqual(seen[0][2]["systemSn"], 31)
        self.assertNotIn("secret-pw", json.dumps(store.load().to_dict()))

    def test_401_automatically_refreshes_token_and_retries(self):
        old_tok = make_jwt({"exp": int(time.time()) + 3600})
        new_tok = make_jwt({"exp": int(time.time()) + 7200})
        store = MemoryStore(Session(emp_no="411000001", auth_token=old_tok, refresh_token="ref-old", exp=int(time.time()) + 3600))
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, url, headers.get("Authorization")))
            if "/RefreshToken" in url:
                self.assertEqual(headers.get("Authorization"), f"Bearer {old_tok}")
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"statusCode": 200, "result": {"auth_token": new_tok, "refresh_token": "ref-new"}}).encode(),
                )
            if headers.get("Authorization") == f"Bearer {old_tok}":
                return HttpResponse(401, {}, b'{"errorMessage":["expired"]}')
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"statusCode": 200, "result": [{"aca": "114-1", "avgSco": 88.5, "class": 3, "series": 8}]}).encode(),
            )

        client = SisClient(store=store, transport=transport)
        ranks = client.get_semester_ranks()
        self.assertEqual(len(ranks), 1)
        self.assertEqual(ranks[0]["avgSco"], 88.5)
        self.assertEqual(store.load().auth_token, new_tok)
        self.assertEqual(store.load().refresh_token, "ref-new")

    def test_download_all_transcripts_validates_pdf_and_writes_clean_name(self):
        tok = make_jwt({"exp": int(time.time()) + 3600})
        store = MemoryStore(Session(emp_no="411000001", auth_token=tok, refresh_token="ref-1", exp=int(time.time()) + 3600))

        def transport(method, url, headers, body, timeout):
            if "GetAllRecordList" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({
                        "statusCode": 200,
                        "result": [
                            {"hy": 114, "ht": 1, "stuno": "411000001", "hyHtDesc": "114學年度第1學期", "docTypeDesc": "數位成績單"},
                            {"hy": 114, "ht": 2, "stuno": "411000001", "hyHtDesc": "114學年度第2學期", "docTypeDesc": "數位成績單"},
                        ],
                    }).encode(),
                )
            if "ExportSemeTranscriptFile" in url:
                return HttpResponse(
                    200,
                    {
                        "content-type": "application/pdf",
                        "content-disposition": 'attachment;filename="raw_(1.2.3.4)_ts.pdf"',
                    },
                    b"%PDF-1.7\nfake pdf bytes",
                )
            raise AssertionError(f"Unexpected URL: {url}")

        client = SisClient(store=store, transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            files = client.download_all_transcripts(tmp, ranking=True)
            self.assertEqual(len(files), 2)
            for item in files:
                p = Path(item["path"])
                self.assertTrue(p.is_file())
                self.assertTrue(p.read_bytes().startswith(b"%PDF-"))
                self.assertNotIn("1.2.3.4", p.name)
                self.assertIn("含排名", p.name)

    def test_download_rejects_non_pdf_payload(self):
        tok = make_jwt({"exp": int(time.time()) + 3600})
        store = MemoryStore(Session(emp_no="411000001", auth_token=tok, exp=int(time.time()) + 3600))

        def transport(method, url, headers, body, timeout):
            return HttpResponse(200, {"content-type": "text/html"}, b"<html>error</html>")

        client = SisClient(store=store, transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SisError):
                client.download_transcript(114, 2, tmp)

    def test_missing_session_raises_auth_error(self):
        client = SisClient(store=MemoryStore(), cred_store=MemoryCredStore())
        with self.assertRaises(AuthError):
            client.get_grades()

    def test_expired_session_and_failed_refresh_automatically_relogins_from_cred_store(self):
        expired_tok = make_jwt({"exp": int(time.time()) - 100})
        fresh_tok = make_jwt({"exp": int(time.time()) + 1200})
        store = MemoryStore(Session(emp_no="411000001", auth_token=expired_tok, refresh_token="dead-ref", exp=int(time.time()) - 100))
        cred_store = MemoryCredStore(("411000001", "saved-pw"))

        def transport(method, url, headers, body, timeout):
            if "/RefreshToken" in url:
                return HttpResponse(400, {}, b'{"errorMessage":["invalid refresh token"]}')
            if "/LdapLogin" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"statusCode": 200, "result": {"auth_token": fresh_tok, "refresh_token": "new-ref"}}).encode(),
                )
            return HttpResponse(200, {"content-type": "application/json"}, b'{"statusCode":200,"result":[]}')

        client = SisClient(store=store, cred_store=cred_store, transport=transport)
        self.assertEqual(client.get_semester_ranks(), [])
        self.assertEqual(store.load().auth_token, fresh_tok)


class CliAndMcpTests(unittest.TestCase):
    def _client(self):
        tok = make_jwt({"exp": int(time.time()) + 3600})
        store = MemoryStore(Session(emp_no="411000001", auth_token=tok, exp=int(time.time()) + 3600))

        def transport(method, url, headers, body, timeout):
            if "GradesInquiry/Grades" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({
                        "statusCode": 200,
                        "result": [{"hy": 114, "htPeriod": 1, "couCNa": "相對論", "credit": 3, "scoreDisplay": "92", "reqSelCNa": "必修"}],
                    }).encode(),
                )
            if "GetAllRecordList" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"statusCode": 200, "result": [{"hy": 114, "ht": 1, "stuno": "411000001"}]}).encode(),
                )
            if "ExportSemeTranscriptFile" in url:
                return HttpResponse(200, {"content-type": "application/pdf"}, b"%PDF-1.7\nok")
            return HttpResponse(200, {"content-type": "application/json"}, b'{"statusCode":200,"result":{}}')

        return SisClient(store=store, transport=transport)

    def test_cli_transcript_download_json(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                rc = cli_main(["transcript", "download", "--all", "--out", tmp, "--json"], client=client)
            finally:
                sys.stdout = old
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(len(payload["files"]), 1)
            self.assertTrue(Path(payload["files"][0]["path"]).is_file())

    def test_mcp_initialize_list_and_call(self):
        client = self._client()
        init = handle_message(client, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(init["result"]["serverInfo"]["name"], "fju-sis-mcp")
        listed = handle_message(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in listed["result"]["tools"]}
        self.assertIn("sis_transcript_download", names)
        with tempfile.TemporaryDirectory() as tmp:
            req = json.dumps({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "sis_transcript_download", "arguments": {"all": True, "out_dir": tmp}},
            }) + "\n"
            out = io.StringIO()
            serve_stdio(client, stdin=io.StringIO(req), stdout=out)
            reply = json.loads(out.getvalue().strip())
            self.assertFalse(reply["result"].get("isError", False))
            body = json.loads(reply["result"]["content"][0]["text"])
            self.assertEqual(len(body["files"]), 1)


class ExtendedFeaturesTests(unittest.TestCase):
    def _session(self):
        tok = make_jwt({"exp": int(time.time()) + 3600})
        return MemoryStore(Session(emp_no="411000001", auth_token=tok, exp=int(time.time()) + 3600))

    def test_enrollment_cert_download_defaults_to_current_semester(self):
        seen = []

        def transport(method, url, headers, body, timeout):
            seen.append(url)
            if "GetStuInfo" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({
                        "statusCode": 200,
                        "result": {
                            "hisStuStatusInfo": [
                                {"hy": 114, "ht": 1, "isReg": True, "isCurrent": False},
                                {"hy": 114, "ht": 2, "isReg": True, "isCurrent": True},
                            ]
                        },
                    }).encode(),
                )
            if "OnlineStuStatusCertApply/Download" in url:
                return HttpResponse(200, {"content-type": "application/pdf"}, b"%PDF-1.7\ncert")
            raise AssertionError(url)

        client = SisClient(store=self._session(), transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            res = client.download_enrollment_cert(tmp, lang="en")
            self.assertEqual(res["hy"], 114)
            self.assertEqual(res["ht"], 2)
            self.assertEqual(res["lang"], "en")
            self.assertTrue(Path(res["path"]).is_file())
            self.assertIn("version=2", seen[-1])
            self.assertIn("stuNO=411000001", seen[-1])

    def test_registration_and_course_search(self):
        seen = []

        def transport(method, url, headers, body, timeout):
            seen.append(url)
            if "/Register/api/NotFile/Info" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"statusCode": 200, "result": {"hy": 114, "ht": 2, "registerStatus": 1, "errorFields": []}}).encode(),
                )
            if "CourseStudentQueryByBasics" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"statusCode": 200, "result": {"totalItemCount": 1, "result": [{"couCNa": "相對論", "tchCNa": "王老師"}]}}).encode(),
                )
            if "CourseStudentQueryByGDPT" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"statusCode": 200, "result": {"totalItemCount": 1, "result": [{"couCNa": "人文通識"}]}}).encode(),
                )
            raise AssertionError(url)

        client = SisClient(store=self._session(), transport=transport)
        reg = client.get_registration_status()
        self.assertEqual(reg["registerStatus"], 1)
        c1 = client.search_courses(hy=114, ht=2, keyword="相對論")
        self.assertEqual(c1["result"][0]["couCNa"], "相對論")
        c2 = client.search_courses(hy=114, ht=2, general_edu=True)
        self.assertEqual(c2["result"][0]["couCNa"], "人文通識")

    def test_leave_draft_never_calls_confirm_and_uploads_attachment(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, url))
            if "/RefList/RefLeave" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"statusCode": 200, "result": [{"refLeaveSn": 3, "leaveCna": "病假", "docList": [{"refDocSn": 9}]}]}).encode(),
                )
            if "/Course/Section" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"statusCode": 200, "result": [{"sectNo": 1, "sectionNo": "D1", "sectionCna": "第1節"}, {"sectNo": 2, "sectionNo": "D2", "sectionCna": "第2節"}]}).encode(),
                )
            if "/Student/Contact" in url:
                return HttpResponse(200, {"content-type": "application/json"}, b'{"statusCode":200,"result":{"phoneNumber":"0900000000","emailAccount":"a@example.com"}}')
            if method == "POST" and url.endswith("/StuLeave"):
                self.assertIn(b"UploadFiles[0].uploadFile", body)
                return HttpResponse(200, {"content-type": "application/json"}, b'{"statusCode":200,"result":{"leaveApplySn":999}}')
            if method == "GET" and url.endswith("/StuLeave/999/SelCou"):
                return HttpResponse(200, {"content-type": "application/json"}, b'{"statusCode":200,"result":[{"jonCouSn":1,"tchNo":123}]}')
            if method == "POST" and url.endswith("/StuLeave/999/SelCou"):
                return HttpResponse(200, {"content-type": "application/json"}, b'{"statusCode":200,"result":true}')
            if url.endswith("/StuLeave/999/LeaveAlert"):
                return HttpResponse(200, {"content-type": "application/json"}, b'{"statusCode":200,"result":[]}')
            raise AssertionError(f"Unexpected call: {method} {url}")

        client = SisClient(store=self._session(), transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp) / "receipt.jpg"
            proof.write_bytes(b"\xff\xd8\xff\xe0fakejpg")
            res = client.create_leave_draft(
                hy=114,
                ht=2,
                leave_type="病假",
                begin_date="2026-11-25",
                end_date="2026-11-25",
                begin_section="D1",
                end_section="D2",
                reason="發燒看診",
                attachments=[proof],
            )
            self.assertEqual(res["leaveApplySn"], 999)
            self.assertFalse(res["submitted"])
            self.assertTrue(res["draft_only"])
            self.assertEqual(res["selected_courses"], 1)
            self.assertEqual(res["attachments"], 1)
            self.assertFalse(any("/Confirm" in u for _, u in calls))


if __name__ == "__main__":
    unittest.main()
