#!/usr/bin/env bash
# Self-signed certificate for TESTING the HARP server over HTTPS.
# Browsers will warn about it; use a real certificate (e.g. from your
# institution or Let's Encrypt) for anything other users will open.
set -euo pipefail
out="${1:-certs}"
host="${2:-localhost}"
mkdir -p "$out"
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout "$out/key.pem" -out "$out/cert.pem" \
  -subj "/CN=$host" -addext "subjectAltName=DNS:$host,DNS:localhost,IP:127.0.0.1"
chmod 600 "$out/key.pem"
echo "Created $out/cert.pem and $out/key.pem"
echo "Run: HARP_SSL_CERT=$out/cert.pem HARP_SSL_KEY=$out/key.pem python3 -m harp_server.serve"
