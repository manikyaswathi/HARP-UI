"""
Start the HARP sweep server over HTTPS.

    HARP_SSL_CERT=cert.pem HARP_SSL_KEY=key.pem python3 -m harp_server.serve

Environment:
  HARP_SSL_CERT / HARP_SSL_KEY  certificate and private key (PEM). Required
                                unless HARP_BEHIND_PROXY=1.
  HARP_HOST / HARP_PORT         default 0.0.0.0:8443
  HARP_BEHIND_PROXY=1           TLS is terminated by nginx/Caddy/Apache in
                                front of us; serve plain HTTP on HARP_PORT and
                                trust its X-Forwarded-Proto header.
"""

import os
import sys

import uvicorn


def main():
    host = os.environ.get("HARP_HOST", "0.0.0.0")
    port = int(os.environ.get("HARP_PORT", "8443"))
    if os.environ.get("HARP_BEHIND_PROXY") == "1":
        uvicorn.run("harp_server.app:app", host=host, port=port, proxy_headers=True,
                    forwarded_allow_ips=os.environ.get("HARP_PROXY_IPS", "127.0.0.1"))
        return
    cert, key = os.environ.get("HARP_SSL_CERT"), os.environ.get("HARP_SSL_KEY")
    if not (cert and key and os.path.isfile(cert) and os.path.isfile(key)):
        sys.exit("Set HARP_SSL_CERT and HARP_SSL_KEY to a certificate and key (PEM).\n"
                 "For testing, create a self-signed pair with scripts/make_dev_cert.sh")
    print(f"HARP sweep server on https://{host}:{port}")
    uvicorn.run("harp_server.app:app", host=host, port=port, ssl_certfile=cert, ssl_keyfile=key)


if __name__ == "__main__":
    main()
