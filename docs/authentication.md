# Authentication

Account registration closes after the first account is created. See the
[security policy](../SECURITY.md#who-should-have-access) for access limits.

## Recover access

Changing your password in **Account** signs out other devices but keeps the current browser signed in.
If you've forgotten your password, run this from the directory containing `compose.yml`:

```bash
docker compose exec server millwright auth reset-password --username owner
```

Replace `owner` with your username. A reset signs that account out on all devices.

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

Someone with provider access can still sign in through OIDC after a password reset. Signing out of
Millwright doesn't sign you out of the provider.
