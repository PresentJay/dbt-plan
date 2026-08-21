# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in dbt-plan, please report it responsibly:

1. **Do NOT open a public GitHub issue.**
2. Email security concerns to the maintainers via [GitHub Security Advisories](https://github.com/PresentJay/dbt-plan/security/advisories/new).

We will respond within 48 hours and work with you to understand and address the issue.

## Scope

dbt-plan is a static analysis tool that reads compiled SQL files and manifest.json from disk. It does not:
- Connect to any database or warehouse
- Execute SQL or dbt commands (except in the optional `dbt-plan run` convenience wrapper)
- Store or transmit any data

Relevant security considerations:
- **Path traversal**: compiled SQL directory scanning should not escape project boundaries
- **Symlink attacks**: snapshot operations validate paths before `shutil.rmtree`
- **Input validation**: dialect names restricted to alphanumeric, exit codes to 0-255

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.3.x   | Yes       |
| 0.2.x   | No        |
| 0.1.x   | No        |
