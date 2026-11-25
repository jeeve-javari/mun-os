# RG52 Mini experimental port

File-backed image that boots the stock dArkOS RK3562 chain and starts the
official MUN OS v0.1.0-dev.5 shell. This is not a second operating system:
partition 4 of `dArkOS_rg52mini_trixie_06242026.img` receives the MUN
userspace from the published QEMU image.

| Piece | Source |
| --- | --- |
| Bootloader, kernel 5.10.226, `rk3562-rg52mini.dtb`, extlinux | dArkOS `v06242026`, unmodified |
| Shell, card service, launcher, reset, standby | MUN OS `v0.1.0-dev.5` (`mun-os-0.1.0-dev-qemu-arm64.qcow2`) |
| Qt 6.8 libraries the shell was linked against | that same image, where the board image does not already provide the SONAME |

`assemble.py` refuses to write a root that is not Debian 13 with kernel
modules `5.10.226`, and it checks afterwards that every byte outside
partition 4 still matches.

Nothing here has been powered on in an RG52 Mini. GPU acceleration, audio,
the internal display's rotation, the physical controls, and a Game Card
reader are unverified. The only microSD slot named by dArkOS is the boot
disk (`mmcblk1`); the card service is left with no board slot, so it does
not treat that disk as a Game Card.

See [ASSEMBLY.md](ASSEMBLY.md) and [FIRST-BOOT.md](FIRST-BOOT.md).
