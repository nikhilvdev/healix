# Security Policy

## Supported Versions

`healix` is pre-release and moving quickly. Security fixes target the latest
released version only — please upgrade before reporting an issue to confirm
it's still present.

| Version | Supported |
| ------- | --------- |
| Latest release on PyPI | ✅ |
| Anything older | ❌ |

Once a stable release ships, this table will be updated to reflect which
versions receive backported fixes.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Use GitHub's private vulnerability reporting instead:
[Report a vulnerability](https://github.com/nikhilvdev/healix/security/advisories/new)
(the "Security" tab of this repository → "Report a vulnerability"). This
opens a private advisory visible only to maintainers until a fix is ready.

Please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce, or a minimal proof-of-concept
- The affected version(s)

Healix drives real browsers and, once login handling lands, applies credentials
from `.env` to pages it crawls. Reports about credential leakage (into a run
config, manifest, output JSON, logs, events, or screenshots), unintended
navigation outside the configured `domain_scope`, or unsafe handling of crawled
page content are in scope and especially welcome.

We'll acknowledge new reports as soon as possible, typically within a few
business days, and keep you updated as we investigate and prepare a fix.
Once a fix is released, we'll credit the reporter (unless you'd prefer to
stay anonymous) in the release notes and publish a GitHub Security Advisory.
