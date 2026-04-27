import cv2
import dlib
import numpy as np
from scipy.spatial import distance
from picamera2 import Picamera2
import time

detector  = dlib.get_frontal_face_detector()
predictor = dlib.shape_predictor("shape_predictor_68_face_landmarks.dat")

LEFT_EYE  = list(range(42, 48))
RIGHT_EYE = list(range(36, 42))

def ear(landmarks, indices):
    pts = np.array([(landmarks.part(i).x, landmarks.part(i).y) for i in indices])
    A = distance.euclidean(pts[1], pts[5])
    B = distance.euclidean(pts[2], pts[4])
    C = distance.euclidean(pts[0], pts[3])
    return (A + B) / (2.0 * C)

picam2 = Picamera2()
picam2.configure(picam2.create_preview_configuration(
    main={"format": "XRGB8888", "size": (640, 480)}
))
picam2.start()
time.sleep(2)

print("Look at the camera. Blink normally. Watch the EAR values.")
print("Note your open-eye value and your blink value.")
print("Press Q to quit.\n")

while True:
    frame = picam2.capture_array()
    gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = detector(gray)

    for face in faces:
        shape = predictor(gray, face)
        left_ear  = ear(shape, LEFT_EYE)
        right_ear = ear(shape, RIGHT_EYE)
        avg_ear   = (left_ear + right_ear) / 2.0

        label = f"EAR: {avg_ear:.3f}"
        color = (0, 255, 0) if avg_ear > 0.20 else (0, 0, 255)

        cv2.putText(frame, label, (30, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 2)

        print(f"EAR: {avg_ear:.3f} {'<-- BLINK' if avg_ear < 0.20 else ''}")

    cv2.imshow("EAR Calibration", frame)
    if cv2.waitKey(1) == ord("q"):
        break

cv2.destroyAllWindows()
picam2.stop()