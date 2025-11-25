import subprocess
import os


name = input("Enter the person's name: ").strip()
if not name:
    print("No name entered. Using 'unknown'.")
    name = "unknown"


print("\nCapturing images...")
subprocess.run(["python3", "image_capture.py", name])


print("\n️Training model on new images...")
subprocess.run(["python3", "model_training.py"])

# -----------------------------
# STEP 4: Launch face recognition
# -----------------------------
print("\nStarting facial recognition...")
subprocess.run(["python3", "face_rec_test.py"])
