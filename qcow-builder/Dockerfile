# syntax=docker/dockerfile:1
# qcow-builder: turn a restic whole-/ snapshot into a bootable qcow2.
# Native aarch64 Linux container; run --privileged with a big /scratch
# bind-mount. See README.md.
FROM fedora:latest

RUN dnf install -y \
      restic \
      qemu-img \
      btrfs-progs \
      xfsprogs \
      dosfstools \
      gdisk \
      util-linux \
      rsync \
      kmod \
      policycoreutils \
      grub2-efi-aa64 \
      shim-aa64 \
      python3 \
    && dnf clean all

COPY build.sh /usr/local/bin/qcow-build
RUN chmod +x /usr/local/bin/qcow-build

# M3 first-boot reconstitution: handler + offline/online units, baked into
# the produced image by build.sh step 5b.
COPY firstboot-reconstitute.sh \
     firstboot-reconstitute-offline.service \
     firstboot-reconstitute-online.service \
     /usr/local/share/qcow-builder/

ENTRYPOINT ["/usr/local/bin/qcow-build"]
