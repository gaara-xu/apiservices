# Mac AI 服务（待实现）

仅在 Mac 执行模型加载、图片特征提取、喜好分类器训练及评分。110 发起计算请求；Mac 不承担业务目录扫描、业务任务领取、网页或业务数据库。

拟使用 Python、PyTorch、Transformers 和 scikit-learn。先验证固定 SigLIP 2 特征加轻量分类器的效果，再决定是否微调。接口、依赖版本和计算并发均待实现验证。

## 已有工具

`scripts/download_model.py` 为已执行成功的下载及校验工具；依据 `resources/model-info.json` 中的大小和哈希校验。它使用本机既定模型路径，下载临时文件和结果记录保存在图像识别项目内。仅在显式准备模型时使用镜像下载，正常 AI 服务不依赖公网。

模型目录：`/Users/gaara/hades/AILocal/ollama-models/siglip2-so400m-patch14-384`。这只是存储位置，通过 Python 加载，不使用 Ollama 推理服务。
