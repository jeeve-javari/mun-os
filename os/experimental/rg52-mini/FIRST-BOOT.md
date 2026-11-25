# First boot on an RG52 Mini

This image has not been booted on hardware. Offline checks cover the
partition table, the untouched boot chain, Btrfs, and the shell's shared
libraries. They do not cover the panel, the Mali GPU, audio, or the buttons.

The cloud build machine cannot see a Windows drive letter. Nothing below
writes `E:\`, the handheld's internal storage, or its bootloader. Flash only
after you have looked at the disk and agreed to replace it.

## What should happen

1. The stock RK3562 bootloader loads `/boot/extlinux/extlinux.conf`, kernel
   `5.10.226`, and `rk3562-rg52mini.dtb`. Those files are the dArkOS
   `v06242026` files.
2. dArkOS `firstboot.service` may take tty1, grow the Btrfs root, and
   reformat the small last partition as exFAT. That is the stock dArkOS
   script, not a MUN change. The shell waits until that service exits.
3. MUN Shell should then open the panel through Qt's `linuxfb` plugin on DRM
   dumb buffers (software rendering, not the Mali GPU) and draw the real
   v0.1.0-dev.5 interface.
4. tty2 is a getty. The dArkOS accounts, including `ark`, are unchanged.
   EmulationStation does not start while `/etc/mun/rg52-mini-port` exists.

If the picture is sideways, the panel is the 720×1280 portrait screen whose
console dArkOS rotates with `fbcon=rotate:1`. Qt does not follow that. On
the device, set this in
`/etc/systemd/system/mun-shell.service.d/50-rg52-mini.conf` and reboot:

```
Environment=QT_QPA_PLATFORM=linuxfb:tty=/dev/tty1:rotation=90
```

Game Cards are not wired. dArkOS names `mmcblk1` as the boot card. The card
service admits a card only from a configured slot or from a virtio disk, and
this image configures neither, so the boot card is not treated as a Game Card.

## Identify the card on Windows

Run these in PowerShell. They only read.

```powershell
Get-Disk | Select-Object Number, FriendlyName, SerialNumber, BusType, PartitionStyle, @{N='SizeGB';E={[math]::Round($_.Size/1GB,2)}}, IsSystem, IsBoot, IsOffline
Get-Partition | Select-Object DiskNumber, PartitionNumber, DriveLetter, @{N='SizeGB';E={[math]::Round($_.Size/1GB,2)}}, Type
Get-Volume -DriveLetter E | Format-List DriveLetter, FileSystemLabel, FileSystem, DriveType, @{N='SizeGB';E={[math]::Round($_.Size/1GB,2)}}
```

Use the disk only if all of these are true:

- it is the disk whose partition carries `E:`
- `IsSystem` is false and `IsBoot` is false
- its size is the microSD card, not the desktop's internal disk
- you have said, in this session, that this disk number may be erased

## Write the image

Use Raspberry Pi Imager, balenaEtcher, or Rufus in DD mode. Give that
program `mun-os-rg52mini-experimental.img` (or the `.zst` after
decompressing) and the disk number from the read-only check. Do not use a
raw `dd` against `\\.\PhysicalDriveN` unless the imager cannot see the card.

After writing, eject the card, put it in the RG52 Mini, and power on without
USB power first if the bootloader gives no sign of life while plugged in
(that note is from the dArkOS `v06242026` release).

A useful first result is the MUN shell on the internal panel. A black panel,
a sideways panel, or a stuck dArkOS first-boot dialog are all real results;
tty2 is there for the `ark` login if a keyboard is attached.
