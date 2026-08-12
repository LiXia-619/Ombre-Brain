# O2-H · 外部只读协议数值摘要规范

状态：施工分支契约；仅用于 O2-A ↔ O2-B 的外部只读召回协议。

## 版本与适用范围

O2-H 将握手与结果 schema 显式提升为 `ombre-read-protocol-v2` /
`ombre-structured-recall-v2`，并要求 `protocol_version=2` 与
`digest_profile=ombre-fixed6-numeric-v1`。v1 结果不能被静默当成 v2 验收。

该规范只控制 Ombre Brain 外部只读召回的 item / result digest。它不能替换或改变
Our Home 内部生命事件、连续性、权限、房间事件或住民签发使用的既有 canonical JSON。

## 唯一数值形式

- `relevance` 必须是 `0..1` 的有限数，O2-A 先规范到最多六位小数；
- `affect.valence` 与 `affect.arousal` 只能是 `null` 或 `-1..1` 的有限数，
  O2-A 同样先规范到最多六位小数；
- 上述三个字段进入摘要时固定写成六位小数，例如 `0.000000`、`1.000000`、
  `-0.333333`；负零统一为 `0.000000`；
- 其余协议数值必须是安全整数，不能借固定小数规则放宽任意浮点字段；
- `NaN`、无穷、布尔伪装数值、越界 affect 或非安全整数全部失败关闭。

对象键序继续采用协议既定的确定性顺序，字符串使用 JSON 转义，摘要算法仍是
SHA-256。握手必须逐项核准 schema、版本与 digest profile 后才允许召回。

## 双方证明

O2-A 与 O2-B 各自保存并执行同一组金标准向量，至少覆盖积分 relevance、积分 affect、
负零、正负小数与空结果 envelope。双方必须得到完全相同的 canonical bytes 与 SHA-256。

真实隔离验收还必须让 O2-A 从含 `0.0 / 1.0` affect 的合成虚拟 vault 返回结果，
由 O2-B 重新计算 item / result digest；重复结果须一致，故障实例仍须降级为空，两份
虚拟 vault 前后逐字节与元数据不变。

本阶段不部署 O2-A，不接真实 vault、凭据、记忆或住民，不启用写入、touch、dream、
reflect 或房间 checkpoint。
