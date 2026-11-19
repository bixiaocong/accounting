# 小账本

一个本地记账网页。页面负责展示和录入，数据直接保存在项目目录下的 `data.json`。

## 启动

双击 `start.command`，浏览器会自动打开：

```text
http://127.0.0.1:8765
```

也可以在终端里运行：

```bash
cd "/Users/bicongcong/Documents/小工具/accounting"
python3 server.py
```

关闭运行服务的终端窗口即可停止。

> 不要直接双击 `index.html`。直接打开文件时，浏览器没有权限写入本地 JSON 文件，页面会提示先启动本地服务。

## 数据文件

所有记录都保存在：

```text
/Users/bicongcong/Documents/小工具/accounting/data.json
```

每次新增或删除都会实时写入。数据结构：

```json
{
  "version": 1,
  "records": [
    {
      "id": "唯一ID",
      "date": "2026-11-02",
      "item": "午餐",
      "amount": 32.5,
      "category": "餐饮",
      "createdAt": 1793625510798
    }
  ]
}
```

- `date`：日期，格式为 `YYYY-MM-DD`
- `item`：消费项目
- `amount`：金额，单位为元
- `category`：分类
- `createdAt`：记录创建时间的时间戳

## 备份

页面右上角的 **下载备份** 会下载一份带日期的 JSON 备份。

也可以直接复制 `data.json` 文件保存到别处。恢复时使用 **导入**，注意导入会覆盖当前 `data.json` 中的记录。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `index.html` | 页面界面和交互 |
| `server.py` | 本地服务，负责读写 `data.json` |
| `data.json` | 记账数据 |
| `start.command` | macOS 双击启动脚本 |
