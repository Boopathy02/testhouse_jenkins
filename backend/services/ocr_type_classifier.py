from PIL import Image
import torch
import os
from torchvision import transforms, models

# Define output label map
_label_map = {0: "button", 1: "textbox", 2: "label"}

# Preprocess for MobileNet
_transform = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
])

# Load fine-tuned MobileNet from disk (replace path if needed)
model_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ml_models_training", "models", "mobilenet_v2_ocr.pth"))


_model = models.mobilenet_v2(pretrained=False)
_model.classifier[1] = torch.nn.Linear(_model.last_channel, 3)
_model.load_state_dict(torch.load(model_path, map_location="cpu"))  # Load weights
_model.eval()

def classify_ocr_type(image_path: str) -> str:
    try:
        image = Image.open(image_path).convert("RGB")
        input_tensor = _transform(image).unsqueeze(0)
        with torch.no_grad():
            output = _model(input_tensor)
            predicted_class = output.argmax(dim=1).item()
            return _label_map.get(predicted_class, "unknown")
    except Exception as e:
        print(f"[OCR TYPE ERROR] Failed to classify '{image_path}': {e}")
        return "unknown"
