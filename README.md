# WP Remote Deploy

Desktop GUI (Tkinter) that installs WordPress on remote Ubuntu servers over
SSH, following the steps from the
[Ubuntu WordPress tutorial](https://ubuntu.com/tutorials/install-and-configure-wordpress)
(Apache + MySQL + PHP), but fully automated via [WP-CLI](https://wp-cli.org/)
so no manual web install wizard is needed.

## Requirements

- Python 3.9+
- `pip install -r requirements.txt` (installs `paramiko`)
- Target server: fresh Ubuntu 22.04/24.04 with SSH access (root, or a sudo
  user with passwordless sudo — see note below)
- Port 80 (and 443 if you enable SSL) reachable from the internet if you want
  the site publicly accessible

## Usage

1. `python wp_deploy_gui.py`
2. Paste your rows (tab-separated — copy straight out of Excel/Google
   Sheets/etc.) into the top box and click **Parse rows**.
3. For each pasted column, pick its role from the dropdown (Domain, IP
   Address, SSH Password, WP Admin Email, ...). Columns you don't need can
   stay **Ignore**, or use **Note** to carry them through to the results
   file for your own bookkeeping (e.g. registrar, persona name) without the
   tool acting on them.
4. Set SSH port / default username / auth method (password or key file).
5. Point "Themes folder" at the directory holding per-site theme zips (default
   `C:\olegka\work\workingsites`) — see **Themes** below.
6. Click **Deploy All** (or select specific rows and **Deploy Selected**).
   Progress streams into the Log panel; each row's status updates in the
   table.
7. Click **Export results CSV...** to save a full per-site record (including
   DB passwords). WP admin passwords are also appended automatically to
   `credentials.txt` next to the program — see **Credentials file** below.

## What it does per server

- `apt-get install` apache2, mysql-server, php + common extensions
- Creates a dedicated MySQL database + user for the site (the username is
  capped at MySQL's 32-character limit for long domains)
- Installs WP-CLI if not already present
- Downloads WordPress and runs `wp core install` non-interactively (site
  title, admin email, locale guessed from a "Country" column if you map one).
  **The WP admin login is always the site's domain name** (e.g.
  `example-site.com`); the password is either mapped from a column or
  auto-generated.
- If a matching `<domain>.zip` exists in the themes folder, uploads it over
  SFTP, unzips it into `wp-content/themes/`, and activates it as the site's
  theme (see **Themes** below). If no matching archive is found, the site
  just keeps the default WP theme.
- Writes an Apache vhost for the domain, enables it, reloads Apache
- Opens the firewall for Apache if `ufw` is active
- **Always requests a Let's Encrypt (certbot) certificate** — no checkbox,
  no prompt. If the domain's DNS isn't pointed at the server yet, certbot
  fails and logs `CERTBOT_FAILED`, but the rest of the install still
  completes over plain HTTP; just point DNS and re-run certbot manually
  later.

## Themes

For each site, put its theme in the themes folder as either:

- a plain folder named exactly `<domain>` (e.g. `example-site.com/`)
  containing the theme files (`style.css`, `functions.php`, `index.php`,
  ...) directly inside it — the tool zips it up on the fly before
  uploading, or
- a ready-made `<domain>.zip` (e.g. `example-site.com.zip`) whose single
  top-level folder contains those same files.

If both exist for a domain, the zip takes priority. Either way, the theme
is uploaded over SFTP, unzipped into `wp-content/themes/`, and activated
with `wp theme activate`. A site with neither is left on the default
WordPress theme.

## Credentials file

Every successfully deployed site appends one line to `credentials.txt`,
created next to the program (the `.py`/`.exe`), in the form:

```
domain.com - <wp admin password>
```

This file accumulates across runs — treat it like a password vault.

## Failure diagnostics

If a deploy fails (other than on wrong credentials), the program reconnects to
the server, collects evidence and logs a conclusion after `ВИСНОВОК ДІАГНОСТИКИ`
in the Log panel.

Each run also leaves `/var/log/wp_deploy_diag.log` on the server: a timeline of
steps plus memory, CPU steal and I/O wait samples every 5 seconds. The previous
run's copy is kept as `.prev`.

## Security notes

- **`credentials.txt` and the exported results CSV contain plaintext
  passwords.** Store them somewhere safe and delete them once you've copied
  what you need elsewhere.
- Prefer SSH key auth over password auth where possible.
- If your SSH user isn't `root`, the script runs via `sudo -H bash -s` — that
  requires the user to be able to `sudo` without an interactive password
  prompt (e.g. listed in `/etc/sudoers.d/` with `NOPASSWD:ALL`), otherwise
  the deploy will hang waiting for a password that never comes.
- The tool trusts whatever host key the server presents on first connect
  (`AutoAddPolicy`) for convenience with freshly-provisioned servers. If you
  need strict host-key verification, edit `wp_deploy_core.py` accordingly.
