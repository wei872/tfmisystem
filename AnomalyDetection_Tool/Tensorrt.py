from ultralytics import YOLO

#（venv）python -m pip install tensorrt

# 加载您的模型
model = YOLO(r'D:\DateFile\AnomalyModel\weight\yolo_20260608.pt')

# 转换为 TensorRT（FP16 加速）
model.export(format='engine', half=True)

