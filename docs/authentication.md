# Authentication

Create your account the first time you open Millwright. You can sign in with that username and password,
or add OIDC to use an existing identity provider.

Every account has full access to the instance. There are no read-only accounts or per-library permissions.

## Create the first account

Open the web interface and choose a username and password. Finish this step before making Millwright
accessible to other people. Account registration closes after the first account is created.

## Change your password

Open **Account**, enter your current password, and enter the new password twice. Choose **Change password**.
You stay signed in on this browser. Your other browsers and devices must sign in again.

## Reset a forgotten password

You need terminal access to the server. From the directory containing `compose.yml`, run:

```bash
docker compose exec server millwright auth reset-password --username owner
```

Replace `owner` with your username. Enter the new password twice when prompted, then sign in again.
The reset signs that account out on all devices.

## Configure OIDC

Create and test your local account first. You'll still be able to use its password if the provider is
unavailable.

Register a confidential client at your OIDC provider. Set its callback URL to your Millwright address
followed by `/api/auth/oidc/callback`, for example:

```text
https://millwright.example.org/api/auth/oidc/callback
```

Add these settings to `config.toml`, normally `/config/millwright/config.toml` in a container:

```toml
[auth.oidc]
username = "owner"
issuer = "https://id.example.org"
client_id = "millwright"
secret = "file:secrets/oidc_secret"
redirect_uri = "https://millwright.example.org/api/auth/oidc/callback"
post_login_url = "https://millwright.example.org/"
```

Replace `owner` with your local username and fill in your provider's issuer and client ID. Put the client
secret in `secrets/oidc_secret` beside `config.toml`. The callback URL must exactly match the one registered
at the provider, including scheme, hostname, and port.

Restart Millwright, then test both password and OIDC login. If you use a reverse proxy, check its
[HTTPS settings](installation.md#reverse-proxies) too.

> [!IMPORTANT]
> Everyone allowed to use this OIDC client signs in as the account named by `username` above. Restrict
> access at the provider. Millwright doesn't create a separate account for each person.

## Remove OIDC access

Remove the person's access at the provider. To end existing Millwright logins too, reset the mapped
account's password. This signs everyone using that account out.

Changing a local password doesn't stop someone who still has provider access from signing in through
OIDC again. Likewise, logging out of Millwright doesn't log you out of the provider.

## Back up your account

Configuration exports don't include accounts. Include PostgreSQL in your
[backup plan](recovery.md#database-and-filesystem-backups) if you want to preserve your login when
recovering the instance.
