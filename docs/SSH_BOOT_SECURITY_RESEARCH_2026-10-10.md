# BTRFS Harbor security research — October 10, 2026

### Confirmed upstream vulnerabilities relevant to a resumable SSH design

- btrbk CVE-2026-62943 / GHSA-pf45-7g54-65h5: versions below
  0.32.7 using ssh_filter_btrbk.sh as authorized_keys forced-command
  could allow a trailing shell command after an allowed prefix. Fixed in
  v0.32.7 (2026-09). Harbor must not copy the vulnerable filter or rely on
  a regex-only forced command. Source:
  https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=1148559
- SSHFS CVE-2026-47187 and CVE-2026-48711: fixed in sshfs 3.7.6,
  May 2026 (symlink escape and SSH argument injection). If a future Harbor
  optional SSHFS adapter is implemented, reject older binaries and enforce
  symlink containment, strict host key checking and private mount namespaces.
  Sources:
  https://github.com/libfuse/sshfs/releases/tag/sshfs-3.7.6
  https://www.openwall.com/lists/oss-security/2026/05/30/3

### Engineering choice

Do NOT force users into a new SSHFS mount merely to make v2 appear
complete. First prove remote append/chunk checkpoint persistence,
the same host-key/identity validation across reboot, and real SSH outage
+ re-authentication replay on an isolated account. The existing inherited
SSH raw backup remains available but is NOT advertised as resumable v2.

For EFI bare-metal restore, prefer ReaR 2.9 native disk reconstruction,
then a Harbor-specific restore bridge; ReaR itself clearly separates
storage reconstruction, data restore and bootloader deployment:
https://relax-and-recover.org/documentation/release-notes-2-9
A rescue ISO without ESP and /boot data captured by Harbor cannot provide
a working clone; QEMU/OVMF clean disk boot verification is obligatory.
