"""
Core deployment logic: builds the remote bash script (following the Ubuntu
WordPress tutorial: https://ubuntu.com/tutorials/install-and-configure-wordpress)
and runs it over SSH via paramiko, streaming output back through a callback.
"""

import datetime
import os
import posixpath
import re
import secrets
import socket
import string
import tempfile
import threading
import time
import zipfile

import paramiko

COUNTRY_LOCALES = {
    "germany": "de_DE",
    "deutschland": "de_DE",
    "england": "en_GB",
    "uk": "en_GB",
    "united kingdom": "en_GB",
    "great britain": "en_GB",
    "usa": "en_US",
    "united states": "en_US",
    "us": "en_US",
    "ukraine": "uk",
    "france": "fr_FR",
    "spain": "es_ES",
    "italy": "it_IT",
    "poland": "pl_PL",
    "netherlands": "nl_NL",
    "canada": "en_CA",
    "austria": "de_DE",
    "switzerland": "de_CH",
}

PHP_VERSION = "8.4"

CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY = 15  # seconds between attempts

# After a failed run, how long to keep trying to get back in to collect evidence.
RECOVERY_ATTEMPTS = 10
RECOVERY_RETRY_DELAY = 30
COLLECT_MAX_SECONDS = 120
SILENCE_NOTICE_SECONDS = 60

# Failure evidence is written here so it survives the SSH session dying. Runs
# first in the script. A separate monitor process samples the server every 5s;
# the SCRIPT_SIGNAL / SCRIPT_EXIT markers tell how the script ended (neither
# marker means SIGKILL, which cannot be trapped).
_DIAG_START = r"""
DIAG_LOG=/var/log/wp_deploy_diag.log
if [ -f "$DIAG_LOG" ]; then mv -f "$DIAG_LOG" "$DIAG_LOG.prev" || true; fi
diag_note() { echo "$(date '+%F %T %Z') $*" >> "$DIAG_LOG" 2>/dev/null || true; }
diag_note "RUN_START pid=$$ boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
for diag_sig in HUP INT TERM PIPE; do
  trap "diag_note SCRIPT_SIGNAL=$diag_sig; exit 1" "$diag_sig"
done
trap 'diag_rc=$?; diag_note "SCRIPT_EXIT rc=$diag_rc"' EXIT

DIAG_MON=$(mktemp /tmp/wp_diag_mon.XXXXXX) || DIAG_MON=/tmp/wp_diag_mon.$$
cat > "$DIAG_MON" <<'DIAGMON'
target=$1
log=$2
read_cpu() {
  read -r _ u n s i w q sq st _ < /proc/stat
  st=${st:-0}
  cpu_total=$((u+n+s+i+w+q+sq+st)); cpu_wait=$w; cpu_steal=$st
}
read_cpu
p_total=$cpu_total; p_wait=$cpu_wait; p_steal=$cpu_steal
SECONDS=0
while kill -0 "$target" 2>/dev/null && [ "$SECONDS" -lt 7200 ]; do
  sleep 5
  read_cpu
  dt=$((cpu_total-p_total)); [ "$dt" -gt 0 ] || dt=1
  steal=$((100*(cpu_steal-p_steal)/dt)); iow=$((100*(cpu_wait-p_wait)/dt))
  p_total=$cpu_total; p_wait=$cpu_wait; p_steal=$cpu_steal
  avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
  swapf=$(awk '/^SwapFree:/{print int($2/1024)}' /proc/meminfo)
  top=$(ps -eo rss=,comm= --sort=-rss 2>/dev/null | awk 'NR==1{printf "%s:%dMB",$2,$1/1024}')
  echo "$(date '+%F %T') SAMPLE up=$(cut -d. -f1 /proc/uptime)s load=$(cut -d' ' -f1 /proc/loadavg) avail=${avail}MB swapfree=${swapf}MB steal=${steal}% iowait=${iow}% top=$top" >> "$log"
  sync "$log" 2>/dev/null
done
echo "$(date '+%F %T') MONITOR_END" >> "$log"
sync "$log" 2>/dev/null
rm -f -- "$0"
DIAGMON
setsid nohup bash "$DIAG_MON" "$$" "$DIAG_LOG" >/dev/null 2>&1 </dev/null &

echo 'Preparing server. Environment:'
echo "DIAG boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
echo "DIAG uptime: $(uptime 2>/dev/null)"
echo "DIAG virt: $(systemd-detect-virt 2>/dev/null; true)"
echo "DIAG kernel: $(uname -r), cpus: $(nproc 2>/dev/null)"
echo "DIAG memory.max: $(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo n/a)"
echo "DIAG disk: $(df -h / 2>/dev/null | tail -n1)"
free -m 2>/dev/null || true
echo 'DIAG largest processes (RSS KB):'
ps -eo rss,comm --sort=-rss 2>/dev/null | head -n 8 || true
"""

# Retries ONLY actual lock contention (up to 15 min: 180 x 5s), logging every
# 6th attempt (~30s). Any other apt-get failure (missing package, no candidate,
# broken repo, ...) is permanent - retrying it for 15 minutes would just look
# like the tool hung, so fail fast. Output is streamed, not buffered, so that a
# run dying inside apt-get shows exactly how far it got. stdin is /dev/null
# because this script itself is being read from stdin (bash -s).
_APT_RETRY = r"""
apt_retry() {
  local n=0 max=180 out rc
  out=$(mktemp)
  diag_note "STEP apt-get $*"
  until [ "$n" -ge "$max" ]; do
    apt-get "$@" </dev/null 2>&1 | tee "$out"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 0 ]; then
      diag_note "STEP_DONE apt-get $1"
      rm -f "$out"
      return 0
    fi
    echo "apt-get $1 exited with code $rc"
    if [ "$rc" -gt 128 ]; then echo "apt-get $1 was killed by signal $((rc-128))"; fi
    diag_note "STEP_FAILED apt-get $1 rc=$rc"
    if ! grep -qi 'could not get lock\|unable to acquire the dpkg\|resource temporarily unavailable' "$out"; then
      rm -f "$out"
      return 1
    fi
    n=$((n+1))
    if [ $((n % 6)) -eq 0 ]; then
      echo "apt/dpkg is locked (probably unattended-upgrades), retrying ($n/$max)..."
    fi
    sleep 5
  done
  rm -f "$out"
  return 1
}
"""

