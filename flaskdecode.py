from flask import Flask, request, Response
import xml.etree.ElementTree as ET
import json
from threading import Lock, Thread
import time
import uuid
import math
import requests

app = Flask(__name__)

SEND_PRIMARY_TARGETS = True

aircraft_data = {}
data_lock = Lock()
aircraft_guids = {}
previous_cps = {}

METAR_API_URL = "https://avwx.rest/api/metar/KSTL"
METAR_API_TOKEN = "g94zTP-R-fxYPniQuFYh2kUu1FmpXSuh9KjNqRpldKo"
METAR_UPDATE_INTERVAL = 300

altimeter_value = 29.92
altimeter_timestamp = time.time()
altimeter_lock = Lock()

def calculate_ground_track(vx, vy):
    if vx == 0 and vy == 0:
        return 0
    track = math.atan2(vx, vy) * 180 / math.pi
    return int((track + 360) % 360)

def calculate_ground_speed(vx, vy):
    speed = math.sqrt(vx**2 + vy**2)
    return int(speed)

def fetch_and_update_altimeter():
    def fetch_metar():
        global altimeter_value, altimeter_timestamp
        
        try:
            headers = {"Authorization": f"Bearer {METAR_API_TOKEN}"}
            response = requests.get(METAR_API_URL, headers=headers, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                if "altimeter" in data and "value" in data["altimeter"]:
                    new_value = data["altimeter"]["value"]
                    with altimeter_lock:
                        altimeter_value = new_value
                        altimeter_timestamp = time.time()
                    print(f"[METAR] Updated altimeter to {altimeter_value} inHg at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(altimeter_timestamp))}")
                    return True
            else:
                print(f"[METAR] API error: {response.status_code}")
        except requests.exceptions.Timeout:
            print(f"[METAR] Request timeout")
        except Exception as e:
            print(f"[METAR] Error fetching data: {e}")
        return False
    
    print("[METAR] Fetching initial METAR data...")
    for attempt in range(3):
        try:
            if fetch_metar():
                break
            if attempt < 2:
                print(f"[METAR] Retrying (attempt {attempt + 2}/3)...")
                time.sleep(5)
        except Exception as e:
            print(f"[METAR] Initial fetch attempt {attempt + 1} failed: {e}")
    
    print(f"[METAR] Starting periodic updates every {METAR_UPDATE_INTERVAL} seconds")
    while True:
        time.sleep(METAR_UPDATE_INTERVAL)
        try:
            fetch_metar()
        except Exception as e:
            print(f"[METAR] Periodic fetch error: {e}")

def correct_altitude_for_pressure(altitude_ft, altimeter_inHg):
    standard_altimeter = 29.92126
    constant = 145442.2
    exponent = 0.190261
    
    correction = constant * (1 - (altimeter_inHg / standard_altimeter) ** exponent)
    corrected_altitude = altitude_ft + correction
    
    return int(corrected_altitude)

def get_current_altimeter():
    with altimeter_lock:
        return altimeter_value, altimeter_timestamp

def get_or_create_guid(identifier):
    if identifier not in aircraft_guids:
        aircraft_guids[identifier] = str(uuid.uuid4())
    return aircraft_guids[identifier]

def get_squawk_guid(squawk):
    if squawk and squawk != "" and squawk != "1200":
        key = f"squawk_{squawk}"
        return get_or_create_guid(key)
    return None

def parse_ldr_direction(direction_str):
    if not direction_str:
        return None
    direction_map = {
        'N': 2,
        'NE': 3,
        'E': 6,
        'SE': 9,
        'S': 8,
        'SW': 7,
        'W': 4,
        'NW': 1
    }
    return direction_map.get(direction_str.upper())

def format_track_update(track_data):
    callsign = track_data["callsign"]
    squawk = track_data.get("squawk")
    
    squawk_guid = get_squawk_guid(squawk)
    if squawk_guid:
        guid = squawk_guid
    else:
        identifier = track_data.get("mode_s_hex") if not track_data.get("has_flight_plan") else callsign
        guid = get_or_create_guid(identifier)
    
    is_primary_only = not track_data.get("squawk") and not track_data.get("mode_s")
    altitude_type = 2 if is_primary_only else 0
    
    update = {
        "UpdateType": 0,
        "Guid": guid,
        "TimeStamp": track_data["timestamp"],
        "Location": {
            "Latitude": track_data["lat"],
            "Longitude": track_data["lon"]
        },
        "Altitude": {
            "Value": track_data["alt"],
            "AltitudeType": altitude_type
        },
        "GroundSpeed": track_data.get("ground_speed"),
        "GroundTrack": track_data.get("ground_track"),
        "VerticalRate": track_data.get("vVert")
    }
    
    if callsign and not is_primary_only:
        update["Callsign"] = callsign
    if track_data.get("owner"):
        update["Owner"] = track_data.get("owner")
    if track_data.get("squawk"):
        update["Squawk"] = track_data.get("squawk")
    if track_data.get("mode_s") is not None and track_data.get("mode_s") != 0:
        update["ModeSCode"] = track_data.get("mode_s")
    
    return update

def format_flight_plan_update(track_data):
    callsign = track_data["callsign"]
    squawk = track_data.get("squawk")
    
    squawk_guid = get_squawk_guid(squawk)
    if squawk_guid:
        guid = squawk_guid
    else:
        guid = get_or_create_guid(callsign)
    
    aircraft_type = track_data.get("aircraft_type", "")
    
    update = {
        "UpdateType": 1,
        "Guid": guid,
        "TimeStamp": track_data["timestamp"],
        "Callsign": callsign,
        "AssociatedTrackGuid": guid,
        "AircraftType": aircraft_type,
        "WakeCategory": track_data.get("wake_category", ""),
        "FlightRules": track_data.get("flight_rules", "IFR")
    }
    
    if track_data.get("equipment_suffix") and track_data.get("equipment_suffix") != "unavailable":
        update["EquipmentSuffix"] = track_data["equipment_suffix"]
    if track_data.get("scratchpad1") and track_data.get("scratchpad1") != "unassigned":
        update["Scratchpad1"] = track_data["scratchpad1"]
    if track_data.get("scratchpad2") and track_data.get("scratchpad2") != "unassigned":
        update["Scratchpad2"] = track_data["scratchpad2"]
    if track_data.get("assigned_alt") and track_data.get("assigned_alt") != 0:
        update["AssignedAltitude"] = track_data["assigned_alt"]
    elif track_data.get("requested_alt") and track_data.get("requested_alt") != 0:
        update["RequestedAltitude"] = track_data["requested_alt"]
    if track_data.get("runway") and track_data.get("runway") != "":
        update["Runway"] = track_data["runway"]
    if track_data.get('entry_fix') and track_data.get('entry_fix') != 'unassigned':
        update['EntryFix'] = track_data.get('entry_fix')
    if track_data.get('exit_fix') and track_data.get('exit_fix') != 'unassigned':
        update['ExitFix'] = track_data.get('exit_fix')
    if track_data.get('origin'):
        update['Origin'] = track_data.get('origin')
    if track_data.get('destination'):
        update['Destination'] = track_data.get('destination')
    if track_data.get("owner"):
        update["Owner"] = track_data["owner"]
    
    update["PendingHandoff"] = track_data.get("handoff_status", "")
    if track_data.get("squawk"):
        update["Squawk"] = track_data["squawk"]
        update["AssignedSquawk"] = track_data["squawk"]
    if track_data.get("ldr_direction"):
        update["LDRDirection"] = track_data["ldr_direction"]
    
    return update

@app.route("/updates", methods=["POST", "GET"])
def updates():
    if request.method == "POST":
        xml_data = request.data.decode("utf-8")
        
        try:
            root = ET.fromstring(xml_data)
            
            for record in root.findall("record"):
                track = record.find("track")
                if track is None:
                    continue
                
                lat = float(track.findtext("lat", default="0"))
                lon = float(track.findtext("lon", default="0"))
                alt = int(track.findtext("reportedAltitude", default="0"))
                
                current_altimeter, altimeter_ts = get_current_altimeter()
                corrected_alt = correct_altitude_for_pressure(alt, current_altimeter)
                
                age_seconds = time.time() - altimeter_ts
                if age_seconds > METAR_UPDATE_INTERVAL:
                    print(f"[ALTITUDE] Warning: Altimeter is {int(age_seconds)}s old (threshold: {METAR_UPDATE_INTERVAL}s)")
                
                vx = int(track.findtext("vx", default="0"))
                vy = int(track.findtext("vy", default="0"))
                vVert = int(track.findtext("vVert", default="0"))
                
                mode_s_hex = track.findtext("acAddress", default="")
                track_squawk = track.findtext("reportedBeaconCode", default="")
                
                flight_plan = record.find("flightPlan")
                if flight_plan is not None:
                    callsign = flight_plan.findtext("acid", default="N/A")
                    scratchpad1 = flight_plan.findtext("scratchPad1", default="")
                    scratchpad2 = flight_plan.findtext("scratchPad2", default="")
                    squawk = flight_plan.findtext("assignedBeaconCode", default="")
                    wake_category = flight_plan.findtext("category", default="")
                    requested_alt = int(flight_plan.findtext("requestedAltitude", default="0"))
                    assigned_alt = int(flight_plan.findtext("assignedAltitude", default="0"))
                    runway = flight_plan.findtext("runway", default="")
                    cps = flight_plan.findtext("cps", default="")
                    ocr_status = flight_plan.findtext("ocr", default="")
                    
                    if callsign not in previous_cps:
                        previous_cps[callsign] = ""
                    
                    prev_cps = previous_cps[callsign]
                    
                    if ocr_status and ("pending" in ocr_status.lower()):
                        handoff_status = cps if cps else ""
                        if "intrafacility" in ocr_status.lower() and cps:
                            previous_cps[callsign] = cps
                    else:
                        handoff_status = ""
                        if cps != prev_cps and cps:
                            previous_cps[callsign] = cps
                    
                    owner = previous_cps[callsign]
                    
                    entry_fix = flight_plan.findtext('entryFix', default='')
                    exit_fix = flight_plan.findtext('exitFix', default='')
                    flight_rules = flight_plan.findtext("flightRules", default="IFR")
                    equipment_suffix = flight_plan.findtext("eqptSuffix", default="")
                    ac_type = flight_plan.findtext("acType", default="")
                    lld = flight_plan.findtext("lld", default="")
                    ldr_direction = parse_ldr_direction(lld)
                    
                    # For departures, display exit fix in the first data tag slot
                    if not scratchpad1 and exit_fix:
                        scratchpad1 = exit_fix
                    
                    has_flight_plan = True
                else:
                    has_flight_plan = False
                    scratchpad1 = ""
                    scratchpad2 = ""
                    squawk = track_squawk if mode_s_hex and mode_s_hex != "" and mode_s_hex != "000000" else ""
                    wake_category = ""
                    requested_alt = 0
                    assigned_alt = 0
                    runway = ""
                    owner = ""
                    entry_fix = ""
                    exit_fix = ""
                    handoff_status = ""
                    flight_rules = "IFR"
                    equipment_suffix = ""
                    ac_type = ""
                    ldr_direction = None
                
                mode_s = int(mode_s_hex, 16) if mode_s_hex else None
                
                if not has_flight_plan:
                    callsign = squawk if squawk else mode_s_hex if mode_s_hex else ""
                
                if callsign == "N/A":
                    continue
                
                enhanced = record.find("enhancedData")
                aircraft_type = ac_type if ac_type else (enhanced.findtext("aircraftType", default="") if enhanced is not None else "")
                origin = enhanced.findtext('departureAirport', default='') if enhanced is not None else ''
                destination = enhanced.findtext('destinationAirport', default='') if enhanced is not None else ''
                
                ground_track = calculate_ground_track(vx, vy)
                ground_speed = calculate_ground_speed(vx, vy)
                
                aircraft_info = {
                    "callsign": callsign,
                    "lat": lat,
                    "lon": lon,
                    "alt": corrected_alt,
                    "vx": vx,
                    "vy": vy,
                    "vVert": vVert,
                    "ground_track": ground_track,
                    "ground_speed": ground_speed,
                    "squawk": squawk,
                    "aircraft_type": aircraft_type,
                    "wake_category": wake_category,
                    "scratchpad1": scratchpad1,
                    "scratchpad2": scratchpad2,
                    "requested_alt": requested_alt,
                    "assigned_alt": assigned_alt,
                    "runway": runway,
                    "owner": owner,
                    "handoff_status": handoff_status,
                    "entry_fix": entry_fix,
                    "exit_fix": exit_fix,
                    "origin": origin,
                    "destination": destination,
                    "has_flight_plan": has_flight_plan,
                    "mode_s": mode_s,
                    "mode_s_hex": mode_s_hex,
                    "flight_rules": flight_rules,
                    "equipment_suffix": equipment_suffix,
                    "ldr_direction": ldr_direction,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                }
                
                with data_lock:
                    if mode_s_hex and mode_s_hex != "" and mode_s_hex != "000000":
                        key = mode_s_hex
                    else:
                        key = callsign if callsign != "N/A" else "unknown"
                    
                    aircraft_data[key] = aircraft_info
                
                is_primary_only = not aircraft_info.get("squawk") and not aircraft_info.get("mode_s")
                if not is_primary_only or SEND_PRIMARY_TARGETS:
                    track_update = format_track_update(aircraft_info)
                    print(f"Sending TrackUpdate: {json.dumps(track_update)}")
                
                if aircraft_info.get("has_flight_plan") and (aircraft_info.get("squawk") or aircraft_info.get("mode_s")):
                    flight_plan_update = format_flight_plan_update(aircraft_info)
                    print(f"Sending FlightPlanUpdate: {json.dumps(flight_plan_update)}")
        
        except ET.ParseError as e:
            print("XML parse error:", e)
        
        return "OK", 200
    
    else:
        def event_stream():
            last_sent_track = {}
            last_sent_fp = {}
            while True:
                try:
                    with data_lock:
                        for key, aircraft in aircraft_data.items():
                            is_primary_only = not aircraft.get("squawk") and not aircraft.get("mode_s")
                            if is_primary_only and not SEND_PRIMARY_TARGETS:
                                continue
                            
                            track_update = format_track_update(aircraft)
                            track_json = json.dumps(track_update, separators=(',', ':'))
                            
                            if key not in last_sent_track or last_sent_track[key] != track_json:
                                yield track_json + "\n"
                                last_sent_track[key] = track_json
                            
                            if aircraft.get("has_flight_plan", False) and (aircraft.get("squawk") or aircraft.get("mode_s")):
                                flight_plan = format_flight_plan_update(aircraft)
                                fp_json = json.dumps(flight_plan, separators=(',', ':'))
                                
                                if key not in last_sent_fp or last_sent_fp[key] != fp_json:
                                    yield fp_json + "\n"
                                    last_sent_fp[key] = fp_json
                    
                    time.sleep(1)
                except Exception as e:
                    print(f"Error in event stream: {e}")
                    time.sleep(1)
        
        return Response(event_stream(), mimetype="application/x-ndjson")

if __name__ == "__main__":
    metar_thread = Thread(target=fetch_and_update_altimeter, daemon=True)
    metar_thread.start()
    
    app.run(host="0.0.0.0", port=5000)
