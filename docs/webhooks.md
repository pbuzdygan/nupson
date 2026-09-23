# Webhook notifications

Configure a webhook URL under **Settings → Webhook notifications**. NUPSON sends
power transitions, UPS communication changes, battery warnings, recovery steps,
and Wake-on-LAN results as HTTP `POST` requests.

## Payload

```json
{
  "source": "nupson",
  "notification": {
    "title": "Utility power is unavailable",
    "message": "The UPS is powering protected devices from its battery.",
    "facts": [
      {"name": "Battery", "value": "96%"},
      {"name": "Runtime", "value": "39 min"},
      {"name": "Load", "value": "16%"}
    ]
  },
  "event": {
    "id": 42,
    "created_at": "2026-09-19T12:00:00+00:00",
    "level": "warning",
    "kind": "power_state",
    "message": "UPS is running on battery",
    "data": {
      "to": "outage",
      "battery_charge": 96,
      "runtime_seconds": 2340,
      "load_percent": 16
    }
  }
}
```

`notification` is presentation-ready, localized text. `event` retains stable
technical fields for automation. Discord webhook URLs receive the same
information as a formatted embed.

A `2xx` response confirms delivery. Failed deliveries are recorded in the event
log. Delivery runs in a background queue so a slow receiver does not block UPS
monitoring.

## Recovery notifications

Recovery is reported as separate steps:

- `recovery_pending`: utility power returned and NUPSON is confirming that it
  remains continuously available;
- `waiting_for_charge`: continuity is confirmed, but the configured minimum
  battery charge has not yet been reached;
- `waking`: the power and battery conditions are satisfied and host checks are
  running;
- `normal`: the recovery procedure and wake queue are complete.

This distinction prevents a finished `00:00` timer from looking like an active
power-confirmation phase while NUPSON is actually waiting for battery charge.

## HMAC verification

When a webhook secret is configured, NUPSON adds:

```text
X-NUPSON-Signature: sha256=<hex HMAC-SHA256 of the raw request body>
```

Compute HMAC-SHA256 over the exact raw request bytes with the shared secret and
compare it to the header using a constant-time comparison. Reject a request if
the signature is absent or invalid.

The secret is write-only in the API. Leaving the secret field empty preserves
the current value. Clearing the webhook URL disables delivery.

## Operational guidance

- use HTTPS unless the receiver is on an isolated trusted network;
- treat webhook URLs and HMAC secrets as credentials;
- do not commit them to `.env`, documentation, screenshots, or issue reports;
- configure the receiver to return quickly and process expensive work
  asynchronously;
- make automation idempotent by recording `event.id`.
