# Set up ClassDojo Photos on Windows:
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/deanhtid/dojo-archive/main/install.ps1 | iex"
# DOJO_ARCHIVE_SOURCE replaces the download link, and DOJO_ARCHIVE_NO_RUN=1 skips the first run.
# Invoke-Expression runs this in the parent's own window, so it uses a script block and never calls exit.
& {
    $ErrorActionPreference = 'Stop'
    $source = $env:DOJO_ARCHIVE_SOURCE
    if (-not $source) { $source = 'https://github.com/deanhtid/dojo-archive/archive/refs/heads/main.zip' }

    Write-Host 'Setting up ClassDojo Photos.'
    if (-not (Get-Command uvx -ErrorAction SilentlyContinue)) {
        Write-Host 'Installing a helper program. This takes a minute.'
        powershell -NoProfile -ExecutionPolicy ByPass -Command 'irm https://astral.sh/uv/install.ps1 | iex' | Out-Null
        $bin = if ($env:XDG_BIN_HOME) { $env:XDG_BIN_HOME } else { Join-Path $env:USERPROFILE '.local\bin' }
        $env:Path = "$bin;$env:Path"
        if (-not (Get-Command uvx -ErrorAction SilentlyContinue)) {
            Write-Host 'Could not install the helper program. Check the internet connection, then paste the line again.'
            return
        }
    }
    $uvx = (Get-Command uvx).Source
    $arguments = @('--python', '3.15', '--from', $source, 'dojo-archive')

    # A batch file reads the OEM code page, so the user's folder name stays out of it.
    $uvxInBatch = "`"" + $uvx.Replace($env:USERPROFILE, '%USERPROFILE%') + "`""
    $quotedArguments = ($arguments | ForEach-Object { "`"$_`"" }) -join ' '
    # The launcher updates quietly first, then runs from the saved copy, so a computer with no
    # internet still starts the tool, which then explains the problem in plain words.
    $update = "$uvxInBatch --quiet --refresh-package dojo-archive $quotedArguments --version >nul 2>&1"
    $run = "$uvxInBatch --quiet --offline $quotedArguments --pause %*"
    $stopped = 'echo ClassDojo Photos could not start. Check the internet connection, then try again.'
    $lines = @('@echo off', $update, $run, 'if errorlevel 1 (', $stopped, 'pause', ')')
    $launcher = Join-Path ([Environment]::GetFolderPath('Desktop')) 'ClassDojo Photos.bat'
    Set-Content -LiteralPath $launcher -Value $lines -Encoding Ascii
    Write-Host 'Added ClassDojo Photos to your Desktop. Double-click it each time you want new photos.'

    if (-not $env:DOJO_ARCHIVE_NO_RUN) {
        Write-Host 'Starting the first run now. The first time can take a few minutes.'
        & $uvx --quiet --refresh-package dojo-archive @arguments
    }
}
