@echo off
rem Run tubedigest with the project's virtualenv python, from any folder.
rem Example: C:\DEV\tube\digest\td add "@handle"
pushd "%~dp0"
".venv\Scripts\python.exe" -m tubedigest %*
set rc=%errorlevel%
popd
exit /b %rc%
