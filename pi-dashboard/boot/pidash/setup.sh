#!/bin/bash
# PiDash setup. Runs as root from pidash-setup.service on every boot:
#  - first boot: installs everything, then reboots into the dashboard
#  - later: does nothing, unless the files in the "pidash" folder on the SD
#    card were changed, in which case the new version is installed.
set -u
BOOT=/boot/firmware; [ -d "$BOOT/pidash" ] || BOOT=/boot
SRC="$BOOT/pidash"
CONF="$BOOT/dashboard-settings.txt"
LOG="$BOOT/pidash-setup-log.txt"
DONE=/var/lib/pi-dashboard/.setup-done
STAMP=/var/lib/pi-dashboard/.installed-version

src_sum() { (cd "$SRC" && find . -type f | LC_ALL=C sort | xargs -d '\n' sha256sum | sha256sum | cut -d' ' -f1); }

UPDATE=0
NEWSUM=$(src_sum)
if [ -f "$DONE" ]; then
    if [ "$(cat "$STAMP" 2>/dev/null)" = "$NEWSUM" ]; then
        command -v ffmpeg >/dev/null && exit 0
        UPDATE=2    # the video converter could not be downloaded before: try again
    else
        UPDATE=1
    fi
fi

exec > >(tee -a "$LOG" /var/log/pidash-setup.log) 2>&1

say() {
    echo "$(date '+%F %T') $*"
    printf '\r\n[PiDash setup] %s\r\n' "$*" > /dev/tty3 2>/dev/null || true
}
splash() {  # splash PICTURE: show it until something else is shown
    pkill -f "pidash-splash fb" 2>/dev/null
    python3 "$SRC/pidash-splash" fb "$1" 7200 >/dev/null 2>&1 &
}
getconf() {  # getconf KEY DEFAULT
    local v
    v=$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CONF" 2>/dev/null | tail -n1 | tr -d '\r')
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    echo "${v:-$2}"
}

wait_for_clock() {  # the Pi has no clock battery; package signatures need the right date
    for i in $(seq 1 60); do
        [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] && return 0
        sleep 2
    done
}

if [ "$UPDATE" = 2 ]; then
    export DEBIAN_FRONTEND=noninteractive
    sleep 120   # the dashboard starts first
    wait_for_clock
    apt-get -o Acquire::http::Timeout=20 -o Acquire::Retries=1 update
    nice -n 19 apt-get install -y --no-install-recommends ffmpeg && say "Video converter installed."
    apt-get clean
    exit 0
fi

if [ "$UPDATE" = 0 ]; then
chvt 1 2>/dev/null || true
splash setup
say "Setting up your dashboard. This takes 15-30 minutes on a Pi Zero 2 W - please wait..."

WIFI_SSID=$(getconf WIFI_SSID "")
WIFI_PASSWORD=$(getconf WIFI_PASSWORD "")
WIFI_COUNTRY=$(getconf WIFI_COUNTRY US)
TZONE=$(getconf TIMEZONE "")
HN=$(getconf HOSTNAME dashboard)
U=$(getconf USERNAME pi)
P=$(getconf PASSWORD changeme)
SSH=$(getconf ENABLE_SSH no)

# ---------------------------------------------------------------- system
if command -v raspi-config >/dev/null; then
    raspi-config nonint do_wifi_country "$WIFI_COUNTRY" || true
fi
rfkill unblock wifi 2>/dev/null || true
if [ -n "$TZONE" ] && [ -e "/usr/share/zoneinfo/$TZONE" ]; then
    timedatectl set-timezone "$TZONE" || ln -sf "/usr/share/zoneinfo/$TZONE" /etc/localtime
fi
if [ "$(hostname)" != "$HN" ]; then
    if command -v raspi-config >/dev/null; then raspi-config nonint do_hostname "$HN"
    else hostnamectl set-hostname "$HN"; sed -i "s/127.0.1.1.*/127.0.1.1\t$HN/" /etc/hosts; fi
    hostname "$HN" || true
