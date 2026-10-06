$ErrorActionPreference = 'Stop'
$compilerFinder = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $compilerFinder)) { throw 'Install Microsoft C++ Build Tools first.' }
$compilerInstall = & $compilerFinder -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $compilerInstall) { throw 'MSVC x64 toolchain was not found.' }
$compilerVersion = (Get-Content -LiteralPath (Join-Path $compilerInstall 'VC\Auxiliary\Build\Microsoft.VCToolsVersion.default.txt')).Trim()
$compilerRoot = Join-Path $compilerInstall ('VC\Tools\MSVC\' + $compilerVersion)
$sdkRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10'
$sdkVersion = Get-ChildItem -LiteralPath (Join-Path $sdkRoot 'Include') -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'um\Windows.h') } | Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty Name
if (-not $sdkVersion) { throw 'Windows SDK was not found.' }
$env:INCLUDE = (Join-Path $compilerRoot 'include') + ';' + (Join-Path $sdkRoot "Include\$sdkVersion\ucrt") + ';' + (Join-Path $sdkRoot "Include\$sdkVersion\shared") + ';' + (Join-Path $sdkRoot "Include\$sdkVersion\um")
$env:LIB = (Join-Path $compilerRoot 'lib\x64') + ';' + (Join-Path $sdkRoot "Lib\$sdkVersion\ucrt\x64") + ';' + (Join-Path $sdkRoot "Lib\$sdkVersion\um\x64")
Push-Location $PSScriptRoot
try {
    New-Item -ItemType Directory -Force -Path 'build' | Out-Null
    & (Join-Path $compilerRoot 'bin\Hostx64\x64\cl.exe') /nologo /LD /EHsc /std:c++17 /utf-8 /D_WIN32_WINNT=0x0A00 /DWINVER=0x0A00 bridge.cpp /Fobuild/ /Fe:build/wechat_twin_native.dll /link bcrypt.lib
    if ($LASTEXITCODE -ne 0) { throw "Native bridge build failed: $LASTEXITCODE" }
    & (Join-Path $compilerRoot 'bin\Hostx64\x64\cl.exe') /nologo /EHsc /std:c++17 /utf-8 /D_WIN32_WINNT=0x0A00 /DWINVER=0x0A00 ../app/native/account_probe.cpp /Fobuild/account_probe.obj /Fe:build/account_probe.exe
    if ($LASTEXITCODE -ne 0) { throw "Account probe build failed: $LASTEXITCODE" }
} finally { Pop-Location }
