#!/usr/bin/env python3
"""Install MUN OS v0.1.0-dev.5 userspace into a dArkOS RG52 Mini disk image.

The RK3562 boot chain (partition table, U-Boot, resource partition, FAT boot
partition, kernel, device tree, extlinux) is left byte-for-byte unchanged.
Only partition 4, the Btrfs root, is modified.

This does not flash any physical disk. The result is a file-backed image.
It has not booted on hardware until someone tests it on an RG52 Mini.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

MUN_VERSION = "0.1.0-dev.5"
MUN_QCOW_SHA256 = "dcf93d57719b5c9e90aa1017bc8e914be8b214ce6d46c05f8a4335949c94ae50"
MUN_RAW_SHA256 = "40205a72b7de4ecf5d5de9001156383d985e3abefd800dd6d11f477ea15c59f1"
DARKOS_RELEASE = "v06242026"

# Base C library and the loader stay the board's. Qt 6.8 in the pinned MUN
# image needs GLIBC_2.38 and GLIBCXX_3.4.31; dArkOS v06242026 provides 2.41
# and 3.4.33, and its libstdc++/libm/libgcc_s/libdrm/libasound match the MUN
# image byte for byte. Replacing libc would change every other program.
CORE_SONAMES = {
    "libc.so.6",
    "libm.so.6",
    "libdl.so.2",
    "libpthread.so.0",
    "librt.so.1",
    "libresolv.so.2",
    "libutil.so.1",
    "libanl.so.1",
    "ld-linux-aarch64.so.1",
    "libstdc++.so.6",
    "libgcc_s.so.1",
}

LIB_DIRS = (
    "usr/lib/aarch64-linux-gnu",
    "lib/aarch64-linux-gnu",
)

# Taken from the published image, not from this checkout: the image was built
# from source commit fa5344bee486, which is not this tree's HEAD.
COPY_PATHS = (
    "opt/mun",
    "usr/local/libexec/mun-launch-cleanup",
    "usr/local/libexec/mun-power",
    "usr/local/libexec/mun-reset",
    "usr/local/libexec/mun-standby",
    "etc/systemd/system/mun-cardd.service",
    "etc/systemd/system/mun-launch-cleanup@.service",
    "etc/systemd/system/mun-launchd.service",
    "etc/systemd/system/mun-reset-commit.service",
    "etc/systemd/system/mun-reset.service",
    "etc/systemd/system/mun-shell-restore.service",
    "etc/systemd/system/mun-shell.service",
    "etc/systemd/system/mun-standby.service",
    "etc/systemd/system/run-mun-launch.mount",
    "usr/lib/sysusers.d/mun-cardd.conf",
    "usr/lib/sysusers.d/mun-launchd.conf",
    "usr/lib/sysusers.d/mun-shell.conf",
    "usr/lib/tmpfiles.d/mun-launchd.conf",
    "etc/sudoers.d/mun-shell",
    "usr/share/doc/mun-shell",
    "usr/lib/mun/release",
    "usr/share/fonts/opentype/inter",
    "usr/lib/python3/dist-packages/pyudev",
    "usr/lib/python3/dist-packages/pyudev-0.24.3.egg-info",
    "usr/lib/aarch64-linux-gnu/qt6",
)

ENABLE_LINKS = (
    ("graphical.target.wants", "mun-shell.service"),
    ("multi-user.target.wants", "mun-shell.service"),
    ("multi-user.target.wants", "mun-cardd.service"),
    ("multi-user.target.wants", "mun-launchd.service"),
    ("multi-user.target.wants", "run-mun-launch.mount"),
    ("sysinit.target.wants", "mun-standby.service"),
    ("sysinit.target.wants", "mun-reset.service"),
)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    log("+ " + " ".join(cmd))
    return subprocess.run(cmd, check=True, text=True, **kwargs)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_regions(path: Path, spans: list[tuple[int, int]]) -> list[str]:
    digests = []
    with path.open("rb") as handle:
        for start, end in spans:
            handle.seek(start)
            digest = hashlib.sha256()
            remaining = end - start
            while remaining:
                chunk = handle.read(min(8 << 20, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            if remaining:
                raise SystemExit(f"short read of {path} at {start}:{end}")
            digests.append(digest.hexdigest())
    return digests


def partition_table(image: Path) -> str:
    result = run(["sfdisk", "-d", str(image)], capture_output=True)
    return result.stdout


def partition_four(image: Path) -> tuple[int, int]:
    text = partition_table(image)
    match = None
    for line in text.splitlines():
        found = re.search(r"img4 : start=\s*(\d+), size=\s*(\d+),", line)
        if found:
            match = found
            break
    if not match:
        raise SystemExit(f"partition 4 not found in {image}")
    start = int(match.group(1)) * 512
    size = int(match.group(2)) * 512
    return start, size


def outside_spans(image: Path) -> list[tuple[int, int]]:
    start, size = partition_four(image)
    end = start + size
    total = image.stat().st_size
    if not (0 < start < end <= total):
        raise SystemExit(f"partition 4 range {start}:{end} outside {total}")
    return [(0, start), (end, total)]


def guestunmount(mountpoint: Path) -> None:
    if mountpoint.is_mount():
        run(["guestunmount", str(mountpoint)])


def guestmount(image: Path, mountpoint: Path) -> None:
    mountpoint.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LIBGUESTFS_BACKEND"] = "direct"
    env.pop("LIBGUESTFS_DEBUG", None)
    env.pop("LIBGUESTFS_TRACE", None)
    run(
        [
            "guestmount",
            "--rw",
            "-a",
            str(image),
            "-m",
            "/dev/sda4",
            str(mountpoint),
        ],
        env=env,
    )


def ignore_pycache(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__" or name.endswith(".pyc")}


def copy_path(src_root: Path, dest_root: Path, relative: str) -> None:
    source = src_root / relative
    if not source.exists() and not source.is_symlink():
        raise SystemExit(f"MUN image is missing {relative}")
    destination = dest_root / relative
    if source.is_dir() and not source.is_symlink():
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, symlinks=True, ignore=ignore_pycache)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or destination.exists():
            destination.unlink()
        if source.is_symlink():
            os.symlink(os.readlink(source), destination)
        else:
            shutil.copy2(source, destination)
    log(f"copied {relative}")


def find_soname(root: Path, soname: str) -> Path | None:
    for directory in LIB_DIRS:
        candidate = root / directory / soname
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return None


def is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def needed_libraries(path: Path) -> list[str]:
    real = path.resolve()
    if not is_elf(real):
        return []
    output = subprocess.check_output(["readelf", "-d", str(real)], text=True, errors="replace")
    return re.findall(r"Shared library: \[([^\]]+)\]", output)


def copy_soname(mun: Path, dest: Path, soname: str) -> Path:
    source = find_soname(mun, soname)
    if source is None:
        raise SystemExit(f"MUN image has no {soname}")
    relative = source.relative_to(mun)
    target = dest / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        real = source.resolve()
        real_target = dest / real.relative_to(mun)
        real_target.parent.mkdir(parents=True, exist_ok=True)
        if not real_target.exists():
            shutil.copy2(real, real_target)
        if target.is_symlink() or target.exists():
            target.unlink()
        os.symlink(os.readlink(source), target)
        return real_target
    if not target.exists():
        shutil.copy2(source, target)
    return target


def copy_qt_libraries(mun: Path, dest: Path) -> list[Path]:
    copied: list[Path] = []
    source_dir = mun / "usr/lib/aarch64-linux-gnu"
    for entry in sorted(source_dir.iterdir()):
        if not entry.name.startswith("libQt6"):
            continue
        destination = dest / "usr/lib/aarch64-linux-gnu" / entry.name
        if destination.is_symlink() or destination.exists():
            destination.unlink()
        if entry.is_symlink():
            os.symlink(os.readlink(entry), destination)
        else:
            shutil.copy2(entry, destination)
            copied.append(destination)
    log(f"copied {len(list(source_dir.glob('libQt6*')))} Qt library names")
    return copied


def close_library_set(mun: Path, dest: Path, roots: list[Path]) -> tuple[list[str], list[str]]:
    """Copy SONAMEs that the MUN binaries need and the board image lacks."""
    queue = [path for path in roots if path.exists()]
    seen: set[Path] = set()
    copied: list[str] = []
    kept: list[str] = []
    while queue:
        current = queue.pop()
        real = current.resolve()
        if real in seen:
            continue
        seen.add(real)
        for soname in needed_libraries(real):
            if find_soname(dest, soname) is not None:
                kept.append(soname)
                continue
            if soname in CORE_SONAMES:
                raise SystemExit(f"{real.name} needs {soname}, which is not on the board image")
            installed = copy_soname(mun, dest, soname)
            copied.append(soname)
            queue.append(installed)
            log(f"library {soname}")
    return sorted(set(copied)), sorted(set(kept))


def elf_files_under(root: Path) -> list[Path]:
    found = []
    if not root.exists():
        return found
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            if is_elf(path):
                found.append(path)
    return found


def write_release(dest: Path) -> None:
    path = dest / "usr/lib/mun/release"
    text = path.read_text()
    fields = {}
    order = []
    for line in text.splitlines():
        if not line.strip() or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        fields[key] = value.strip()
        order.append(key)
    fields["environment"] = '"rg52-mini"'
    fields["profile"] = '"rg52-mini"'
    fields["base_environment"] = fields.get("base_environment", '"qemu-arm64"')
    fields["base_profile"] = fields.get("base_profile", '"qemu-dev"')
    fields["hardware"] = '"AISLPC RG52 Mini (Rockchip RK3562)"'
    for key in ("base_environment", "base_profile", "hardware"):
        if key not in order:
            order.append(key)
    path.write_text("".join(f"{key} = {fields[key]}\n" for key in order))
    log("rewrote /usr/lib/mun/release environment=rg52-mini")


def write_integration(dest: Path) -> None:
    marker = dest / "etc/mun/rg52-mini-port"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        "\n".join(
            [
                "port=rg52-mini",
                f"mun={MUN_VERSION}",
                f"mun_image_sha256={MUN_QCOW_SHA256}",
                f"darkos={DARKOS_RELEASE}",
                "boot=unchanged dArkOS RK3562 extlinux",
                "display=qt-linuxfb-drm-software",
                "gpu=not-enabled",
                "audio=unverified",
                "controls=unverified",
                "game_card_slot=not-configured",
                "hardware_boot=not-tested",
                "",
            ]
        )
    )
    shell_dropin = dest / "etc/systemd/system/mun-shell.service.d/50-rg52-mini.conf"
    shell_dropin.parent.mkdir(parents=True, exist_ok=True)
    shell_dropin.write_text(
        """[Unit]
