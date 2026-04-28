# percentagebot OCR rebuild

This bot scans Marvel Contest of Champions battlegroup screenshots and saves only:

- battlegroup number
- player names marked RESERVED

## Required Fly secret

```bash
fly secrets set DISCORD_TOKEN="your_discord_bot_token"
```

## Commands

```txt
OCR
!scan
  Scan attached image(s), or reply to an image with !scan.
!confirm [scan_id]
  Save detected reserved players into their battlegroup.
!confirm [scan_id] replace
  Replace that battlegroup's saved reserved list with the scan result.
!reject [scan_id]
  Reject a pending scan.

Viewing
!list
!viewbg [number]

Data Management
!rename old name -> new name
!clear [player]
!clearbg [number]
!wipe
!exportdata
!importdata

Setup
!setscanchannel [channel_id]
!setlogchannel [channel_id]
!viewsetup
```

## Deploy

```bash
fly deploy
```