fi

# ------------------------------------------------------------------ user
if ! id "$U" >/dev/null 2>&1; then
    FIRST=$(getent passwd 1000 | cut -d: -f1)
    if [ -n "$FIRST" ] && [ -x /usr/lib/userconf-pi/userconf ]; then
        /usr/lib/userconf-pi/userconf "$U" "$(openssl passwd -6 "$P")" || true
    fi
    id "$U" >/dev/null 2>&1 || useradd -m -s /bin/bash "$U"
fi
echo "$U:$P" | chpasswd
for g in sudo video render input audio gpio netdev plugdev tty; do
    getent group "$g" >/dev/null && usermod -aG "$g" "$U"
done
systemctl disable userconfig.service 2>/dev/null || true
UHOME=$(getent passwd "$U" | cut -d: -f6)
UID_N=$(id -u "$U")

case "${SSH,,}" in yes|true|1) systemctl enable --now ssh || true ;; esac

# ----------------------------------------------------------------- wifi
if [ -n "$WIFI_SSID" ] && [ "$WIFI_SSID" != "YourWiFiName" ] && command -v nmcli >/dev/null; then
    if ! nmcli -t -f NAME con show | grep -qx "pidash-wifi"; then
        say "Connecting to Wi-Fi \"$WIFI_SSID\"..."
        if [ -n "$WIFI_PASSWORD" ]; then
            nmcli con add type wifi con-name pidash-wifi ifname wlan0 ssid "$WIFI_SSID" \
                wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PASSWORD" \
                connection.autoconnect yes 802-11-wireless.hidden yes || true
        else
            nmcli con add type wifi con-name pidash-wifi ifname wlan0 ssid "$WIFI_SSID" \
                connection.autoconnect yes || true
        fi
    else
        nmcli con modify pidash-wifi 802-11-wireless.ssid "$WIFI_SSID" ${WIFI_PASSWORD:+wifi-sec.psk "$WIFI_PASSWORD"} || true
    fi
    nmcli con up pidash-wifi || true
fi

say "Waiting for internet..."
ok=0
for i in $(seq 1 60); do
    if curl -fsS --max-time 6 -o /dev/null http://deb.debian.org/ 2>/dev/null || \
       curl -fsS --max-time 6 -o /dev/null http://archive.raspberrypi.com/ 2>/dev/null; then ok=1; break; fi
    sleep 5
done
if [ "$ok" != 1 ]; then
    say "NO INTERNET. Check WIFI_SSID / WIFI_PASSWORD in dashboard-settings.txt on the SD card (bootfs drive). Retrying in 1 minute..."
    splash wifi; sleep 5   # time to draw it
    exit 1
fi

splash setup
# -------------------------------------------------------------- packages
wait_for_clock
say "Downloading and installing the screen software (the slow part)..."
export DEBIAN_FRONTEND=noninteractive
for attempt in 1 2 3; do
    apt-get update && break
    sleep 20
done
BROWSER=chromium
apt-cache show chromium >/dev/null 2>&1 || BROWSER=chromium-browser
apt-get install -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
    cage "$BROWSER" fonts-noto-color-emoji fonts-dejavu-core python3 python3-dateutil curl || {
    say "Package install failed - will retry in 1 minute."; splash retry; sleep 5; exit 1; }
apt-get install -y python3-gpiozero python3-lgpio 2>/dev/null || true
apt-get install -y python3-pil || true
apt-get install -y wlr-randr || true
apt-get install -y --no-install-recommends ffmpeg || true   # makes videos small enough for the Pi
apt-get clean

