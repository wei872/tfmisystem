from ultralytics import YOLO

#（venv）python -m pip install tensorrt

# 加载您的模型
model = YOLO(r'D:\DateFile\AnomalyModel\weight\yolo_20260608.pt')

# 转换为 TensorRT（FP16 加速）
#
# imgsz 必须显式写死，不能省略。原因（依据 ultralytics==8.4.75 源码，
# 与 requirements.txt 锁定版本一致）：
#
#   1. engine/model.py:705-706  export() 不传 imgsz 时取的是
#      self.model.args["imgsz"]，即 .pt 里固化的训练参数，而不是
#      cfg/default.yaml:16 的全局默认 640。也就是说导出尺寸取决于
#      当初训练时用的尺寸，不可控也不可见。
#
#   2. cfg/default.yaml:88  dynamic: False（默认）。engine 的输入形状
#      在导出时就被固化（exporter.py:637 用 torch.zeros(batch,3,*imgsz)
#      造 dummy 张量定型）。
#
#   3. engine/predictor.py:414-415  推理时若 engine 非 dynamic，会用
#      engine 元数据里的 imgsz **覆盖**调用方传入的 imgsz：
#          if hasattr(self.model, "imgsz") and not getattr(self.model, "dynamic", False):
#              self.args.imgsz = self.model.imgsz
#      所以一旦 engine 按 640 导出，config.yaml 里的 image_size: 1280
#      会被静默覆盖成 640 —— 不报错、不告警，小缺陷召回率直接下降。
#
# 本项目 config.yaml → yolo.image_size = 1280，导出尺寸必须与之一致。
# 改这里之前先改 config.yaml，两者必须同步。
model.export(format='engine', half=True, imgsz=1280)

# 导出后建议校验 engine 实际固化的输入形状（在目标机上跑）：
#
#   import json, tensorrt as trt
#   logger = trt.Logger(trt.Logger.WARNING)
#   with open(r'./Modelfile/yolo_20260608.engine', 'rb') as f, trt.Runtime(logger) as rt:
#       n = int.from_bytes(f.read(4), 'little')      # ultralytics 的元数据长度前缀
#       meta = json.loads(f.read(n))
#       print("engine metadata imgsz =", meta.get("imgsz"))
#       eng = rt.deserialize_cuda_engine(f.read())
#       for i in range(eng.num_io_tensors):
#           name = eng.get_tensor_name(i)
#           if eng.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
#               print("输入张量:", name, eng.get_tensor_shape(name))
#
# 期望输出 (1, 3, 1280, 1280)。若是 (1, 3, 640, 640)，说明用的还是旧 engine。

