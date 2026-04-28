# Percentagebot OCR Rebuild

This rebuild ignores the old percentage system and only tracks:

- Battlegroup number
- Player names marked `RESERVED`

It is tuned for the MCOC battlegroup screenshots where the text column appears right of the portraits and left of the item/champion boxes.

## Fly setup

Set the Discord token as a Fly secret:

```bash
fly secrets set DISCORD_TOKEN="your_discord_bot_token" -a percentagebot
```

Deploy:

```bash
fly deploy -a percentagebot
```

The bot writes persistent data to:

```txt
/data/reservations.json
/data/config.json
```

## Commands

```txt
!scan
!scan debug
!confirm [scan_id]
!confirm [scan_id] replace
!reject [scan_id]

!list
!viewbg [number]

!rename [old] [new]
!clear [player]
!wipe CONFIRM
!exportdata
!importdata

!setscanchannel [channel_id]
!clearscanchannel
!setlogchannel [channel_id]
```

## OCR notes

Use `!scan debug` when a screenshot fails. The bot will show row OCR lines, which makes crop/OCR tuning easier.

The parser uses Tesseract instead of EasyOCR so it can run on a small Fly VM.
