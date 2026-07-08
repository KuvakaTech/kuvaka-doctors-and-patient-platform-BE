# Security Policy

This project handles healthcare data (patient records, clinical notes,
prescriptions). Please report vulnerabilities responsibly and do not open a
public GitHub issue for security-sensitive findings.

## Reporting a Vulnerability

Email the maintainers at **security@kuvaka.io** with:

- A description of the vulnerability and its potential impact
- Steps to reproduce, including any proof-of-concept
- The affected version/commit

You should receive an acknowledgement within 3 business days. We'll work with
you to understand and validate the issue, and to agree on a disclosure
timeline once a fix is available.

## Supported Versions

While this project is pre-1.0, only the `main` branch receives security fixes.

## Scope

In scope: authentication/authorization bypass, data leakage between doctor and
patient accounts, injection vulnerabilities, insecure direct object references
on clinical records, and any path to unauthorized access of PHI (protected
health information).