# Run on a fresh connection after a failure. COLLECT lines feed diagnose_failure.
_DIAG_COLLECT = r"""
L=/var/log/wp_deploy_diag.log
KERN_RE='out of memory|oom-kill|killed process'
sec() { echo; echo "=== $* ==="; }
echo "COLLECT boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
echo "COLLECT virt=$(systemd-detect-virt 2>/dev/null; true)"
if dmesg >/dev/null 2>&1; then echo "COLLECT dmesg_readable=1"; else echo "COLLECT dmesg_readable=0"; fi
echo "COLLECT oom_kernel=$( { dmesg 2>/dev/null; journalctl -k -b --no-pager 2>/dev/null; } | grep -ciE "$KERN_RE")"
echo "COLLECT cgroup_oom_kill=$(find /sys/fs/cgroup -name memory.events -exec cat {} + 2>/dev/null | awk '/^oom_kill /{s+=$2} END{print s+0}')"
echo "COLLECT ubc_failcnt=$(awk 'NR>2{s+=$NF} END{print s+0}' /proc/user_beancounters 2>/dev/null)"
if [ -f "$L" ]; then
  echo "COLLECT script_exit=$(grep -o 'SCRIPT_EXIT rc=[0-9]*' "$L" | tail -n1 | cut -d= -f2)"
  echo "COLLECT script_signal=$(grep -o 'SCRIPT_SIGNAL=[A-Z]*' "$L" | tail -n1 | cut -d= -f2)"
  echo "COLLECT last_step=$(grep ' STEP' "$L" | tail -n1 | cut -d' ' -f4-)"
  RUN_PID=$(sed -n 's/.*RUN_START pid=\([0-9]*\).*/\1/p' "$L" | tail -n1)
  if [ -n "$RUN_PID" ] && kill -0 "$RUN_PID" 2>/dev/null; then echo "COLLECT script_running=1"; else echo "COLLECT script_running=0"; fi
  awk '/ SAMPLE /{for(i=1;i<=NF;i++){if($i~/^avail=/){v=$i;gsub(/[^0-9]/,"",v);v+=0;if(!seen||v<a){a=v;seen=1}}if($i~/^steal=/){v=$i;gsub(/[^0-9]/,"",v);v+=0;if(v>s)s=v}}}END{if(seen)print "COLLECT min_avail_mb=" a;print "COLLECT max_steal_pct=" s+0}' "$L"
else
  echo "COLLECT diag_log_missing=1"
fi
echo "COLLECT dpkg_broken=$(dpkg --audit 2>/dev/null | wc -l)"
sec Time; date; date -u; uptime
sec Reboots and shutdowns; last -x reboot shutdown 2>/dev/null | head -n 6
sec Memory; free -m; cat /sys/fs/cgroup/memory.max 2>/dev/null
sec Kernel messages about memory, disk and hangs
{ dmesg -T 2>/dev/null || journalctl -k -b --no-pager 2>/dev/null; } \
  | grep -iE "$KERN_RE|segfault|blocked for more|hung task|i/o error|ext4-fs error|soft lockup" | tail -n 30
sec Kernel log of the previous boot; journalctl -k -b -1 --no-pager -n 30 2>/dev/null
sec OpenVZ beancounters; cat /proc/user_beancounters 2>/dev/null
sec Run timeline
if [ -f "$L" ]; then grep -v ' SAMPLE ' "$L" | tail -n 40; sec Last monitor samples; grep ' SAMPLE ' "$L" | tail -n 24; fi
sec sshd log; journalctl -u ssh --since '-2 hours' --no-pager -n 30 2>/dev/null
sec System log: kills, restarts, failures
journalctl --since '-2 hours' --no-pager 2>/dev/null | grep -iE 'kill|oom|shutdown|reboot|failed|watchdog' | tail -n 40
sec Largest processes; ps -eo rss,etime,comm --sort=-rss 2>/dev/null | head -n 12
sec Processes running now as apt, dpkg, mysql; ps -eo pid,etime,args 2>/dev/null | grep -E '[a]pt-get|[d]pkg|[m]ysqld' | cut -c1-150
sec dpkg state; dpkg --audit 2>/dev/null | head -n 20
sec MySQL service; systemctl status mysql --no-pager -n 10 2>/dev/null | cut -c1-200
sec Last apt terminal log; tail -n 30 /var/log/apt/term.log 2>/dev/null
"""

