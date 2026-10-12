#!/bin/sh
# ExaCarib box image (ADR 0049), the last step of the build boot: copy the system
# just set up onto the image disk (/dev/vdb) with two root slots:
#   1 BIOS boot · 2 EXABOOT (EFI system partition, GRUB and its grubenv)
#   3 exaroot-A (this system) · 4 exaroot-B (empty until the first update)
# GRUB is installed for both BIOS and UEFI boxes.
set -eux
d=/dev/vdb
sgdisk -Z "$d"
sgdisk -n1:2048:+1M -t1:EF02 -c1:bios -n2:0:+128M -t2:EF00 -c2:EXABOOT \
  -n3:0:+3G -t3:8300 -c3:exaroot-A -n4:0:+3G -t4:8300 -c4:exaroot-B "$d"
partprobe "$d" || true
udevadm settle
mkfs.vfat -F 32 -n EXABOOT "${d}2"
mkfs.ext4 -q -L exaroot-A "${d}3"
mkfs.ext4 -q -L exaroot-B "${d}4"
mkdir -p /mnt/a /mnt/esp
mount "${d}3" /mnt/a
rsync -aHAXx --numeric-ids --exclude='/tmp/*' --exclude='/var/tmp/*' --exclude=/mnt --exclude=/root/ab-disk.sh --exclude=/root/exacarib-build.sh --exclude=/var/log/exacarib-build.log / /mnt/a/
mkdir -p /mnt/a/mnt /mnt/a/boot/efi
cat >/mnt/a/etc/fstab <<F
LABEL=exaroot-A / ext4 rw,discard,errors=remount-ro 0 1
LABEL=EXABOOT /boot/efi vfat umask=0077,nofail 0 2
F
umount /mnt/a
mount "${d}2" /mnt/esp
grub-install --target=i386-pc --boot-directory=/mnt/esp "$d"
grub-install --target=x86_64-efi --efi-directory=/mnt/esp --boot-directory=/mnt/esp --removable --no-nvram
cp /usr/lib/exacarib/grub-ab.cfg /mnt/esp/grub/grub.cfg
grub-editenv /mnt/esp/grub/grubenv create
grub-editenv /mnt/esp/grub/grubenv set slot=A
umount /mnt/esp
sync
