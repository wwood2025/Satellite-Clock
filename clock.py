#!/usr/bin/env python3
# Workshop LED Clock — GPS primary, NTP fallback, chimes, web alarm
# Requires: pygame, pyserial, ntplib, flask, requests
# Place digital-7.ttf, chime_hour.wav, chime_half.wav, alarm.wav in same folder

import os
import sys
import time
import datetime
import re
import pygame
import serial
import ntplib
import threading
import json
import requests
from flask import Flask, request, render_template_string
import socket

# -----------------------
# CONFIG
# -----------------------
FONT_FILENAME = "digital-7.ttf"
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
ALARM_FILE = "alarm_time.json"
ALARM_SOUND_FILE = "alarm.wav"

# -----------------------
# STATE
# -----------------------
gps_fix_dt = None
fix_quality = 0
satellites_used = 0
fix_type = "NO FIX"
best_snr = 0
last_gps_receive = None

last_time_source = "Startup"
last_ntp_query = 0.0
last_ntp_server_used = None

display_time = datetime.datetime.now()
last_monotonic = time.monotonic()

# Chimes
last_chime_hour = None
last_chime_half = None

# Alarm
try:
    with open(ALARM_FILE) as f:
        alarm_time = json.load(f)
except:
    alarm_time = {"hour": None, "minute": None}
last_alarm_triggered = None

# -----------------------
# GPS Setup
# -----------------------
try:
    gps_serial = serial.Serial(GPS_PORT, GPS_BAUD, timeout=0.1)
except Exception as e:
    print(f"[WARN] Could not open GPS serial {GPS_PORT}: {e}")
    gps_serial = None

# -----------------------
# Pygame Setup
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

pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
try:
    chime_hour = pygame.mixer.Sound(os.path.join(os.path.dirname(__file__), CHIME_HOUR_FILE))
    chime_half = pygame.mixer.Sound(os.path.join(os.path.dirname(__file__), CHIME_HALF_FILE))
    alarm_sound = pygame.mixer.Sound(os.path.join(os.path.dirname(__file__), ALARM_SOUND_FILE))
except Exception as e:
    print(f"[WARN] Could not load sound files: {e}")
    chime_hour = chime_half = alarm_sound = None

# -----------------------
# NMEA parsing
# -----------------------
gsv_snr_re = re.compile(r",(\d{1,2}),")

def parse_nmea_line(line):
    global gps_fix_dt, fix_quality, satellites_used, fix_type, best_snr, last_gps_receive

    line = line.strip()
    if not line: return

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
                gps_fix_dt = utc_dt + datetime.timedelta(hours=UTC_OFFSET_HOURS)
                if fix_quality > 0:
                    last_gps_receive = time.monotonic()
        except: pass

    elif line.startswith("$GPGGA"):
        parts = line.split(",")
        try:
            fix_quality = int(parts[6]) if parts[6] else 0
            satellites_used = int(parts[7]) if parts[7] else 0
            if fix_quality > 0:
                last_gps_receive = time.monotonic()
        except: pass

    elif line.startswith("$GPGSA"):
        parts = line.split(",")
        try:
            mode = parts[2]
            fix_type = {"1":"NO FIX","2":"2D FIX","3":"3D FIX"}.get(mode,"NO FIX")
            if fix_quality > 0:
                last_gps_receive = time.monotonic()
        except: pass

    elif line.startswith("$GPGSV"):
        parts = line.split(",")
        try:
            for idx in (7,11,15,19):
                if len(parts) > idx and parts[idx].isdigit():
                    snr_val = int(parts[idx])
                    if snr_val > best_snr:
                        best_snr = snr_val
            if fix_quality > 0:
                last_gps_receive = time.monotonic()
        except: pass

# -----------------------
# NTP helper
# -----------------------
def query_ntp_once():
    global last_ntp_server_used
    client = ntplib.NTPClient()
    for s in NTP_SERVERS:
        try:
            resp = client.request(s, version=3, timeout=3)
            last_ntp_server_used = s
            dt = datetime.datetime.utcfromtimestamp(resp.tx_time)
            return dt, f"NTP: {s}"
        except: continue
    return None, "System time (offline)"

# -----------------------
# Flask Web Alarm Interface
# -----------------------
app = Flask(__name__)

