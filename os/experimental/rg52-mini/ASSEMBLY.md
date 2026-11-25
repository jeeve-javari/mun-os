# Assembling the RG52 Mini image

The host kernel used to develop this port has no Btrfs and no VFAT. Assembly
mounts partition 4 with libguestfs, whose appliance kernel does have Btrfs.
`guestmount` needs `/dev/kvm` or TCG, a kernel under `/boot` for supermin
(`linux-image-virtual` is enough; do not boot the machine with it), and
`btrfs-progs`.

Inputs, both checked against the publisher's hashes before use:

- dArkOS `v06242026`: `dArkOS_rg52mini_trixie_06242026.img` (the three
  `7z` parts from that GitHub release, joined by `7z x` on the `.001` file).
- MUN OS `v0.1.0-dev.5`: `mun-os-0.1.0-dev-qemu-arm64.qcow2`, SHA-256
  `dcf93d57719b5c9e90aa1017bc8e914be8b214ce6d46c05f8a4335949c94ae50`.
  `qemu-img convert -O raw` produces the raw disk whose SHA-256 is
  `40205a72b7de4ecf5d5de9001156383d985e3abefd800dd6d11f477ea15c59f1`
  (`artifacts.raw_image` in that release's `BUILD-INFO.json`).

Mount the MUN root read-only (`loop0p2` is the ext4 root) and copy the
dArkOS image before writing:

```sh
sudo losetup -fP --show --read-only mun-os-0.1.0-dev-qemu-arm64.raw
sudo mount -o ro /dev/loop0p2 /mnt/mun
cp -a --sparse=always dArkOS_rg52mini_trixie_06242026.img \
    mun-os-rg52mini-experimental.img
sudo python3 assemble.py \
    --source dArkOS_rg52mini_trixie_06242026.img \
    --image mun-os-rg52mini-experimental.img \
    --mun /mnt/mun \
    --report VALIDATION.txt
```

`assemble.py` installs `/opt/mun`, the MUN systemd units, Qt, Inter, and
pyudev; creates `mun-shell` and `mun-game`; starts the shell from
`graphical.target` (and from `multi-user.target`, which is what a partially
booted board still reaches); leaves `getty@tty2` as a recovery login; and
keeps EmulationStation from starting while `/etc/mun/rg52-mini-port` exists.
dArkOS `firstboot.service` still runs and the shell waits for it, so the
stock first-boot resize of the Btrfs root can finish and release tty1.

The script then checks:

- the partition table is unchanged
- SHA-256 of every byte before partition 4, and of every byte after it, is unchanged
- `btrfs check` on partition 4
- `ld-linux-aarch64.so.1 --list` resolves `mun-shell` and `libqlinuxfb.so`

It does not compress the image. Compress a copy when you need one:

```sh
zstd -T0 -3 -k mun-os-rg52mini-experimental.img
sha256sum mun-os-rg52mini-experimental.img mun-os-rg52mini-experimental.img.zst
```

Do not flash the result from this step. [FIRST-BOOT.md](FIRST-BOOT.md) is the
hand-off to a physical card, and it waits for an explicit yes.
