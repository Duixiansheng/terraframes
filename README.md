# 巨量遥感影像批量处理系统

TerraFrames 2.0 · 按块重投影和裁切。相同网格直接读取对应窗口。

## 运行

需要 Python 3.13。在仓库目录运行：

```sh
python -m pip install -r requirements.txt
python make_demo.py demo
python inventory.py demo demo/20240101_reference.tif inventory.csv
python grid_processing.py demo/20240201_half_pixel_grid.tif demo/20240101_reference.tif aligned.tif
```
