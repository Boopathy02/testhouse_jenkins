from ultralytics import YOLO
import os

# Absolute path to yolov8 config file
config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "yolov8_config.yaml"))

model = YOLO("yolov8n.pt")

model.train(
    data=config_path,
    epochs=50,
    imgsz=640,
    project="../models",         # Save under backend/ml_models_training/models/
    name="ui_elements_yolov8",   # Folder: models/ui_elements_yolov8/
    device="cpu"                 # Or "cuda" if available
)
