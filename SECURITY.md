# Security policy

## Supported versions

Only the latest release gets security fixes. Before the first release, that means the `main`
branch. Upgrade to the newest version before reporting, if you can.

## Reporting a vulnerability

Please report vulnerabilities **privately**, not in a public issue, pull request or discussion.

Use GitHub's private vulnerability reporting: on this repository's **Security** tab, choose
**Report a vulnerability**
([direct link](https://github.com/PramodBasavarajMenasi/siftwise/security/advisories/new)).
Only the maintainers see the report, and we can work on a fix and an advisory together there.

## What to include

- what the vulnerability is and what an attacker could do with it (for example: read another
  repo's history, bypass the API token, inject into a CI workflow or the PR comment);
- the affected component (server, CLI, GitHub Action, MCP server, Docker image) and version or
  commit;
- steps to reproduce, or a proof of concept, against a server you run yourself;
- any suggested fix or mitigation.

Please don't test against servers or repositories you don't own, and leave out real tokens or
private test data.

## What to expect

This is a small project maintained on a best-effort basis. You should get a first response
within a few days. We'll confirm the issue, agree on a fix and a disclosure date with you, and
credit you in the advisory unless you'd rather not be named.

## Scope notes

Some limits are by design and documented in the README's
[Security and limits](README.md#security-and-limits): for example, one API token gives full
access to every repo on a server, and the base-branch `known_flaky` rule assumes your
workflow files are protected. Reports that show these can be exploited in ways the README
doesn't describe are welcome.
