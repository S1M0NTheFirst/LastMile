"""Camera helper for motor_bridge.py. Runs as its own process, never by hand.

The CSI camera libraries (GStreamer/Argus) can crash the whole process, for
example when nvargus-daemon is restarted underneath them. Running the camera
here means a crash only ends this helper; the bridge keeps driving the car and
starts a new helper on the next camera request.

Writes JPEG frames to the bridge, each as a 4-byte big-endian length then the
bytes, and exits when the bridge closes the pipe. Exit code 3 means the camera
could not be opened.

The frames travel on a private copy of stdout. The camera drivers print status
lines ("GST_ARGUS: Setup Complete...") to stdout themselves, and those must not
end up inside the frame stream, so stdout is pointed at stderr (the bridge log).
"""
import os
import struct
import sys
import time

import cv2

WIDTH, HEIGHT = 640, 360
FLIP = 2 if os.getenv("FLIP_CAMERA") else 0  # nvvidconv flip-method 2 = rotate 180
QUALITY = int(os.getenv("CAMERA_JPEG_QUALITY", "65"))
MAX_FPS = int(os.getenv("CAMERA_MAX_FPS", "15"))  # frames above this rate are dropped before encoding
PIPELINE = (
    "nvarguscamerasrc ! video/x-raw(memory:NVMM), width=1280, height=720, framerate=30/1 ! "
    "nvvidconv flip-method={flip} ! video/x-raw, width={w}, height={h}, format=BGRx ! "
    "videoconvert ! video/x-raw, format=BGR ! appsink drop=true max-buffers=1"
)


def main():
    out = os.fdopen(os.dup(1), "wb")  # the frame channel
    os.dup2(2, 1)  # anything the camera libraries print now goes to the log
    cap = cv2.VideoCapture(PIPELINE.format(flip=FLIP, w=WIDTH, h=HEIGHT), cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        sys.exit(3)
    last = 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                sys.exit(4)
            if time.time() - last < 1.0 / MAX_FPS:
                continue
            last = time.time()
            ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
            if not ok:
                continue
            data = data.tobytes()
            out.write(struct.pack(">I", len(data)) + data)
            out.flush()
    except (BrokenPipeError, ValueError):
        pass  # the bridge closed the pipe: it is done with the camera
    finally:
        cap.release()


if __name__ == "__main__":
    main()
