# 巨量遥感影像批量处理系统

TerraFrames 4.0 · 按月生成均值和有效观测次数图，逐块按组归并。

## 运行

需要 Python 3.13。在仓库目录运行：

```sh
python -m pip install -r requirements.txt
python make_demo.py demo
python inventory.py demo demo/20240101_reference.tif inventory.csv
python grid_processing.py demo/20240201_half_pixel_grid.tif demo/20240101_reference.tif aligned.tif
python batch_processing.py demo demo/20240101_reference.tif aligned-output --workers 4
python temporal.py demo demo/20240101_reference.tif monthly-output --band 1
```
