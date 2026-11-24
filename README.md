# FJU-SIS-MCP

輔仁大學學生資訊平台（[sis.fju.edu.tw](https://sis.fju.edu.tw/#/)）CLI 與 MCP Server。

零外部相依套件（僅使用 Python 標準函式庫），支援查詢歷年各科成績、各學期排名與下載官方數位成績單 PDF。

## 功能

- **登入管理**：使用輔大 LDAP 帳號登入，取得 `auth_token` 與 `refresh_token` 後存入系統認證管理員，**不保存明文密碼**；權杖到期時自動續期。
- **歷年成績查詢**：查詢所有學期或指定學年度／學期的各科成績與學分。
- **學期排名與平均**：查詢各學期平均成績、班級排名與系／年級排名。
- **官方數位成績單 PDF 下載**：列出所有可下載學期，並直接下載官方數位成績單 PDF（支援含排名／不含排名版本，自動檢查 `%PDF-` 檔案標頭）。
- **MCP Server**：內建 stdio JSON-RPC 2.0 MCP Server（`fjusis serve`），可供 AI 代理直接呼叫。

## 安裝

需要 Python 3.10+：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## 登入與安全性

- **Windows**：權杖僅儲存在 Windows Credential Manager（`FJU-SIS-MCP/session`），不寫入明文檔案或設定檔。
- **Linux / macOS**：儲存在 `~/.config/fju-sis/session.json`（權限 `0600`）。
- 本工具不接受 `--password` 命令列參數，避免密碼殘留在終端機歷史紀錄。

```powershell
# 互動式輸入學號與密碼（密碼不回顯、不保存）
fjusis login

# 驗證登入狀態
fjusis whoami

# 清除本機登入狀態
fjusis logout
```

## 常用指令

```powershell
# 查詢歷年各科成績（可加 --hy 114 --ht 2 篩選，或加 --json）
fjusis grades

# 查詢各學期平均與排名
fjusis ranks

# 列出所有可下載的數位成績單
fjusis transcript list

# 下載全部學期的官方數位成績單 PDF（預設含排名）
fjusis transcript download --all --out ./transcripts

# 下載指定學期、不含排名版本
fjusis transcript download --hy 114 --ht 2 --no-ranking --out ./transcripts
```

## 作為 MCP Server 使用

```powershell
fjusis serve
```

提供以下唯讀工具：
- `sis_whoami`
- `sis_grades`
- `sis_ranks`
- `sis_transcript_list`
- `sis_transcript_download`

## 執行測試

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
