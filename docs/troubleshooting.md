# Troubleshooting

Fixes for common problems, sorted by where they show up.
Error messages are listed as pihello prints them, after `pihello: `, so you can search this page for yours.

**On this page:** [Start with `pihello doctor`](#start-with-pihello-doctor) · [New shells](#new-shells) · [Connection](#connection) ·
[Login](#login) · [Output](#output) · [Still stuck](#still-stuck)

## Start with `pihello doctor`

```sh
pihello doctor
```

It checks every step from the config file to the API session, shows where each setting comes from, and prints a hint for every problem.
Give it the same options as the failing command to test them before you save them, for example `pihello doctor -u https://pi.hole --ca-cert ~/tls_ca.crt`.
See [`pihello doctor`](commands.md#pihello-doctor) for a sample run.

## New shells

### Nothing appears in new shells

That's `-q` hiding an error.
Run `pihello` without `-q` to see it.

If `pihello` on its own works, the startup snippet isn't running or can't find pihello:
the [snippets](integrations.md#shell-greeting) skip pihello in non-interactive shells and when it isn't on the shell's `PATH`.

### New shells are slow when the Pi-hole is offline

pihello waits for the network timeout, 3 seconds by default.
Lower it in the config file:

```toml
# ~/.config/pihello/config.toml
timeout = 1
```

When the Pi-hole is online, a [cache](configuration.md#caching) (`cache_ttl = 30`) makes most greetings skip the network entirely.

## Connection

### `could not resolve host 'pi.hole'`

`pi.hole` only resolves on machines that use the Pi-hole as their DNS server.
Use its IP address or another host name.
If the Pi-hole is your only DNS server and it is down, no names resolve at all.

### `no connection to … within 3s`

The host isn't answering at all, so waiting longer won't help.
Check the address, that the Pi-hole is up, and that you're on its network (for example, not away from home or off the VPN).

### `no response from … within 3s`

pihello connected, but the Pi-hole didn't answer in time.
It may be down or overloaded; raise the limit with `--timeout` (or `timeout` in the config).

### `connection to … refused`

Nothing listens at that address and port.
Check that Pi-hole is running there and that the port is right (a Docker container may publish the web server on another port).

### `… redirects to HTTPS`

pihello never follows redirects, so the password can't be sent to another host by accident.
Use the `https://` URL from the hint, and set up the certificate as described in the next section.

### `TLS certificate verification failed`

Pi-hole's HTTPS certificate is self-signed.
Copy `/etc/pihole/tls_ca.crt` from the Pi-hole and use `--ca-cert tls_ca.crt` (or `verify_tls = "tls_ca.crt"`).
`-k` / `--insecure` skips the check.

The certificate is issued for `pi.hole` (Pi-hole's `webserver.domain`), so connect with that name.
Connecting by IP address fails with a host name mismatch; add `pi.hole` to `/etc/hosts` if your DNS doesn't know it.
[HTTPS and certificates](configuration.md#https-and-certificates) explains every option.

### `TLS handshake with … failed`

The port doesn't seem to speak HTTPS.
Use `http://`, or Pi-hole's HTTPS port (443 by default).

### `… doesn't look like a Pi-hole v6 API`

Something answered, but not a Pi-hole v6 API.
Check that the URL points at the Pi-hole web server (e.g. `http://pi.hole`).
Pi-hole v5 and older aren't supported; use `pihello==0.3.0` for them.

## Login

### `Pi-hole rejected the password`

Check the password.
An app password from *Settings → Web interface / API* works too.

If you're sure the password is right and several shells started at the same moment, it may have been Pi-hole's login rate limit in disguise:
with an app password, Pi-hole can answer "wrong password" instead of "rate-limited".
pihello retries the replies it can recognise as such, and a [cache](configuration.md#caching) (`cache_ttl = 30`) avoids most logins.

### `two-factor authentication enabled`

pihello can't enter TOTP codes.
Create an app password in the Pi-hole web interface (*Settings → Web interface / API → Configure app password*) and use that.
App passwords bypass two-factor authentication.

### `Pi-hole has no free API sessions`

Pi-hole allows 16 API sessions at a time by default (`webserver.api.max_sessions`).
pihello always logs out, but other tools may not.
Old sessions expire after 30 minutes, or you can delete them under *Settings → Web interface / API*.

### `Pi-hole is rate-limiting login attempts`

Pi-hole accepts at most 3 logins per second, so opening many terminals at once (e.g. tmux restoring panes) can trip it.
pihello retries up to twice, after a random 0.35–1.1 s pause and within the timeout.
Set `cache_ttl = 30` so new shells within 30 seconds reuse the last result instead of logging in.

## Output

### `?` instead of a value

The value isn't available on your Pi-hole, or the variable name has a typo.
`pihello --strict` names the culprit, and `pihello vars` shows what's there.
[Missing values](templates.md#missing-values) explains when a value counts as missing and how to replace the `?`.

### `?` in place of symbols like ●, ↑ or box lines

The terminal's locale isn't UTF-8, for example `LANG=en_GB.ISO-8859-1`, which is common on older Raspberry Pi OS installs.
Check it with `locale`.
pihello prints `?` for the characters the encoding can't show.

Switch to a UTF-8 locale: run `sudo raspi-config` → *Localisation Options* → *Locale*, or `sudo dpkg-reconfigure locales`, and pick e.g. `en_GB.UTF-8`.
Or use `--preset plain`, which is ASCII-only.

### Symbols missing from a template saved in Windows PowerShell 5.1

Windows PowerShell 5.1 re-encodes the output of `>` as UTF-16 and loses symbols such as ● and · on the way.
Delete the file and save the preset again with `-o`, which writes UTF-8:

```sh
pihello presets default --source -o ~/.config/pihello/greeting.txt
```

### Wrong or no colors

Set `--color 256` or `--color truecolor` if your terminal supports more colors than pihello detects.
Check that `NO_COLOR` isn't set.
Use `--color always` when piping, since piped output has no colors otherwise.
[Colors](templates.md#colors) explains how pihello picks the color depth.

### Lines wrap or the layout breaks

Each preset has a minimum width; narrower terminals wrap its lines.
`pihello presets --no-preview` lists the widths.
Pick a preset with a smaller width (`status` is the narrowest), clip long lines with `-c` instead of wrapping them, or turn wrapping off with `-W 0`.

## Still stuck

[Open an issue](https://github.com/pavelgar/pihello/issues) with the output of `pihello doctor`; it never contains your password.
For an internal error (exit status 1), add the output of the failing command with `--debug`.
