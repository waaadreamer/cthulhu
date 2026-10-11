#!/usr/bin/env bash
# Authorize a galaxy node's tunnel key. Run as root on the temple:
#   sudo /opt/cthulhu/deploy/authorize-tunnel.sh 'ssh-ed25519 AAAA... cthulhu-galaxy@node'
# The key can only open a forward to 127.0.0.1:7500 (enforced per key and by sshd Match).
set -euo pipefail
KEY="${1:?usage: authorize-tunnel.sh '<public key>'}"
[[ "$KEY" =~ ^ssh-(ed25519|rsa)\ [A-Za-z0-9+/=]+(\ .*)?$ ]] || { echo "not an ssh public key" >&2; exit 1; }
AUTH=/var/lib/cthulhu-tunnel/.ssh/authorized_keys
BODY=$(echo "$KEY" | awk '{print $2}')
if grep -q "$BODY" "$AUTH" 2>/dev/null; then echo "already authorized"; exit 0; fi
echo "restrict,port-forwarding,permitopen=\"127.0.0.1:7500\" $KEY" >> "$AUTH"
chown cthulhu-tunnel:cthulhu-tunnel "$AUTH"; chmod 600 "$AUTH"
echo "authorized: $(echo "$KEY" | awk '{print $3}')"
