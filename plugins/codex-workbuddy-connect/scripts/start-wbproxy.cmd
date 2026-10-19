@echo off
rem 启动 WorkBuddy -> Codex 本地代理（平时不需要，codex-wb.cmd 会自动拉起）
rem 本文件必须保持 GBK(cp936) + CRLF。
setlocal
if not defined WB_KEEP_PROXY (
    set "HTTP_PROXY="
    set "HTTPS_PROXY="
    set "ALL_PROXY="
    set "http_proxy="
    set "https_proxy="
    set "all_proxy="
    set "NO_PROXY=127.0.0.1,localhost,::1"
    set "no_proxy=127.0.0.1,localhost,::1"
)
call "%~dp0_findpy.cmd"
if not defined PYEXE (
    echo [wb] 找不到 python 解释器。请安装 Python 3.10+，或设置 WORKBUDDY_AI_PYTHON。
    exit /b 1
)
"%PYEXE%" "%~dp0..\bin\wbproxy.py" %*