# PHP repository + install, hardened with what was learned upgrading the live
# servers:
#   * add-apt-repository needs api.launchpad.net; on some hosts that times out
#     (Python traceback, nothing added) -> add ppa:ondrej/php by hand with its
#     signing key from keyserver.ubuntu.com, over IPv4;
#   * the PPA is deprecated: for the newest Ubuntu releases it has no suite,
#     or an empty one (resolute/26.04) -> packages.sury.org, which also takes
#     over when the PPA is already configured but still has no php<ver>;
#   * never continue (and never touch Apache) unless php<ver> is really
#     installable - otherwise Apache ends up with no PHP module at all;
#   * leave exactly one mod_php enabled (an older one, e.g. Ubuntu's own 8.1,
#     must be disabled or Apache serves the wrong version).
# $PHPV must be set before this block runs. OS_RELEASE is overridable for tests.
_PHP_SETUP = r"""
. "${OS_RELEASE:-/etc/os-release}"
add_ondrej_manual() {
  local k tmp
  tmp=$(mktemp)
  for k in 14AA40EC0831756756D7F66C4F4EA0AAE5267A6C B8DC7E53946656EFBCE4C1DD71DAEAAB4AD4CAB6; do
    curl -4 -fsSL -m 30 "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x$k" 2>/dev/null \
      | gpg --dearmor 2>/dev/null >> "$tmp" || true
  done
  if [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    echo "PHP_REPO_FAILED: could not download the ondrej/php signing key from keyserver.ubuntu.com."
    exit 1
  fi
  install -m 644 "$tmp" /usr/share/keyrings/ondrej-php.gpg
  rm -f "$tmp"
  echo "deb [signed-by=/usr/share/keyrings/ondrej-php.gpg] https://ppa.launchpadcontent.net/ondrej/php/ubuntu $VERSION_CODENAME main" \
    > /etc/apt/sources.list.d/ondrej-php.list
  echo "ondrej/php added by hand (/etc/apt/sources.list.d/ondrej-php.list)."
}
php_available() { apt-cache policy "php$PHPV-cli" 2>/dev/null | grep -qE 'Candidate: [0-9]'; }
# The PPA can publish a suite with no packages in it (resolute/26.04 did), so
# check for php<ver>-cli itself, not just that the suite's Release file exists.
ppa_has_php() {
  curl -4 -fsSL -m 30 "https://ppa.launchpadcontent.net/ondrej/php/ubuntu/dists/$VERSION_CODENAME/main/binary-$(dpkg --print-architecture)/Packages.gz" 2>/dev/null \
    | gunzip 2>/dev/null | grep -q "^Package: php$PHPV-cli\$"
}
use_sury() {
  rm -f /etc/apt/sources.list.d/ondrej-ubuntu-php-*.list /etc/apt/sources.list.d/ondrej-ubuntu-php-*.sources \
        /etc/apt/sources.list.d/ondrej-php.list
  if ! grep -rqs 'packages.sury.org' /etc/apt/sources.list /etc/apt/sources.list.d/; then
    curl -4 -fsSL -m 60 -o /usr/share/keyrings/deb.sury.org-php.gpg https://packages.sury.org/php/apt.gpg
    printf 'Types: deb\nURIs: https://packages.sury.org/php/\nSuites: %s\nComponents: main\nSigned-By: /usr/share/keyrings/deb.sury.org-php.gpg\n' \
      "$VERSION_CODENAME" > /etc/apt/sources.list.d/php.sources
  fi
}
sury_configured() { grep -rqs 'packages.sury.org' /etc/apt/sources.list /etc/apt/sources.list.d/; }

if php_available; then
  echo "PHP $PHPV packages are already available."
else
  if [ -n "${VERSION_CODENAME:-}" ] && ! ppa_has_php; then
    echo "ppa:ondrej/php has no PHP $PHPV for '$VERSION_CODENAME' - using packages.sury.org instead."
    use_sury
  elif ! grep -rqs 'ondrej' /etc/apt/sources.list /etc/apt/sources.list.d/; then
    echo "Adding the ondrej/php repository..."
    AAR_RC=0
    AAR_OUT=$(timeout 120 add-apt-repository -y ppa:ondrej/php 2>&1 </dev/null) || AAR_RC=$?
    if [ "$AAR_RC" -ne 0 ]; then
      echo "add-apt-repository failed with exit code $AAR_RC (137 = killed, 124 = timed out):"
      printf '%s\n' "$AAR_OUT" | tail -n 5
      echo "Adding ppa:ondrej/php by hand instead..."
      rm -f /etc/apt/sources.list.d/ondrej-ubuntu-php-*.list /etc/apt/sources.list.d/ondrej-ubuntu-php-*.sources
      add_ondrej_manual
    fi
  fi
  apt_retry update -y $APT_RELEASEINFO_OK || echo "WARNING: apt-get update reported errors - continuing."
  # ondrej/php is deprecated in favour of packages.sury.org: if it still has
  # no php<ver> (e.g. it was added on an earlier run), switch over and retry.
  if ! php_available && [ -n "${VERSION_CODENAME:-}" ] && ! sury_configured; then
    echo "PHP $PHPV is still not available from ondrej/php - switching to packages.sury.org."
    use_sury
    apt_retry update -y $APT_RELEASEINFO_OK || echo "WARNING: apt-get update reported errors - continuing."
  fi
  if ! php_available; then
    echo "PHP_REPO_FAILED: no PHP $PHPV packages are available (neither ondrej/php nor packages.sury.org has them for this server?)."
    apt-cache policy "php$PHPV-cli" 2>&1 | head -n 5 || true
    exit 1
  fi
fi
"""

# Runs after the PHP packages are installed: exactly one mod_php (ours).
_PHP_SWITCH = r"""
if [ ! -e "/etc/apache2/mods-available/php$PHPV.load" ]; then
  echo "PHP_INSTALL_FAILED: libapache2-mod-php$PHPV is not installed - Apache would have no PHP."
  exit 1
fi
for m in $(ls /etc/apache2/mods-enabled 2>/dev/null | sed -n 's/^\(php[0-9][0-9]*\.[0-9][0-9]*\)\.load$/\1/p'); do
  if [ "$m" != "php$PHPV" ]; then
    echo "Disabling $m (only PHP $PHPV should be active)."
    a2dismod "$m" >/dev/null
  fi
done
a2enmod "php$PHPV" >/dev/null
update-alternatives --set php "/usr/bin/php$PHPV" >/dev/null 2>&1 \
  || echo "WARNING: could not switch the php CLI to /usr/bin/php$PHPV."
echo "PHP CLI: $(php -r 'echo PHP_VERSION;' 2>/dev/null || echo none)"
"""

# Confirms over HTTP (through Apache on localhost) which PHP the site gets.
# $WEB_ROOT and $DOMAIN must be set.
_PHP_VERIFY = r"""
PHPCHK="phpver_$(date +%s)_$RANDOM.php"
printf '<?php echo "PHPVER=" . PHP_VERSION;' > "$WEB_ROOT/$PHPCHK"
chmod 644 "$WEB_ROOT/$PHPCHK"
WEBVER=$(curl -skL --max-time 30 --resolve "$DOMAIN:80:127.0.0.1" --resolve "$DOMAIN:443:127.0.0.1" \
  "http://$DOMAIN/$PHPCHK" 2>/dev/null | sed -n 's/.*PHPVER=\([0-9][0-9.]*\).*/\1/p' | head -n1 || true)
rm -f "$WEB_ROOT/$PHPCHK"
case "$WEBVER" in
  "$PHPV".*) echo "PHP_CHECK_OK: Apache serves PHP $WEBVER." ;;
  "") echo "WARNING: could not confirm the PHP version over HTTP." ;;
  *)
    echo "PHP_VERSION_MISMATCH: Apache serves PHP $WEBVER instead of $PHPV.x."
    ls /etc/apache2/mods-enabled/php* 2>/dev/null || true
    exit 1
    ;;
esac
"""


def slugify(domain: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", domain.strip().lower()).strip("_")
    return slug[:32] if slug else "site"


def gen_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def locale_for_country(country: str) -> str:
    if not country:
        return "en_US"
    return COUNTRY_LOCALES.get(country.strip().lower(), "en_US")


def sh_quote(value: str) -> str:
    """Safe single-quote escaping for embedding values in the bash heredoc."""
    return "'" + str(value).replace("'", "'\\''") + "'"


_SKIP_DIRS = {".git", ".svn", "node_modules", "__MACOSX", ".idea", ".vscode"}
_SKIP_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}

