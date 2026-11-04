# Deploy the test site

Deployed on 2026-11-02 and changed on 2026-11-03 (per-run hosts, ADR-009). You need a small Linux VM, a domain in Cloudflare, and `gcloud` access.

1. DNS, in Cloudflare, all type A to the VM IP, proxy off (DNS only): `legacy`, `ai`, and the wildcards `*.legacy` and `*.ai`. The wildcard does not match the bare name, so the explicit records stay.
1a. Large site (ADR-028, 2026-11-04): six more A records to the same IP, DNS only: `legacy-l`, `*.legacy-l`, `ai-l`, `*.ai-l`, `geo-l`, `*.geo-l`. Add the DNS records before you reload Caddy, so the certificate checks of the bare names succeed. Copy `runs/identity_reference.json` to the VM: the server does nothing for a verified agent without it.
2. Install Caddy (the official apt repo) and `uv` on the VM.
3. Add the Cloudflare module to Caddy: `sudo caddy add-package github.com/caddy-dns/cloudflare`.
4. Make a Cloudflare API token: zone `wanderingcodersv.dev` only, permissions Zone DNS Edit and Zone Zone Read, expiry 90 days. Send it to the VM without printing it. Run this on your laptop and paste the token at the hidden prompt:
   ```
   read -rs CF && printf 'CF_API_TOKEN=%s\n' "$CF" | gcloud compute ssh <vm> --zone <zone> --command "sudo install -m 600 -o root -g root /dev/stdin /etc/caddy/cf.env" ; unset CF
   ```
5. Create `/etc/systemd/system/caddy.service.d/cloudflare.conf` with `[Service]` and `EnvironmentFile=/etc/caddy/cf.env`.
6. Copy the repo to `/srv/geobench` (no git here, so use `tar` over `gcloud compute ssh`). Run `uv sync --frozen --no-dev` as the user `geobench`.
7. Copy `Caddyfile` to `/etc/caddy/Caddyfile`. Restart Caddy. The wildcard certificate takes under a minute.
8. Copy `geobench.service` to `/etc/systemd/system/`. Replace `YOUR_PUBLIC_IP` with your laptop's public IP. Run `systemctl enable --now geobench`. The app needs about 15 seconds to start.
9. Check from your laptop:
   - `curl https://legacy.<domain>/robots.txt` returns the robots file.
   - `curl https://<8 random characters>.legacy.<domain>/__health` returns `{"ok":true,...}` from your IP and 404 from any other.
   - A host with a bad token (not 8 characters of a to z and 0 to 9) returns 421.
10. In `plans/*.yaml`, set `domain`, `host_mode: per_run` and `pull_cmd` (a `gcloud compute scp` of `/srv/geobench/runs/requests.jsonl`).

Switching the interceptor: edit `--mode record` to `--mode enforce` in the unit file and run `systemctl restart geobench`. The server reads `identity_reference.json` at start, so restart it after every probe. `identity_reference.json` lives on the laptop. Copy it to `/srv/geobench/runs/` for the server to use it.

Admin IP: only the `--admin-ip` address may call `/__health` and `/__time`. If your home IP changes, edit that line in the unit file and restart the service.

Token rotation: the Cloudflare token expires after 90 days (31 January 2027). The runner stops a batch if the certificate has fewer than 14 days left.

History: ADR-003 first said one wildcard certificate. We used one certificate per host on 2026-11-02, then went back to wildcard certificates on 2026-11-03 for run hosts (ADR-009).
