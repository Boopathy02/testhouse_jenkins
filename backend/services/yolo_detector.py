from ultralytics import YOLO
from PIL import Image
import os
import math
from collections import Counter

# Load your trained YOLOv8 model (adjust path if needed)
# Set absolute path to trained model
model_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ml_models_training", "models", "ui_elements_yolov8", "weights", "best.pt"))
model = YOLO(model_path)



# Allowed UI types
ALLOWED_CLASSES = set(model.names.values())  # Accept all class names from the model

# Class names for debug logs
CLASS_NAMES = model.names

def iou(boxA, boxB):
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])
    interArea = max(0, xB - xA) * max(0, yB - yA)
    boxAArea = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1])
    boxBArea = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1])
    return interArea / float(boxAArea + boxBArea - interArea + 1e-6)

def center_distance(boxA, boxB):
    ax, ay = (boxA[0] + boxA[2]) / 2, (boxA[1] + boxA[3]) / 2
    bx, by = (boxB[0] + boxB[2]) / 2, (boxB[1] + boxB[3]) / 2
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)

def detect_ui_elements_yolo(image_path: str, ocr_bbox: tuple[int, int, int, int], verbose: bool = False) -> tuple[int, int, int, int, str, float]:
    """
    Detect UI components in full screenshot and return most relevant match for OCR region.
    Returns (x, y, w, h, detected_type, confidence_score)
    """
    image = Image.open(image_path).convert("RGB")
    results = model.predict(source=image, conf=0.10, save=False, verbose=False)[0]

    ocr_x, ocr_y, ocr_w, ocr_h = ocr_bbox
    ocr_box = [ocr_x, ocr_y, ocr_x + ocr_w, ocr_y + ocr_h]
    best_iou = 0
    best_box = ocr_box
    best_class = "unknown"
    min_distance = float("inf")
    
    class_counts = Counter()
    ignored_classes = []

    for box in results.boxes:
        cls_id = int(box.cls)
        cls_name = CLASS_NAMES.get(cls_id, "unknown").strip().lower()

        if cls_name not in ALLOWED_CLASSES:
            ignored_classes.append(cls_name)
            continue

        class_counts[cls_name] += 1

        x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
        detection_box = [x1, y1, x2, y2]
        iou_val = iou(ocr_box, detection_box)

        if iou_val > best_iou:
            best_iou = iou_val
            best_box = detection_box
            best_class = cls_name
        elif best_iou < 0.05:
            dist = center_distance(ocr_box, detection_box)
            if dist < min_distance:
                min_distance = dist
                best_box = detection_box
                best_class = cls_name

    final_x, final_y = best_box[0], best_box[1]
    final_w, final_h = best_box[2] - best_box[0], best_box[3] - best_box[1]
    confidence = round(float(best_iou if best_iou > 0 else 0.0), 2)

    if verbose:
        # print(f"[YOLO DETECT] Classes detected: {dict(class_counts)}")
        if ignored_classes:
            # print(f"[YOLO DETECT] Ignored classes: {ignored_classes}")
            pass
        # print(f"[YOLO DETECT] Selected type: {best_class} with IOU={best_iou:.2f} for OCR text bbox={ocr_bbox}")

    return final_x, final_y, final_w, final_h, best_class, confidence
