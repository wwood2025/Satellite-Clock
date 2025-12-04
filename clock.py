#!/usr/bin/env python3
# Workshop LED Clock — GPS primary, NTP fallback, rich GPS diagnostics, chimes
# Requires: pygame, pyserial, ntplib
# Put digital-7.ttf, chime_hour.wav, chime_half.wav in the same folder as this script.

import os
import sys
import time
import datetime
import re
import pygame
import serial
import ntplib

# -----------------------
# CONFIG
# -----------------------
FONT_FILENAME = "digital-7.ttf"   # must be in same folder as script
FONT_SIZE_TIME = 620
FONT_SIZE_SEC = 180
FONT_SIZE_DATE = 90
FONT_SIZE_BOTTOM = 48

GPS_PORT = "/dev/serial0"
GPS_BAUD = 9600
UTC_OFFSET_HOURS = -5

NTP_SERVERS = ["pool.ntp.org", "time.google.com", "time.cloudflare.com"]
NTP_CHECK_INTERVAL = 30.0

CHIME_HOUR_FILE = "chime_hour.wav"
CHIME_HALF_FILE = "chime_half.wav"

# -----------------------
# STATE
# -----------------------
gps_fix_dt = None
fix_quality = 0
satellites_used = 0
fix_type = "NO FIX"
best_snr = 0
last_gps_receive = None  # last time a valid GPS fix was received

last_time_source = "Startup"
last_ntp_query = 0.0
last_ntp_server_used = None

display_time = None
last_monotonic = time.monotonic()

# For chimes
last_chime_hour = None
last_chime_half = None

# -----------------------
# Initialize serial
# -----------------------
try:
    gps_serial = serial.Serial(GPS_PORT, GPS_BAUD, timeout=0.1)
except Exception as e:
    print(f"[WARN] Could not open GPS serial {GPS_PORT}: {e}")
    gps_serial = None

# -----------------------
# Initialize pygame + fonts + mixer
# -----------------------
pygame.init()
pygame.display.set_caption("Workshop LED Clock")
screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)

BLACK = (0, 0, 0)
LED_COLOR = (255, 80, 0)
SECOND_COLOR = (255, 80, 0)
DATE_COLOR = (255, 180, 60)
BOTTOM_COLOR = DATE_COLOR

def load_font_file(filename, size):
    path = os.path.join(os.path.dirname(__file__), filename)
    if os.path.exists(path):
        return pygame.font.Font(path, size)
    else:
        return pygame.font.SysFont("dejavusans", size, bold=True)

clock_font = load_font_file(FONT_FILENAME, FONT_SIZE_TIME)
second_font = load_font_file(FONT_FILENAME, FONT_SIZE_SEC)
date_font = pygame.font.SysFont("dejavusans", FONT_SIZE_DATE)
bottom_font = pygame.font.SysFont("dejavusans", FONT_SIZE_BOTTOM)

# Initialize mixer for sound
pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
try:
    chime_hour = pygame.mixer.Sound(os.path.join(os.path.dirname(__file__), CHIME_HOUR_FILE))
    chime_half = pygame.mixer.Sound(os.path.join(os.path.dirname(__file__), CHIME_HALF_FILE))
except Exception as e:
    print(f"[WARN] Could not load chime files: {e}")
    chime_hour = None
    chime_half = None

# -----------------------
# NMEA parsing helpers
# -----------------------
gsv_snr_re = re.compile(r",(\d{1,2}),")

def parse_nmea_line(line):
    global gps_fix_dt, fix_quality, satellites_used, fix_type, best_snr, last_gps_receive

    line = line.strip()
    if not line:
        return

    # GPRMC => time + date + status
    if line.startswith("$GPRMC") or line.startswith("$GNRMC"):
        parts = line.split(",")
        try:
            status = parts[2]
            raw_time = parts[1]
            raw_date = parts[9]
            if status == "A" and raw_time and raw_date:
                hh = int(raw_time[0:2])
                mm = int(raw_time[2:4])
                ss = int(raw_time[4:6])
                dd = int(raw_date[0:2])
                mo = int(raw_date[2:4])
                yy = int(raw_date[4:6]) + 2000
                utc_dt = datetime.datetime(yy, mo, dd, hh, mm, ss)
                local_dt = utc_dt + datetime.timedelta(hours=UTC_OFFSET_HOURS)
                gps_fix_dt = local_dt
                if fix_quality > 0:
                    last_gps_receive = time.monotonic()
        except Exception:
            pass

    # GPGGA => fix quality + satellites
    elif line.startswith("$GPGGA"):
        parts = line.split(",")
        try:
            fq = int(parts[6]) if parts[6] != "" else 0
            sats = int(parts[7]) if parts[7] != "" else 0
            fix_quality = fq
            satellites_used = sats
            if fq > 0:
                last_gps_receive = time.monotonic()
        except Exception:
            pass

    # GPGSA => fix type
    elif line.startswith("$GPGSA"):
        parts = line.split(",")
        try:
            mode = parts[2]
            if mode == "1":
                fix_type = "NO FIX"
            elif mode == "2":
                fix_type = "2D FIX"
            elif mode == "3":
                fix_type = "3D FIX"
            if fix_quality > 0:
                last_gps_receive = time.monotonic()
        except Exception:
            pass

    # GPGSV => SNR
    elif line.startswith("$GPGSV"):
        parts = line.split(",")
        try:
            for idx in (7, 11, 15, 19):
                if len(parts) > idx and parts[idx].isdigit():
                    snr_val = int(parts[idx])
                    if snr_val > best_snr:
                        best_snr = snr_val
            if fix_quality > 0:
                last_gps_receive = time.monotonic()
        except Exception:
            pass
    
    # ------------------------------------------------------------
    # Global update: any valid fix updates Last_gps_receive
    # -----------------------------------------------------------
    if gps_fix_dt is not None:
        # Either GGA says valid fix OR GSA indicates 2D/3D
        if fix_quality > 0 or fix_type !="NO FIX":
            last_gps_receive = time.monotonic()
