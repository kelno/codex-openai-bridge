# Remove the old rule
netsh interface portproxy delete v4tov4 listenport=8000 listenaddress=0.0.0.0

# Get the WSL IP address
 = wsl hostname -I | ForEach-Object { /bin/bash.Split(" ")[0] }

# Add the new rule
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8000 connectaddress= connectport=8000
