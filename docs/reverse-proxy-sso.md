# Putting Oracle behind an authenticating proxy

Oracle has no login of its own. For a single operator that is the point — it binds to
loopback and every request is `LOCAL_IDENTITY_USERNAME`. To share it with other people, put
an authenticating reverse proxy in front and let Oracle trust the identity that proxy
asserts.

This works with anything that can authenticate a session and set request headers:
Authelia, Authentik, oauth2-proxy, Cloudflare Access, a corporate SSO gateway.

## The contract

The proxy must, on **every** request it forwards:

| Header | Value |
| --- | --- |
| `X-Gateway-Secret` | The server-held shared secret, matching Oracle's `GATEWAY_SECRET`. |
| `Remote-User` | The stable username from the verified session. This selects the persistent user row. |
| `Remote-Groups` | Comma-separated groups. Membership of `IDENTITY_ADMIN_GROUP` grants administrator visibility. |
| `Remote-Email`, `Remote-Name` | Optional, for display. |

It must also **strip any inbound copy of those headers** before adding its own. A proxy that
forwards a client-supplied `Remote-User` has handed every visitor an identity picker.

Oracle's side:

```ini
IDENTITY_REQUIRE_GATEWAY=true
IDENTITY_TRUST_HEADERS=true
GATEWAY_SECRET=<a long random value, server-side only>
IDENTITY_ADMIN_GROUP=admin
APP_AUTH_TOKEN=
VITE_GATEWAY_URL=https://oracle.example.com/
ORACLE_PUBLIC_ORIGIN=https://oracle.example.com
```

Generate the secret with something like `openssl rand -base64 36`. It is never exposed to a
browser, a URL, client-side configuration or source control.

## How it fails

Deliberately, and closed:

- **Missing or wrong secret → `401`**, with code `gateway_auth_required` or
  `invalid_gateway_secret`. A supplied-but-wrong secret **never** falls back to local
  identity. That fallback is exactly the bug this design exists to prevent.
- **Valid secret, no `Remote-User` → `401`**, loudly, rather than a silent anonymous session.
- **Invalid configuration → the process refuses to start.** `IDENTITY_REQUIRE_GATEWAY=true`
  without `GATEWAY_SECRET`, or with `IDENTITY_TRUST_HEADERS=false`, is a startup error, not
  a runtime surprise.
- **Browser writes from another `Origin` → rejected**, checked against
  `ORACLE_PUBLIC_ORIGIN`. Trusted non-browser requests with no `Origin` remain usable.

## Proxy requirements beyond headers

- **Return `401` to API callers, not an HTML login page.** A redirect to a login form breaks
  `fetch` and leaves the UI reporting nonsense. Content-negotiate, or exempt `/api/*` from
  the HTML redirect and answer with a status.
- **Do not buffer `/api/events/*`.** It is Server-Sent Events; buffering makes the live view
  appear frozen and then dump everything at once. In nginx: `proxy_buffering off;` and a long
  `proxy_read_timeout` on that location.
- **Allow request bodies of at least 16 MiB**, so five context documents survive JSON
  escaping.
- Forward `Authorization` untouched if you also use `APP_AUTH_TOKEN`; normally you should
  not — one authentication layer is enough, and Oracle should not present a second login.

The bundled `docker/nginx.conf` already does the SSE and body-size parts for the container's
internal proxy; your outer gateway needs the same.

## Event streams and tickets

`EventSource` cannot send headers. Oracle issues a **single-use ticket** so a stream can
authenticate through the query string instead — see `app/services/events/tickets.py`. Your
proxy therefore has to admit the ticketed stream request the same way it admits any other
authenticated request; the ticket authenticates to Oracle, not to your gateway.

## Verifying it

Before you trust it, check all four of these from outside:

```bash
# 1. No secret at all: 401
curl -i https://oracle.example.com/api/me

# 2. Forged identity without the secret: 401, not a session
curl -i -H "Remote-User: someone" https://oracle.example.com/api/me

# 3. A real browser session: 200, and the body names the verified user
#    with "source": "gateway"

# 4. A second account sees none of the first account's runs (404, not 403 —
#    the existence of another user's run is not disclosed)
```

`docker/smoke-workspaces.py` automates the equivalent checks against a disposable container:
fail-closed authentication, origin enforcement, per-user lists, cross-user 404s, report
access, explicit administrator visibility, and a busy response that conceals another user's
run title.
