@echo off
rem ============================================================================
rem  repo-autopilot 插件：双击即可的一键安装
rem
rem  依次做：
rem    1) 找 Python、查版本与依赖；逐文件 sha256 校验随包自带的那份 repo-autopilot；
rem    2) 生成 host.local.js（dynamic Package 那条路用）；
rem    3) 装进 dsh profile —— 先走包管理器（A），失败自动退回免包管理器（B）。
rem
rem  装完**重开一个会话**即可看到 repo_autopilot_check 工具。
rem
rem  可用环境变量：
rem    DSH_PROFILE   装进哪个 profile（默认 web，即启动命令里的 --profile）
rem    DSH_HOME      默认 D:\dsh\home（由 harness 设置）
rem ============================================================================
setlocal
set "HERE=%~dp0"
if "%DSH_PROFILE%"=="" set "DSH_PROFILE=web"

echo === repo-autopilot 插件一键安装 ===
echo 插件目录：%HERE%
echo 目标 profile：%DSH_PROFILE%
echo.

rem 环境检查 + 完整性校验 + 生成 host.local.js + 装进 profile，全在 install.ps1 / install_profile.py 里。
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%scripts\install.ps1" -EmitHost -InstallProfile
if errorlevel 1 (
  echo.
  echo [!] 没装成。常见原因与处理：
  echo     * 缺件：按上面的提示补 Python / 依赖 / 仓库；
  echo     * 权限：装 profile 要写 %%DSH_HOME%%\profiles —— 在 DSH 会话里跑会被沙箱拦，
  echo       请在资源管理器里双击本文件，或在一个普通终端里运行；
  echo     * 想强制走免包管理器那条路：
  echo       python scripts\install_profile.py --profile %%DSH_PROFILE%% --method bundle
  pause
  exit /b 1
)

echo.
echo === 装好了 ===
echo   1) 重开一个会话（或重启 harness），让它按新 profile 装载；
echo   2) 会话里试一句：repo_autopilot_check(mode='doctor')
pause
endlocal
