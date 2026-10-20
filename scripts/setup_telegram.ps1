# Saves your Telegram bot token and chat ID so goldbot can send alerts.
# Run from PowerShell:  powershell -ExecutionPolicy Bypass -File C:\gbot\scripts\setup_telegram.ps1

$ErrorActionPreference = 'Stop'
Write-Host "`nGoldbot Telegram setup`n" -ForegroundColor Cyan
Write-Host "First, in Telegram: message @BotFather, send /newbot, and follow its questions."
Write-Host "It gives you a token like 123456789:AAH...`n"

while ($true) {
    $secure = Read-Host "Paste the token here (it stays hidden) and press Enter" -AsSecureString
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim().Trim('"')
    try {
        $me = Invoke-RestMethod "https://api.telegram.org/bot$token/getMe" -TimeoutSec 15
        Write-Host "Token works: your bot is @$($me.result.username)" -ForegroundColor Green
        break
    } catch {
        Write-Host "Telegram didn't accept that token. Copy it again from BotFather's message (the whole line with the colon) and retry." -ForegroundColor Yellow
    }
}

Write-Host "`nNow open @$($me.result.username) in Telegram, press Start (or send 'hi')."
$chatId = $null
while (-not $chatId) {
    Read-Host "Press Enter once you've sent it a message"
    $updates = Invoke-RestMethod "https://api.telegram.org/bot$token/getUpdates" -TimeoutSec 15
    $chat = $updates.result | ForEach-Object { $_.message.chat } | Where-Object { $_ } | Select-Object -Last 1
    if ($chat) { $chatId = $chat.id } else { Write-Host "No message from you yet. Send the bot a message and try again." -ForegroundColor Yellow }
}

[Environment]::SetEnvironmentVariable('TELEGRAM_BOT_TOKEN', $token, 'User')
[Environment]::SetEnvironmentVariable('TELEGRAM_CHAT_ID', "$chatId", 'User')
$body = @{ chat_id = $chatId; text = "[goldbot] Telegram is set up. You'll get trade alerts here." } | ConvertTo-Json
Invoke-RestMethod "https://api.telegram.org/bot$token/sendMessage" -Method Post -ContentType 'application/json' -Body $body | Out-Null
Write-Host "`nSaved. A test message was sent to your Telegram (chat $chatId)." -ForegroundColor Green
Write-Host "Tell Claude it's done so both bots get restarted and pick this up."