HTML_PAGE = """
<!DOCTYPE html>
<html>
<head><title>Workshop Clock Control</title></head>
<body>
<h1>Set Alarm</h1>
<form action="/set_alarm" method="post">
Hour: <input type="number" name="hour" min="0" max="23" required value="{{hour}}"><br>
Minute: <input type="number" name="minute" min="0" max="59" required value="{{minute}}"><br>
<input type="submit" value="Set Alarm">
</form>
<p>Current Alarm: {{hour}}:{{minute}}</p>

<form action="/test_alarm" method="post">
<input type="submit" value="Test Alarm">
</form>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_PAGE, hour=alarm_time["hour"], minute=alarm_time["minute"])

@app.route("/set_alarm", methods=["POST"])
def set_alarm():
    global alarm_time
    alarm_time["hour"] = int(request.form["hour"])
    alarm_time["minute"] = int(request.form["minute"])
    with open(ALARM_FILE, "w") as f:
        json.dump(alarm_time, f)
    return f"Alarm set for {alarm_time['hour']:02d}:{alarm_time['minute']:02d}. <a href='/'>Back</a>"

@app.route("/test_alarm", methods=["POST"])
def test_alarm():
    if alarm_sound:
        alarm_sound.play(loops=2)  # play 3 times in a row
    return "Test alarm played! <a href='/'>Back</a>"

def run_webserver():
    # Use use_reloader=False to prevent Flask from spawning a second process
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)

# Start Flask in a daemon thread so it won't block Pygame
threading.Thread(target=run_webserver, daemon=True).start()

# -----------------------
# Main Loop
# -----------------------
try:
    while True:
        for evt in pygame.event.get():
            if evt.type == pygame.KEYDOWN and evt.key == pygame.K_ESCAPE:
                pygame.quit()
                sys.exit()

        # Read GPS
        if gps_serial:
            try:
                for _ in range(6):
                    raw = gps_serial.readline().decode("ascii", errors="ignore")
                    if not raw: break
                    parse_nmea_line(raw)
            except: pass

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
                    display_time = ntp_dt_utc + datetime.timedelta(hours=UTC_OFFSET_HOURS)
                    last_time_source = ntp_status
                else:
                    if display_time is None:
                        display_time = datetime.datetime.now()
                    else:
                        display_time += datetime.timedelta(seconds=elapsed)
                    last_time_source = "System time (offline)"
            else:
                display_time += datetime.timedelta(seconds=elapsed)

        now = display_time
        time_str = now.strftime("%H:%M")
        sec_str = now.strftime("%S")
        date_str = now.strftime("%A, %B %d %Y")

        # Draw
        screen.fill(BLACK)
        time_surf = clock_font.render(time_str, True, LED_COLOR)
        sec_surf = second_font.render(sec_str, True, SECOND_COLOR)
        date_surf = date_font.render(date_str, True, LED_COLOR)

        # --- render status (GPS/NTP) at bottom ---
        sw, sh = screen.get_width(), screen.get_height()

        status_text = last_time_source
        status_surf = bottom_font.render(status_text, True, BOTTOM_COLOR)
        status_rect = status_surf.get_rect(center=(sw // 2, sh - 40))
        
        # Positioning for big items (time/date)
        time_rect = time_surf.get_rect(center=(sw//2, sh//2 - 220))
        sec_rect = sec_surf.get_rect(center=(sw//2, sh//2 + 120))
        date_rect = date_surf.get_rect(center=(sw//2, sh//2 + 260))

        # Chimes
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

        # Alarm
        if alarm_time["hour"] is not None and alarm_time["minute"] is not None:
            if now.hour == alarm_time["hour"] and now.minute == alarm_time["minute"] and now.second == 0:
                if last_alarm_triggered != (now.hour, now.minute):
                    print("ALARM! Playing sound...")
                    if alarm_sound:
                        alarm_sound.play(loops=2)
                    last_alarm_triggered = (now.hour, now.minute)

        # Blit the major elements
        screen.blit(time_surf, time_rect)
        screen.blit(sec_surf, sec_rect)
        screen.blit(date_surf, date_rect)
        screen.blit(status_surf, status_rect)

        pygame.display.flip()
        time.sleep(0.08)

except KeyboardInterrupt:
    pygame.quit()
    sys.exit()
