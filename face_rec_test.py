import face_recognition
import cv2
import numpy as np
from picamera2 import Picamera2
import time
import pickle
from datetime import datetime

# -------------------------------
# SETTINGS (TUNE THESE FOR SPEED)
# -------------------------------
CAMERA_RESOLUTION = (640, 480)   # Lower = faster
CV_SCALER = 6                    # Higher = faster, less accurate
PROCESS_EVERY = 2                # Process every Nth frame (2 = good balance)
LOG_COOLDOWN = 10                # Seconds between logs per person
# -------------------------------

print("[INFO] Loading encodings...")
with open("encodings.pickle", "rb") as f:
    data = pickle.loads(f.read())
known_face_encodings = data["encodings"]
known_face_names = data["names"]

# Camera setup
picam2 = Picamera2()
picam2.configure(picam2.create_preview_configuration(
    main={"format": 'XRGB8888', "size": CAMERA_RESOLUTION}
))
picam2.start()

face_locations = []
face_encodings = []
face_names = []
frame_count = 0
start_time = time.time()
fps = 0
frame_id = 0

# Last time each face was logged
last_seen = {}


def process_frame(frame):
    """Downscale, detect, encode and identify faces."""
    global face_locations, face_encodings, face_names, last_seen

    # Fast resize
    resized_frame = cv2.resize(frame, (0, 0), fx=(1/CV_SCALER), fy=(1/CV_SCALER), interpolation=cv2.INTER_NEAREST)
    rgb = cv2.cvtColor(resized_frame, cv2.COLOR_BGR2RGB)

    # Fast HOG face detection (default)
    face_locations = face_recognition.face_locations(rgb)
    face_encodings = face_recognition.face_encodings(rgb, face_locations)

    face_names = []

    for face_encoding in face_encodings:
        matches = face_recognition.compare_faces(known_face_encodings, face_encoding)
        name = "Unknown"

        face_distances = face_recognition.face_distance(known_face_encodings, face_encoding)
        best_match_index = np.argmin(face_distances)

        if matches[best_match_index]:
            name = known_face_names[best_match_index]

            # Anti-spam logging
            now = time.time()
            if name not in last_seen or (now - last_seen[name]) > LOG_COOLDOWN:
                now_str = datetime.now().strftime("%H:%M:%S")
                print(f"{name} is here at {now_str}")
                last_seen[name] = now

        face_names.append(name)

    return frame


def draw_results(frame):
    """Draw boxes and names on the output frame."""
    for (top, right, bottom, left), name in zip(face_locations, face_names):
        # Re-scale face box
        top *= CV_SCALER
        right *= CV_SCALER
        bottom *= CV_SCALER
        left *= CV_SCALER

        cv2.rectangle(frame, (left, top), (right, bottom), (244, 42, 3), 2)
        cv2.rectangle(frame, (left, top - 30), (right, top), (244, 42, 3), -1)
        cv2.putText(frame, name, (left + 5, top - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1)

    return frame


def calculate_fps():
    """FPS counter."""
    global frame_count, start_time, fps
    frame_count += 1
    elapsed = time.time() - start_time
    if elapsed >= 1:
        fps = frame_count / elapsed
        frame_count = 0
        start_time = time.time()
    return fps


# -----------------------------
#          MAIN LOOP
# -----------------------------
print("[INFO] Starting high-FPS face recognition...")

while True:
    frame = picam2.capture_array()
    frame_id += 1

    # Process every N frames
    if frame_id % PROCESS_EVERY == 0:
        processed_frame = process_frame(frame)
    else:
        processed_frame = frame

    display_frame = draw_results(processed_frame)

    # Show FPS
    current_fps = calculate_fps()
    cv2.putText(display_frame, f"FPS: {current_fps:.1f}",
                (display_frame.shape[1] - 150, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

    # Live clock
    now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cv2.putText(display_frame, now_time, (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)

    cv2.imshow("Video", display_frame)

    if cv2.waitKey(1) == ord("q"):
        break

cv2.destroyAllWindows()
picam2.stop()
print("[INFO] Finished.")
