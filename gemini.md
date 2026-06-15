The Project StructureCreate a folder on your computer called treefrog-streams. Inside, create two subfolders: backend and frontend. Your folder structure should look exactly like this:Plaintexttreefrog-streams/
├── frontend/
│   ├── index.html        (The public website)
│   ├── worker.js         (The Cloudflare Edge router)
│   └── wrangler.toml     (Cloudflare config)
└── backend/
    ├── docker-compose.yml(Proxmox deploy file)
    ├── requirements.txt  (Python dependencies)
    └── engine.py         (The health & failover script)
Here is the exact code for every file.Part 1: The Cloudflare Edge (Frontend)This handles the public website and the /s/ link redirects. It costs nothing to run and handles infinite traffic.1. frontend/wrangler.tomlThis tells Cloudflare how to deploy your project. You will need to replace the id with your actual Cloudflare KV Namespace ID once you create it in the Cloudflare dashboard.Ini, TOMLname = "treefrog-streams"
main = "worker.js"
compatibility_date = "2024-06-15"

[site]
bucket = "./" # Serves index.html from this folder

[[kv_namespaces]]
binding = "STREAM_KV"
id = "YOUR_KV_NAMESPACE_ID_HERE"
2. frontend/worker.jsThis is the Edge router. If someone visits /s/abc, it instantly checks KV and redirects them. If they visit the root domain, it serves the website.JavaScriptexport default {
  async fetch(request, env) {
    const url = new URL(request.url);
    
    // 1. Handle Stream Redirects
    if (url.pathname.startsWith('/s/')) {
      const token = url.pathname.split('/')[2];
      
      // Look up the active stream URL from Cloudflare KV
      const targetStreamUrl = await env.STREAM_KV.get(token);

      if (!targetStreamUrl) {
        return new Response('Stream Offline or Invalid Token', { status: 404 });
      }

      // Fast 302 Redirect to the actual source URL
      return new Response(null, {
        status: 302,
        headers: {
          'Location': targetStreamUrl,
          'Cache-Control': 'no-cache, no-store',
          'Access-Control-Allow-Origin': '*'
        }
      });
    }

    // 2. Serve the Static Website (index.html)
    return env.ASSETS.fetch(request);
  }
};
3. frontend/index.htmlA clean, modern, dark-mode MVP interface using Tailwind CSS. It requires zero build tools—just open it in a browser.HTML<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tree Frog Streams</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = { darkMode: 'class', theme: { extend: { colors: { frog: '#22c55e' } } } }
    </script>
</head>
<body class="bg-gray-900 text-white font-sans antialiased selection:bg-frog selection:text-white">
    
    <nav class="border-b border-gray-800 bg-gray-900/50 backdrop-blur-md sticky top-0 z-50">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
            <div class="flex items-center gap-2">
                <span class="text-2xl">🐸</span>
                <span class="font-bold text-xl tracking-tight">Tree Frog <span class="text-frog">Streams</span></span>
            </div>
            <a href="/playlist.m3u" class="bg-gray-800 hover:bg-gray-700 text-sm font-medium px-4 py-2 rounded-lg transition-colors">
                Download M3U
            </a>
        </div>
    </nav>

    <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12">
        <div class="text-center mb-12">
            <h1 class="text-4xl md:text-5xl font-extrabold tracking-tight mb-4">Clean, Reliable Live TV.</h1>
            <p class="text-gray-400 text-lg max-w-2xl mx-auto">100% free, family-friendly streams, monitored every 30 minutes for ultimate reliability.</p>
        </div>

        <!-- Search Bar -->
        <div class="max-w-xl mx-auto mb-12">
            <input type="text" placeholder="Search channels (e.g., PBS, Weather)..." 
                   class="w-full bg-gray-800 border border-gray-700 rounded-xl px-5 py-4 text-white focus:outline-none focus:ring-2 focus:ring-frog transition-all placeholder-gray-500 shadow-lg">
        </div>

        <!-- Channel Grid MVP -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
            <!-- Channel Card Example -->
            <div class="bg-gray-800 rounded-xl p-4 border border-gray-700 hover:border-frog transition-colors cursor-pointer group">
                <div class="aspect-video bg-gray-900 rounded-lg mb-3 flex items-center justify-center">
                    <span class="text-gray-500 group-hover:text-frog transition-colors">News Logo</span>
                </div>
                <h3 class="font-semibold truncate">Local News Network</h3>
                <p class="text-xs text-gray-400 mt-1 flex items-center gap-1">
                    <span class="w-2 h-2 rounded-full bg-green-500"></span> 99.9% Uptime
                </p>
            </div>
            <!-- Add more static cards here for the MVP -->
        </div>
    </main>

</body>
</html>
Part 2: The Proxmox Engine (Backend)This runs securely in a Docker container on your Proxmox server. It checks stream health and pushes the working URLs directly to Cloudflare.4. backend/docker-compose.ymlYAMLversion: '3.8'

