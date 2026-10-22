# dev48 Entry验收证据索引

最终交付以`verified/`中的全量测试、覆盖率和安装制品核验为准；其artifact-verification.json记录最终wheel及源码包摘要。

本目录根下保存第一轮（新增71项、全量2188项）及第一次独立安装的原始结果。`final/`保存同价新行情处理方案复核期间的中间质量运行；后续实现改为复核最新容量并把新序号归属当前子单，最终代码重新完整验收并记录到`verified/`。原有结果全部保留作过程记录，最终结论使用源码与制品匹配的verified证据。

windows-boundary和quant-boundary为本轮只读文件hash比较；最终比对另存final-boundary文件。量化项目同期外部变动单独列出，本轮操作没有写入该项目。
