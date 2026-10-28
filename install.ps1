# Download with Invoke-RestMethod, or run locally. Requires Python 3.11+.
# Settings are environment variables so `irm ... | iex` works too; see README.
& {
    $ErrorActionPreference = 'Stop'
    $probe = 'import sys; print(sys.executable) if sys.version_info >= (3, 11) else sys.exit(1)'
    $python = $null
    $candidates = if ($env:VENASTINE_PYTHON) { @($env:VENASTINE_PYTHON) } else { @('py', 'python', 'python3') }
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        $probeArgs = if ($candidate -eq 'py') { @('-3', '-c', $probe) } else { @('-c', $probe) }
        try {
            $found = & $candidate @probeArgs 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { $python = ($found | Select-Object -Last 1).Trim(); break }
        } catch { continue }
    }
    if (-not $python) { throw 'Install Python 3.11+, or set VENASTINE_PYTHON to its executable, then run this installer again.' }
    $helper = Join-Path ([System.IO.Path]::GetTempPath()) ('venastine-install-' + [guid]::NewGuid().ToString('N') + '.py')
    $oldProtocol = [Net.ServicePointManager]::SecurityProtocol
    try {
        [Net.ServicePointManager]::SecurityProtocol = $oldProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/YahyaHammad/Venastine-Research-Harness/main/scripts/install.py' -OutFile $helper
        & $python $helper @args
        if ($LASTEXITCODE -ne 0) { throw "Venastine installation failed (exit $LASTEXITCODE)." }
    } finally {
        [Net.ServicePointManager]::SecurityProtocol = $oldProtocol
        if (Test-Path -LiteralPath $helper) { Remove-Item -LiteralPath $helper -Force }
    }
} @args