else
# ------------------------------------------------ update of a working Pi
say "Updating PiDash..."
U=$(stat -c %U /var/lib/pi-dashboard)
UID_N=$(id -u "$U")
systemctl stop pidash-watchdog.service pidash-kiosk.service 2>/dev/null   # frees memory for the update
trap 'systemctl start --no-block pidash-kiosk.service pidash-watchdog.service' EXIT   # the screen comes back whatever happens
splash update
NEED=""
python3 -c 'import PIL' 2>/dev/null || NEED="$NEED python3-pil"
command -v wlr-randr >/dev/null || NEED="$NEED wlr-randr"
command -v ffmpeg >/dev/null || NEED="$NEED ffmpeg"
if [ -n "$NEED" ]; then
    wait_for_clock
    export DEBIAN_FRONTEND=noninteractive
    apt-get -o Acquire::http::Timeout=20 -o Acquire::Retries=1 update || true
    for p in $NEED; do apt-get install -y --no-install-recommends "$p" || say "Could not install $p (PiDash works without it)."; done
    apt-get clean
fi
fi

# ------------------------------------------------------------------- app
say "Installing PiDash..."
mkdir -p "$BOOT/photos" "$BOOT/videos"
rm -rf /opt/pi-dashboard
cp -r "$SRC/app" /opt/pi-dashboard
chmod -R a+rX /opt/pi-dashboard
mkdir -p /var/lib/pi-dashboard
chown -R "$U:" /var/lib/pi-dashboard

