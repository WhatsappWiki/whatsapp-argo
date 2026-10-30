# WhatsApp Argo for Python

<a id="contact"></a>

[Telegram: @ttjjda](https://t.me/ttjjda)

[中文](#zh) · [English](#en) · [Contact](#contact)

<a id="zh"></a>

## 中文

用纯 Python 读取、修改和写入 Argo 1.2 二进制值及 wire-type schema store。提供通用 `Codec`，以及按 doc ID 选择外部 schema 的 `OperationRegistry`，可接入已有的 WhatsApp 节点处理流程。

Python **3.10+**，运行时仅依赖标准库，MIT 许可证。默认标量布局兼容官方 Erlang 实现；类型、Header 和错误编码选项见 [Argo 协议与 API](https://github.com/WhatsappWiki/whatsapp-technical-wiki/blob/main/protocol/argo.md#zh)。

### 安装

在本项目根目录执行：

```sh
python -m pip install .
```

项目名为 `whatsapp-argo`，Python 导入名为 `whatsapp_argo`。

### 快速开始

下面的示例可直接运行，不需要手机、网络或外部文件：

```python
from whatsapp_argo import Codec, Header

schema = {
    "type": "RECORD",
    "fields": [
        {"name": "title", "of": {"type": "STRING"}},
        {"name": "count", "of": {"type": "VARINT"}},
    ],
}
codec = Codec(schema)
header = Header(out_of_band_field_errors=True, self_describing_errors=False)
payload = codec.write({"title": "hello", "count": 7}, header=header)
document = codec.read(payload)
assert document.value == {"title": "hello", "count": 7}

# 修改当前对象，再生成新的字节。
document.value["count"] = 8
updated = codec.write(document)
assert updated != payload
assert codec.read(updated).value["count"] == 8
```

示例使用 Header `0x04`；`Header()` 默认 `0x0c`。读取返回 `Document`，写入会复用其 schema、Header 与格式设置。业务接入、完整类型、错误和逐字节回写见 [Argo 协议文章](https://github.com/WhatsappWiki/whatsapp-technical-wiki/blob/main/protocol/argo.md#zh)。

### 测试与构建

```sh
python -m pip install '.[test]'
python -m pytest
python -m pip install build
python -m build
```

测试使用合成数据，包括 320 份由官方实现生成并自行解码的独立向量；覆盖所有 128 种 Header 组合、类型与错误、schema store、改值、截断和资源限制。测试不需要手机、账号、网络或私有文件。

### 分发内容与文档

wheel 包含运行库、许可证和 README 元数据；sdist 另含合成单元测试和测试向量。发行包不包含采集/路由核验工具、私有验证记录、真实抓包、完整 WhatsApp schema、账号凭据或手机 hook 脚本。

- [Argo 协议与完整 API](https://github.com/WhatsappWiki/whatsapp-technical-wiki/blob/main/protocol/argo.md#zh)：类型、Header、schema、业务接入与逐字节回写。
- [合成测试向量](tests/fixtures/official-vectors.json)：输入模型、编码结果和生成来源。
- [许可证与引用](THIRD_PARTY_NOTICES.md) · [Argo 1.2 规范](https://msolomon.github.io/argo/versions/1.2/spec) · [官方 Erlang 实现](https://github.com/WhatsApp/erlang-argo)。

[English](#en)

<a id="en"></a>

## English

Read, modify and write Argo 1.2 binary values and wire-type schema stores in pure Python. The generic `Codec` and doc-ID-based `OperationRegistry` accept external schemas and can integrate with an existing WhatsApp node-processing flow.

Requires **Python 3.10+**, with only the standard library at runtime. MIT licensed. Default scalar placement is compatible with the official Erlang implementation. See the [Argo protocol and API](https://github.com/WhatsappWiki/whatsapp-technical-wiki/blob/main/protocol/argo.md#en) for types, headers and error-encoding options.

### Installation

Run from the project root:

```sh
python -m pip install .
```

Distribution name: `whatsapp-argo`. Python import: `whatsapp_argo`.

### Quick start

This example runs without a phone, network connection or external files:

```python
from whatsapp_argo import Codec, Header

schema = {
    "type": "RECORD",
    "fields": [
        {"name": "title", "of": {"type": "STRING"}},
        {"name": "count", "of": {"type": "VARINT"}},
    ],
}
codec = Codec(schema)
header = Header(out_of_band_field_errors=True, self_describing_errors=False)
payload = codec.write({"title": "hello", "count": 7}, header=header)
document = codec.read(payload)
assert document.value == {"title": "hello", "count": 7}

# Edit the current object, then generate new bytes.
document.value["count"] = 8
updated = codec.write(document)
assert updated != payload
assert codec.read(updated).value["count"] == 8
```

The example uses header `0x04`; `Header()` defaults to `0x0c`. Reading returns a `Document`, and writing reuses its schema, header and format settings. See the [Argo article](https://github.com/WhatsappWiki/whatsapp-technical-wiki/blob/main/protocol/argo.md#en) for business integration, complete types, errors and exact rewriting.

### Tests and builds

```sh
python -m pip install '.[test]'
python -m pytest
python -m pip install build
python -m build
```

Tests use synthetic data, including 320 independent vectors encoded and decoded by the official implementation. Coverage includes all 128 header combinations, types/errors, schema stores, mutation, truncation and resource limits. Tests require no phone, account, network connection or private files.

### Distribution contents and documentation

The wheel contains the runtime, licenses and README metadata; the sdist additionally contains synthetic unit tests and vectors. Distributions exclude capture/route-verification tools, private validation records, actual captures, complete WhatsApp schemas, account credentials and device hook scripts.

- [Argo protocol and full API](https://github.com/WhatsappWiki/whatsapp-technical-wiki/blob/main/protocol/argo.md#en): types, headers, schemas, business integration and exact rewriting.
- [Synthetic vectors](tests/fixtures/official-vectors.json): input models, encoded results and generation provenance.
- [License and attribution](THIRD_PARTY_NOTICES.md) · [Argo 1.2 specification](https://msolomon.github.io/argo/versions/1.2/spec) · [Official Erlang implementation](https://github.com/WhatsApp/erlang-argo).

[中文](#zh)
