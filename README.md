# 巨量遥感影像批量处理系统

**TerraFrames · 多时相 GeoTIFF 影像处理**

以一张参考影像定义格网，批量检查输入、对齐像元和生成月度合成。工作台显示任务进度、完成项、失败项、复用项和实测耗时。

[**下载 v5.0.0 · Windows 64 位便携 ZIP**](https://github.com/Duixiansheng/terraframes/releases/download/v5.0.0/terraframes-v5.0.0-windows-x64.zip) · [历史版本](https://github.com/Duixiansheng/terraframes/tags)

## 主要功能

### 盘点影像

快速盘点读取文件头，检查坐标系、像元尺寸、格线、范围和无效值声明，并输出 CSV。样片册可按日期查看和放大缩略图，查看 NoData 覆盖和内容平移抽查结果。

### 统一格网

选择参考影像，也可用参考像元坐标裁切一个矩形窗口。系统按 GeoTIFF 块读写，支持最近邻、双线性和平均重采样。网格已经匹配且源影像完整覆盖目标时，直接读对应窗口，跳过重投影。输出是分块 GeoTIFF，以 NaN 标记无效像元，保留数值比例和偏移元数据。

### 并行运行与续跑

不同影像可并行处理；工作内存预算会限制并行数和 GDAL 块缓存。输出可选择 DEFLATE 压缩或无压缩。任务完成后记录源文件和相关文件的大小、修改时间、目标格网与参数。使用相同输出目录重跑时，可复用输入和产物记录一致的任务。缺失产物会重算，失败项写入 `alignment-report.json`。文件指纹用于快速续跑，采用文件属性校验。

### 生成月度合成

根据影像采集日期或文件名分月，选择一个有相同物理含义的波段。逐块计算有效值均值和观测次数，输出到同一个双波段 GeoTIFF。应用源数据的比例与偏移；单位不一致时报告失败。未识别日期的影像列入报告。有效值按照源数据的 NoData 和掩膜判断。大月份分组归并，中间文件暂存在输出盘。

## 性能实测

在本机用 12 张合成双波段 1024 × 1024 GeoTIFF 做两轮对齐。其中 6 张可直接读窗，6 张需双线性重采样，输出启用 DEFLATE。两轮交换先跑串行或并行的顺序。

| 运行方式 | 中位耗时 |
| --- | ---: |
| 单工人 | 9.89 秒 |
| 4 个并行工人 | 2.80 秒 |
| 复用全部 12 个已有结果 | 0.11 秒 |

两种并行配置的输出逐块、逐像元一致。这个样本中，4 工人约快 3.54 倍。完整数据见 [基准结果](benchmark-reference.json)。

## 快速开始

1. 解压便携包，打开 `TerraFrames.exe`，点击「批处理工作台」。
2. 影像目录选压缩包内的 `demo`。参考图选其中的 `20240101_reference.tif`，另选一个输出目录。
3. 先运行「快速盘点」看各图层的问题，再选「批量对齐」。对齐产物位于输出目录的 `aligned` 子目录。
4. 保持参数和输出目录不变，再运行一次以查看复用结果。体验月度合成时，可为合成结果选一个单独的输出目录。

源影像以只读方式打开。工作内存是调度预算，系统实际峰值还受源文件块结构、驱动和预览影响。并行收益取决于压缩、重采样、磁盘和输入大小。

## 命令行

使用带 Tk 的 Python 3.13。

```sh
python -m pip install -r requirements.txt
python make_demo.py demo
python desktop.py demo
python inventory.py demo demo/20240101_reference.tif inventory.csv
python grid_processing.py demo/20240201_half_pixel_grid.tif demo/20240101_reference.tif aligned-scene.tif
python batch_processing.py demo demo/20240101_reference.tif aligned-output --workers 4 --memory-mib 512
python temporal.py demo demo/20240101_reference.tif monthly-output --band 1
python benchmark.py demo demo/20240101_reference.tif bench-scratch --workers 4 --rounds 2
```

基准命令在独立临时目录运行两种并行配置，逐块比较全部产物的像元，记录串行、并行和续跑的耗时。
