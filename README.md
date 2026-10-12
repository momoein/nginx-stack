# Shared production Nginx

One Nginx serves multiple projects on a single Linux server. Nginx uses host
networking; each application publishes a unique localhost port. Certbot issues
certificates on demand, and one host cron job renews them and reloads Nginx.

```text
example.com      -> Nginx :443 -> 127.0.0.1:18081 -> project A container :8080
app.example.net  -> Nginx :443 -> 127.0.0.1:18082 -> project B container :3000
```

There is no shared Docker network, container-name discovery, Docker socket mount,
or background Certbot container. Initial website configuration and certificate
issuance are manual; subsequent renewals are automatic once cron is installed.

## Requirements

- A single Linux server with rootful Docker Engine **28 or newer**, Compose v2,
  `openssl`, `curl`, cron, `flock` (util-linux), and logrotate.
- Ports 80 and 443 available on the host and open to the internet. Host networking
  means Nginx binds those ports directly; do not add Compose `ports` mappings.
- Each requested domain's public DNS points to the server. Publish an AAAA record
  only when IPv6 works end to end. The server should have IPv6 socket support for
  the provided `[::]` listeners; remove those listeners consistently if disabled.
- Outbound DNS and HTTPS for Certbot and image downloads.
- Cron mail delivery or an existing monitoring integration that reports job
  failures. Scheduling a command alone does not configure alert delivery.

