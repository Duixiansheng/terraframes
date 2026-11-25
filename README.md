# 巨量遥感影像批量处理系统

TerraFrames 1.0 · 快速盘点 GeoTIFF 文件头，通过参考图检查各影像格网和覆盖。

## 运行

需要 Python 3.13。在仓库目录运行：

```sh
python -m pip install -r requirements.txt
python make_demo.py demo
python inventory.py demo demo/20240101_reference.tif inventory.csv
```