After=firstboot.service

[Service]
# The unit from MUN OS already selects Qt's linuxfb plugin on DRM dumb
# buffers (QT_QPA_PLATFORM and QT_QPA_FB_DRM). That is the path this port
# boots with. Mali / eglfs is intentionally not selected.
#
# dArkOS describes the internal panel as 720x1280 portrait and rotates the
# framebuffer console (fbcon=rotate:1). Qt does not follow fbcon. If the
# picture is sideways on hardware, set:
#   Environment=QT_QPA_PLATFORM=linuxfb:tty=/dev/tty1:rotation=90
# and daemon-reload. Do that on the device; it is not verified here.
"""
    )
    es_dropin = dest / "etc/systemd/system/emulationstation.service.d/50-mun-port.conf"
    es_dropin.parent.mkdir(parents=True, exist_ok=True)
    es_dropin.write_text(
        """[Unit]
# The port marker means this root is being used as MUN OS. Remove
# /etc/mun/rg52-mini-port and `systemctl enable emulationstation` to
# return to the dArkOS frontend.
ConditionPathExists=!/etc/mun/rg52-mini-port
"""
    )
    wants = dest / "etc/systemd/system/multi-user.target.wants/emulationstation.service"
    if wants.is_symlink() or wants.exists():
        wants.unlink()
        log("removed emulationstation from multi-user.target.wants")


def enable_units(dest: Path) -> None:
    systemd = dest / "etc/systemd/system"
    for directory, name in ENABLE_LINKS:
        folder = systemd / directory
        folder.mkdir(parents=True, exist_ok=True)
        link = folder / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(Path("..") / name)
    getty_wants = dest / "etc/systemd/system/getty.target.wants"
    getty_wants.mkdir(parents=True, exist_ok=True)
    tty2 = getty_wants / "getty@tty2.service"
    if tty2.is_symlink() or tty2.exists():
        tty2.unlink()
    tty2.symlink_to("/usr/lib/systemd/system/getty@.service")
    default = systemd / "default.target"
    if default.is_symlink() or default.exists():
        default.unlink()
    default.symlink_to("/usr/lib/systemd/system/graphical.target")
    log("enabled MUN units, getty@tty2, default graphical.target")


def create_users(dest: Path) -> None:
    run(["systemd-sysusers", f"--root={dest}"])
    passwd = (dest / "etc/passwd").read_text()
    for user in ("root", "ark", "mun-shell", "mun-game"):
        if not any(line.startswith(user + ":") for line in passwd.splitlines()):
            raise SystemExit(f"passwd is missing {user}")
    group = (dest / "etc/group").read_text()
    # audio is granted by mun-shell.service SupplementaryGroups, not by a
    # sysusers membership. video, input and render are the memberships the
    # published sysusers file asks for.
    for name in ("video", "input", "render"):
        line = next(item for item in group.splitlines() if item.startswith(name + ":"))
        members = line.split(":")[-1].split(",") if line.split(":")[-1] else []
        if "mun-shell" not in members:
            raise SystemExit(f"mun-shell was not added to {name}: {line}")
    if not any(line.startswith("audio:") for line in group.splitlines()):
        raise SystemExit("the audio group is missing")
    log("users mun-shell and mun-game are present")


def dynamic_list(dest: Path, program: str) -> str:
    qemu = dest / "usr/bin/qemu-aarch64-static"
    if not qemu.exists():
        shutil.copy2("/usr/bin/qemu-aarch64-static", qemu)
        qemu.chmod(0o755)
    result = subprocess.run(
        [
            "chroot",
            str(dest),
            "/usr/bin/qemu-aarch64-static",
            "/lib/ld-linux-aarch64.so.1",
            "--list",
            program,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0 or "not found" in output:
        raise SystemExit(f"dynamic linker failed for {program} (exit {result.returncode}):\n{output}")
    return output


def ldconfig(dest: Path) -> None:
    qemu = dest / "usr/bin/qemu-aarch64-static"
    if not qemu.exists():
        shutil.copy2("/usr/bin/qemu-aarch64-static", qemu)
        qemu.chmod(0o755)
    run(["chroot", str(dest), "/usr/bin/qemu-aarch64-static", "/sbin/ldconfig"])


def remove_qemu(dest: Path) -> None:
    qemu = dest / "usr/bin/qemu-aarch64-static"
    if qemu.exists():
        qemu.unlink()


def check_root_identity(dest: Path) -> None:
    release = (dest / "etc/os-release").read_text()
    if 'VERSION_ID="13"' not in release:
        raise SystemExit("partition 4 is not Debian 13")
    modules = dest / "lib/modules"
    names = sorted(path.name for path in modules.iterdir()) if modules.is_dir() else []
    if "5.10.226" not in names:
        raise SystemExit(f"expected kernel modules 5.10.226, found {names}")
    if not (dest / "opt/mun/shell/bin/mun-shell").is_file():
        raise SystemExit("mun-shell was not installed")


def btrfs_check(image: Path) -> str:
    loop = run(["losetup", "-fP", "--show", str(image)], capture_output=True).stdout.strip()
    try:
        run(["partx", "-u", loop])
        part = f"{loop}p4"
        for _ in range(50):
            if Path(part).exists():
                break
            time.sleep(0.1)
        else:
            raise SystemExit(f"{part} did not appear after partx")
        result = subprocess.run(
            ["btrfs", "check", part],
            check=False,
            text=True,
            capture_output=True,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            raise SystemExit(f"btrfs check failed:\n{output}")
        return output
    finally:
        run(["losetup", "-d", loop])


def assemble(source: Path, mun: Path, image: Path, report_path: Path) -> None:
    if os.geteuid() != 0:
        raise SystemExit("run as root (guestmount and losetup)")
    shell = mun / "opt/mun/shell/bin/mun-shell"
    if not shell.is_file():
        raise SystemExit(f"mount the MUN root at {mun} first")
    if image.stat().st_size != source.stat().st_size:
        raise SystemExit("working image size does not match the dArkOS source")

    log("hashing bytes outside partition 4")
    spans = outside_spans(image)
    before_regions = sha256_regions(image, spans)
    before_table = partition_table(image)
    log(f"outside sha256 {before_regions}")

    mountpoint = Path("/mnt/darkos")
    guestunmount(mountpoint)
    log("mounting partition 4")
    guestmount(image, mountpoint)
    try:
        check_root_identity_before(mountpoint)
        for relative in COPY_PATHS:
            copy_path(mun, mountpoint, relative)
        qt_files = copy_qt_libraries(mun, mountpoint)
        roots = [mountpoint / "opt/mun/shell/bin/mun-shell", *qt_files]
        roots.extend(elf_files_under(mountpoint / "usr/lib/aarch64-linux-gnu/qt6"))
        copied, kept = close_library_set(mun, mountpoint, roots)
        write_release(mountpoint)
        write_integration(mountpoint)
        enable_units(mountpoint)
        create_users(mountpoint)
        ldconfig(mountpoint)
        shell_list = dynamic_list(mountpoint, "/opt/mun/shell/bin/mun-shell")
        linuxfb = (
            "/usr/lib/aarch64-linux-gnu/qt6/plugins/platforms/libqlinuxfb.so"
        )
        linuxfb_list = dynamic_list(mountpoint, linuxfb)
        remove_qemu(mountpoint)
        check_root_identity(mountpoint)
        mode = stat.S_IMODE((mountpoint / "etc/sudoers.d/mun-shell").stat().st_mode)
        if mode != 0o440:
            raise SystemExit(f"sudoers mode is {oct(mode)}, expected 0o440")
    except Exception:
        remove_qemu(mountpoint)
        raise
    finally:
        log("unmounting partition 4")
        guestunmount(mountpoint)

    log("checking the partition table and Btrfs")
    after_regions = sha256_regions(image, spans)
    after_table = partition_table(image)
    if before_regions != after_regions:
        raise SystemExit(f"bytes outside partition 4 changed: {before_regions} -> {after_regions}")
    if before_table != after_table:
        raise SystemExit("partition table changed")
    btrfs_output = btrfs_check(image)
    log("hashing the assembled image")
    image_sha = sha256_file(image)

    report = "\n".join(
        [
            "RG52 Mini experimental MUN OS image",
            "hardware boot: not tested",
            f"mun: {MUN_VERSION}",
            f"mun qcow2 sha256: {MUN_QCOW_SHA256}",
            f"mun raw sha256: {MUN_RAW_SHA256}",
            f"darkos: {DARKOS_RELEASE}",
            f"image: {image}",
            f"image bytes: {image.stat().st_size}",
            f"image sha256: {image_sha}",
            f"bytes before partition 4 sha256: {after_regions[0]}",
            f"bytes after partition 4 sha256: {after_regions[1]}",
            "partition table: unchanged",
            "bytes outside partition 4: unchanged",
            "root: Debian 13, kernel modules 5.10.226, Btrfs",
            "display path: Qt linuxfb on DRM, software renderer",
            "emulationstation: not started while /etc/mun/rg52-mini-port exists",
            "recovery: getty@tty2, existing dArkOS accounts unchanged",
            "libraries copied:",
            *[f"  {name}" for name in copied],
            "",
            "mun-shell dynamic linker:",
            shell_list.rstrip(),
            "",
            "libqlinuxfb dynamic linker:",
            linuxfb_list.rstrip(),
            "",
            "btrfs check:",
            btrfs_output,
            "",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    sys.stdout.write(report)
    log(f"wrote {report_path}")


def check_root_identity_before(dest: Path) -> None:
    release = (dest / "etc/os-release").read_text()
    if 'VERSION_ID="13"' not in release:
        raise SystemExit("refusing to modify a root that is not Debian 13")
    modules = sorted(path.name for path in (dest / "lib/modules").iterdir())
    if modules != ["5.10.226"]:
        raise SystemExit(f"refusing to modify a root whose modules are {modules}")
    if not (dest / "etc/systemd/system/emulationstation.service").is_file():
        raise SystemExit("refusing to modify a root without emulationstation.service")
    log(f"root identity ok: Debian 13, modules {modules}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="pristine dArkOS image, not modified")
    parser.add_argument("--image", type=Path, required=True, help="file-backed working copy")
    parser.add_argument("--mun", type=Path, required=True, help="mounted MUN OS root filesystem")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    assemble(args.source, args.mun, args.image, args.report)


if __name__ == "__main__":
    main()