rm -rf /var/lib/pi-dashboard/photo-cache
# a new version may manage videos the old one could not
rm -f /var/lib/pi-dashboard/video-cache/*.bad /var/lib/pi-dashboard/video-cache/*.try /var/lib/pidash-watchdog/video-off
for f in pidash-kiosk pidash-screen pidash-watchdog pidash-splash; do
    install -m 0755 "$SRC/$f" "/usr/local/bin/$f"
done
mkdir -p /usr/local/share/pidash-splash
install -m 0644 "$SRC"/splash/*.bin /usr/local/share/pidash-splash/
cmp -s "$SRC/pidash-setup.service" /etc/systemd/system/pidash-setup.service || cp "$SRC/pidash-setup.service" /etc/systemd/system/pidash-setup.service

cat > /etc/pam.d/cage <<'EOF'
auth    required pam_unix.so nullok
account required pam_unix.so
session required pam_unix.so
session required pam_systemd.so
EOF

cat > /etc/systemd/system/pi-dashboard.service <<EOF
[Unit]
Description=PiDash dashboard server
After=network.target

[Service]
User=$U
Environment=PIDASH_DATA=/var/lib/pi-dashboard
Environment=PIDASH_BOOT_SETTINGS=$CONF
Environment=PIDASH_PHOTOS=$BOOT/photos
Environment=PIDASH_VIDEOS=$BOOT/videos
Environment=MALLOC_ARENA_MAX=1
OOMScoreAdjust=-300
ExecStart=/usr/bin/python3 /opt/pi-dashboard/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/pidash-kiosk.service <<EOF
[Unit]
Description=PiDash kiosk screen
Wants=pi-dashboard.service dbus.socket systemd-logind.service
After=pi-dashboard.service systemd-user-sessions.service plymouth-quit-wait.service dbus.socket systemd-logind.service getty@tty1.service
Conflicts=getty@tty1.service
ConditionPathExists=/dev/tty0

[Service]
Type=simple
User=$U
PAMName=cage
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
StandardInput=tty-fail
StandardOutput=journal
StandardError=journal
UtmpIdentifier=tty1
UtmpMode=user
Environment=XDG_RUNTIME_DIR=/run/user/$UID_N
Environment=PIDASH_BOOT_SETTINGS=$CONF
ExecStart=/usr/local/bin/pidash-kiosk
Restart=always
RestartSec=5
TimeoutStopSec=10

[Install]
WantedBy=graphical.target
EOF

cat > /etc/systemd/system/pidash-splash.service <<EOF
[Unit]
Description=PiDash splash screen
DefaultDependencies=no
After=local-fs-pre.target
Conflicts=shutdown.target
Before=shutdown.target

[Service]
Environment=PIDASH_BOOT_SETTINGS=$CONF
ExecStart=/usr/bin/python3 -S /usr/local/bin/pidash-splash fb start 90
SuccessExitStatus=1 15

[Install]
WantedBy=sysinit.target
EOF

cat > /etc/systemd/system/pidash-watchdog.service <<EOF
[Unit]
Description=PiDash watchdog (restarts a frozen screen)
After=pi-dashboard.service pidash-kiosk.service

[Service]
Environment=PIDASH_BOOT_SETTINGS=$CONF
ExecStart=/usr/local/bin/pidash-watchdog
Restart=always
RestartSec=10
TimeoutStopSec=15
Nice=-5
OOMScoreAdjust=-900

[Install]
WantedBy=multi-user.target
EOF
mkdir -p /var/lib/pidash-watchdog

# ------------------------------------------------------- less for the Pi to do
# package lists and manual pages were refreshed at random times of the day
for x in apt-daily.timer apt-daily-upgrade.timer man-db.timer; do
    systemctl disable --now "$x" 2>/dev/null || true
done
# swap goes to compressed memory first; use it early, before things stall
cat > /etc/sysctl.d/99-pidash.conf <<'EOF'
vm.swappiness=100
vm.page-cluster=0
vm.vfs_cache_pressure=150
EOF
# the screen driver does not need the 64 MB the firmware sets aside
if [ -f "$BOOT/config.txt" ] && ! grep -q '^gpu_mem' "$BOOT/config.txt"; then
    printf '\n[all]\n# PiDash: more memory for the dashboard\ngpu_mem=32\n' >> "$BOOT/config.txt"
fi

if [ "$UPDATE" = 0 ]; then
# more swap helps Chromium on 512 MB of RAM
if [ -f /etc/dphys-swapfile ]; then
    sed -i 's/^#\?CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
    systemctl restart dphys-swapfile 2>/dev/null || true
fi

fi

# quiet start: text goes to the hidden screen 3 (Ctrl+Alt+F3), the splash is on 1
CMDLINE="$BOOT/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    NEW=$(head -n1 "$CMDLINE" | sed 's/console=tty1\b/console=tty3/')
    for w in consoleblank=0 logo.nologo vt.global_cursor_default=0 quiet loglevel=3 systemd.show_status=false udev.log_level=3; do
        case " $NEW " in *" $w "*) ;; *) NEW="$NEW $w" ;; esac
    done
    if [ "$NEW" != "$(head -n1 "$CMDLINE")" ] && echo "$NEW" | grep -q 'root='; then
        echo "$NEW" > "$CMDLINE.new" && sync && mv -f "$CMDLINE.new" "$CMDLINE"
    fi
fi
if [ -f "$BOOT/config.txt" ] && ! grep -q '^disable_splash' "$BOOT/config.txt"; then
    printf '\n[all]\n# PiDash: no rainbow picture at power on\ndisable_splash=1\n' >> "$BOOT/config.txt"
fi
systemctl mask getty@tty1.service 2>/dev/null   # no login text under the splash (log in on Ctrl+Alt+F2)

systemctl daemon-reload
systemctl enable pi-dashboard.service pidash-kiosk.service pidash-watchdog.service pidash-splash.service
systemctl set-default graphical.target
touch "$DONE"
echo "$NEWSUM" > "$STAMP"
sync
if [ "$UPDATE" = 1 ]; then
    if [ "$(cat "$STAMP" 2>/dev/null)" != "$NEWSUM" ]; then
        say "Could not save the update state; not restarting."   # never restart in a loop
        systemctl restart pi-dashboard.service pidash-watchdog.service pidash-kiosk.service
        exit 0
    fi
    say "Update finished. Restarting..."
    sleep 3
    systemctl reboot
    exit 0
fi
say "All done! Restarting into your dashboard."
sleep 5
systemctl reboot
