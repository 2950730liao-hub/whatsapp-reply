# WhatsApp CRM 首发规则文档

## 概述

本文档定义 WhatsApp CRM 系统中消息收发的标准化规则，确保 LID 和手机号之间的正确映射，避免客户重复和数据混乱。

---

## 一、消息接收规则

### 1.1 消息源格式

Neonize 返回的消息源 `Sender` 可能是以下格式：

| 格式 | 示例 | 说明 |
|------|------|------|
| 手机号格式 | `8618028865868@s.whatsapp.net` | 标准手机号 JID |
| LID 格式 | `253425249968230@lid` | WhatsApp 内部 LID |

### 1.2 处理流程

```
收到消息
    ↓
提取 JID User 部分
    ↓
检测是否为 LID 格式 (@lid)
    ↓
是 → 查询 LID 映射表 → 转换为手机号
否 → 直接使用手机号
    ↓
使用手机号查询/创建 CRM 客户
```

### 1.3 代码实现

```python
# 1. 提取 JID 信息
if hasattr(sender_jid, 'User'):
    sender_phone = sender_jid.User
    sender_server = sender_jid.Server
elif isinstance(sender_jid, str):
    sender_phone = sender_jid.split("@")[0]
    sender_server = sender_jid.split("@")[1]

# 2. LID 转换为手机号
if sender_server == "lid":
    sender_phone = self._lid_to_phone(sender_phone)

# 3. 查询/创建客户
customer = db.query(Customer).filter(Customer.phone == sender_phone).first()
```

---

## 二、消息发送规则

### 2.1 发送目标格式

CRM 中存储的客户标识是**手机号**（如 `8618028865868`）。

### 2.2 处理流程

```
从 CRM 获取客户手机号
    ↓
使用 build_jid(phone) 构建 JID
    ↓
调用 Neonize send_message
    ↓
Neonize 自动处理路由
```

### 2.3 代码实现

```python
# 1. 从 CRM 获取手机号
phone = customer.phone  # "8618028865868"

# 2. 构建 JID
jid_obj = build_jid(phone)  # 8618028865868@s.whatsapp.net

# 3. 发送消息
self.client.send_message(jid_obj, message)
```

### 2.4 重要说明

- **不需要**将手机号转换为 LID 再发送
- `build_jid()` 会自动构建 `手机号@s.whatsapp.net` 格式
- Neonize 内部会自动处理路由到正确的 LID
- 发送到 `8618028865868@s.whatsapp.net` 可以正确送达 `18028865868`

---

## 三、数据存储规则

### 3.1 CRM 客户资料库

| 字段 | 格式 | 示例 |
|------|------|------|
| phone | 手机号（不带+号） | `8618028865868` |
| name | 显示名称 | `2950730liao` |

**禁止**存储 LID 格式（如 `253425249968230`）到 phone 字段。

### 3.2 LID 映射表（Neonize 数据库）

```sql
-- whatsmeow_lid_map 表结构
lid TEXT  -- LID，如 "253425249968230"
pn  TEXT  -- 手机号，如 "8618028865868"
```

### 3.3 通讯录（Neonize 数据库）

```sql
-- whatsmeow_contacts 表结构
their_jid      TEXT  -- 对方 JID，如 "253425249968230@lid"
push_name      TEXT  -- 显示名称
full_name      TEXT  -- 全名
```

---

## 四、数据同步规则

### 4.1 通讯录同步流程

```
登录 WhatsApp
    ↓
从 Neonize 读取通讯录 (whatsmeow_contacts)
    ↓
提取 their_jid 的 User 部分
    ↓
如果是 LID 格式 → 查询 LID 映射表转换为手机号
    ↓
使用手机号创建/更新 CRM 客户
    ↓
清理不在通讯录中的客户
```

### 4.2 名称优先级

1. `full_name`（全名）
2. `push_name`（显示名称）
3. 手机号（兜底）

---

## 五、常见问题

### Q1: 为什么收到消息时有时是 LID，有时是手机号？

A: 取决于对方如何发送：
- 如果对方是通讯录联系人，可能返回手机号格式
- 如果对方不是通讯录联系人，或隐私设置，可能返回 LID 格式

### Q2: 发送消息时需要转换为 LID 吗？

A: **不需要**。直接使用手机号的 JID 格式（`8618028865868@s.whatsapp.net`），Neonize 会自动处理路由。

### Q3: 如何避免客户重复？

A: 
1. 接收消息时，统一将 LID 转换为手机号
2. 使用手机号作为唯一标识查询客户
3. 定期运行同步脚本清理重复客户

### Q4: LID 和手机号的关系是什么？

A: 
- LID 是 WhatsApp 内部标识符（如 `253425249968230`）
- 每个 LID 对应一个手机号
- 映射关系存储在 `whatsmeow_lid_map` 表中

---

## 六、整改记录

### 2026-04-19 整改

**问题**: 18028865868 发送消息时显示为 253425249968230（LID），导致 CRM 中创建了两个客户。

**整改措施**:
1. 在 `neonize_client.py` 中添加 `_lid_to_phone()` 方法
2. 消息接收时自动将 LID 转换为手机号
3. 运行 `fix_duplicate_customers.py` 合并重复客户

**结果**: 
- 客户数量从 2 个合并为 1 个
- 所有消息正确关联到手机号客户
- 新消息自动使用手机号格式

---

## 七、相关文件

| 文件 | 说明 |
|------|------|
| `neonize_client.py` | 消息收发处理，包含 LID 转换逻辑 |
| `contact_sync_service.py` | 通讯录同步服务 |
| `fix_duplicate_customers.py` | 重复客户修复脚本 |
| `whatsapp_crm` | Neonize 数据库（通讯录、LID 映射） |
| `data/whatsapp_crm.db` | CRM 数据库（客户、消息） |
