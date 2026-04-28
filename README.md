# Percentagebot OCR Reservation Rebuild v5

This version is built for the smallest Fly machine.

It removes OpenCV, NumPy, EasyOCR, and pytesseract. OCR is done by calling the system `tesseract` binary directly through subprocess. This keeps the Python process much lighter.

## Deploy

Set your Discord token once:

```bash
fly secrets set DISCORD_TOKEN="your_token_here" -a percentagebot
```

Deploy:

```bash
fly deploy -a percentagebot
```

## Scan commands

```txt
!scan
!scan debug
!scan bg2
!confirm [scan_id]
!confirm [scan_id] replace
!reject [scan_id]
```

Use `!scan bg2` when the image is BG2 and you do not care about reading the header. The bot will still OCR the reserved names.

## Data commands

```txt
!list
!viewbg [number]
!rename [old] [new]
!clear [player]
!wipe CONFIRM
!exportdata
!importdata
```

## Setup commands

```txt
!setscanchannel [channel_id]
!clearscanchannel
!setlogchannel [channel_id]
```
