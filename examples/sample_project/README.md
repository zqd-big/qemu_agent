# 示例 Project 占位说明

本目录仅用于说明你可以准备怎样的输入材料（不包含真实源码）。

建议准备两个 zip：

- `driver.zip`
  - Linux/RTOS 驱动源码（`.c/.h`）
  - 可选：寄存器定义头文件、DTS、设计说明、注释文档
- `reference_qemu.zip`
  - 同平台或同类型外设的 QEMU 设备模型源码（`.c/.h`）
  - 可选：相关 SoC 总线/中断控制器实现片段

上传后系统会在 `data/projects/<project_id>/` 下展开、分析并生成：

- `artifacts/analysis.json`
- `artifacts/questions.json`
- `artifacts/answers.json`
- `artifacts/<device_name>.c`
- `artifacts/report.md`（可选）