_STYLE_HEADER = (
    "/*\n"
    "Theme Name: {name}\n"
    "Version: 1.0\n"
    "*/\n"
)


def locate_theme_root(folder_path: str, domain: str = "", max_depth: int = 4):
    """
    Find the directory that is the *real* WordPress theme root, i.e. the one
    that directly contains style.css. The user's site folder is not always
    the theme itself: the theme may sit one or two levels deeper
    (<domain>/<domain>/style.css, <domain>/wp-content/themes/<name>/style.css,
    ...). Breadth-first, so the shallowest match wins; if several themes sit
    at the same depth, prefer one whose name matches the domain.
    Returns the directory path, or None if no style.css exists anywhere.
    """
    level = [folder_path]
    dom = domain.lower()
    dom_slug = slugify(domain) if domain else ""
    for _depth in range(max_depth + 1):
        found, next_level = [], []
        for cur in level:
            try:
                entries = os.listdir(cur)
            except OSError:
                continue
            if any(e.lower() == "style.css" for e in entries):
                found.append(cur)
            for e in entries:
                full = os.path.join(cur, e)
                if e.lower() not in _SKIP_DIRS and os.path.isdir(full):
                    next_level.append(full)
        if found:
            for cand in found:
                base = os.path.basename(cand).lower()
                if dom and (base == dom or slugify(base) == dom_slug):
                    return cand
            # skip bundled default themes if there is something else
            custom = [c for c in found
                      if not os.path.basename(c).lower().startswith("twenty")]
            return (custom or found)[0]
        level = next_level
    return None


def _zip_theme_folder(folder_path: str, domain: str) -> str:
    """Zip the theme found inside folder_path into a temp .zip with `domain`
    as the single top-level directory, and with style.css guaranteed to be
    directly inside that directory (WordPress refuses to see a theme
    otherwise - that is what produces \"The '<domain>' theme could not be
    found\" on `wp theme activate`)."""
    root = locate_theme_root(folder_path, domain) or folder_path

    fd, tmp_zip_path = tempfile.mkstemp(prefix=f"theme_{slugify(domain)}_", suffix=".zip")
    os.close(fd)
    has_style = False
    with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS]
            for name in files:
                if name.lower() in _SKIP_FILES:
                    continue
                file_path = os.path.join(cur, name)
                rel = os.path.relpath(file_path, root).replace(os.sep, "/")
                if rel.lower() == "style.css":
                    # normalise "Style.css" -> "style.css" (Linux is case-sensitive)
                    rel = "style.css"
                    has_style = True
                    with open(file_path, "rb") as f:
                        data = f.read()
                    if b"theme name:" not in data[:8192].lower():
                        data = _STYLE_HEADER.format(name=domain).encode("utf-8") + data
                    zf.writestr(f"{domain}/{rel}", data)
                    continue
                zf.write(file_path, f"{domain}/{rel}")
        if not has_style:
            # No style.css anywhere (plain PHP/block theme without header):
            # add a minimal one so WordPress can register the theme.
            zf.writestr(f"{domain}/style.css", _STYLE_HEADER.format(name=domain))
    return tmp_zip_path


def _find_theme_entry(themes_dir: str, domain: str):
    """Case-insensitive lookup of <domain>.zip or <domain>/ (also tries the
    name without a leading 'www.'). Returns (path, is_dir) or (None, False)."""
    wanted = {domain.lower()}
    if domain.lower().startswith("www."):
        wanted.add(domain.lower()[4:])
    try:
        entries = os.listdir(themes_dir)
    except OSError:
        return None, False
    for e in entries:
        full = os.path.join(themes_dir, e)
        low = e.lower()
        if low.endswith(".zip") and low[:-4] in wanted and os.path.isfile(full):
            return full, False
    for e in entries:
        full = os.path.join(themes_dir, e)
        if e.lower() in wanted and os.path.isdir(full):
            return full, True
    return None, False


def find_theme_source(themes_dir: str, domain: str):
    """
    Look for a theme for `domain` in themes_dir: either a ready-made
    <domain>.zip, or a plain <domain>/ folder (zipped on the fly into a temp
    file). Returns (zip_path, is_temp) or (None, False) if neither exists.
    """
    if not themes_dir:
        return None, False
    themes_dir = os.path.normpath(themes_dir.strip().strip('"'))
    path, is_dir = _find_theme_entry(themes_dir, domain)
    if not path:
        return None, False
    if is_dir:
        return _zip_theme_folder(path, domain), True
    return path, False


_creds_lock = threading.Lock()


def append_credentials(path: str, domain: str, password: str) -> None:
    """Append a 'domain - password' line to the credentials file (thread-safe)."""
    with _creds_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{domain} - {password}\n")


