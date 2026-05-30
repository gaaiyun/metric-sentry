# metric-sentry

把业务指标当代码测：将核心指标的定义与实算值纳入版本管理，每次改动自动快照并 diff，谁悄悄改了 join/filter 导致 MRR 异动当场拦在合并前。

完整实现见 feat/v3-reconceive 分支。