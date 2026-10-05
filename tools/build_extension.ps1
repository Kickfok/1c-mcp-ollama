param(
    # Каталог файловой информационной базы для проверки расширения
    [Parameter(Mandatory = $true)][string]$Infobase,
    # Пользователь базы; пароль - переменная окружения MCP_1C_PASSWORD (в командной строке его не видно)
    [string]$User = "",
    # Путь к 1cv8.exe; по умолчанию - самая новая платформа в Program Files
    [string]$Platform = "",
    # Выгрузить собранное расширение в build/MCP_Сервер.cfe для релиза
    [switch]$Dump
)
# Загружает расширение MCP_Сервер из src/extension в информационную базу, проверяет модули во всех
# контекстах, обновляет базу и при -Dump выгружает build/MCP_Сервер.cfe.
#
#   powershell -File tools/build_extension.ps1 -Infobase C:\Bases\Test -User Администратор -Dump

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root "src\extension"
$extension = "MCP_Сервер"

if (-not $Platform) {
    $candidates = Get-ChildItem "$env:ProgramFiles\1cv8", "${env:ProgramFiles(x86)}\1cv8" -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName "bin\1cv8.exe") } |
        Sort-Object { [version]($_.Name -replace '[^0-9.]', '') } -Descending
    if (-not $candidates) { throw "Не найдена платформа 1С: укажите -Platform <путь к 1cv8.exe>" }
    $Platform = Join-Path $candidates[0].FullName "bin\1cv8.exe"
}

$logDir = Join-Path $root "build\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null

function Invoke-Designer([string]$Step, [string[]]$Arguments) {
    $log = Join-Path $logDir "$Step.log"
    $result = Join-Path $logDir "$Step.res"
    Remove-Item $log, $result -ErrorAction SilentlyContinue
    $auth = @("/F`"$Infobase`"", "/DisableStartupDialogs")
    if ($User) { $auth += "/N`"$User`"" }
    if ($env:MCP_1C_PASSWORD) { $auth += "/P`"$env:MCP_1C_PASSWORD`"" }
    $process = Start-Process $Platform -ArgumentList (@("DESIGNER") + $auth + $Arguments + @("/Out`"$log`"", "/DumpResult`"$result`"")) -Wait -PassThru
    $code = (Get-Content $result -ErrorAction SilentlyContinue | Select-Object -First 1)
    Get-Content $log -Encoding Default -ErrorAction SilentlyContinue | Where-Object { $_.Trim() } | ForEach-Object { "  $_" }
    if ("$code".Trim() -ne "0") { throw "$Step завершился с кодом $code (процесс $($process.ExitCode)); журнал: $log" }
    "OK  $Step"
}

Invoke-Designer "load" @("/LoadConfigFromFiles`"$source`"", "-Extension", $extension)
Invoke-Designer "check" @("/CheckModules", "-ThinClient", "-WebClient", "-Server", "-ExternalConnection", "-Extension", $extension)
Invoke-Designer "update" @("/UpdateDBCfg", "-Extension", $extension)
if ($Dump) {
    $cfe = Join-Path $root "build\$extension.cfe"
    Invoke-Designer "dump" @("/DumpCfg`"$cfe`"", "-Extension", $extension)
    "Расширение выгружено: $cfe"
}