def build_script(job: dict) -> str:
    """
    job keys: domain, wp_title, wp_admin_user, wp_admin_password,
    wp_admin_email, db_password, locale, remote_theme_zip (optional).
    SSL (Let's Encrypt) is always requested.
    """
    php = job.get("php_version") or PHP_VERSION
    domain = job["domain"]
    slug = slugify(domain)
    db_name = f"wp_{slug}"
    # MySQL limits account names to 32 characters; reserve room for "wp_".
    db_user = f"wp_{slug[:29]}"
    web_root = posixpath.join("/var/www", domain)

    q = sh_quote
    parts = [
        "set -e",
        "export DEBIAN_FRONTEND=noninteractive",
        _DIAG_START,
        # Some freshly-booted VPS images have no working DNS resolution for
        # the first stretch after boot (or a broken /etc/resolv.conf out of
        # the box). Wait up to 2 minutes for it to come up on its own, then
        # fall back to public resolvers so apt isn't stuck forever.
        "dns_ok() { getent hosts archive.ubuntu.com >/dev/null 2>&1; }",
        "n=0; until dns_ok || [ \"$n\" -ge 24 ]; do sleep 5; n=$((n+1)); done",
        "if ! dns_ok; then "
        "echo 'DNS still not resolving after 2 minutes - falling back to public resolvers.'; "
        "printf 'nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n' > /etc/resolv.conf; fi",
        # Fresh Ubuntu servers almost always run unattended-upgrades right
        # after boot, which can hold the dpkg lock for several minutes
        # (longer with a kernel update in the queue). Stop/mask the timers
        # that would re-trigger it so it doesn't grab the lock again mid-run,
        # then wait out whatever instance is already running.
        "systemctl stop apt-daily.timer apt-daily-upgrade.timer "
        "unattended-upgrades.service >/dev/null 2>&1 || true",
        "systemctl mask apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true",
        _APT_RETRY,
        # Third-party repos sometimes rename themselves (ondrej/php became
        # "PPA for PHP (deprecated)"), and apt-get update then fails with
        # rc=100 until the change is accepted. Accept Label/Origin changes
        # only; suite/codename changes still need a human.
        "APT_RELEASEINFO_OK='-o Acquire::AllowReleaseInfoChange::Label=true "
        "-o Acquire::AllowReleaseInfoChange::Origin=true'",
        "apt_retry update -y $APT_RELEASEINFO_OK",
        # Some minimal cloud images ship with only the 'main' component
        # enabled, which is missing most PHP packages (they live in
        # 'universe') and can even make plain 'curl' look uninstallable.
        "apt_retry install -y software-properties-common curl ca-certificates gnupg",
        # add-apt-repository is a memory-hungry Python tool that has been seen
        # getting SIGKILLed on small VPSes, so only run it when actually needed.
        "enable_component() { apt-cache policy | grep -q \"/$1 \" && return 0; "
        "local rc=0; timeout 120 add-apt-repository -y \"$1\" >/dev/null 2>&1 </dev/null || rc=$?; "
        "[ \"$rc\" -eq 0 ] || echo \"add-apt-repository $1 exited with code $rc "
        "(137 = killed, 124 = timed out)\"; }",
        "enable_component universe",
        "enable_component multiverse",
        f"PHPV={php}",
        "diag_note STEP php-repository",
        _PHP_SETUP,
        f"apt_retry install -y apache2 mysql-server php{php} php{php}-cli "
        f"php{php}-mysql php{php}-curl php{php}-gd php{php}-mbstring "
        f"php{php}-xml php{php}-zip libapache2-mod-php{php} unzip wget curl",
        _PHP_SWITCH,
        "diag_note STEP services-and-wordpress",
        "a2enmod rewrite >/dev/null 2>&1 || true",
        "systemctl enable apache2 mysql >/dev/null 2>&1 || true",
        "systemctl start apache2 mysql",
        # WP-CLI
        "if ! command -v wp >/dev/null 2>&1; then "
        "curl -sS -o /usr/local/bin/wp "
        "https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar && "
        "chmod +x /usr/local/bin/wp; fi",
        f"mkdir -p {q(web_root)}",
        f"cd {q(web_root)}",
        # Database: gated on wp-config.php (like the wp config create step
        # below) so a retry against an already-provisioned site never resets
        # the DB user's password to this run's freshly-generated one while
        # leaving the old password sitting in the existing wp-config.php.
        f"if [ ! -f wp-config.php ]; then mysql -u root <<'SQL'\n"
        f"CREATE DATABASE IF NOT EXISTS `{db_name}` DEFAULT CHARACTER SET utf8mb4;\n"
        f"CREATE USER IF NOT EXISTS '{db_user}'@'localhost' IDENTIFIED BY '{job['db_password']}';\n"
        f"ALTER USER '{db_user}'@'localhost' IDENTIFIED BY '{job['db_password']}';\n"
        f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_user}'@'localhost';\n"
        f"FLUSH PRIVILEGES;\n"
        f"SQL\n"
        f"fi",
        f"if [ ! -f wp-load.php ]; then wp core download --allow-root --locale={q(job['locale'])}; fi",
        f"if [ ! -f wp-config.php ]; then wp config create --allow-root "
        f"--dbname={q(db_name)} --dbuser={q(db_user)} --dbpass={q(job['db_password'])} --dbhost=localhost; fi",
        f"if ! wp core is-installed --allow-root >/dev/null 2>&1; then "
        f"wp core install --allow-root --url={q('http://' + domain)} --title={q(job['wp_title'])} "
        f"--admin_user={q(job['wp_admin_user'])} --admin_password={q(job['wp_admin_password'])} "
        f"--admin_email={q(job['wp_admin_email'])} --skip-email; fi",
        # wp core install only sets the password on the very first install;
        # a retry against an already-installed site (e.g. after an earlier
        # attempt failed on a later step) would otherwise leave the live WP
        # password out of sync with whatever this run writes to
        # credentials.txt. Force it to match on every run so the file is
        # always accurate.
        f"wp user update {q(job['wp_admin_user'])} --user_pass={q(job['wp_admin_password'])} "
        f"--allow-root",
        # WP File Manager, so sites can be managed from the WP admin without
        # needing separate SSH/SFTP access per site.
        "if ! wp plugin is-installed wp-file-manager --allow-root; then "
        "wp plugin install wp-file-manager --allow-root; fi",
        "wp plugin activate wp-file-manager --allow-root",
        f"chown -R www-data:www-data {q(web_root)}",
        f"find {q(web_root)} -type d -exec chmod 755 {{}} \\;",
        f"find {q(web_root)} -type f -exec chmod 644 {{}} \\;",
    ]

    if job.get("remote_theme_zip"):
        remote_zip = job["remote_theme_zip"]
        themes_dir = posixpath.join(web_root, "wp-content", "themes")
        parts += [
            "THEME_STAGE=$(mktemp -d)",
            f"unzip -o {q(remote_zip)} -d \"$THEME_STAGE\" >/dev/null",
            f"rm -f {q(remote_zip)}",
            # The theme root is whatever directory actually holds style.css
            # (shallowest one). Do NOT assume it is the first top-level dir
            # of the zip - ready-made zips are often nested differently.
            'THEME_CSS=$(find "$THEME_STAGE" -type f -iname style.css '
            "| awk '{print length($0) \"\\t\" $0}' | sort -n | head -n1 | cut -f2-)",
            'if [ -z "$THEME_CSS" ]; then echo "THEME_NO_STYLE_CSS (no style.css in the uploaded theme)"; else '
            'THEME_SRC_DIR=$(dirname "$THEME_CSS"); '
            'if [ "$THEME_SRC_DIR" = "$THEME_STAGE" ]; then THEME_SLUG=' + q(domain) + '; '
            'else THEME_SLUG=$(basename "$THEME_SRC_DIR"); fi; '
            f'rm -rf {q(themes_dir)}/"$THEME_SLUG"; '
            f'mkdir -p {q(themes_dir)}/"$THEME_SLUG"; '
            f'cp -a "$THEME_SRC_DIR"/. {q(themes_dir)}/"$THEME_SLUG"/; '
            f'chown -R www-data:www-data {q(themes_dir)}/"$THEME_SLUG"; '
            f'find {q(themes_dir)}/"$THEME_SLUG" -type d -exec chmod 755 {{}} \\; ; '
            f'find {q(themes_dir)}/"$THEME_SLUG" -type f -exec chmod 644 {{}} \\; ; '
            'echo "Theme installed as: $THEME_SLUG"; '
            f'wp theme activate "$THEME_SLUG" --allow-root --path={q(web_root)} || {{ '
            'echo THEME_ACTIVATE_FAILED; '
            f'ls -la {q(themes_dir)}/"$THEME_SLUG" | head -n 25; '
            f'wp theme list --allow-root --path={q(web_root)} || true; }}; fi',
            'rm -rf "$THEME_STAGE"',
        ]

    parts += [
        "diag_note STEP apache-vhost",
        # Apache vhost
        f"cat > /etc/apache2/sites-available/{slug}.conf <<VHOST\n"
        f"<VirtualHost *:80>\n"
        f"    ServerName {domain}\n"
        f"    ServerAlias www.{domain}\n"
        f"    DocumentRoot {web_root}\n"
        f"    <Directory {web_root}>\n"
        f"        AllowOverride All\n"
        f"        Require all granted\n"
        f"    </Directory>\n"
        f"    ErrorLog \\${{APACHE_LOG_DIR}}/{slug}-error.log\n"
        f"    CustomLog \\${{APACHE_LOG_DIR}}/{slug}-access.log combined\n"
        f"</VirtualHost>\n"
        f"VHOST",
        f"a2ensite {slug}.conf >/dev/null",
        # A previous (possibly interrupted) run on this server may have left
        # an enabled *-le-ssl.conf from certbot whose shared
        # /etc/letsencrypt/options-ssl-apache.conf is now missing, which
        # breaks Apache entirely and blocks every reload from here on. If
        # configtest is currently broken, disable any such leftover SSL
        # vhosts first - the certbot step further down will recreate them
        # (and the missing shared file) correctly.
        "if ! apache2ctl configtest >/dev/null 2>&1; then "
        "echo 'apache2 configtest is currently failing - disabling any leftover "
        "*-le-ssl.conf from an earlier run so certbot can recreate them cleanly.'; "
        "for f in /etc/apache2/sites-enabled/*-le-ssl.conf; do "
        "[ -e \"$f\" ] || continue; "
        "a2dissite \"$(basename \"$f\" .conf)\" >/dev/null 2>&1 || true; "
        "done; fi",
        # Surface *why* on failure - "Job for apache2.service failed" alone
        # doesn't say what's actually wrong (bad vhost, stale certbot SSL
        # conf referencing a missing cert, ...); configtest + the tail of
        # the journal usually does.
        "if ! systemctl reload apache2; then "
        "echo 'APACHE_RELOAD_FAILED - diagnostics follow:'; "
        "apache2ctl configtest 2>&1 || true; "
        "journalctl -xeu apache2.service --no-pager -n 30 2>&1 || true; "
        "exit 1; fi",
        "if command -v ufw >/dev/null 2>&1 && ufw status | grep -q 'Status: active'; then "
        "ufw allow 'Apache Full' >/dev/null 2>&1 || true; fi",
        f"WEB_ROOT={q(web_root)}",
        f"DOMAIN={q(domain)}",
        _PHP_VERIFY,
    ]

    # SSL is mandatory: always request a Let's Encrypt certificate. If DNS
    # isn't pointed at the server yet, certbot fails but the rest of the
    # install (already done above) still stands on plain HTTP.
    parts += [
        "apt_retry install -y certbot python3-certbot-apache",
        f"certbot --apache -d {q(domain)} -d {q('www.' + domain)} "
        f"--non-interactive --agree-tos -m {q(job['wp_admin_email'])} --redirect || "
        f"echo 'CERTBOT_FAILED (DNS not pointed yet?)'",
    ]

    parts += [
        "systemctl unmask apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true",
        "systemctl start apt-daily.timer apt-daily-upgrade.timer "
        "unattended-upgrades.service >/dev/null 2>&1 || true",
    ]

    parts.append(f"echo DEPLOY_OK {q(domain)}")
    return "\n".join(parts) + "\n"


