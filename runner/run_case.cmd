@echo off
rem Windows launcher for run_case.sh. The worker execs the runner by path on
rem Windows (no bash on PATH is assumed), so this finds one and hands over --
rem stdin (the case spec) and every environment variable pass straight through,
rem and the exit code comes straight back, which is the whole contract.
rem
rem Bash candidates, in order: %BASH% if set, Git for Windows, blueCFD's MSYS2.
rem Git's bash is preferred even for the native blueCFD runtime -- run_case.sh
rem builds blueCFD's environment itself (see bluecfd_env), so it needs a bash
rem with cygpath and tar, not blueCFD's shell.
setlocal
if defined BASH goto :run
if exist "%ProgramFiles%\Git\bin\bash.exe" set "BASH=%ProgramFiles%\Git\bin\bash.exe" & goto :run
if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" set "BASH=%ProgramFiles(x86)%\Git\bin\bash.exe" & goto :run
if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "BASH=%LocalAppData%\Programs\Git\bin\bash.exe" & goto :run
if exist "C:\blueCFD-Core-2024\msys64\usr\bin\bash.exe" set "BASH=C:\blueCFD-Core-2024\msys64\usr\bin\bash.exe" & goto :run
echo run_case.cmd: no bash found (set BASH, or install Git for Windows) 1>&2
exit /b 1
:run
"%BASH%" "%~dp0run_case.sh"
exit /b %ERRORLEVEL%