services:
  tf-engine:
    image: python:3.11-slim
    container_name: treefrog-engine
    restart: unless-stopped
    volumes:
      - ./:/app
    working_dir: /app
    environment:
      - CF_API_TOKEN=your_cloudflare_api_token
      - CF_ACCOUNT_ID=your_cloudflare_account_id
      - CF_KV_NAMESPACE=your_kv_namespace_id
    command: /bin/sh -c "pip install -r requirements.txt && python engine.py"
5. backend/requirements.txtPlaintextrequests==2.31.0
schedule==1.2.1
6. backend/engine.pyThis is the core Python worker. It simulates the database check, pings the streams, and pushes the winner to Cloudflare.Pythonimport os
import requests
import schedule
import time

# Cloudflare Credentials from Environment Variables
CF_API_TOKEN = os.getenv("CF_API_TOKEN")
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_KV_NAMESPACE = os.getenv("CF_KV_NAMESPACE")

# MVP Dummy Database: Mapping short tokens to potential stream sources
CHANNELS = {
    "news1": [
        "https://example.com/primary-news.m3u8", # Primary
        "https://example.com/backup-news.m3u8"   # Backup
    ]
}

def check_stream(url):
    """Pings a stream URL to see if it responds."""
    try:
        # Just grab the headers to save bandwidth
        response = requests.head(url, timeout=5)
        return response.status_code == 200
    except requests.RequestException:
        return False

def update_cloudflare_kv(token, actual_url):
    """Pushes the working URL directly to Cloudflare KV."""
    if not all([CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_NAMESPACE]):
        print("Missing Cloudflare credentials. Skipping KV update.")
        return

    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE}/values/{token}"
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "text/plain"
    }
    
    response = requests.put(url, headers=headers, data=actual_url)
    if response.status_code == 200:
        print(f"✅ Success: Updated /{token} -> {actual_url}")
    else:
        print(f"❌ Failed to update CF KV for {token}")

def run_health_checks():
    """Loops through channels, finds the first working stream, and updates KV."""
    print("\n--- Running Health Checks ---")
    for token, streams in CHANNELS.items():
        working_stream = None
        
        for stream_url in streams:
            print(f"Checking {stream_url}...")
            if check_stream(stream_url):
                working_stream = stream_url
                break # Stop at the first working stream (Primary)
        
        if working_stream:
            update_cloudflare_kv(token, working_stream)
        else:
            print(f"⚠️ ALL STREAMS OFFLINE FOR: {token}")

if __name__ == "__main__":
    print("🐸 Tree Frog Engine Started!")
    # Run immediately on startup
    run_health_checks()
    
    # Schedule to run every 30 minutes
    schedule.every(30).minutes.do(run_health_checks)
    
    while True:
        schedule.run_pending()
        time.sleep(1)
Deployment StepsOrder matters here to ensure the backend can talk to the frontend correctly.1.Configure Cloudflare:Create KV and get API keys.Log into Cloudflare, go to Workers & Pages > KV, and create a namespace called treefrog-streams. Write down the Namespace ID. Then, go to your profile settings and create an API Token with "Workers KV Storage" edit permissions.2.Deploy Frontend:Requires Node.js installed.Open your terminal, navigate to the frontend folder, and update the wrangler.toml file with your Namespace ID. Run npm install -g wrangler, log in with wrangler login, and deploy the site by typing wrangler deploy.3.Configure Backend:Update Docker environment variables.Open the backend/docker-compose.yml file. Replace the placeholder values under environment with your real Cloudflare Account ID, API Token, and KV Namespace ID.4.Launch the Engine:Proxmox / Docker command.Move the backend folder to your Proxmox server (via SSH or SFTP). Navigate into the folder and run docker compose up -d. The engine will immediately begin health-checking streams and syncing them to Cloudflare.



The Self-Hosted Fix
Instead of relying on a public website that will inevitably get blocked or shut down, you can spin up the open-source Pluto for Channels (jonmaddox/pluto-for-channels) container.

You pass it the credentials for a free Pluto TV account, and it authenticates directly with their API to generate a localized, rock-solid M3U playlist and EPG XML right on your own network.

Spin up the container:

Bash
docker run -d --restart unless-stopped --name pluto-for-channels -p 8080:80 \
  -e PLUTO_USERNAME='your@email.com' \
  -e PLUTO_PASSWORD='yourpassword' \
  jonmaddox/pluto-for-channels

2. **Get your local links:** The container will output a clean, standard playlist directly to your local network at `http://[YOUR_SERVER_IP]:8080/playlist.m3u`.

3. **Load it up:** Drop that local IP address directly into the IPTV player on your Onn boxes or your custom middleware. 

Because it's running locally and handling the token authentication itself, it bypasses the ad-insertion parsing errors and `.m3u8` player rejections that kill the public links. It just feeds a stable stream straight to your player.

<FollowUp label="Want the docker-compose version for this?" query="Yes, please write out the docker-compose.yml file for the Pluto container."/>