def _connect_with_retry(client, connect_kwargs: dict, domain: str, log_cb,
                        attempts: int = CONNECT_ATTEMPTS,
                        delay: int = CONNECT_RETRY_DELAY) -> None:
    """Connect, retrying transient failures such as "Error reading SSH protocol
    banner" (sshd too busy to answer, common on a loaded or just-booted VPS).
    Wrong credentials are never retried."""
    for attempt in range(1, attempts + 1):
        try:
            client.connect(**connect_kwargs)
            return
        except paramiko.AuthenticationException:
            raise
        except (paramiko.SSHException, EOFError, OSError) as e:
            client.close()
            if attempt == attempts:
                raise
            log_cb(f"[{domain}] Connection attempt {attempt}/{attempts} failed ({e}); "
                   f"retrying in {delay}s...")
            time.sleep(delay)


def _run_remote(client, runner: str, script: str, on_line,
                silence_notice=None, max_seconds=None):
    """Feed `script` to `runner` and pass each output line to on_line. Returns
    the exit status: -1 means none was received, which paramiko reports both
    for a lost connection and for a remote process killed by a signal."""
    channel = client.get_transport().open_session()
    # No PTY: avoids shell-prompt/bracketed-paste echo noise and is not
    # needed since we assume root or passwordless sudo.
    channel.set_combine_stderr(True)
    channel.exec_command(runner)
    channel.sendall(script.encode("utf-8"))
    channel.shutdown_write()

    channel.settimeout(0.5)
    started = last_output = time.monotonic()
    notified_at = 0
    buf = b""
    while True:
        try:
            chunk = channel.recv(4096)
        except socket.timeout:
            if channel.exit_status_ready():
                break
            now = time.monotonic()
            if max_seconds and now - started > max_seconds:
                channel.close()
                return -1
            silent = now - last_output
            if silence_notice and silent >= silence_notice and now - notified_at >= silence_notice:
                notified_at = now
                on_line(f"(no output from the server for {int(silent)}s, still waiting)")
            continue
        if not chunk:  # EOF: remote side closed the channel
            break
        last_output = time.monotonic()
        buf += chunk
        while b"\n" in buf:
            raw_line, buf = buf.split(b"\n", 1)
            text = raw_line.decode("utf-8", errors="replace").rstrip("\r")
            if text:
                on_line(text)

    text = buf.decode("utf-8", errors="replace").strip()
    if text:
        on_line(text)
    return channel.recv_exit_status()


