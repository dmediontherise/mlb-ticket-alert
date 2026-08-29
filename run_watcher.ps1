# Run MLB Ticket Alert Watcher Daemon (Yankees & Mets)
Write-Host "Starting MLB Ticket Alert Watcher (Yankee Stadium & Citi Field under $50)..." -ForegroundColor Green
python C:\Users\mrlgp\projects\mlb-ticket-alert\monitor.py --daemon --interval 600
