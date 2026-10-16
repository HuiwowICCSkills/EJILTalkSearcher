# Locate a Python that can run this skill, then hand off to
# scripts/ejiltalk_search.py.  beautifulsoup4 is the only Python dependency;
# Node.js 22+ and Chrome are needed only for the headless-browser fallback.
$ejilCandidates = @(
    (Join-Path $PSScriptRoot 'venv\Scripts\python.exe'),
    (Join-Path $PSScriptRoot 'venv/bin/python'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
    (Join-Path $env:USERPROFILE '.workbuddy\binaries\python\versions\3.13.12\python.exe')
)
$ejilPathPython = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($ejilPathPython) { $ejilCandidates += $ejilPathPython }
$ejilPathPython3 = (Get-Command python3 -ErrorAction SilentlyContinue).Source
if ($ejilPathPython3) { $ejilCandidates += $ejilPathPython3 }

$ejilPython = $null
foreach ($ejilCandidate in $ejilCandidates) {
    if (-not $ejilCandidate -or -not (Test-Path -LiteralPath $ejilCandidate -PathType Leaf)) { continue }
    & $ejilCandidate -c 'import bs4' 2>$null
    if ($LASTEXITCODE -eq 0) { $ejilPython = $ejilCandidate; break }
}
if (-not $ejilPython) {
    throw 'No Python with beautifulsoup4 was found. Run: python -m pip install -r requirements.txt (or create venv/).'
}
$ejilArguments = foreach ($ejilArgument in $args) {
    if ($ejilArgument -is [array]) { $ejilArgument -join ',' } else { $ejilArgument }
}
& $ejilPython -X utf8 (Join-Path $PSScriptRoot 'scripts\ejiltalk_search.py') $ejilArguments
exit $LASTEXITCODE
