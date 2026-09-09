import os
from PIL import Image

splits = ['train', 'val']
categories = ['Rice___Healthy', 'Rice___Blast']

for split in splits:
    for category in categories:
        folder_path = os.path.join('dataset', split, category)
        os.makedirs(folder_path, exist_ok=True)
        for i in range(5):
            img = Image.new('RGB', (224, 224), color=(34, 139, 34))
            img.save(os.path.join(folder_path, f'sample_{i}.jpg'))

print("✅ Dataset generated successfully!")