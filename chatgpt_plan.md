This is actually where I'd split the architecture:

Cloudflare Pages + Workers

Use for:

Public website
Channel browser
EPG display
Playlist downloads
Stream redirect/obfuscation layer
Admin login UI
API gateway
VPS (Docker)

Use for:

M3U imports
Channel consolidation
Health checking
EPG processing
Database
Playlist generation

This gives you the lowest bandwidth and resource usage while keeping the system functional.

Tree Frog Streams Architecture v1
User Flow
Public User

Visits:

free.tfplus.stream

Sees:

Channel count
Categories
Search
EPG browser
Download playlist button
IPTV setup guide
Tree Frog Plus CTA
Admin

Logs into:

admin.free.tfplus.stream

Can:

Add M3U URL
Upload M3U file
Create bouquets
Review duplicate matches
View dead streams
View uptime reports
Channel Consolidation System

This is the heart of the platform.

Example

Source A:

BBC News

Source B:

BBC News HD

Source C:

BBC NEWS

System normalizes:

bbc news

Creates:

Channel:
BBC News

Stores:

Primary Stream
Backup Stream 1
Backup Stream 2
Playlist Output

User only sees:

BBC News

Never sees duplicates.

Failover

Primary dies:

Backup automatically used

No duplicate channels.

No user action.

Channel Branding Rules

Do NOT rename channels.

Leave:

CNN
BBC News
Fox Weather

exactly as they are.

Group Branding Rules

Rename groups.

Instead of:

Kids

Output:

🐸 Tree Frog Free | Kids

Instead of:

News

Output:

🐸 Tree Frog Free | News

Instead of:

Sports

Output:

🐸 Tree Frog Free | Sports
Logo Rules

Priority order:

1

Existing channel logo.

Keep it.

2

Known logo library.

Example:

logos/bbc-news.png
logos/cnn.png
3

Fallback

treefrog-default.png

Only if no logo exists.

Stream Obfuscation

Never expose source URLs directly.

Public Playlist

Instead of:

https://provider.com/live/channel.m3u8

Output:

https://free.tfplus.stream/s/8af91b
Worker

Lookup:

8af91b

Returns:

302 redirect

to:

actual source URL

Benefits:

Harder to scrape
Easy stream replacement
Allows failover
Allows statistics
Health Monitoring

Every 30 minutes.

Online
status = online
Offline
status = offline

Record:

offline_since
Offline 3 Days

Automatically:

disabled

Removed from playlist.

Admin notification sent.

Recovery

If stream works again:

enabled

Added back automatically.

Website Features
Home Page

Display:

1,274 Working Channels
98.3% Average Availability
42 Categories
Updated Every 30 Minutes
Search

Search:

CNN
PBS
BBC

Instant filtering.

Categories

Show:

News
Kids
Sports
Movies
Documentary
Lifestyle
Channel Page

Show:

Channel Name
Category
Logo
Current Program
Next Program
Availability Score
Playlist Page

Buttons:

Download M3U
View EPG
Setup Guide
Tree Frog Plus CTA

Not aggressive.

Small banners.

Example:

Want more channels, better reliability, premium EPG and additional features?

Upgrade to Tree Frog Plus.

Button:

Visit TFPlus.stream
Database

SQLite initially.

Tables:

channels
streams
bouquets
epg
redirect_tokens
health_logs

No need for Postgres initially.

M3U Import Flow

Admin enters:

https://example.com/list.m3u

System:

Downloads file
Parses channels
Normalizes names
Finds duplicates
Creates backups
Assigns bouquet
Runs initial health check
EPG

Store XMLTV.

Map channels by:

tvg-id

preferred

then

name matching

fallback

Deployment
VPS

Docker Compose:

treefrog-api
treefrog-worker
treefrog-db

Memory:

512MB-1GB

CPU:

1 core

Should comfortably handle hundreds to a few thousand streams.

Cloudflare
Pages

Host:

free.tfplus.stream

Static frontend.

Workers

Handle:

/s/*

redirects

and

lightweight API caching

MVP Milestones
Phase 1
Import M3U
Consolidate channels
Generate playlist
Health checks
Phase 2
Bouquets
Logos
EPG
Phase 3
Cloudflare Worker redirects
Statistics
Uptime scoring
Phase 4
Public website
Search
Channel pages
Plus CTA
Phase 5
User accounts
Favorites
Custom playlists

The biggest thing I'd emphasize to the developer is:

Tree Frog Streams is not an IPTV service. It is a channel registry, health monitor, failover engine, EPG manager, and playlist publisher.

That mindset leads to the correct architecture and keeps the system lightweight enough to run on a tiny VPS while using Cloudflare for most of the public-facing traffic.