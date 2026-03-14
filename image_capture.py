import cv2
import os
from datetime import datetime
from picamera2 import Picamera2
import time
import sys

if len(sys.argv) > 1:
    PERSON_NAME = sys.argv[1]
else:
    PERSON_NAME = input("Enter person name: ")

def create_folder(name):
    person_folder = os.path.join("dataset", name)
    os.makedirs(person_folder, exist_ok=True)
    return person_folder

def capture_photos(name):
    folder = create_folder(name)

    picam2 = Picamera2()
    picam2.configure(picam2.create_preview_configuration(
        main={"format": "XRGB8888", "size": (640, 480)}
    ))
    picam2.start()
    time.sleep(2)

    photo_count = 0
    print(f"Taking photos for {name}. Press SPACE to capture, 'q' to quit.")

    while True:
        frame = picam2.capture_array()
        cv2.imshow("Capture", frame)

        key = cv2.waitKey(1) & 0xFF

        if key == ord(" "):
            photo_count += 1
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = os.path.join(folder, f"{name}_{timestamp}.jpg")
            cv2.imwrite(filepath, frame)
            print(f"Photo {photo_count} saved: {filepath}")

        elif key == ord("q"):
            break

    cv2.destroyAllWindows()
    picam2.stop()
    print(f"Done. {photo_count} photos saved for {name} in {folder}/")

if __name__ == "__main__":
    capture_photos(PERSON_NAME)