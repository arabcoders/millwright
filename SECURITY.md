# Security policy

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/arabcoders/millwright/security/advisories/new)
for security problems. Include a description of the impact, steps to reproduce it, the affected commit,
and relevant logs or configuration.

Report vulnerabilities in FFmpeg or another dependency to that project's maintainers. Report problems in Millwright's 
use of those tools here.

## Who should have access

Millwright is an administrative application. Every account and API key holder can change settings and process or 
replace library files. There are no read-only accounts or per-library permissions. Only give access to people you trust.

Uploaded plugins run Python with the server's privileges. They aren't sandboxed. FFmpeg extras also
allow powerful tool options. Only install trusted plugins and use options you understand.

## Flow exports and share codes

Flow exports and share codes intentionally include credentials stored in plugin inputs, such as
passwords and API keys, so imported flows retain their configuration. JSON files, ZIP archives, and
share codes aren't encrypted. Remove credentials before sharing them or committing them to a repository.

## OIDC access

Everyone permitted to use an OIDC client signs in as the configured Millwright account. Limit client
access at the provider. Provider roles and email addresses don't give different permissions in Millwright.

Removing provider access doesn't end existing Millwright logins. Reset the mapped account's password
to sign those devices out too. A person who still has provider access can sign in again after a local
password change. See [Authentication](docs/authentication.md).

## Media safety

Nodes work within a shared workspace rather than direct access to library or trash folder. The server verifies
converted files before replacing originals and keeps recovery information for interrupted replacements. Bypasses of 
those path checks, authentication checks, or replacement protections are security concerns.

File checks compare path and size, not content. A file changed at the same path without changing its
size isn't detected. This is an accepted limitation. Don't treat recovery or trash as protection
against every kind of media loss.
See [Recovery and backups](docs/recovery.md).
