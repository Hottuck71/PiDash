PiDash - family dashboard OS for Raspberry Pi Zero 2 W
=======================================================

Shows your Cozi calendar, the weather, countdowns and a clock on any HDMI
screen (touchscreens work too), with your own photos and videos behind it.
Based on Raspberry Pi OS Lite (Trixie, 64-bit).

SET UP
------
1. Flash pidash.img to a microSD card (8 GB or bigger) with
   Raspberry Pi Imager ("Choose OS" > "Use custom") or balenaEtcher.
   In Imager, answer NO when it asks about OS customisation.
2. Take the card out and put it back in your PC. Open the drive called
   "bootfs" and edit  dashboard-settings.txt  with Notepad:
     - WIFI_SSID / WIFI_PASSWORD   (required)
     - COZI_URL                    (your Cozi calendar feed link)
     - WEATHER_LOCATION, TIMEZONE, PASSWORD
   Save, eject the card.
3. Put the card in the Pi, connect the screen, power it on.
   The first boot installs the screen software from the internet.
   That takes about 15-30 minutes on a Zero 2 W. Progress is shown on
   screen and written to pidash-setup-log.txt on the bootfs drive.
   The Pi restarts by itself into the dashboard when it is done.

GETTING YOUR COZI LINK
----------------------
Sign in at my.cozi.com on a computer > Settings > "Shared Cozi
Calendars" > choose the whole family or one person > copy the
calendar feed (ICS) link. It starts with https://rest.cozi.com/...
Paste it into dashboard-settings.txt. Google / Outlook / iCloud "secret
iCal" links also work.

CALENDAR CHANGES
----------------
PiDash looks at your calendar every 2 minutes (CALENDAR_MINUTES in
dashboard-settings.txt) and changes the screen by itself; there is no need
to restart the Pi. The bottom right corner shows when it last looked.
To look right away: press R, or tap the "Calendar checked" text, or Menu >
Check calendar now.

PHOTO AND VIDEO BACKGROUND
--------------------------
Copy pictures (JPG, PNG, WEBP, GIF, BMP) into the "photos" folder and videos
(MP4, MOV and most others) into the "videos" folder on the bootfs drive. They
play as one slideshow behind the calendar and weather, videos without sound.
If both folders are empty the background is dark.
The Pi first makes a small copy of each new video in the background. That
takes a few minutes for a normal clip and much longer for 4K; until then
only the pictures show. VIDEO_SECONDS sets how long a video may play.
Turn the Pi off before taking the card out.

CHANGING SETTINGS
-----------------
Turn the Pi off, put the card in your PC, edit dashboard-settings.txt and
put the card back. Theme, layout, widgets, photo timing, night dimming, countdowns,
calendars and weather are read again whenever the file changes. Wi-Fi,
network name and login are only used the first time.
Nothing on the Pi can be opened from a phone or PC (SSH only if you turned
it on).

UPDATES
-------
Replace the "pidash" folder on the bootfs drive with a newer one. The Pi
installs it at the next start and restarts once; settings are kept.

CONTROLS ON THE PI
------------------
Touchscreen:  swipe left/right = change page
              swipe up/down on Home = change layout
              tap the faint (≡) button top-right = options menu
Keyboard:     <- / ->  pages        Up / Down  layouts
              S  move widgets       T  theme
              M  menu               A  auto-cycle on/off
              P  next photo or video
              O  rotate screen      C  12/24 h    R  check calendar now
              1-5 jump to a page
Buttons:      set GPIO_BUTTONS="yes" (pins listed in the settings file)

Pages: Home, Week, Month, Agenda, Weather.
Home layouts: Classic, Mirrored, Stacked, Focus, Big clock - each with up
to 4 boxes. WIDGETS in dashboard-settings.txt picks what goes in them: Clock,
Weather, 7-day forecast, Agenda or Countdowns.

IF THE SCREEN FREEZES
---------------------
PiDash notices within about 2 minutes and restarts the screen by itself.
What happened is written to pidash-health-log.txt on the bootfs drive.

TROUBLESHOOTING
---------------
- Startup text is hidden behind the splash screen. It is still there on
  another screen: Ctrl+Alt+F3 on a keyboard (F2 = login, F1 = back).
- "NO INTERNET" on screen: fix the Wi-Fi name/password in
  dashboard-settings.txt (it is case-sensitive). The Pi only supports
  2.4 GHz Wi-Fi. Setup retries automatically every minute.
- Weather says location not found: try just the city name.
- Calendar empty: check COZI_URL in dashboard-settings.txt. The Agenda
  page shows the error if a calendar cannot be loaded.
- Login (keyboard or SSH): the USERNAME / PASSWORD from the settings
  file. Logs:  journalctl -u pi-dashboard -u pidash-kiosk -u pidash-watchdog
