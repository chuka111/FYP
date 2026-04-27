import face_recognition
import cv2
import numpy as np
from picamera2 import Picamera2
import time
import pickle
from datetime import datetime
import requests
import dlib
from scipy.spatial import distance

# ── Settings ──────────────────────────────────────────────────────────────────
CAMERA_RESOLUTION = (640, 480)
CV_SCALER          = 6
PROCESS_EVERY      = 2
LOG_COOLDOWN       = 10       # seconds between clock events per person
BLINK_COOLDOWN     = 5        # seconds to wait for a blink after recognition
EAR_THRESHOLD      = 0.28     # below this = eye closed
EAR_CONSEC_FRAMES  = 1        # frames eye must be closed to count as blink

API_BASE   = "http://127.0.0.1:8000"
DEVICE_KEY = "raspberrypi4fyp"

# ── Load face encodings ────────────────────────────────────────────────────────
print("[INFO] Loading encodings...")
with open("encodings.pickle", "rb") as f:
    data = pickle.loads(f.read())
known_face_encodings = data["encodings"]
known_face_names     = data["names"]

# ── Load dlib landmark detector ────────────────────────────────────────────────
print("[INFO] Loading landmark detector...")
detector  = dlib.get_frontal_face_detector()
predictor = dlib.shape_predictor("shape_predictor_68_face_landmarks.dat")

# Eye landmark indices
LEFT_EYE  = list(range(42, 48))
RIGHT_EYE = list(range(36, 42))

# ── Camera ────────────────────────────────────────────────────────────────────
picam2 = Picamera2()
picam2.configure(
    picam2.create_preview_configuration(
        main={"format": "XRGB8888", "size": CAMERA_RESOLUTION}
    )
)
picam2.start()

# ── State ─────────────────────────────────────────────────────────────────────
face_locations = []
face_names     = []
frame_id       = 0
frame_count    = 0
start_time     = time.time()
fps            = 0

last_seen      = {}   # name: last clock event time

# Liveness state per person
liveness       = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

def eye_aspect_ratio(landmarks, eye_indices):
    pts = np.array([(landmarks.part(i).x, landmarks.part(i).y) for i in eye_indices])
    A = distance.euclidean(pts[1], pts[5])
    B = distance.euclidean(pts[2], pts[4])
    C = distance.euclidean(pts[0], pts[3])
    return (A + B) / (2.0 * C)