# -----------------------
# NTP helper
# -----------------------
def query_ntp_once():
    global last_ntp_server_used
    client = ntplib.NTPClient()
    for s in NTP_SERVERS:
        try:
            resp = client.request(s, version=3, timeout=3)
            dt = datetime.datetime.utcfromtimestamp(resp.tx_time)
            last_ntp_server_used = s
            return dt, f"NTP: {s}"
        except:
            continue
    return None, "System time (offline)"

# -----------------------
# Initialize display_time
# -----------------------
display_time = datetime.datetime.now()
last_monotonic = time.monotonic()

# -----------------------
# Main Loop
# -----------------------
try:
    while True:
        for evt in pygame.event.get():
            if evt.type == pygame.KEYDOWN and evt.key == pygame.K_ESCAPE:
                pygame.quit()
                sys.exit()

        # Read GPS serial lines
        if gps_serial:
            try:
                for _ in range(6):
                    raw = gps_serial.readline().decode("ascii", errors="ignore")
                    if not raw:
                        break
                    parse_nmea_line(raw)
            except Exception:
                pass

        # Time update
        now_mon = time.monotonic()
        elapsed = now_mon - last_monotonic
        last_monotonic = now_mon

        gps_age = (time.monotonic() - last_gps_receive) if last_gps_receive else 1e9
        have_gps = gps_fix_dt is not None and fix_quality > 0 and gps_age < 10.0

        if have_gps:
            if display_time is None:
                display_time = gps_fix_dt
            else:
                if gps_fix_dt > display_time + datetime.timedelta(seconds=1.5):
                    display_time = gps_fix_dt
                else:
                    display_time += datetime.timedelta(seconds=elapsed)
            last_time_source = f"GPS: {fix_type} | Sats:{satellites_used} | SNR:{best_snr} dB"
        else:
            now_now = time.monotonic()
            if (now_now - last_ntp_query) > NTP_CHECK_INTERVAL or last_ntp_server_used is None:
                ntp_dt_utc, ntp_status = query_ntp_once()
                last_ntp_query = now_now
                if ntp_dt_utc:
                    local_dt = ntp_dt_utc + datetime.timedelta(hours=UTC_OFFSET_HOURS)
                    display_time = local_dt
                    last_time_source = ntp_status
                else:
                    if display_time is None:
                        display_time = datetime.datetime.now()
                    else:
                        display_time += datetime.timedelta(seconds=elapsed)
                    last_time_source = "System time (offline)"
            else:
                display_time += datetime.timedelta(seconds=elapsed)

        # build strings
        now = display_time
        time_str = now.strftime("%H:%M")
        sec_str = now.strftime("%S")
        date_str = now.strftime("%A, %B %d %Y")

        screen.fill(BLACK)
        time_surf = clock_font.render(time_str, True, LED_COLOR)
        sec_surf = second_font.render(sec_str, True, SECOND_COLOR)
        date_surf = date_font.render(date_str, True, LED_COLOR)

        # bottom line
        if have_gps:
            gps_age_sec = int(gps_age)
            bottom_text = f"GPS: {fix_type} | Sats:{satellites_used} | Best SNR:{best_snr} dB | age:{gps_age_sec}s"
        else:
            if last_ntp_server_used:
                bottom_text = f"NTP: {last_ntp_server_used}"
            else:
                bottom_text = "NTP: (no server)"
        bottom_surf = bottom_font.render(bottom_text, True, BOTTOM_COLOR)

        # --- Check chimes ---
        minute = now.minute
        hour = now.hour
        if chime_hour and last_chime_hour != hour and minute == 0:
            chime_hour.play()
            last_chime_hour = hour
        if chime_half and last_chime_half != hour and minute == 30:
            chime_half.play()
            last_chime_half = hour
        if minute not in (0, 30):
            last_chime_hour = None
            last_chime_half = None

        # positioning
        sw, sh = screen.get_width(), screen.get_height()
        time_rect = time_surf.get_rect(center=(sw // 2, sh // 2 - 220))
        sec_rect = sec_surf.get_rect(center=(sw // 2, sh // 2 + 120))
        date_rect = date_surf.get_rect(center=(sw // 2, sh // 2 + 300))
        bottom_rect = bottom_surf.get_rect(center=(sw // 2, sh // 2 + 380))

        screen.blit(time_surf, time_rect)
        screen.blit(sec_surf, sec_rect)
        screen.blit(date_surf, date_rect)
        screen.blit(bottom_surf, bottom_rect)

        pygame.display.flip()
        time.sleep(0.08)

except KeyboardInterrupt:
    pygame.quit()
    sys.exit()
