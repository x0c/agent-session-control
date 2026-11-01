# Security

## Supported versions

Security fixes target the latest released version. Upgrade to 0.1.1 or newer
before reporting a problem already addressed by that release.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/x0c/agent-session-control/security/advisories/new).
Do not disclose a security issue in a public issue before coordinated disclosure.

Include the package version, operating system, affected runtime/version and a
minimal reproduction using only sessions and files you own. Remove credentials,
private conversation content and unrelated environment variables from evidence.

Same-user namespaces prevent accidental cross-session operations; they do not
provide hostile-tenant isolation. See [the contract](docs/CONTRACT.md) for the
ownership, uncertain-delivery and persistence guarantees.