Docker versions before 28 have a known issue where localhost-published ports can
be reachable from other machines on the same L2 network. Use a current patched
Engine and verify application ports from another machine. See [port publishing](https://docs.docker.com/engine/network/port-publishing/).

This setup uses HTTP-01: port 80 must remain public for renewal. Wildcards need
a separate DNS-01 workflow. Multi-server routing, automatic domain discovery,
and zero-downtime application releases are outside this setup.

## Install the infrastructure

Use `/opt/nginx-stack` in the examples below. If installing elsewhere, update
cron and logrotate paths. Scripts locate the stack relative to themselves and
can be invoked from any working directory.

```bash
cd /opt/nginx-stack
cp .env.example .env
mkdir -p letsencrypt/conf letsencrypt/www letsencrypt/logs static
chmod 700 letsencrypt/conf letsencrypt/logs
docker compose config --quiet
docker compose pull
docker compose up -d
./scripts/reload.sh
```

Run Docker commands as an account authorized to access the daemon. Root cron
runs the scripts as root. Do not commit `.env`, certificate state, challenge
files, logs, or deployed static files; `.gitignore` excludes them.

Both image references in `.env.example` include immutable multi-platform
digests. Ordinary `up` starts only Nginx; the Certbot service has a `tools`
profile and is started explicitly with `docker compose run`.

The default configuration starts without any certificates. Unknown HTTP hosts
receive 404; unknown TLS server names are rejected during the handshake.
Templates live outside `nginx/conf.d` and are never loaded automatically.

## Add a website: example.com

### 1. Start the application

In the project's own Compose file:

```yaml
services:
  app:
    image: your-application:1.0.0 # Replace with your actual application image.
    restart: unless-stopped
    ports:
      - "127.0.0.1:18081:8080"
```

The application must listen on `0.0.0.0:8080` inside its container. Nginx reaches
it through `127.0.0.1:18081` on the host. Other projects can use container port
8080 too, but each needs a unique **host** port. Keep databases on the project's
private network without publishing them.

```bash
# Run from the application's directory.
docker compose up -d
curl -I http://127.0.0.1:18081
```

Example allocation; replace these rows with your deployed sites:

| Website names | Host endpoint | Container port | Certificate name |
| --- | --- | --- | --- |
| example.com, www.example.com | 127.0.0.1:18081 | 8080 | example.com |
| app.example.net | 127.0.0.1:18082 | 3000 | app.example.net |

The stable host port survives container recreation, so redeploying an app does
not require a Nginx reload. While an application is stopped, its site can return
502/504; unrelated sites keep working.

### 2. Configure HTTP before issuance

```bash
cd /opt/nginx-stack
cp templates/http.conf.example nginx/conf.d/example.com.conf
./scripts/reload.sh
```

For a different site, edit every `server_name` before reloading. Include `www`
only when its DNS is configured. The HTTP-only template returns 404 outside
the ACME location until HTTPS exists.

Verify the challenge path from outside the server:

```bash
mkdir -p letsencrypt/www/.well-known/acme-challenge
printf 'acme-path-ok\n' > letsencrypt/www/.well-known/acme-challenge/probe
curl --fail http://example.com/.well-known/acme-challenge/probe
curl --fail http://www.example.com/.well-known/acme-challenge/probe
rm letsencrypt/www/.well-known/acme-challenge/probe
```

For additional domains, the reload is essential: writing a new site file does
not change the running Nginx configuration.

### 3. Request the certificate

Replace the email and domain names with your own:

```bash
docker compose run --rm --no-deps certbot certonly \
  --non-interactive \
  --webroot -w /var/www/certbot \
  --cert-name example.com \
  --email admin@example.com \
  --agree-tos \
  -d example.com \
  -d www.example.com
```

Use one certificate per website, including its aliases as needed. Always set
`--cert-name` explicitly; the HTTPS configuration references that lineage name.
All names on the certificate must remain valid for renewal. Do not add HTTPS
configuration if issuance fails. For repeated onboarding experiments, add
`--dry-run` to test issuance without saving a certificate, then issue normally.

Certbot saves its renewal settings under `letsencrypt/conf`. No manual issuance
command is needed again for ordinary renewal. Avoid running multiple issuance
or renewal commands simultaneously; Certbot locks its shared storage.

### 4. Enable HTTPS

After successful issuance:

```bash
cp templates/https.conf.example nginx/conf.d/example.com.conf
./scripts/reload.sh
curl -I http://example.com
curl --fail -I https://example.com
```

For other websites, change hostnames, certificate paths, and the localhost port
before reloading. Replace the HTTP-only file rather than keeping two files with
the same server names. The resulting configuration looks like:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name example.com www.example.com;
    include /etc/nginx/snippets/acme.conf;
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name example.com www.example.com;
    ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:18081;
        include /etc/nginx/snippets/proxy.conf;
    }
}
```

TLS 1.2/1.3 are set in `nginx/nginx.conf`. Shared proxy headers support normal
HTTP and WebSockets. Configure your application's trusted-proxy setting for the
Nginx connection; do not blindly trust arbitrary client forwarding headers.
Upload limits, timeouts, and streaming/SSE buffering belong in the individual
site configuration when needed. No HSTS policy is enabled automatically.

For static sites, follow the same bootstrap and issuance steps, then use
`templates/static.conf.example`. Put readable files under `static/example.com`;
Nginx mounts the directory read-only. The template uses normal file/directory
routing. For an SPA, explicitly change its fallback to `/index.html`.

## Automatic renewal and alerts

First test renewal and reload separately:

```bash
cd /opt/nginx-stack
docker compose run --rm --no-deps certbot renew --dry-run
./scripts/reload.sh
./scripts/renew.sh
sudo ./scripts/check-certificates.sh
```

`renew.sh` saves Certbot's result, then always attempts a validated Nginx reload.
This loads successfully renewed certificates even if another certificate failed.
It exits nonzero if renewal, validation, or reload fails. An invalid configuration
prevents reload, leaving the running configuration in place. Fix or revert the
invalid file before restarting Nginx or retrying the reload.

Nginx is gracefully reloaded after each check, even when no certificate changed.
There are no deploy hooks requiring access from Certbot to Nginx. The dry run
tests ACME validation; it does not replace production certificates or exercise
`renew.sh`'s reload step. Test that step separately as above.

Install the following with `sudo crontab -e` **after replacing MAILTO with your
operations address and confirming cron mail delivery**. Times use the server's
timezone. The failure handler sends recent renewal log output to cron mail;
without it, redirecting output would hide failures from cron mail.

```cron
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=ops@example.com
17 3,15 * * * flock -n /run/lock/nginx-stack-renew.lock /opt/nginx-stack/scripts/renew.sh >> /var/log/nginx-stack-renew.log 2>&1 || { tail -n 50 /var/log/nginx-stack-renew.log; exit 1; }
37 4 * * * /opt/nginx-stack/scripts/check-certificates.sh
```

Use only this renewal scheduler. Certbot is a one-off container, so use `run`,
not `exec`, for Certbot commands. `flock` prevents overlapping scheduled checks.

Certificate checks use the host's OpenSSL and flag missing/invalid certificates
within existing lineages and certificates expired or expiring within 14 days.
Healthy certificates produce no output. Empty storage produces a notice, since
that is normal only before onboarding. Run this script as root so private
certificate directory permissions do not hide lineages. It checks stored files;
also configure your existing external monitoring to check each public HTTPS
endpoint, which catches DNS, serving, and reload problems.

Install this as `/etc/logrotate.d/nginx-stack`, adjusting paths if necessary:

```text
/var/log/nginx-stack-renew.log /opt/nginx-stack/letsencrypt/logs/letsencrypt.log {
    daily
    rotate 14
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
    su root root
}
```

Certbot also rotates its own logs. Nginx uses Docker's bounded `local` logging
driver; inspect it with `docker compose logs --tail 100 nginx`.

## Maintenance

**Inspect certificates and the served certificate:**

```bash
docker compose run --rm --no-deps certbot certificates
timeout 10 openssl s_client -connect example.com:443 -servername example.com \
  </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates
