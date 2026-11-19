# Millwright

Millwright is a self-hosted [FFmpeg](https://ffmpeg.org/) automation tool. Convert individual files or
apply rules to a library to change codecs, containers, audio tracks, and subtitles.

> [!WARNING]
> I've tested NVIDIA, Intel QSV, and software encoders on my own library AMD encoders are not tested.

### If you like my work, you might also like my other projects:

* [WatchState](https://github.com/arabcoders/watchstate): Sync your media servers without relying on 3rd parties.
* [YTPTube](https://github.com/arabcoders/ytptube): Media downloader, automation, and library manager for yt-dlp.
* [DCompose](https://github.com/arabcoders/dcompose): Manage your servers/nodes compose stacks from one WebUI.

## Screenshots

Dashboard:

![Dashboard](ui/public/images/screenshots/dashboard.jpg)

Statistics:

![Statistics](ui/public/images/screenshots/stats.jpg)

Job details:

![Job details and flow decisions](ui/public/images/screenshots/job-view.jpg)

## How it works

Add a **Library** and preview a **Flow** to see which files it would change. **Nodes** run conversions
on the server or on other machines. Millwright checks the output before replacing the original.

> [!WARNING]
> [Keep originals in Trash](docs/recovery.md#retain-originals) while trying a new flow.

## What you can do

- Convert video with software or hardware encoders.
- Remux MKV and MP4 files, convert selected audio tracks, and filter tracks by language.
- Extract subtitles, burn subtitles into video, and embed metadata from NFO or yt-dlp sidecars.
- Use conditions to apply different processing to different files.
- Schedule processing, wait for new files to finish copying, and pause work without cancelling active jobs.
- Play media in the browser and inspect conversion logs, progress, and before-and-after media details.
- Restore retained originals and track disk space saved.
- Receive import notifications from Sonarr or Radarr and notify them after a conversion.

## Install

Follow the [installation guide](docs/installation.md) to run the installer, claim a node, and add
your first library. See the [documentation index](docs/README.md) for setup guides and references.

## Project Policy

Millwright is built for my own needs and use cases first. Discuss contributions with me before starting any work that i 
may decline to accept. Read [CONTRIBUTING.md](CONTRIBUTING.md). I use AI tools in this project. You're welcome to use the 
project if it works for you. However, if you object to that, then this project is not good fit for you. Build your own 
using technologies you approve of.

## Community

For short questions, join the [Discord server](https://discord.gg/G3GpVR8xpb). Millwright is maintained as a solo 
project, so replies may take some time.

If you want to support the work financially, please donate to a children's charity such as
[Make-A-Wish international](https://worldwish.org) instead.
