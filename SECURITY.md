# Security policy

## Supported versions

Only the latest release gets fixes. Currently that is 0.1.x.

## Reporting a vulnerability

Do not open a public issue. Use GitHub private vulnerability reporting: on this repository, open the **Security** tab and choose **Report a vulnerability**.

Include what you ran, what happened, and the impact you expect. Leave out real keys, hostnames and bot ids. You should get a first answer within 7 days.

## Scope and threat model

wake is a small CLI for bots and agent sessions that share one host. Read the [Security model](README.md#security-model) section first. In short:

- Sender labels (`--source`, `WAKE_SOURCE`) are self-asserted. They are labels, not authentication.
- Thread and inbox files are world-writable by design. Any local user on the host can read and forge thread lines.
- Every payload includes the sender's hostname, user name and working directory.
- In guest mode, `WAKE_HOSTOPS` runs shell commands on the host. wake quotes or validates every value it puts into those commands.

Reports that matter most: command injection through any argument or config value, key files written with loose permissions, a way around the loop guard, or a way to wake a bot without its key.
