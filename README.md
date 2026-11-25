# 巨量遥感影像批量处理系统

TerraFrames 3.0 · 并行处理独立文件，按预算限制并行度，重跑时复用已完成产物。

## 运行

需要 Python 3.13。在仓库目录运行：

```sh
python -m pip install -r requirements.txt
python make_demo.py demo
python inventory.py demo demo/20240101_reference.tif inventory.csv
python grid_processing.py demo/20240201_half_pixel_grid.tif demo/20240101_reference.tif aligned.tif
python batch_processing.py demo demo/20240101_reference.tif aligned-output --workers 4
```
