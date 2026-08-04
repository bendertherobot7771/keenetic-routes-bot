# Keenetic Routes Bot

[🇷🇺 Русский](README.md) | 🇬🇧 English

Telegram bot for managing **native KeeneticOS routing** directly on a router
through Entware.

It works with the same system objects used by the Keenetic web interface:

- **Routing → DNS routes** (`/staticRoutes/dns`);
- **Routing → IPv4 routes** (`/staticRoutes/ipv4`).

The bot does not create custom `iptables`, `ipset`, or DNS services. Changes
remain visible in the web UI and are saved through the standard
`system.configuration.save` command.

> This is an independent project and is not an official Keenetic product.
> Back up the router configuration before first use.

## Features

- View, create, populate, and delete FQDN lists.
- Add and remove domains, IP addresses, and CIDRs in bulk: one per line or
  separated by spaces, commas, or `;`.
- Find and remove requested domains across every FQDN list with a per-list report.
- Warn when a domain already exists or is covered by a wildcard parent domain.
- Remove exact duplicates and redundant subdomains across all FQDN lists.
- Show the real list names configured in the Keenetic web UI.
- Show the number of DNS lists and their total number of entries.
- Create, enable, disable, and delete native DNS routing rules.
- Add and manage native IPv4 routes in bulk.
- List system interface IDs.
- Use `exclusive` DNS routes.
- Ask for confirmation before destructive actions.
- Restrict access to an allowlist of Telegram user IDs.
- Start automatically with Entware after a router reboot.

## How it works

The bot uses Keenetic's local RCI endpoint:

```text
http://127.0.0.1:79/rci
```

It does not require the router administrator password.

| Object | Read | Write |
|---|---|---|
| FQDN lists | `show.sc.object-group.fqdn` | `object-group.fqdn` |
| DNS rules | `show.sc.dns-proxy.route` | `dns-proxy.route` |
| IPv4 routes | `show.sc.ip.route` | `ip.route` |

The Telegram Bot API is used through long polling: no public server, webhook,
or port forwarding is needed.

## Requirements

