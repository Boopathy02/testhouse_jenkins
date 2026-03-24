# import os
# import torch
# import torch.nn as nn
# import torch.optim as optim
# from torchvision import datasets, models, transforms
# from torch.utils.data import DataLoader
# from tqdm import tqdm

# # Parameters
# num_classes = 3
# batch_size = 16
# epochs = 10
# lr = 1e-4
# device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# # Paths
# base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# data_dir = os.path.join(base_dir, "data", "ocr_type")
# model_dir = os.path.join(base_dir, "models")
# os.makedirs(model_dir, exist_ok=True)
# model_save_path = os.path.join(model_dir, "mobilenet_v2_ocr.pth")

# # Transform
# transform = transforms.Compose([
#     transforms.Resize((224, 224)),
#     transforms.ToTensor(),
# ])

# # Dataset and DataLoader
# train_data = datasets.ImageFolder(data_dir, transform=transform)
# train_loader = DataLoader(train_data, batch_size=batch_size, shuffle=True)

# # Model
# model = models.mobilenet_v2(pretrained=True)
# model.classifier[1] = nn.Linear(model.last_channel, num_classes)
# model = model.to(device)

# # Loss and Optimizer
# criterion = nn.CrossEntropyLoss()
# optimizer = optim.Adam(model.parameters(), lr=lr)

# # Training Loop
# for epoch in range(epochs):
#     model.train()
#     running_loss = 0.0
#     correct = 0

#     for inputs, labels in tqdm(train_loader, desc=f"Epoch {epoch+1}/{epochs}"):
#         inputs, labels = inputs.to(device), labels.to(device)
#         optimizer.zero_grad()
#         outputs = model(inputs)
#         loss = criterion(outputs, labels)
#         loss.backward()
#         optimizer.step()

#         running_loss += loss.item()
#         correct += (outputs.argmax(1) == labels).sum().item()

#     acc = 100 * correct / len(train_loader.dataset)
#     print(f"Epoch {epoch+1}/{epochs}, Loss: {running_loss:.4f}, Accuracy: {acc:.2f}%")

# # Save model
# torch.save(model.state_dict(), model_save_path)
# print(f"[✅] Model saved to {model_save_path}")



# ============================= MY NEW UPDATED CODE ==============================================
import os
import torch
import torch.nn as nn
import torch.optim as optim
from torchvision import datasets, models, transforms
from torch.utils.data import DataLoader
from tqdm import tqdm

# Parameters
num_classes = 3
batch_size = 16
epochs = 10
lr = 1e-4
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Paths
base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
data_dir = os.path.join(base_dir, "data", "ocr_type")
model_dir = os.path.join(base_dir, "models")
os.makedirs(model_dir, exist_ok=True)
model_save_path = os.path.join(model_dir, "mobilenet_v2_ocr.pth")

# Transform
transform = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
])

# Dataset and DataLoader
train_data = datasets.ImageFolder(data_dir, transform=transform)
train_loader = DataLoader(train_data, batch_size=batch_size, shuffle=True)

# Model
model = models.mobilenet_v2(pretrained=True)
model.classifier[1] = nn.Linear(model.last_channel, num_classes)
model = model.to(device)

# Loss and Optimizer
criterion = nn.CrossEntropyLoss()
optimizer = optim.Adam(model.parameters(), lr=lr)

# Training Loop
for epoch in range(epochs):
    model.train()
    running_loss = 0.0
    correct = 0

    # ✅ Use a single persistent progress bar for this epoch
    progress_bar = tqdm(
        train_loader,
        desc=f"Epoch {epoch + 1}/{epochs}",
        leave=True,              # keeps the bar in one line
        dynamic_ncols=True       # fits your terminal width
    )

    for inputs, labels in progress_bar:
        inputs, labels = inputs.to(device), labels.to(device)
        optimizer.zero_grad()
        outputs = model(inputs)
        loss = criterion(outputs, labels)
        loss.backward()
        optimizer.step()

        running_loss += loss.item()
        correct += (outputs.argmax(1) == labels).sum().item()

    acc = 100 * correct / len(train_loader.dataset)
    # print(f"Epoch {epoch+1}/{epochs}, Loss: {running_loss:.4f}, Accuracy: {acc:.2f}%")

# Save model
torch.save(model.state_dict(), model_save_path)
# print(f"ml_models_training/scripts/train_mobilnet.py [✅] Model saved to {model_save_path}")