def toggle_clock(name, confidence=None):
    try:
        r = requests.post(
            f"{API_BASE}/clock/toggle",
            headers={"X-Device-Key": DEVICE_KEY},
            json={"name": name, "confidence": confidence},
            timeout=3,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("[API] toggle failed:", e)
        return None

def check_blink(frame_gray, face_loc):
    """
    Given a grayscale frame and a face location tuple (top,right,bottom,left),
    returns the average EAR for both eyes.
    """
    top, right, bottom, left = face_loc
    # Scale back up since we downscaled for recognition
    rect = dlib.rectangle(
        left * CV_SCALER, top * CV_SCALER,
        right * CV_SCALER, bottom * CV_SCALER
    )
    shape = predictor(frame_gray, rect)
    left_ear  = eye_aspect_ratio(shape, LEFT_EYE)
    right_ear = eye_aspect_ratio(shape, RIGHT_EYE)
    return (left_ear + right_ear) / 2.0

# ── Main loop ─────────────────────────────────────────────────────────────────
print("[INFO] Starting face recognition with liveness detection...")

while True:
    frame    = picam2.capture_array()
    frame_id += 1

    # Convert to grayscale for dlib (full resolution)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # Downscale for face recognition
    if frame_id % PROCESS_EVERY == 0:
        resized = cv2.resize(
            frame, (0, 0),
            fx=1/CV_SCALER, fy=1/CV_SCALER,
            interpolation=cv2.INTER_NEAREST
        )
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        face_locations = face_recognition.face_locations(rgb)
        face_encodings = face_recognition.face_encodings(rgb, face_locations)
        face_names     = []

        for face_encoding, face_loc in zip(face_encodings, face_locations):
            name = "Unknown"

            if known_face_encodings:
                distances        = face_recognition.face_distance(known_face_encodings, face_encoding)
                best_idx         = int(np.argmin(distances))
                matches          = face_recognition.compare_faces(known_face_encodings, face_encoding)

                if matches[best_idx]:
                    name       = known_face_names[best_idx]
                    confidence = float(distances[best_idx])
                    now        = time.time()

                    # ── Liveness check ──────────────────────────────────────
                    if name not in liveness:
                        liveness[name] = {
                            "waiting":    False,
                            "deadline":   0,
                            "ear_consec": 0,
                            "confirmed":  False,
                        }

                    state = liveness[name]

                    # Start liveness window if not already waiting
                    # and cooldown has passed
                    cooldown_ok = (name not in last_seen or
                                   now - last_seen[name] > LOG_COOLDOWN)

                    if cooldown_ok and not state["waiting"]:
                        state["waiting"]    = True
                        state["deadline"]   = now + BLINK_COOLDOWN
                        state["ear_consec"] = 0
                        state["confirmed"]  = False
                        print(f"[LIVENESS] Waiting for blink from {name}...")

                    # Check EAR while waiting
                    if state["waiting"] and not state["confirmed"]:
                        try:
                            ear = check_blink(gray, face_loc)
                            if ear < EAR_THRESHOLD:
                                state["ear_consec"] += 1
                            else:
                                if state["ear_consec"] >= EAR_CONSEC_FRAMES:
                                    # Blink confirmed!
                                    print(f"[LIVENESS] Blink confirmed for {name}")
                                    state["confirmed"] = True
                                    state["waiting"]   = False
                                    last_seen[name]    = now

                                    result = toggle_clock(name, confidence)
                                    if result and result.get("current"):
                                        print(f"[API] {name} → {result['current']['status']}")
                                state["ear_consec"] = 0

                        except Exception as e:
                            print(f"[LIVENESS] EAR error: {e}")

                        # Timeout — no blink detected
                        if now > state["deadline"]:
                            print(f"[LIVENESS] Timeout for {name} — liveness failed")
                            state["waiting"]    = False
                            state["ear_consec"] = 0
                            # Reset so they can try again after cooldown
                            last_seen[name]     = now - (LOG_COOLDOWN - 3)

            face_names.append(name)

    # ── Draw ──────────────────────────────────────────────────────────────────
    for (top, right, bottom, left), name in zip(face_locations, face_names):
        top    *= CV_SCALER
        right  *= CV_SCALER
        bottom *= CV_SCALER
        left   *= CV_SCALER

        state  = liveness.get(name, {})
        waiting = state.get("waiting", False)

        # Box colour: yellow while waiting for blink, green/red otherwise
        colour = (0, 255, 255) if waiting else (244, 42, 3)

        cv2.rectangle(frame, (left, top), (right, bottom), colour, 2)
        cv2.rectangle(frame, (left, top - 30), (right, top), colour, -1)
        cv2.putText(frame, name, (left + 5, top - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1)

        # Show liveness prompt below the box
        if waiting:
            time_left = max(0, round(state["deadline"] - time.time(), 1))
            cv2.putText(
                frame,
                f"Blink to confirm ({time_left}s)",
                (left, bottom + 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1
            )

    # FPS counter
    frame_count += 1
    elapsed = time.time() - start_time
    if elapsed >= 1:
        fps         = frame_count / elapsed
        frame_count = 0
        start_time  = time.time()

    cv2.putText(frame, f"FPS: {fps:.1f}",
                (frame.shape[1] - 150, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

    cv2.putText(frame, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)

    cv2.imshow("PunchIn", frame)
    if cv2.waitKey(1) == ord("q"):
        break

cv2.destroyAllWindows()
picam2.stop()
print("[INFO] Finished.")