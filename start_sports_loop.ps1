$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

. .\load_bot_settings.ps1

function Set-BotProcessStatus {
    param([bool]$Running)
    $mutex = [System.Threading.Mutex]::new($false, "Local\PolymakerBotProcessStatus")
    $locked = $false
    try {
        $locked = $mutex.WaitOne(10000)
        if (-not $locked) { throw "Timed out waiting for bot process status lock" }
        $path = Join-Path $PSScriptRoot "bot_processes.json"
        if (Test-Path -LiteralPath $path) {
            try { $data = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } catch { $data = [pscustomobject]@{} }
        } else { $data = [pscustomobject]@{} }
        if (-not ($data.PSObject.Properties.Name -contains "sports")) {
            $data | Add-Member -NotePropertyName "sports" -NotePropertyValue ([pscustomobject]@{})
        }
        $data.sports = [pscustomobject]@{
            pid = $PID
            running = $Running
            script = "start_sports_loop.ps1"
            started_at = if ($Running) { (Get-Date).ToString("s") } else { $data.sports.started_at }
            stopped_at = if ($Running) { $null } else { (Get-Date).ToString("s") }
        }
        $data | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $path -Encoding UTF8
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

if (-not $env:SPORTS_EXECUTION_MODE) {
    $env:SPORTS_EXECUTION_MODE = "paper"
}

if (-not $env:ALLOW_LIVE_TRADING) {
    $env:ALLOW_LIVE_TRADING = "false"
}

$env:SPORTS_RUN_LOOP = "true"

if (-not $env:SPORTS_STARTING_BALANCE) {
    $env:SPORTS_STARTING_BALANCE = "300"
}

if (-not $env:SPORTS_SCAN_INTERVAL_MINUTES) {
    $env:SPORTS_SCAN_INTERVAL_MINUTES = "5"
}

if (-not $env:SPORTS_QUIET_HOURS_ENABLED) {
    $env:SPORTS_QUIET_HOURS_ENABLED = "true"
}

if (-not $env:SPORTS_QUIET_START_HOUR) {
    $env:SPORTS_QUIET_START_HOUR = "21"
}

if (-not $env:SPORTS_QUIET_END_HOUR) {
    $env:SPORTS_QUIET_END_HOUR = "9"
}

if (-not $env:SPORTS_ODDS_CACHE_MAX_HOURS) {
    $env:SPORTS_ODDS_CACHE_MAX_HOURS = "6"
}

if (-not $env:SPORTS_KEYS) {
    $env:SPORTS_KEYS = "baseball_mlb,baseball_npb,baseball_kbo,basketball_nba,basketball_wnba,basketball_ncaab,americanfootball_ncaaf,americanfootball_nfl_preseason,americanfootball_nfl,icehockey_nhl,tennis_atp,tennis_wta,cricket_asia_cup,cricket_big_bash,cricket_caribbean_premier_league,cricket_icc_trophy,cricket_icc_world_cup,cricket_icc_world_cup_womens,cricket_international_t20,cricket_ipl,cricket_odi,cricket_psl,cricket_t20_blast,cricket_t20_world_cup,cricket_t20_world_cup_womens,cricket_the_hundred,cricket_the_hundred_womens"
}

if (-not $env:SPORTS_ALL_ACTIVE_ENABLED) {
    $env:SPORTS_ALL_ACTIVE_ENABLED = "true"
}

if (-not $env:SPORTS_ALL_ACTIVE_INCLUDE_OUTRIGHTS) {
    $env:SPORTS_ALL_ACTIVE_INCLUDE_OUTRIGHTS = "false"
}

if (-not $env:SPORTS_ALL_ACTIVE_EXCLUDED_GROUPS) {
    $env:SPORTS_ALL_ACTIVE_EXCLUDED_GROUPS = "Politics"
}

if (-not $env:SPORTS_DYNAMIC_KALSHI_SERIES_ENABLED) {
    $env:SPORTS_DYNAMIC_KALSHI_SERIES_ENABLED = "true"
}

if (-not $env:SPORTS_KALSHI_SERIES_PREFIXES) {
    $env:SPORTS_KALSHI_SERIES_PREFIXES = "KXMLB,KXNPB,KXKBO,KXNBA,KXWNBA,KXNCAAMB,KXNCAAB,KXNCAAF,KXCFB,KXNFL,KXNHL,KXATP,KXWTA,KXCPL,KXCRICKETT20I,KXCRICKETWOMENT20I,KXT20MATCH,KXIPL,KXCRICKETODI,KXCRICKETWOMENODI,KXODIMATCH,KXPSL,KXHUNDRED,KXWHUNDRED"
}

if (-not $env:SPORTS_KALSHI_MARKET_SERIES) {
    $env:SPORTS_KALSHI_MARKET_SERIES = "KXMLBGAME,KXMLBSPREAD,KXMLBTOTAL,KXMLBRFI,KXNPBGAME,KXNPBTOTAL,KXKBOGAME,KXKBOTOTAL,KXNBAGAME,KXNBASPREAD,KXNBATOTAL,KXWNBAGAME,KXWNBASPREAD,KXWNBATOTAL,KXNCAAMBGAME,KXNCAAMBSPREAD,KXNCAAMBTOTAL,KXNCAABGAME,KXNCAABSPREAD,KXNCAABTOTAL,KXNCAAFGAME,KXNCAAFSPREAD,KXNCAAFTOTAL,KXCFBGAME,KXCFBSPREAD,KXCFBTOTAL,KXNFLGAME,KXNFLSPREAD,KXNFLTOTAL,KXNHLGAME,KXNHLSPREAD,KXNHLTOTAL,KXATPMATCH,KXATPGAME,KXATPCHALLENGERMATCH,KXCHALLENGERMATCH,KXATPDOUBLES,KXATPGTOTAL,KXATPGAMETOTAL,KXWTAMATCH,KXWTAGAME,KXWTACHALLENGERMATCH,KXWTADOUBLES,KXWTAGTOTAL,KXCPLMATCH,KXCRICKETT20IMATCH,KXCRICKETWOMENT20IMATCH,KXT20MATCH,KXIPLGAME,KXCRICKETODIMATCH,KXCRICKETWOMENODIMATCH,KXODIMATCH,KXPSLGAME,KXHUNDREDMATCH,KXWHUNDREDMATCH"
}

if (-not $env:SPORTS_KALSHI_DATE_MAX_DAYS) {
    $env:SPORTS_KALSHI_DATE_MAX_DAYS = "1"
}

if (-not $env:BOT_PICKS_ENABLED) {
    $env:BOT_PICKS_ENABLED = "true"
}

if (-not $env:BOT_PICKS_REFRESH_HOURS) {
    $env:BOT_PICKS_REFRESH_HOURS = "12"
}

if (-not $env:BOT_PICKS_SCHEDULE_ENABLED) {
    $env:BOT_PICKS_SCHEDULE_ENABLED = "true"
}

if (-not $env:BOT_PICKS_MORNING_HOUR) {
    $env:BOT_PICKS_MORNING_HOUR = "10"
}

if (-not $env:BOT_PICKS_AFTERNOON_HOUR) {
    $env:BOT_PICKS_AFTERNOON_HOUR = "14"
}

if (-not $env:BOT_PICKS_STAGGER_MINUTES) {
    $env:BOT_PICKS_STAGGER_MINUTES = "10"
}

if (-not $env:SPORTS_BOT_PICK_MIN_CONFIDENCE) {
    $env:SPORTS_BOT_PICK_MIN_CONFIDENCE = "0.55"
}

if (-not $env:SPORTS_BOT_PICK_ALLOW_TRACKING) {
    $env:SPORTS_BOT_PICK_ALLOW_TRACKING = "true"
}

if (-not $env:SPORTS_BOT_PICK_ALWAYS_PAPER) {
    $env:SPORTS_BOT_PICK_ALWAYS_PAPER = "true"
}

if (-not $env:SPORTS_BOT_PICK_MARKETLESS_PAPER) {
    $env:SPORTS_BOT_PICK_MARKETLESS_PAPER = "true"
}

if (-not $env:SPORTS_BOT_PICK_WATCH_BETTER_LINES) {
    $env:SPORTS_BOT_PICK_WATCH_BETTER_LINES = "true"
}

if (-not $env:SPORTS_BOT_PICK_MAX_LINE_IMPROVEMENT) {
    $env:SPORTS_BOT_PICK_MAX_LINE_IMPROVEMENT = "2.5"
}

if (-not $env:SPORTS_STALE_OPEN_HOURS) {
    $env:SPORTS_STALE_OPEN_HOURS = "36"
}

if (-not $env:SPORTS_LIVE_ORDER_ENABLED) {
    $env:SPORTS_LIVE_ORDER_ENABLED = "false"
}

if (-not $env:SPORTS_LIVE_TIME_IN_FORCE) {
    $env:SPORTS_LIVE_TIME_IN_FORCE = "immediate_or_cancel"
}

if (-not $env:SPORTS_LIVE_MAX_PRICE_CENTS) {
    $env:SPORTS_LIVE_MAX_PRICE_CENTS = "70"
}

if (-not $env:SPORTS_ODDS_DAILY_CREDIT_LIMIT) {
    $env:SPORTS_ODDS_DAILY_CREDIT_LIMIT = "0"
}

$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

function Test-SportsPythonRuntime {
    param([Parameter(Mandatory = $true)][string]$PythonExecutable)
    # Windows PowerShell turns native stderr into an ErrorRecord. Temporarily
    # keep that expected import failure non-terminating while it is captured;
    # the launcher itself remains stop-on-error everywhere else.
    $priorErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $preflightOutput = & $PythonExecutable -c "import websockets; print('Sports runtime preflight OK: websockets=' + websockets.__version__)" 2>&1
        $preflightOk = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $priorErrorActionPreference
    }
    if ($preflightOk) { $preflightOutput | Write-Output }
    return $preflightOk
}

if (Test-Path -LiteralPath $bundledPython) {
    if (Test-SportsPythonRuntime -PythonExecutable $bundledPython) {
        Set-BotProcessStatus $true
        & $bundledPython sports_paper_bettor.py
        $exitCode = $LASTEXITCODE
        Set-BotProcessStatus $false
        exit $exitCode
    }
    Write-Warning "Bundled Python is missing the Sports runtime dependencies; trying the system Python."
}

$python = (Get-Command python -ErrorAction SilentlyContinue)
if ($python) {
    if (Test-SportsPythonRuntime -PythonExecutable $python.Source) {
        Set-BotProcessStatus $true
        & $python.Source sports_paper_bettor.py
        $exitCode = $LASTEXITCODE
        Set-BotProcessStatus $false
        exit $exitCode
    }
}

$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) {
    if (Test-SportsPythonRuntime -PythonExecutable $py.Source) {
        Set-BotProcessStatus $true
        & $py.Source sports_paper_bettor.py
        $exitCode = $LASTEXITCODE
        Set-BotProcessStatus $false
        exit $exitCode
    }
}

throw "Could not find a Python runtime with the Sports dependencies installed. Run: python -m pip install -r requirements.txt"
