# LastMile

# JetRacer Object Detection with Fixed Movement Scheme

Autonomous behavior for the JetRacer AI kit: the car uses onboard object detection to recognize objects in its camera feed and executes a predetermined movement in response. No manual driving required.

## Overview

This project runs entirely on the Jetson Nano's host Python environment (JetRacer's stock Jupyter Lab, port 8888). No Docker container or separate build step required. It uses OpenCV's built-in DNN module with a pretrained SSD-MobileNet-v2 model (trained on COCO, 80 object classes) to detect objects in real time and trigger fixed motor responses.

## How It Works

1. **Camera capture**: frames are grabbed continuously from the CSI camera via `jetcam`.
2. **Object detection**: each frame (or every Nth frame, for performance) is run through SSD-MobileNet-v2 via `cv2.dnn`, returning the detected object's class and confidence.
3. **Action mapping**: the highest-confidence detection is looked up in a class-to-action dictionary (e.g. `"stop sign"` maps to stop, `"person"` maps to turn left).
4. **Movement execution**: the mapped action sets `NvidiaRacecar`'s steering/throttle for a fixed duration, then returns to idle.
5. **Live preview**: the camera feed, with detection boxes and labels drawn on it, streams to an `ipywidgets` image widget so you can watch what the model sees while it runs.

## Hardware

- JetRacer AI Kit (Jetson Nano)
- CSI camera (single camera setup)

## Software Requirements

Everything below runs from the stock JetRacer Jupyter Lab environment. No additional installs needed beyond what ships with the kit:

- `jetcam` (camera access)
- `jetracer` (motor control via `NvidiaRacecar`)
- `opencv-python` (`cv2`, including the `dnn` module)
- `ipywidgets` (live preview)
- `numpy`

## Setup

1. Open the notebook in your JetRacer Jupyter Lab (port 8888).
2. Run the model download cell: this pulls SSD-MobileNet-v2's COCO weights (~65MB) and config file. One-time download, safe to re-run.
3. Run the remaining cells in order: camera init → model load → car init → action mapping → live preview widget → main loop.
4. Use the Jupyter stop button to interrupt the main loop safely; the car will stop rather than freeze mid-maneuver.

## Configuring Detected Objects and Actions

Edit the `ACTIONS` dictionary to map any of the 80 COCO class names to a movement function:

```python
ACTIONS = {
    "stop sign": stop_car,
    "person": turn_left,
    "bottle": turn_right,
}
```

Movement functions (`stop_car`, `turn_left`, `turn_right`, `drive_forward`) control steering angle, throttle, and duration. Adjust these values to tune each maneuver.

## Known Issues / Notes

- **Throttle direction**: depending on ESC/motor wiring, positive throttle may drive the car backward. Fix by setting `car.throttle_gain = -1.0` once after creating the `NvidiaRacecar` object, rather than negating throttle in every action function.
- **Detection speed**: object detection runs on CPU (standard OpenCV builds on Jetson typically lack CUDA-enabled `dnn`), so expect roughly 1–4 detections per second rather than real-time. Running detection every Nth frame (rather than every frame) is recommended to keep the live preview responsive.
- **Cooldown**: a cooldown timer between triggered actions prevents a stationary object from re-triggering its action every single frame.

## Project Context

Built as part of an autonomous delivery bot project (Jetson Nano + single camera), also serving as the course project for Advanced Artificial Intelligence.

## Future Improvements

- [ ] Explore TensorRT-optimized inference for faster detection (higher setup complexity, but real-time performance)
- [ ] Train a custom detector for objects specific to the delivery scenario, rather than relying on generic COCO classes
- [ ] Add distance/size-based logic (e.g. only react once an object's bounding box is large enough to indicate proximity)
