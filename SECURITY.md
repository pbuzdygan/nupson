# Security policy

## Supported versions

Security fixes are provided for the latest stable release. Development images
are intended for testing and may change without compatibility guarantees.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting feature for this repository when
available. Do not open a public issue containing an exploit, credentials,
database files, webhook URLs, logs with secrets, or details that would expose
deployed systems.

Include the affected version, deployment model, reproduction steps, impact, and
any suggested mitigation. Remove real credentials and identifying network data.

## Deployment responsibility

NUPSON controls power-related automation and should be deployed only on a
trusted management network. Restrict the dashboard and TCP 3493 with a host
firewall, use the dedicated USB group for a direct USB profile, and place remote
NUT or SNMP management traffic on a trusted network. Prefer SNMPv3 with
authentication and privacy where the UPS supports it. Keep `data/` and `.env`
private, because the generated NUT files contain operational credentials, and
test shutdown/recovery procedures during a maintenance window.
