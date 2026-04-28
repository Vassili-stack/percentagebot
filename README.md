# Percentagebot OCR reservation rebuild

This build is focused on one job: scanning MCOC battlegroup screenshots and saving the players marked RESERVED.

## Deploy

```bash
fly secrets set DISCORD_TOKEN="your_token_here" -a percentagebot
fly deploy -a percentagebot
```

The Fly machine target is 512 MB.

## Best scan command

Use manual battlegroup override when possible:

```txt
!scan bg2
!scan bg2 debug
```

Manual BG avoids wasting OCR work on the header.

## Confirming

```txt
!confirm SCANID
!confirm SCANID replace
!confirm SCANID bg2 replace
```

## Fixing a pending scan before saving

```txt
!editscan SCANID bg2 "bos rocker" "Whec" "Silent.Slayer" "Vazwya"
!showscan SCANID
```

## Data

Saved file:

```txt
/data/reservations.json
```

Commands:

```txt
!list
!viewbg 2
!clearbg 2
!clear "Player Name"
!rename "Old Name" "New Name"
!exportdata
!importdata
!wipe confirm
```
