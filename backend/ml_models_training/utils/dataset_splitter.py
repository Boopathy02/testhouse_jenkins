import os, shutil, random

def split_dataset(image_dir, label_dir, train_ratio=0.8):
    images = [f for f in os.listdir(image_dir) if f.endswith(".jpg")]
    random.shuffle(images)
    split = int(train_ratio * len(images))
    train, val = images[:split], images[split:]

    for folder in ['train', 'val']:
        os.makedirs(f"{image_dir}/{folder}", exist_ok=True)
        os.makedirs(f"{label_dir}/{folder}", exist_ok=True)

    for f in train:
        shutil.move(os.path.join(image_dir, f), os.path.join(image_dir, "train", f))
        shutil.move(os.path.join(label_dir, f.replace(".jpg", ".txt")), os.path.join(label_dir, "train", f.replace(".jpg", ".txt")))
    for f in val:
        shutil.move(os.path.join(image_dir, f), os.path.join(image_dir, "val", f))
        shutil.move(os.path.join(label_dir, f.replace(".jpg", ".txt")), os.path.join(label_dir, "val", f.replace(".jpg", ".txt")))

split_dataset("data/yolo_ui_detection/images", "data/yolo_ui_detection/labels")
