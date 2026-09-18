@echo off
rem ============================================================================
rem  repo-autopilot 插件：双击即可的一键安装
rem
rem  做三件事：
rem    1) 检查/补 Python 与依赖，校验随包自带的那份 repo-autopilot（逐文件 sha256）；
rem    2) 生成 host.local.js（把自带副本的绝对路径写进去）—— dynamic Package 用；
rem    3) 把本目录作为一个 **profile 层**装进 harness（`dsh plugin add`）—— 一键用这条。
rem
rem  装完**重开一个会话**即可看到 repo_autopilot_check 工具。
rem
rem  可用环境变量：
rem    DSH_PROFILE   要装进哪个 profile（默认 standard）
rem    DSH_BIN       dsh 可执行文件路径（默认去几个常见位置找）
rem ============================================================================
setlocal
set "HERE=%~dp0"
if "%DSH_PROFILE%"=="" set "DSH_PROFILE=standard"

echo === repo-autopilot 插件一键安装 ===
echo 插件目录：%HERE%
echo 目标 profile：%DSH_PROFILE%
echo.

rem ---- 1) 环境检查 + 完整性校验 + 生成 host.local.js ----
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%scripts\install.ps1" -EmitHost
if errorlevel 1 (
  echo.
  echo [!] 上面有缺件 —— 先按提示补上，再重新双击本文件。
  pause
  exit /b 1
)

rem ---- 2) 找到 dsh ----
if not "%DSH_BIN%"=="" goto have_dsh
for %%P in (
  "D:\dsh\runtime\dsh\node_modules\.bin\dsh.cmd"
  "%APPDATA%\npm\dsh.cmd"
) do if exist %%P set "DSH_BIN=%%~P"
if "%DSH_BIN%"=="" (
  where dsh >nul 2>nul && set "DSH_BIN=dsh"
)
:have_dsh
if "%DSH_BIN%"=="" (
  echo [!] 没找到 dsh。请设 DSH_BIN 指向它，例如：
  echo       set DSH_BIN=D:\dsh\runtime\dsh\node_modules\.bin\dsh.cmd
  pause
  exit /b 1
)
echo 使用 dsh：%DSH_BIN%

rem ---- 3) 装成 profile 层 ----
echo.
echo === 装进 profile（dsh plugin add）===
call "%DSH_BIN%" plugin --profile "%DSH_PROFILE%" add "%HERE%."
if errorlevel 1 (
  echo.
  echo [!] 安装失败。常见原因与处理：
  echo     * 权限：装 profile 要写 %%DSH_HOME%%\profiles —— 在 DSH 会话里跑会被沙箱拦，
  echo       请直接在资源管理器里双击本文件（或在一个普通终端里运行）。
  echo     * 网络：pnpm 需要能访问 registry；离线时可改用「复制目录 + 手改 profile 清单」。
  pause
  exit /b 1
)

echo.
echo === 装好了 ===
echo   1) 重开一个会话（或重启 harness），让它按新 profile 装载；
echo   2) 在会话里应当能看到工具 repo_autopilot_check，试一句：
echo        repo_autopilot_check(mode='doctor')
echo.
echo   想卸载：call "%DSH_BIN%" plugin --profile "%DSH_PROFILE%" remove repo-autopilot-plugin
pause
endlocal