def _probe_ssh(host: str, port: int, log) -> str:
    """Check the TCP connection and the SSH banner separately. Returns "ok",
    "no_banner" (sshd accepted but stayed silent) or "no_tcp"."""
    result = "no_tcp"
    for i in range(1, 4):
        started = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=10) as sock:
                tcp_ms = int((time.monotonic() - started) * 1000)
                sock.settimeout(30)
                started = time.monotonic()
                try:
                    banner = sock.recv(256).decode("ascii", errors="replace").strip()
                except OSError as e:
                    banner, error = "", e
                else:
                    error = "connection closed by the server"
                took = time.monotonic() - started
                if banner:
                    result = "ok"
                    log(f"SSH probe {i}/3: TCP {tcp_ms} ms, banner after {took:.1f}s: {banner}")
                else:
                    if result != "ok":
                        result = "no_banner"
                    log(f"SSH probe {i}/3: TCP {tcp_ms} ms, NO banner after {took:.1f}s ({error})")
        except OSError as e:
            log(f"SSH probe {i}/3: TCP connection failed: {e}")
        time.sleep(5)
    return result


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def diagnose_failure(f: dict) -> list:
    """Turn the evidence from a failed run into conclusions, most decisive first.
    Keys: probe, unreachable, reconnect_error, boot_id, transport_active,
    exit_status, plus the COLLECT values from the server."""
    out = []
    if f.get("reconnect_error"):
        return [f"Не вдалося зайти на сервер після збою: {f['reconnect_error']}. "
                "Доказів із сервера немає, перевірте доступ вручну."]
    if f.get("unreachable"):
        if f.get("probe") == "no_tcp":
            out.append("СЕРВЕР НЕДОСТУПНИЙ: не відкривається навіть TCP-з'єднання з портом SSH. "
                       "Сервер лежить або порт блокується. Це привід для тікета.")
        elif f.get("probe") == "no_banner":
            out.append("SSHD НЕ ВІДПОВІДАЄ: TCP-з'єднання є, але SSH-банер не приходить. Сервер "
                       "завис чи перевантажений, або з'єднання ріже фільтр. Це привід для тікета.")
        else:
            out.append("ЗВ'ЯЗОК НЕСТАБІЛЬНИЙ: SSH не підключився, але перевірка потім отримала "
                       "банер. Це схоже на періодичні збої мережі або сервера.")
        return out

    before, after = f.get("boot_id"), f.get("boot_id_after")
    oom = (_to_int(f.get("oom_kernel")), _to_int(f.get("cgroup_oom_kill")), _to_int(f.get("ubc_failcnt")))
    if before and after and before != after:
        out.append("СЕРВЕР ПЕРЕЗАВАНТАЖИВСЯ під час встановлення (змінився boot_id), а скрипт "
                   "перезавантаження не запускає. Якщо це зробили не ви, це привід для тікета: "
                   "нода, збій ядра або дії хостера.")
    if oom[0] or oom[1]:
        out.append(f"НЕ ВИСТАЧИЛО ПАМ'ЯТІ: зафіксовано вбивство процесів через OOM (ядро: {oom[0]}, "
                   f"cgroup: {oom[1]}). Потрібен swap або більше RAM, це зазвичай не несправність VPS.")
    if oom[2]:
        out.append(f"ПЕРЕВИЩЕНО ЛІМІТИ КОНТЕЙНЕРА: failcnt у user_beancounters = {oom[2]}. Варто "
                   "спитати хостера про ліміти тарифу.")
    running = f.get("script_running") == "1"
    signal, rc = f.get("script_signal"), f.get("script_exit")
    if running:
        out.append("ОБІРВАВСЯ ЗВ'ЯЗОК, а скрипт досі працює на сервері. Не запускайте деплой "
                   "повторно, доки він не завершиться.")
    elif signal in ("HUP", "PIPE"):
        out.append(f"ОБІРВАЛОСЯ SSH-З'ЄДНАННЯ (скрипт отримав {signal}), а сервер працює далі. "
                   "Схоже на мережу або sshd. Це привід для тікета, якщо повторюється.")
    elif signal:
        out.append(f"СКРИПТ ЗУПИНЕНО ЗЗОВНІ сигналом {signal}. Це не помилка самого скрипта.")
    elif rc and rc != "0":
        out.append(f"ЗВИЧАЙНА ПОМИЛКА КОМАНДИ: скрипт завершився з кодом {rc}. Причину шукайте "
                   "в останніх рядках логу перед FAILED. Це не схоже на проблему хостера.")
    elif not out and f.get("diag_log_missing") != "1" and not rc:
        msg = ("СКРИПТ ВБИТО SIGKILL: він не встиг залишити мітку завершення, а запису про OOM немає.")
        if f.get("dmesg_readable") == "0":
            msg += " Журнал ядра недоступний, тож OOM не можна ні підтвердити, ні виключити."
        msg += " Це привід спитати хостера, що завершує процеси на VPS."
        out.append(msg)
    if not out:
        out.append("Однозначної причини не знайдено, дивіться звіт нижче.")

    if f.get("last_step"):
        out.append(f"Останній записаний крок: {f['last_step']}.")
    if f.get("min_avail_mb") not in (None, "") and _to_int(f["min_avail_mb"]) < 100:
        out.append(f"Найменше доступної пам'яті за час роботи: {f['min_avail_mb']} МБ. "
                   "Це критично мало.")
    if _to_int(f.get("max_steal_pct")) >= 20:
        out.append(f"CPU steal доходив до {f['max_steal_pct']}%: нода перевантажена сусідами. "
                   "Це варто додати в тікет.")
    if f.get("virt") in ("lxc", "lxc-libvirt", "openvz", "docker", "podman", "systemd-nspawn"):
        out.append(f"Це контейнер ({f['virt']}), а не повноцінна VM. Перевірте, чи це відповідає "
                   "тарифу. Обмеження пам'яті контейнера можуть відрізнятися від показів free.")
    if _to_int(f.get("dpkg_broken")):
        out.append("dpkg лишився в незавершеному стані. Перед новим деплоєм виконайте на сервері "
                   "dpkg --configure -a.")
    return out


