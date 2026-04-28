# PercentageBot OCR rebuild

This rebuild only tracks reserved players by battlegroup.

## Runtime model

The bot uses:

- py-cord for Discord
- Pillow for cropping and preprocessing
- the system Tesseract binary through subprocess

It does not use EasyOCR, OpenCV, NumPy, or pytesseract. This keeps memory lower on Fly.

## Fly setup

Set the token:

```bash
fly secrets set DISCORD_TOKEN="YOUR_TOKEN" -a percentagebot
```

Deploy:

```bash
fly deploy -a percentagebot
```

The included `fly.toml` sets the machine to 512 MB shared CPU.

## Discord command flow

Recommended scan command:

```txt
!scan bg2 debug
```

Once the output looks correct:

```txt
!confirm SCANID
```

To replace the saved names for that battlegroup:

```txt
!confirm SCANID replace
```

## Commands

```txt
!scan bg2
!scan bg2 debug
!confirm SCANID
!confirm SCANID replace
!reject SCANID
!list
!viewbg 2
!rename "Old Name" "New Name"
!clear "Player Name"
!clearbg 2
!wipe confirm
!exportdata
!importdata
!setscanchannel CHANNEL_ID
!setlogchannel CHANNEL_ID
!config
```

## Notes

Use the manual battlegroup argument, such as `bg1`, `bg2`, or `bg3`. It avoids wasting OCR memory on the header and is more reliable.

The bot searches for an image in this order:

1. Attachment on the command message
2. Attachment on the replied-to message
3. Most recent image in the last 10 channel messages