curl --fail -I https://example.com
```

**Remove a website:** remove its file from `nginx/conf.d`, then successfully run
`./scripts/reload.sh`. Only afterward, if no other site uses the certificate:

```bash
docker compose run --rm --no-deps certbot delete \
  --non-interactive --cert-name example.com
```

Update the inventory, remove unused static content, and stop the app separately
if appropriate. Deleting a certificate alone does not remove a Nginx reference.

**Backups:** back up all of `letsencrypt/conf`, including `accounts`, `archive`,
`live` symlinks, and `renewal` settings. Preserve permissions, ownership, and
symlinks. Back up `nginx`, `compose.yml`, `.env`, and `static` too. Store backups
securely because they contain private keys. For example, with an existing secure
backup directory, while no certificate operation is running:

```bash
cd /opt/nginx-stack
sudo tar --acls --xattrs -czpf /secure-backups/nginx-stack.tar.gz \
  letsencrypt/conf nginx compose.yml .env static
```

Restore to a clean stack directory as root with `tar --acls --xattrs -xzpf`, then
start Nginx, validate configuration, inspect served HTTPS, and run renewal's
`--dry-run`. Restoring `live` alone breaks its links into `archive`.

**Updates:** inspect the current official image manifests, update both pinned
references in `.env.example` and the production `.env`, then pull and recreate
Nginx. A changing tag does not override the digest. Validate candidate images
before recreating production and retain previous pins for rollback.

```bash
docker buildx imagetools inspect nginx:stable-alpine
docker buildx imagetools inspect certbot/certbot:latest
docker compose pull
docker compose run --rm --no-deps nginx nginx -t
docker compose up -d nginx
./scripts/reload.sh
docker compose run --rm --no-deps certbot renew --dry-run
```

## Verification

Run the operation tests without Docker or production certificates:

```bash
python3 -m unittest discover -s tests -v
docker compose --env-file .env.example config --quiet
```

To run the optional Linux Docker integration test as a daemon-authorized account:

```bash
RUN_DOCKER_TESTS=1 python3 -m unittest discover -s tests -p 'test_nginx_integration.py' -v
```

It uses the pinned Nginx image, temporary local certificates and backends, and
localhost-only high ports. It verifies bootstrap, HTTP/HTTPS and ACME routing,
aliases, forwarding headers, static sites, unknown hosts, application replacement,
and continued service after configuration validation fails. It removes its test
container and temporary state afterward; it never uses production certificates.

The tests use temporary storage, a stub Docker executable, and temporary local
certificates. They verify partial-renewal failure handling, validation failures,
reload failures, invocation outside the stack directory, and expiry checks.

Before production acceptance, also verify two sites route independently,
unknown hosts are rejected, ACME works for every alias, application ports are
unreachable externally, replacing one app requires no Nginx reload, and a stopped
app does not affect the other site. A real public-domain issuance and renewal dry
run must be performed on the deployed server. Test backup restoration separately.

References: [Docker host networking](https://docs.docker.com/engine/network/drivers/host/),
[Certbot renewal](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates),
[Nginx reloads](https://nginx.org/en/docs/control.html).
