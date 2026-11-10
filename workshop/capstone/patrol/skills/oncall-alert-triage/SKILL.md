---
name: oncall-alert-triage
description: 收到 On-call 告警后，核实受影响服务的当前 HTTP 状态，给出有证据的交接。
---

# 告警初判

1. 从告警识别事故 ID、目标服务和异常窗口。保留窗口中的请求数、5xx 比例和 P95。
2. 使用 oncall_probe 向目标服务发起探测，并记录实际 HTTP 状态。一次探测只说明该请求的结果。一次 503 不能确认所有请求均失败，一次 200 也不足以确认整个窗口已经恢复。
3. 有历史记录时，通过 search_inspections 查找同一服务的对照。把当前证据、历史现象和未知原因分别写清楚。
4. 在报告里保留 incident_id、第一次探测的 probe_http_status 和按顺序排列的 probe_http_statuses。assessment 在单次 5xx 时用 failure_observed，在首次探测为 2xx 时用 not_reproduced。说明当前可确认的影响、仍缺的证据和给值班人的下一步。故障开关由学员操作，恢复由后续巡检验证。

## 事故补查与值班交接

收到 `ONCALL_FOLLOWUP` 后，从初判报告接手。先核对事故 ID、服务和环境。通过 `followup_probe` 对同一范围发出三次新探测。逐次调用，收到回执后再发下一次。初判探测只供对照。

按三次新回执判断 `assessment`。全部为 5xx 时填 `persistent`，同时收到 2xx 与 5xx 时填 `intermittent`，全部为 2xx 时填 `not_reproduced`。单次请求只能说明当次结果，`root_cause` 填 `unknown`。在 `analysis` 说明目前证实的影响、依据和判断边界。

复现故障时，建议值班员审批后清除本次演练故障，再核对两个互不重叠的健康窗口。`recovery_plan` 填 `{"action":"clear_lab_fault","approval_required":true,"verification":"two_clean_windows"}`。未复现时继续观察，填 `{"action":"observe","approval_required":false,"verification":"continue_patrol"}`。

只输出一个 JSON 对象，包含 `incident_id`、`service`、`environment_id`、按顺序排列的 `probe_http_statuses`、`assessment`、`root_cause`、`analysis` 和 `recovery_plan`。报告供值班员接手。执行和关闭由外部状态机负责。
