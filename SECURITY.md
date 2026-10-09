# K-Sounds Hub v0.4.7 — security and scope

This application is developed for trusted local networks. The Soundboard Remote
uses an HTTP service on TCP 8765 and UDP discovery on 8766 by default. Do not
port-forward these services, expose them publicly, or use the Remote on untrusted
networks. Configure an OS firewall to restrict access to the intended LAN.

The Remote's existing Android-compatible pairing protocol uses a temporary
six-digit PIN; v0.4.7 limits PIN requests to five per TCP client per five minutes
and rejects direct non-LAN peers by default. The pairing token may still be sent
in a URL over unencrypted HTTP; network eavesdroppers on an untrusted LAN could
observe it. This version is not a replacement for TLS authentication.

Future versions should migrate pairing to an authenticated POST request and
transport tokens using secure headers over authenticated HTTPS.

Do not publish configuration folders, pairing tokens, signed APK keys, user
profile directories, collected logs, backups, or captured audio.