- KeeneticOS 5.0 or newer;
- Entware installed and configured to start automatically;
- router access to `api.telegram.org`;
- a bot created with [@BotFather](https://t.me/BotFather);
- numeric Telegram user IDs for administrators;
- SSH access to Entware for installation.

The project was tested on a Keenetic Ultra KN-1811 running KeeneticOS 5.1.1.
The installer installs `python3`, `ca-certificates`, and `daemonize` from
Entware when required.

## Create the Telegram bot

1. Open [@BotFather](https://t.me/BotFather).
2. Run `/newbot` and save the bot token.
3. Find your numeric Telegram user ID.
4. Do not publish the token or commit it to Git.

Telegram bots cannot initiate a conversation. After installation, open the bot
and send `/start`.

## Installation

### Download from GitHub

Connect to Entware over SSH and run:

```sh
cd /opt/tmp
wget -O keenetic-routes-bot.tar.gz \
  https://github.com/bendertherobot7771/keenetic-routes-bot/archive/refs/heads/master.tar.gz
tar -xzf keenetic-routes-bot.tar.gz
cd keenetic-routes-bot-master
sh scripts/install.sh
```

### Copy from a computer

Copy the project directory or the Entware archive to `/opt/tmp`, then run:

```sh
cd /opt/tmp/keenetic-routes-bot
sh scripts/install.sh
```

The installer asks for:

1. the BotFather token;
2. allowed Telegram user IDs separated by commas;
3. a default system interface ID, for example `u1Host`.

Verify the installation:

```sh
/opt/bin/keenetic-routes-bot check
/opt/bin/keenetic-routes-bot status
```

## Configuration and service management

The configuration file is stored at:

```text
/opt/etc/keenetic-routes-bot/config.env
```

It is created with mode `600` and contains the BotFather token, administrator
allowlist, and RCI settings.

```dotenv
BOT_TOKEN="1234567890:replace_me"
ALLOWED_USERS="123456789,987654321"
RCI_URL="http://127.0.0.1:79/rci"
DEFAULT_INTERFACE="u1Host"
PRIVATE_CHATS_ONLY="true"
MAX_GROUP_ENTRIES="300"
```

The configuration survives normal upgrades and uninstalls. It is removed only
by `uninstall.sh --purge`, storage cleanup, or Entware storage failure.

After editing it manually, restart the bot:

```sh
/opt/bin/keenetic-routes-bot restart
```

The installer creates this executable Entware init script:

```text
/opt/etc/init.d/S99keenetic-routes-bot
```

It starts the bot automatically after `/opt` is mounted during router boot.

```sh
/opt/bin/keenetic-routes-bot start
/opt/bin/keenetic-routes-bot stop
/opt/bin/keenetic-routes-bot restart
/opt/bin/keenetic-routes-bot status
/opt/bin/keenetic-routes-bot check
/opt/bin/keenetic-routes-bot logs
```

Logs are stored in `/opt/var/log/keenetic-routes-bot.log`. The Telegram token
is never written to the log.

## Telegram commands

| Command | Purpose |
|---|---|
| `/start`, `/menu` | Open the main menu |
| `/lists` | Manage FQDN lists and their entries |
| `/rules` | Manage DNS routing rules |
| `/routes` | Manage native IPv4 routes |
| `/interfaces` | Show system interface IDs |
| `/status` | Show KeeneticOS version and object counts |
| `/cancel` | Cancel the current input flow |
| `/help` | Show short help |

### Domains and addresses

For adding or removing entries, multiple values may be sent vertically:

```text
ya.ru
yandex.ru
yandex.com
yandex.by
yandex.kz
yandex.com.tr
```

They may also be sent on one line, separated by spaces, commas, or `;`:

```text
ya.ru yandex.ru yandex.com
```

These formats work when creating a list, adding domains, and removing domains
from a selected list. The bot displays examples before asking for input.

`*.example.com` is normalized to `example.com`; Keenetic includes subdomains
automatically. URLs such as `https://example.com/page` are intentionally
rejected.

Before adding entries, the bot checks every FQDN list. If a domain already
exists, is covered by an existing parent domain, or would itself cover an
existing subdomain, the bot shows the conflicts and asks whether to continue or
cancel the operation.

The **Remove duplicates** button in the DNS lists section scans every list. It
shows how many entries would be removed before asking for confirmation. The
cleanup:

- keeps the first occurrence of an exact duplicate;
- removes a subdomain when a covering parent domain exists in any list;
- leaves IP addresses and CIDRs unchanged.

For example, when `yandex.ru` exists, `search.yandex.ru` and `mail.yandex.ru`
are redundant because Keenetic routing for `yandex.ru` already applies to all
of its subdomains.

The **Remove domains from lists** button accepts a batch in the same formats,
searches every FQDN list, and removes every exact match. Its result reports:

- the real name of every modified list;
- the domains removed from that list;
- requested domains that were not found anywhere.

Global removal accepts domain names only and does not modify IP addresses or
CIDRs.

### IPv4 routes

Use one line per route:

```text
CIDR INTERFACE description
```

For example:

```text
149.154.160.0/20 u1Host telegram
91.108.4.0/22 u1Host telegram
31.13.64.0/18 u1Host "social networks"
```

If `DEFAULT_INTERFACE` is configured, the interface may be omitted.

## Updating and uninstalling

Download and unpack a fresh `master` archive, then run the installer again.
It preserves `config.env`, updates application and service files, checks RCI,
and starts the bot.

```sh
cd /opt/tmp/keenetic-routes-bot-master
sh scripts/install.sh
```

Uninstall while preserving the configuration:

```sh
sh scripts/uninstall.sh
```

Remove the bot and its configuration:

```sh
sh scripts/uninstall.sh --purge
```

Uninstalling the bot does not modify Keenetic DNS lists or routes.

## DNS routing notes

- Clients must use Keenetic as their DNS server. Application-level DoH or DoT
  can bypass FQDN routing.
- KeeneticOS 5.1 limits one FQDN list to 300 entries. Change
  `MAX_GROUP_ENTRIES` only if your KeeneticOS version supports a different
  limit.
- A list cannot be deleted while a DNS rule references it.
- `exclusive` prevents fallback to another connection if the selected
  interface is unavailable.
- On KeeneticOS 5.1, DNS rules belong to the default connection policy.

Official Keenetic documentation:

- [DNS-based routes](https://support.keenetic.ua/extra/kn-1711/en/51150-dns-based-routes.html)
- [KeeneticOS 5.0: object-group fqdn and dns-proxy route](https://support.keenetic.com/titan/kn-1812/en/100151-os-5-0.html)

## Security

- Never publish `config.env` or a BotFather token.
- Allow only trusted Telegram user IDs.
- Private chats are required by default.
- Do not expose the local RCI port to the internet.
- Change temporary SSH passwords after installation.
- Revoke and replace a leaked Telegram bot token through BotFather.

`.gitignore` excludes `config.env`, `.env`, logs, build artifacts, and Python
cache directories.

## Development

The runtime has no third-party Python dependencies.

```sh
python -m compileall -q keenetic_routes_bot
python -m unittest discover -s tests -v
```

Tests cover RCI models and payloads, domain/CIDR normalization, configuration,
and primary Telegram workflows. The optional local deployment helper
`scripts/deploy_ssh.py` requires `paramiko`; its credentials are supplied via
environment variables and are never stored in the project.

## License

[MIT](LICENSE).

The Entware service and Telegram menu concept was inspired by
[KVAS VPN Bot](https://github.com/flathead/kvas_bot). The RCI implementation
was written independently for native KeeneticOS routing.
