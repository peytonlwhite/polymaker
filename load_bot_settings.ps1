$settingsPath = Join-Path $PSScriptRoot "bot_settings.json"

if (Test-Path -LiteralPath $settingsPath) {
    try {
        $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
        foreach ($property in $settings.PSObject.Properties) {
            if ($null -ne $property.Value -and "$($property.Value)" -ne "") {
                [Environment]::SetEnvironmentVariable($property.Name, "$($property.Value)", "Process")
            }
        }
    } catch {
        Write-Warning "Could not load bot_settings.json: $($_.Exception.Message)"
    }
}