def _investigate(connect_kwargs: dict, runner: str, domain: str, facts: dict, log) -> None:
    """Get back onto the server after a failure and pull the evidence."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    prefix = f"[{domain}] "
    log(f"{prefix}Diagnostics: reconnecting to collect evidence...")
    started = time.monotonic()
    try:
        _connect_with_retry(client, connect_kwargs, domain, log,
                            attempts=RECOVERY_ATTEMPTS, delay=RECOVERY_RETRY_DELAY)
    except paramiko.AuthenticationException as e:
        facts["reconnect_error"] = f"авторизація не пройшла ({e})"
        return
    except (paramiko.SSHException, EOFError, OSError) as e:
        log(f"{prefix}Diagnostics: could not reconnect: {e}")
        facts["unreachable"] = True
        facts["probe"] = _probe_ssh(connect_kwargs["hostname"], connect_kwargs["port"],
                                    lambda m: log(prefix + m))
        return
    log(f"{prefix}Diagnostics: back on the server after {int(time.monotonic() - started)}s.")

    def on_line(text):
        log(prefix + text)
        if text.startswith("COLLECT ") and "=" in text:
            key, value = text[len("COLLECT "):].split("=", 1)
            facts["boot_id_after" if key == "boot_id" else key] = value.strip()

    try:
        _run_remote(client, runner, _DIAG_COLLECT, on_line, max_seconds=COLLECT_MAX_SECONDS)
    finally:
        client.close()


def _report_failure(connect_kwargs, runner, domain, facts, log) -> None:
    """Collect evidence and log the conclusions.
    Never raises: a diagnostics problem must not hide the original failure."""
    try:
        if not facts.get("unreachable"):
            _investigate(connect_kwargs, runner, domain, facts, log)
        log(f"[{domain}] ===== ВИСНОВОК ДІАГНОСТИКИ =====")
        for conclusion in diagnose_failure(facts):
            log(f"[{domain}] >>> {conclusion}")
    except Exception as e:
        log(f"[{domain}] Diagnostics failed: {e}")


def run_deploy(host: str, port: int, ssh_user: str, auth: dict, job: dict, log_cb):
    """
    auth: {"method": "password", "password": "..."} or
          {"method": "key", "key_path": "...", "passphrase": "..."}
    log_cb: callable(str) called with each line of output (and status lines).
    Raises on connection/auth failure; returns True on 'DEPLOY_OK' marker found,
    False otherwise. Failures other than bad credentials are investigated first.
    """
    def log(msg):
        log_cb(f"{datetime.datetime.now():%H:%M:%S} {msg}")

    connect_kwargs = dict(hostname=host, port=port, username=ssh_user, timeout=20,
                          banner_timeout=60, auth_timeout=60)
    if auth["method"] == "password":
        connect_kwargs["password"] = auth["password"]
    else:
        pkey = paramiko.RSAKey.from_private_key_file(
            auth["key_path"], password=auth.get("passphrase") or None
        )
        connect_kwargs["pkey"] = pkey

    domain = job["domain"]
    runner = "bash -s" if ssh_user == "root" else "sudo -H bash -s"
    facts = {}
    try:
        ok = _deploy_over_ssh(host, port, ssh_user, job, connect_kwargs, runner, facts, log)
    except paramiko.AuthenticationException:
        raise
    except Exception as e:
        log(f"[{domain}] Deploy interrupted: {e}")
        if not facts.get("connected"):
            facts["unreachable"] = True
            facts["probe"] = _probe_ssh(host, port, lambda m: log(f"[{domain}] {m}"))
        _report_failure(connect_kwargs, runner, domain, facts, log)
        raise
    if not ok:
        _report_failure(connect_kwargs, runner, domain, facts, log)
    return ok


def _deploy_over_ssh(host, port, ssh_user, job, connect_kwargs, runner, facts, log_cb):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    log_cb(f"[{job['domain']}] Connecting to {host}:{port} as {ssh_user}...")
    _connect_with_retry(client, connect_kwargs, job["domain"], log_cb)
    facts["connected"] = True
    # Without keepalives a server that dies mid-run leaves the job "running"
    # forever, because the read loop below never sees the connection drop.
    client.get_transport().set_keepalive(30)
    log_cb(f"[{job['domain']}] Connected to {host}.")

    theme_zip_path = job.get("theme_zip_path")
    if theme_zip_path:
        remote_theme_zip = f"/tmp/theme_{slugify(job['domain'])}.zip"
        log_cb(f"[{job['domain']}] Uploading theme archive "
               f"({os.path.basename(theme_zip_path)})...")
        sftp = client.open_sftp()
        try:
            sftp.put(theme_zip_path, remote_theme_zip)
        finally:
            sftp.close()
            if job.get("theme_zip_is_temp"):
                try:
                    os.remove(theme_zip_path)
                except OSError:
                    pass
        job = {**job, "remote_theme_zip": remote_theme_zip}

    script = build_script(job)
    state = {"ok": False}

    def on_line(text):
        log_cb(f"[{job['domain']}] {text}")
        if text.startswith("DEPLOY_OK"):
            state["ok"] = True
        elif text.startswith("DIAG boot_id="):
            facts["boot_id"] = text.split("=", 1)[1].strip()

    started = time.monotonic()
    try:
        exit_status = _run_remote(client, runner, script, on_line,
                                  silence_notice=SILENCE_NOTICE_SECONDS)
        transport = client.get_transport()
        facts["exit_status"] = exit_status
        facts["transport_active"] = bool(transport and transport.is_active())
        if exit_status == -1:
            error = transport.get_exception() if transport else None
            log_cb(f"[{job['domain']}] No exit status after {int(time.monotonic() - started)}s; "
                   f"SSH connection still up: {facts['transport_active']}"
                   f"{f', transport error: {error}' if error else ''}")
    finally:
        client.close()

    if exit_status != 0 and not state["ok"]:
        log_cb(f"[{job['domain']}] FAILED (exit code {exit_status})")
        return False

    log_cb(f"[{job['domain']}] Done. Site: https://{job['domain']}/  "
           f"WP admin login: {job['wp_admin_user']} / (see credentials.txt)")
    return True
