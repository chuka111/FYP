# PunchIn: Facial Recognition Attendance System (Backend)

My PunchIn is a real-time employee attendance system built on a Raspberry Pi. Employees clock in and out simply by looking at the camera and blinking. This repo is the backend: face recognition, liveness detection, and the API that powers attendance events.

> Frontend repo: https://github.com/chuka111/FYP_react.git

## How it works

1. A camera on the Raspberry Pi continuously scans for faces using `face_recognition` and OpenCV.
2. Detected faces are matched against pre-enrolled employee encodings.
3. To prevent someone clocking in with a photo, the system requires a **live blink** before confirming identity — detected using dlib facial landmarks and eye-aspect-ratio (EAR) calculations.
4. Once liveness is confirmed, the backend logs a clock-in/clock-out event via a FastAPI endpoint.
5. The Next.js frontend reads this data in real time (via Server-Sent Events) so HR staff can see live attendance status.

## Tech stack

- **Python** — core recognition pipeline
- **face_recognition / dlib** — face detection, encoding, and landmark-based liveness detection
- **OpenCV** — camera capture and image processing
- **FastAPI** — REST API for clock events and employee data
- **SQLite** — local data storage
- **Firebase Authentication** — securing the admin and employee dashboard
- **Server-Sent Events (SSE)** — pushing live attendance updates to the frontend
- **Raspberry Pi 4 + Camera Module** — edge hardware

## Why liveness detection?

Face recognition alone can be spoofed with a printed photo or a phone screen. This project adds a lightweight liveness check: after a face is matched, the system waits a few seconds for a blink (measured via eye-aspect-ratio dropping below a threshold across consecutive frames) before confirming the clock event. This was one of the more interesting problems in the project, balancing false rejections (real employees not blinking in time) against spoof resistance.


## Project background

Built as my final year project for a BEng (Hons) in Software and Electronic Engineering. The goal was to combine embedded hardware, computer vision, and a full-stack web application into one working, presentable system from hardware setup on the Pi through to a polished admin and employee dashboard.

## What I'd improve with more time

- Move from SQLite to a proper hosted database for multi-device deployment
- Add a 3D camera that can detect when a video is being used
- Add rate limiting / retry backoff on the liveness check for edge cases (glasses, poor lighting)
- Containerise the backend for easier deployment beyond a single Pi
